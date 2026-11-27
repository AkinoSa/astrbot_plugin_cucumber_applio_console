#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kaggle 云端 Applio RVC + BS-Roformer 推理网关服务
-----------------------------------------------------------
特点：
1. 专为 Kaggle GPU 环境优化，支持人声分离与 RVC 变声全链路 GPU 加速。
2. 支持 skip_separation 纯人声直出模式（跳过 BS-Roformer 与伴奏混音，对齐本地 Applio）。
3. 搭载与本地网关同源的 8 参数合成 IR 卷积混响内核（FFT 卷积 + 峰值保护）。
4. 内置空闲看门狗（Idle Watchdog）：无任务空闲超过设定时间（默认 10 分钟）自动关机。
5. 提供 Gradio API (/convert) 与 FastAPI REST (/api/separate 等) 双通道。
"""

import os
import re
import sys
import math
import time
import uuid
import shutil
import logging
import asyncio
import threading
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any

# 优先载入 Applio 模块
for applio_path in ("/kaggle/working/applio", "/app"):
    if os.path.exists(applio_path) and applio_path not in sys.path:
        sys.path.insert(0, applio_path)

# 设置基础日志
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("KaggleApplioGateway")

# 目录与参数配置
WORKING_DIR = Path(os.environ.get("KAGGLE_WORKING_DIR", "/kaggle/working"))
ASSETS_DIR = WORKING_DIR / "assets"
WEIGHTS_DIR = ASSETS_DIR / "weights"
INDEX_DIR = ASSETS_DIR / "indices"
PYMSS_DIR = ASSETS_DIR / "pymss_weights"
OUTPUT_DIR = WORKING_DIR / "out"
TASK_DIR = WORKING_DIR / "tasks"

for d in (ASSETS_DIR, WEIGHTS_DIR, INDEX_DIR, PYMSS_DIR, OUTPUT_DIR, TASK_DIR):
    d.mkdir(parents=True, exist_ok=True)

# 确保 Applio 目录树与模型链接完整
for applio_root in [WORKING_DIR / "applio", Path("/app")]:
    if applio_root.is_dir():
        if not (WORKING_DIR / "rvc").exists() and (applio_root / "rvc").is_dir():
            try:
                os.symlink(str(applio_root / "rvc"), str(WORKING_DIR / "rvc"))
            except Exception:
                pass
        predictor_sources = {
            "rmvpe.pt": [ASSETS_DIR / "rmvpe" / "rmvpe.pt", ASSETS_DIR / "predictors" / "rmvpe.pt"],
            "fcpe.pt": [ASSETS_DIR / "predictors" / "fcpe.pt"],
        }
        for pred_name, pred_cands in predictor_sources.items():
            pred_src = next((p for p in pred_cands if p.is_file()), None)
            if pred_src is None:
                logger.warning(f"[predictors] 缺失 {pred_name}，对应 f0_method 将不可用")
                continue
            for target in [
                applio_root / "rvc" / "models" / "predictors" / pred_name,
                WORKING_DIR / "rvc" / "models" / "predictors" / pred_name,
            ]:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        shutil.copy2(pred_src, target)
                except Exception:
                    pass

# 空闲关机时长配置（秒），默认 600 秒（10分钟）
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "600"))
SERVER_PORT = int(os.environ.get("PORT", "7865"))

REVERB_SR = 44100
REVERB_TAIL_MAX_SECONDS = 4.0

# ==================== 1. 空闲关机看门狗 (Idle Watchdog) ====================

class IdleWatchdog:
    """监测任务队列与空闲时间，超时自动终止进程释放 Kaggle GPU 配额。"""

    def __init__(self, timeout_seconds: int = 600):
        self.timeout_seconds = timeout_seconds
        self.last_active_time = time.time()
        self.active_jobs = 0
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._monitor, daemon=True, name="IdleWatchdogThread")
        self.thread.start()
        logger.info(f"[看门狗] 已启动，空闲自动关机阈值: {self.timeout_seconds} 秒 ({self.timeout_seconds / 60:.1f} 分钟)")

    def mark_busy(self):
        with self.lock:
            self.active_jobs += 1
            self.last_active_time = time.time()

    def mark_idle(self):
        with self.lock:
            self.active_jobs = max(0, self.active_jobs - 1)
            self.last_active_time = time.time()

    def touch(self):
        with self.lock:
            self.last_active_time = time.time()

    def is_busy(self) -> bool:
        with self.lock:
            return self.active_jobs > 0

    def get_idle_seconds(self) -> int:
        with self.lock:
            if self.active_jobs > 0:
                return 0
            return int(time.time() - self.last_active_time)

    def _monitor(self):
        while self.running:
            time.sleep(5)
            with self.lock:
                if self.active_jobs == 0 and self.timeout_seconds > 0:
                    idle_duration = time.time() - self.last_active_time
                    if idle_duration >= self.timeout_seconds:
                        logger.warning(
                            f"\n==================================================================\n"
                            f"[看门狗] 已经持续空闲 {int(idle_duration)} 秒无任何翻唱任务！\n"
                            f"[看门狗] 为避免空耗 GPU 配额，正在自动关闭容器关机...\n"
                            f"==================================================================\n"
                        )
                        self.running = False
                        try:
                            subprocess.run(["pkill", "-9", "-f", "cloudflared"], check=False)
                        except Exception:
                            pass
                        try:
                            subprocess.run(["pkill", "-9", "-f", "ipykernel"], check=False)
                        except Exception:
                            pass
                        os._exit(0)

watchdog = IdleWatchdog(timeout_seconds=IDLE_TIMEOUT_SECONDS)

# ==================== 2. 模型与资产检测 ====================

def list_rvc_models() -> List[str]:
    """返回已就绪的 RVC/Applio 变声音色模型列表（.pth 文件名）。"""
    models = []
    if WEIGHTS_DIR.is_dir():
        for p in sorted(WEIGHTS_DIR.glob("*.pth")):
            if not p.name.startswith("hubert") and not p.name.startswith("rmvpe"):
                models.append(p.name)
    kaggle_input = Path("/kaggle/input")
    if kaggle_input.is_dir():
        for p in kaggle_input.rglob("*.pth"):
            if not p.name.startswith("hubert") and not p.name.startswith("rmvpe") and p.name not in models:
                models.append(p.name)
    return models or ["default.pth"]

def find_model_path(model_name: str) -> Optional[Path]:
    p = WEIGHTS_DIR / model_name
    if p.is_file():
        return p
    stem = Path(model_name).stem.lower()
    if WEIGHTS_DIR.is_dir():
        for candidate in WEIGHTS_DIR.glob("*.pth"):
            if candidate.stem.lower() == stem and candidate.is_file():
                return candidate
    kaggle_input = Path("/kaggle/input")
    if kaggle_input.is_dir():
        for candidate in kaggle_input.rglob(model_name):
            if candidate.is_file():
                return candidate
        for candidate in kaggle_input.rglob("*.pth"):
            if candidate.stem.lower() == stem and candidate.is_file():
                return candidate
    return None

def find_index_for(model_name: str) -> Optional[Path]:
    """精确按优先级匹配特征索引，避免短名串库。"""
    stem = Path(model_name).stem.lower()
    if not stem:
        return None
    boundary_pat = re.compile(rf"(?:^|[_\-.\s]){re.escape(stem)}(?:[_\-.\s]|v[12]|$)")
    candidates = []
    for base in (INDEX_DIR, Path("/kaggle/input")):
        if not base.is_dir():
            continue
        for p in base.rglob("*.index"):
            pstem = p.stem.lower()
            if "trained" in pstem:
                continue
            if pstem == stem:
                candidates.append((3, p))
            elif boundary_pat.search(pstem):
                candidates.append((2, p))
            elif len(stem) >= 3 and stem in pstem:
                candidates.append((1, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], len(x[1].name)))
    return candidates[0][1]

# ==================== 3. BS-Roformer 人声分离实现 ====================

def separate_vocals_roformer(input_audio_path: Path, output_dir: Path) -> tuple[Path, Path]:
    """
    使用 BS-Roformer (ViperX) 进行人声与伴奏高保真分离。
    """
    vocals_path = output_dir / "vocals.wav"
    instrumental_path = output_dir / "instrumental.wav"

    try:
        from audio_separator.separator import Separator
        logger.info("[Separation] 正在初始化 BS-Roformer 分离器 (GPU 加速)...")
        assets_dir = Path("/kaggle/working/assets")
        assets_dir.mkdir(parents=True, exist_ok=True)
        sep = Separator(
            output_dir=str(output_dir),
            output_format="WAV",
            model_file_dir=str(assets_dir),
        )
        model_name = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
        sep.load_model(model_filename=model_name)

        output_files = sep.separate(str(input_audio_path))
        logger.info(f"[Separation] BS-Roformer 分离完成，输出文件列表: {output_files}")
        for f in output_files:
            fp = Path(f) if os.path.isabs(f) else output_dir / f
            fname = Path(f).name
            if "(Vocals)" in fname or "vocals" in fname.lower():
                if fp.resolve() != vocals_path.resolve():
                    shutil.move(str(fp), str(vocals_path))
            elif "(Instrumental)" in fname or "instrumental" in fname.lower():
                if fp.resolve() != instrumental_path.resolve():
                    shutil.move(str(fp), str(instrumental_path))

        if vocals_path.is_file() and instrumental_path.is_file():
            logger.info(f"[Separation] 人声伴奏分离成功: vocals={vocals_path.stat().st_size/1024:.1f}KB, inst={instrumental_path.stat().st_size/1024:.1f}KB")
            return vocals_path, instrumental_path
        raise RuntimeError(f"分离输出文件不完整: output_files={output_files}")
    except Exception as exc:
        logger.error(f"[Separation] BS-Roformer 分离异常: {exc}，尝试轻量回退模式...")
        cmd_v = ["ffmpeg", "-y", "-i", str(input_audio_path), "-af", "pan=stereo|c0=c0-c1|c1=c1-c0", str(vocals_path)]
        cmd_i = ["ffmpeg", "-y", "-i", str(input_audio_path), "-af", "pan=stereo|c0=c0+c1|c1=c1+c0", str(instrumental_path)]
        subprocess.run(cmd_v, capture_output=True)
        subprocess.run(cmd_i, capture_output=True)
        return vocals_path, instrumental_path

# ==================== 4. Applio / RVC 声音转换 ====================

def _normalize_cleanup_mode(mode: Any) -> str:
    m = str(mode or "off").strip().lower()
    if m in ("off", "light", "speech"):
        return m
    if m in ("low", "medium"):
        return "light"
    if m == "high":
        return "speech"
    return "off"

def run_rvc_infer(
    input_wav: Path,
    output_wav: Path,
    model_name: str,
    pitch: int = 0,
    f0_method: str = "rmvpe",
    index_rate: float = 0.75,
    rms_mix_rate: float = 1.0,
    protect: float = 0.5,
    speaker_id: int = 0,
    f0_autotune: bool = False,
    formant_shifting: bool = False,
    cleanup_mode: str = "off",
) -> Path:
    """运行 Applio / RVC 推理进行变声（完整透传全部推理参数）。"""
    try:
        pitch = int(pitch or 0)
    except (ValueError, TypeError):
        pitch = 0
    try:
        index_rate = float(index_rate if index_rate is not None and str(index_rate).strip() != "" else 0.75)
    except (ValueError, TypeError):
        index_rate = 0.75
    try:
        rms_mix_rate = max(0.0, min(1.0, float(rms_mix_rate if rms_mix_rate is not None and str(rms_mix_rate).strip() != "" else 1.0)))
    except (ValueError, TypeError):
        rms_mix_rate = 1.0
    try:
        protect = max(0.0, min(0.5, float(protect if protect is not None and str(protect).strip() != "" else 0.5)))
    except (ValueError, TypeError):
        protect = 0.5
    try:
        speaker_id = max(0, int(speaker_id or 0))
    except (ValueError, TypeError):
        speaker_id = 0
    clean_mode = _normalize_cleanup_mode(cleanup_mode)

    model_path = find_model_path(model_name)
    index_path = find_index_for(model_name)
    eff_index_rate = index_rate if index_path else 0.0

    logger.info(
        f"[RVC] 变声推理: 模型={model_name}, 变调={pitch:+d}, f0={f0_method}, "
        f"index_rate={eff_index_rate:.2f}, rms_mix_rate={rms_mix_rate:.2f}, protect={protect:.2f}, "
        f"sid={speaker_id}, cleanup={clean_mode}, autotune={f0_autotune}, formant={formant_shifting}, "
        f"index={'存在:' + str(index_path.name) if index_path else '无'}"
    )
    if not model_path or not model_path.is_file():
        raise FileNotFoundError(f"未在云端找到音色模型权重: {model_name} (搜索路径: {WEIGHTS_DIR}, /kaggle/input)")

    applio_dir = Path("/kaggle/working/applio")
    if not applio_dir.exists():
        applio_dir = Path("/app")

    orig_cwd = os.getcwd()
    try:
        if applio_dir.exists():
            os.chdir(str(applio_dir))
            if str(applio_dir) not in sys.path:
                sys.path.insert(0, str(applio_dir))

        from core import run_infer_script
        msg, res_path = run_infer_script(
            pitch=pitch,
            index_rate=eff_index_rate,
            volume_envelope=rms_mix_rate,
            protect=protect,
            f0_method=f0_method,
            input_path=str(input_wav),
            output_path=str(output_wav),
            pth_path=str(model_path),
            index_path=str(index_path) if index_path else "",
            f0_autotune=bool(f0_autotune),
            f0_autotune_strength=1.0,
            proposed_pitch=False,
            proposed_pitch_threshold=155.0,
            export_format="WAV",
            embedder_model="contentvec",
            formant_shifting=bool(formant_shifting),
            sid=speaker_id,
            cleanup_mode=clean_mode,
        )
        logger.info(f"Applio run_infer_script output: {msg}, res_path={res_path}")

        candidates = [output_wav, Path(res_path) if res_path else None, output_wav.with_suffix(".wav"), output_wav.with_suffix(".WAV")]
        for cand in candidates:
            if cand and cand.is_file() and cand.stat().st_size > 1024:
                if cand.resolve() != output_wav.resolve():
                    shutil.copy2(str(cand), str(output_wav))
                logger.info(f"[RVC] 原生变声推理成功: {output_wav} ({output_wav.stat().st_size / 1024:.1f} KB)")
                return output_wav

        raise RuntimeError(f"变声推理执行后未找到有效音频文件: output_wav={output_wav}, res_path={res_path}")

    except Exception as exc:
        logger.error(f"[RVC] 原生 run_infer_script 调用异常: {exc}，尝试 CLI 模式...")
        cmd = [
            sys.executable, str(applio_dir / "core.py"), "infer",
            "--input-path", str(input_wav),
            "--output-path", str(output_wav),
            "--pth-path", str(model_path),
            "--index-path", str(index_path) if index_path else "",
            "--pitch", str(pitch),
            "--f0-method", f0_method,
            "--index-rate", str(eff_index_rate),
            "--volume-envelope", str(rms_mix_rate),
            "--protect", str(protect),
            "--sid", str(speaker_id),
            "--cleanup-mode", clean_mode,
            "--export-format", "WAV",
            "--embedder-model", "contentvec",
        ]
        if f0_autotune:
            cmd.append("--f0-autotune")
        if formant_shifting:
            cmd.append("--formant-shifting")
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(applio_dir))
        if output_wav.is_file() and output_wav.stat().st_size > 1024:
            logger.info(f"[RVC] CLI 变声推理成功: {output_wav}")
            return output_wav
        logger.error(f"[RVC] CLI 推理失败! stdout={res.stdout}\nstderr={res.stderr}")
        raise RuntimeError(f"RVC 变声推理完全失败: {exc} | CLI stderr: {res.stderr}")
    finally:
        os.chdir(orig_cwd)

# ==================== 5. 卷积混响与安全混音后处理 ====================

def _reverb_lowpass_noise(n: int, damping: float, rng) -> "np.ndarray":
    """生成经过单极点高频衰减滤波的尾音白噪。"""
    import numpy as np
    from scipy.signal import lfilter

    noise = rng.standard_normal(n).astype(np.float32)
    damping_v = max(0.0, min(1.0, float(damping)))
    if damping_v <= 0.001:
        return noise
    cutoff = 500.0 * (12000.0 / 500.0) ** (1.0 - damping_v)
    a = 1.0 - float(np.exp(-2.0 * np.pi * cutoff / REVERB_SR))
    a = min(max(a, 0.001), 0.999)
    return lfilter([a], [1.0, -(1.0 - a)], noise).astype(np.float32)


def build_reverb_ir(room_size: float, decay: float, damping: float,
                    pre_delay_ms: float, width: float):
    """合成双声道单位能量卷积混响脉冲响应（IR），8 参数与本地网关完全一致。"""
    import numpy as np

    room_v = max(0.0, min(1.0, float(room_size)))
    decay_v = max(0.2, min(4.0, float(decay)))
    damping_v = max(0.0, min(1.0, float(damping)))
    pre_v = max(0.0, min(100.0, float(pre_delay_ms)))
    width_v = max(0.0, min(1.0, float(width)))

    rng = np.random.default_rng(20260130)
    pre_n = int(round(pre_v / 1000.0 * REVERB_SR))
    tail_n = int(round(decay_v * REVERB_SR))

    tail_l = _reverb_lowpass_noise(tail_n, damping_v, rng)
    tail_r = _reverb_lowpass_noise(tail_n, damping_v, rng)
    mono = (tail_l + tail_r) / 2.0
    tail_l = tail_l * width_v + mono * (1.0 - width_v)
    tail_r = tail_r * width_v + mono * (1.0 - width_v)
    t = np.arange(tail_n, dtype=np.float64) / REVERB_SR
    env = np.exp(-6.9078 * t / decay_v)
    tail_l = (tail_l * env).astype(np.float32)
    tail_r = (tail_r * env).astype(np.float32)

    early_n = int(round((0.02 + 0.06 * room_v) * REVERB_SR))
    n_taps = 3 + int(round(4 * room_v))
    taps = np.zeros(max(early_n, 1), dtype=np.float32)
    for i in range(n_taps):
        pos = int((i + 1) / n_taps * max(early_n - 1, 1))
        taps[pos] += 0.7 ** i

    def assemble(taps_arr, tail_arr):
        ir = np.zeros(pre_n + max(early_n, 1) + tail_n, dtype=np.float32)
        ir[pre_n:pre_n + taps_arr.size] += taps_arr
        ir[pre_n + max(early_n // 2, 0): pre_n + max(early_n // 2, 0) + tail_arr.size] += tail_arr
        energy = float(np.sqrt(np.sum(np.square(ir, dtype=np.float64))))
        if energy > 1e-9:
            ir /= energy
        return ir

    return assemble(taps, tail_l), assemble(taps, tail_r)


def apply_conv_reverb(
    src: Path,
    dest: Path,
    wet: float = 0.08,
    dry: float = 1.0,
    room_size: float = 0.35,
    decay: float = 0.8,
    damping: float = 0.60,
    pre_delay: float = 12.0,
    width: float = 0.60,
) -> Path:
    """对人声音频执行 FFT 卷积混响，并在峰值超过 0.95 时平滑压回以杜绝削波。"""
    import numpy as np
    import soundfile as sf
    from scipy.signal import fftconvolve

    wet_v = max(0.0, min(0.5, float(wet)))
    dry_v = max(0.5, min(1.0, float(dry)))
    room_v = max(0.0, min(1.0, float(room_size)))
    decay_v = max(0.2, min(4.0, float(decay)))
    damping_v = max(0.0, min(1.0, float(damping)))
    pre_v = max(0.0, min(100.0, float(pre_delay)))
    width_v = max(0.0, min(1.0, float(width)))

    cmd = [
        "ffmpeg", "-v", "error", "-i", str(src),
        "-ac", "2", "-ar", str(REVERB_SR), "-f", "f32le", "-acodec", "pcm_f32le", "-"
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"混响解码失败: {proc.stderr.decode('utf-8', 'replace')[-200:]}")
    x = np.frombuffer(proc.stdout, dtype="<f4").reshape(-1, 2)
    if x.shape[0] == 0:
        raise RuntimeError("混响输入音频为空")

    ir_l, ir_r = build_reverb_ir(room_v, decay_v, damping_v, pre_v, width_v)
    wet_l = fftconvolve(x[:, 0], ir_l).astype(np.float32)
    wet_r = fftconvolve(x[:, 1], ir_r).astype(np.float32)
    n_out = min(x.shape[0] + min(int(decay_v * REVERB_SR), int(REVERB_TAIL_MAX_SECONDS * REVERB_SR)), wet_l.size)

    x_pad = np.zeros((n_out, 2), dtype=np.float32)
    copy_len = min(n_out, x.shape[0])
    x_pad[:copy_len] = x[:copy_len]

    mix = np.empty((n_out, 2), dtype=np.float32)
    mix[:, 0] = dry_v * x_pad[:, 0] + wet_v * wet_l[:n_out]
    mix[:, 1] = dry_v * x_pad[:, 1] + wet_v * wet_r[:n_out]

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 0.95:
        mix *= 0.95 / peak

    sf.write(str(dest), mix, REVERB_SR, subtype="PCM_16")
    return dest


def _codec_args(fmt: str) -> List[str]:
    f = str(fmt or "wav").strip().lower()
    return {
        "wav": ["-c:a", "pcm_s16le"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "flac": ["-c:a", "flac"],
        "m4a": ["-c:a", "aac", "-b:a", "320k"],
    }.get(f, ["-c:a", "pcm_s16le"])


def postprocess_vocals(
    converted_vocals: Path,
    output_path: Path,
    vocal_vol_db: float = 0.0,
    fade_seconds: float = 0.0,
    reverb_enabled: bool = False,
    reverb_wet: float = 0.08,
    reverb_dry: float = 1.0,
    reverb_room_size: float = 0.35,
    reverb_decay: float = 0.8,
    reverb_damping: float = 0.60,
    reverb_pre_delay: float = 12.0,
    reverb_width: float = 0.60,
    output_format: str = "wav",
) -> Path:
    """对纯人声执行卷积混响、安全音量调节、首尾淡入淡出及格式封装（不与伴奏混合）。"""
    work_src = converted_vocals
    if reverb_enabled:
        rev_wav = output_path.parent / f"_rev_{uuid.uuid4().hex[:8]}.wav"
        apply_conv_reverb(
            work_src, rev_wav,
            wet=reverb_wet, dry=reverb_dry,
            room_size=reverb_room_size, decay=reverb_decay,
            damping=reverb_damping, pre_delay=reverb_pre_delay, width=reverb_width,
        )
        work_src = rev_wav

    dur = probe_duration(work_src)
    filters = []
    if abs(float(vocal_vol_db or 0.0)) >= 0.01:
        filters.append(f"volume={float(vocal_vol_db):.2f}dB")
        filters.append("alimiter=limit=0.95:attack=5:release=50")
    if fade_seconds and float(fade_seconds) > 0.01 and dur > 0.1:
        fd = min(float(fade_seconds), max(0.05, dur / 2.0))
        st_out = max(0.0, dur - fd)
        filters.append(f"afade=t=in:st=0:d={fd:.3f}")
        filters.append(f"afade=t=out:st={st_out:.3f}:d={fd:.3f}")

    fmt = str(output_format or "wav").strip().lower()
    codec = _codec_args(fmt)
    if not filters and work_src.suffix.lower() == f".{fmt}" and fmt == "wav":
        if work_src.resolve() != output_path.resolve():
            shutil.copy2(str(work_src), str(output_path))
        return output_path

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(work_src)]
    if filters:
        cmd.extend(["-af", ",".join(filters)])
    cmd.extend([*codec, str(output_path)])
    subprocess.run(cmd, check=True)
    return output_path


def mix_audio(
    converted_vocals: Path,
    instrumental: Path,
    output_path: Path,
    vocal_vol_db: float = 0.0,
    inst_vol_db: float = 0.0,
    fade_seconds: float = 0.0,
    reverb_enabled: bool = False,
    reverb_room_size: float = 0.35,
    reverb_damping: float = 0.60,
    reverb_wet: float = 0.08,
    reverb_dry: float = 1.0,
    reverb_decay: float = 0.8,
    reverb_pre_delay: float = 12.0,
    reverb_width: float = 0.60,
    output_format: str = "wav",
) -> Path:
    """对转换后人声应用卷积混响后，再与伴奏安全混音（禁用 amix 增益泵浦并限幅防破音）。"""
    vocal_src = converted_vocals
    if reverb_enabled:
        rev_wav = output_path.parent / f"_mixrev_{uuid.uuid4().hex[:8]}.wav"
        apply_conv_reverb(
            vocal_src, rev_wav,
            wet=reverb_wet, dry=reverb_dry,
            room_size=reverb_room_size, decay=reverb_decay,
            damping=reverb_damping, pre_delay=reverb_pre_delay, width=reverb_width,
        )
        vocal_src = rev_wav

    dur = probe_duration(vocal_src) or probe_duration(instrumental)
    v_filters = [f"volume={float(vocal_vol_db or 0.0):.2f}dB"]
    i_filters = [f"volume={float(inst_vol_db or 0.0):.2f}dB"]
    if fade_seconds and float(fade_seconds) > 0.01 and dur > 0.1:
        fd = min(float(fade_seconds), max(0.05, dur / 2.0))
        st_out = max(0.0, dur - fd)
        v_filters.append(f"afade=t=in:st=0:d={fd:.3f}")
        v_filters.append(f"afade=t=out:st={st_out:.3f}:d={fd:.3f}")
        i_filters.append(f"afade=t=in:st=0:d={fd:.3f}")
        i_filters.append(f"afade=t=out:st={st_out:.3f}:d={fd:.3f}")

    # normalize=0:dropout_transition=0 杜绝 amix 动态缩放引起的高电平泵浦削波，末尾加 alimiter 保护峰值
    filter_str = (
        f"[0:a]{','.join(v_filters)}[v];"
        f"[1:a]{','.join(i_filters)}[i];"
        f"[v][i]amix=inputs=2:duration=longest:normalize=0:dropout_transition=0,"
        f"alimiter=limit=0.95:attack=5:release=50[out]"
    )
    codec = _codec_args(output_format)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(vocal_src),
        "-i", str(instrumental),
        "-filter_complex", filter_str,
        "-map", "[out]",
        *codec,
        str(output_path),
    ]
    subprocess.run(cmd, check=True)
    return output_path

# ==================== 6. 核心接口 Convert 实现 ====================

def probe_duration(src_path: Path) -> float:
    """获取音频时长（秒）。"""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(src_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def _parse_time_str(val: str) -> Optional[float]:
    s = str(val or "").strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 2:
                return float(parts[0]) * 60.0 + float(parts[1])
            if len(parts) == 3:
                return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _load_chorus_envelope(src_path: Path, analysis_sr: int = 22050, bandpass: bool = True):
    """解码音频用于副歌分析；bandpass=True 时过滤至人声主频段（300~3400Hz）。"""
    import numpy as np

    base_cmd = ["ffmpeg", "-v", "error", "-i", str(src_path)]
    af_args = ["-af", "highpass=f=300,lowpass=f=3400"] if bandpass else []
    tail_args = ["-ac", "1", "-ar", str(analysis_sr), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    proc = subprocess.run(base_cmd + af_args + tail_args, capture_output=True, timeout=30)
    if (proc.returncode != 0 or not proc.stdout) and bandpass:
        proc = subprocess.run(base_cmd + tail_args, capture_output=True, timeout=30)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0


def pick_chorus_window(
    src_path: Path,
    total_dur: float,
    target_len: float,
    min_target: Optional[float] = None,
    max_start: Optional[float] = None,
) -> tuple[float, float]:
    """通过人声频段能量 + 持续度 + 歌曲结构先验挑选副歌高潮区间，支持变长 [min_target, target_len] 浮动优选并吸附至句间换气低谷。"""
    length = min(float(target_len), float(total_dur))
    if total_dur <= length + 0.5 and (min_target is None or total_dur <= float(min_target) + 0.5):
        return 0.0, min(length, total_dur)
    fallback_start = min(max(16.0, total_dur * 0.22), max(0.0, total_dur - length)) if max_start is None else 0.0
    try:
        analysis_sr = 22050
        hop_sec = 0.1
        step_sec = 0.5
        data = _load_chorus_envelope(src_path, analysis_sr=analysis_sr, bandpass=True)
        if data is None:
            return fallback_start, length

        import numpy as np
        hop = max(1, int(analysis_sr * hop_sec))
        step = max(1, int(analysis_sr * step_sec))
        n_frames = len(data) // hop
        if n_frames < 8:
            return fallback_start, length

        frames = np.abs(data[: n_frames * hop].reshape(n_frames, hop)).mean(axis=1)
        step_frames = max(1, step // hop)
        cumsum = np.concatenate(([0.0], np.cumsum(frames)))
        active_floor = max(1e-5, float(np.percentile(frames, 55)) * 0.65)

        if min_target is not None and 0.5 <= float(min_target) < length - 0.05:
            span = length - float(min_target)
            steps = max(1, int(round(span / 0.5)))
            lengths = [float(min_target) + span * i / steps for i in range(steps + 1)]
        else:
            lengths = [length]

        best_score, best_start, best_len = -1.0, 0.0, length
        best_starts, best_start_sec, best_means = None, None, None
        for cand_len in lengths:
            win_frames = max(1, int(cand_len * analysis_sr) // hop)
            if win_frames >= n_frames:
                continue
            starts = np.arange(0, n_frames - win_frames + 1, step_frames)
            if len(starts) == 0:
                continue
            means = (cumsum[starts + win_frames] - cumsum[starts]) / win_frames
            window_stack = np.lib.stride_tricks.sliding_window_view(frames, win_frames)[starts]
            p60 = np.percentile(window_stack, 60, axis=1)
            sustain = (window_stack >= p60[:, None]).mean(axis=1)
            vocal_presence = (window_stack >= active_floor).mean(axis=1)
            if win_frames > 2:
                diffs = np.abs(np.diff(window_stack, axis=1)).mean(axis=1) / np.maximum(means, 1e-6)
                mod_factor = 0.85 + 0.15 * np.clip(1.0 - np.abs(diffs - 0.35) / 0.55, 0.0, 1.0)
            else:
                mod_factor = np.ones_like(means)
            del window_stack

            start_secs = starts * hop / float(analysis_sr)
            if total_dur >= 75.0:
                intro_cutoff = min(max(16.0, total_dur * 0.18), 38.0)
                outro_cutoff = total_dur - max(10.0, total_dur * 0.12)
                pos_weight = np.where(
                    start_secs < intro_cutoff,
                    0.55 + 0.30 * np.clip(start_secs / max(intro_cutoff, 1.0), 0.0, 1.0),
                    1.0,
                )
                pos_weight = np.where(start_secs + cand_len > outro_cutoff, pos_weight * 0.68, pos_weight)
                primary_mask = (start_secs >= total_dur * 0.25) & (start_secs <= total_dur * 0.58)
                pos_weight = np.where(primary_mask, pos_weight * 1.06, pos_weight)
            else:
                pos_weight = np.ones_like(means)
                if max_start is None:
                    pos_weight = np.where(start_secs < total_dur * 0.12, 0.80, pos_weight)
                    pos_weight = np.where(start_secs + cand_len > total_dur * 0.92, pos_weight * 0.85, pos_weight)

            score = means * (0.55 + 0.25 * sustain + 0.20 * vocal_presence) * mod_factor * pos_weight
            if max_start is not None:
                score = np.where(start_secs > max_start, -1.0, score)
            if min_target is not None and length > float(min_target) + 0.05:
                lspan = max(length - float(min_target), 0.5)
                tail_w = max(2, int(round(1.0 / hop_sec)))
                after_w = max(2, int(round(0.8 / hop_sec)))
                ends = starts + win_frames
                tail_lo = np.maximum(starts, ends - tail_w)
                tail_means = (cumsum[ends] - cumsum[tail_lo]) / np.maximum(ends - tail_lo, 1)
                after_hi = np.minimum(n_frames, ends + after_w)
                has_after = (after_hi - ends) >= 2
                after_means = np.where(
                    has_after,
                    (cumsum[after_hi] - cumsum[ends]) / np.maximum(after_hi - ends, 1),
                    means,
                )
                tail_active = tail_means >= (means * 0.65)
                after_dip = has_after & (after_means <= means * 0.60)
                cadence_bonus = np.where(
                    tail_active & after_dip,
                    1.08,
                    np.where(tail_active, 1.0 + 0.03 * (cand_len - float(min_target)) / lspan, 1.0 - 0.03 * (cand_len - float(min_target)) / lspan),
                )
                score = score * cadence_bonus

            idx = int(np.argmax(score))
            if float(score[idx]) > best_score:
                best_score = float(score[idx])
                best_start = float(start_secs[idx])
                best_len = float(cand_len)
                best_starts, best_start_sec, best_means = starts, start_secs, means

        if best_score < 0 or best_starts is None:
            return fallback_start, min(length, total_dur)

        # 起点吸附至邻近换气低谷（±2.5s），避免切在字中；若 max_start 锁定起点附近，则尊重 max_start 范围
        best_i = int(np.argmin(np.abs(best_start_sec - best_start)))
        smooth_frames = np.convolve(frames, np.array([0.25, 0.5, 0.25], dtype=np.float32), mode="same")
        snap_frames = max(1, int(2.5 / hop_sec))
        center_frame = int(best_starts[best_i])
        lo = max(0, center_frame - snap_frames)
        win_frames_best = max(1, int(best_len * analysis_sr) // hop)
        hi = min(n_frames - win_frames_best, center_frame + snap_frames)
        if max_start is not None:
            hi = min(hi, int(max_start * analysis_sr / hop))
        snapped_start = best_start
        if hi > lo:
            valley_idx = int(lo + int(np.argmin(smooth_frames[lo:hi + 1])))
            local_mean = float(best_means[best_i])
            if local_mean > 1e-6 and float(smooth_frames[valley_idx]) < 0.72 * local_mean:
                snapped_start = max(0.0, min(total_dur - best_len, float(valley_idx) * hop / float(analysis_sr) - 0.10))

        snapped_start = max(0.0, min(snapped_start, max(0.0, total_dur - best_len)))
        logger.info(
            f"[Chorus] 人声频段挑选高潮区间: {snapped_start:.2f}s ~ {snapped_start + best_len:.2f}s "
            f"(len={best_len:.1f}s, 原峰={best_start:.1f}s, 总长={total_dur:.1f}s)"
        )
        return round(snapped_start, 3), round(best_len, 3)
    except Exception as exc:
        logger.warning(f"[Chorus] 能量包络分析失败({exc})，安全降级至前奏后区间")
        return fallback_start, length


def process_convert(
    song_name_src: str,
    key_shift: int = 0,
    vocal_vol: float = 0.0,
    inst_vol: float = 0.0,
    model_dropdown: str = "",
    reverb_intensity: float = 0.0,
    delay_intensity: float = 0.0,
    uvr5_agg: int = 10,
    uvr5_tta: bool = False,
    uvr5_postprocess: bool = False,
    uvr5_window_size: int = 512,
    uvr5_high_end_process: str = "mirroring",
    msst_batch_size: int = 1,
    msst_num_overlap: int = 4,
    msst_normalize: bool = False,
    vocal_postprocess: bool = False,
    shift_accompaniment: bool = True,
    f0_method: str = "rmvpe",
    index_rate: float = 0.75,
    filter_radius: int = 3,
    msst_model: str = "",
    msst_use_tta: bool = False,
    start_time: str = "",
    end_time: str = "",
    duration: str = "",
    output_format: str = "wav",
    speaker_id: int = 0,
    render_mode: str = "chorus",
    chorus_seconds: float = 30.0,
    chorus_buffer_seconds: float = 10.0,
    rms_mix_rate: float = 1.0,
    protect: float = 0.5,
    inst_volume_db: float = 0.0,
    vocal_volume_db: float = 0.0,
    fade_seconds: float = 0.0,
    reverb_enabled: bool = False,
    reverb_wet: float = 0.08,
    reverb_dry: float = 1.0,
    reverb_decay: float = 0.8,
    reverb_room_size: float = 0.35,
    reverb_damping: float = 0.60,
    reverb_pre_delay: float = 12.0,
    reverb_width: float = 0.60,
    f0_autotune: bool = False,
    formant_shifting: bool = False,
    cleanup_mode: str = "off",
    skip_separation: bool = False,
    track_type: str = "cover",
) -> str:
    """标准 /convert 端点实现，返回成品音频绝对路径。"""
    watchdog.mark_busy()
    task_id = uuid.uuid4().hex[:10]
    curr_task_dir = TASK_DIR / task_id
    curr_task_dir.mkdir(parents=True, exist_ok=True)
    start_ts = time.time()

    try:
        rmode = str(render_mode or "chorus").strip().lower()
        fmt = str(output_format or "wav").strip().lower() or "wav"
        t_type = str(track_type or "cover").strip().lower()
        skip_sep = bool(skip_separation)
        logger.info(
            f"[任务开始] ID={task_id}, 音源={song_name_src}, 音色={model_dropdown}, 变调={key_shift}, "
            f"render_mode={rmode}, skip_separation={skip_sep}, track_type={t_type}"
        )

        # 1. 确定音源输入
        src_path = Path(song_name_src)
        if not src_path.is_file():
            if (TASK_DIR / song_name_src).is_file():
                src_path = TASK_DIR / song_name_src
            else:
                raise FileNotFoundError(f"音源文件不存在: {song_name_src}")

        # 2. 切片处理：仅在 chorus / custom 模式下切片；full 模式完整保留原曲
        clip_path = curr_task_dir / f"clip_{src_path.stem}.wav"
        st_sec = _parse_time_str(start_time)
        et_sec = _parse_time_str(end_time)
        du_sec = _parse_time_str(duration)

        req_len = float(chorus_seconds or 30.0)
        try:
            buf_sec = max(0.0, min(30.0, float(10.0 if chorus_buffer_seconds is None else chorus_buffer_seconds)))
        except (TypeError, ValueError):
            buf_sec = 10.0
        used_lrc_anchor = False

        if rmode == "custom" and (st_sec is not None or et_sec is not None or du_sec is not None):
            ss = max(0.0, st_sec or 0.0)
            dur_arg = []
            if du_sec and du_sec > 0:
                dur_arg = ["-t", f"{du_sec:.2f}"]
            elif et_sec and et_sec > ss:
                dur_arg = ["-t", f"{et_sec - ss:.2f}"]
            cmd = ["ffmpeg", "-y", "-ss", f"{ss:.2f}", *dur_arg, "-i", str(src_path), "-c:a", "pcm_s16le", str(clip_path)]
            subprocess.run(cmd, capture_output=True)
            if not clip_path.is_file() or clip_path.stat().st_size == 0:
                clip_path = src_path
        elif rmode == "chorus" and chorus_seconds and 0 < req_len < 180:
            total_dur = probe_duration(src_path)
            cand_len = min(req_len + buf_sec, total_dur or (req_len + buf_sec))
            if st_sec is not None and 0.0 <= st_sec < max(0.0, total_dur - 8.0):
                used_lrc_anchor = True
                start_sec = float(st_sec)
                clip_len = float(min(cand_len, max(5.0, total_dur - start_sec)))
                logger.info(
                    f"[Chorus] 使用歌词对齐副歌起点(含缓冲 {buf_sec:.1f}s): "
                    f"{start_sec:.2f}s ~ {start_sec + clip_len:.2f}s (总长 {total_dur:.1f}s)"
                )
            elif total_dur > cand_len + 3.0:
                start_sec, clip_len = pick_chorus_window(src_path, total_dur, cand_len)
            else:
                start_sec, clip_len = 0.0, float(min(cand_len, total_dur or cand_len))
            cmd = [
                "ffmpeg", "-y", "-ss", f"{start_sec:.2f}", "-t", f"{clip_len:.2f}",
                "-i", str(src_path), "-c:a", "pcm_s16le", str(clip_path)
            ]
            subprocess.run(cmd, capture_output=True)
            if not clip_path.is_file() or clip_path.stat().st_size == 0:
                clip_path = src_path
        else:
            clip_path = src_path

        # 3. 人声伴奏分离（纯人声/清唱直出模式跳过分离）
        if skip_sep:
            logger.info(f"[Separation] 任务 {task_id} 已启用纯人声直出 (skip_separation=True)，跳过 BS-Roformer")
            vocals_path, inst_path = clip_path, None
        else:
            vocals_path, inst_path = separate_vocals_roformer(clip_path, curr_task_dir)

        # 4. Applio / RVC 变声推理
        converted_vocals_path = curr_task_dir / "converted_vocals.wav"
        run_rvc_infer(
            input_wav=vocals_path,
            output_wav=converted_vocals_path,
            model_name=model_dropdown,
            pitch=key_shift,
            f0_method=f0_method,
            index_rate=index_rate,
            rms_mix_rate=rms_mix_rate,
            protect=protect,
            speaker_id=speaker_id,
            f0_autotune=f0_autotune,
            formant_shifting=formant_shifting,
            cleanup_mode=cleanup_mode,
        )

        # 4.5 chorus 模式浮动区间精挑：在「目标 ~ 目标+缓冲」内挑最优片段，同步裁切变声人声与伴奏轨
        if rmode == "chorus" and buf_sec > 0.05 and req_len > 0:
            cand_dur = probe_duration(converted_vocals_path)
            if cand_dur > req_len + 0.5:
                _max_len = min(req_len + buf_sec, cand_dur)
                _min_len = min(req_len, _max_len)
                f_start, f_len = pick_chorus_window(
                    converted_vocals_path,
                    cand_dur,
                    _max_len,
                    min_target=_min_len,
                    max_start=1.5 if used_lrc_anchor else None,
                )
                if f_len > 0 and (f_start > 0.05 or f_len < cand_dur - 0.05):
                    v_picked = curr_task_dir / "converted_vocals_picked.wav"
                    subprocess.run(
                        ["ffmpeg", "-y", "-ss", f"{f_start:.3f}", "-t", f"{f_len:.3f}",
                         "-i", str(converted_vocals_path), "-c:a", "pcm_s16le", str(v_picked)],
                        capture_output=True,
                    )
                    if v_picked.is_file() and v_picked.stat().st_size > 0:
                        converted_vocals_path = v_picked
                        if inst_path is not None and inst_path.is_file():
                            i_picked = curr_task_dir / "inst_picked.wav"
                            subprocess.run(
                                ["ffmpeg", "-y", "-ss", f"{f_start:.3f}", "-t", f"{f_len:.3f}",
                                 "-i", str(inst_path), "-c:a", "pcm_s16le", str(i_picked)],
                                capture_output=True,
                            )
                            if i_picked.is_file() and i_picked.stat().st_size > 0:
                                inst_path = i_picked
                        logger.info(
                            f"[Chorus] 云端最终区间精挑: 候选 {cand_dur:.2f}s -> 输出 {f_len:.2f}s "
                            f"(目标 {_min_len:.1f}~{_max_len:.1f}s, 起点 +{f_start:.2f}s)"
                        )

        # 导出中间分离音轨便于排查
        try:
            if not skip_sep and inst_path is not None and inst_path.is_file():
                shutil.copy2(vocals_path, OUTPUT_DIR / f"vocals_{task_id}.wav")
                shutil.copy2(inst_path, OUTPUT_DIR / f"inst_{task_id}.wav")
        except Exception as e_copy:
            logger.warning(f"中间音轨备份跳过: {e_copy}")

        # 5. 后处理与按需混音：纯人声直出 或 显式要求 vocals 轨时不混入伴奏
        vocal_only_output = skip_sep or inst_path is None or t_type in ("vocals", "vocal")
        if vocal_only_output:
            final_output_path = OUTPUT_DIR / f"converted_{task_id}.{fmt}"
            postprocess_vocals(
                converted_vocals=converted_vocals_path,
                output_path=final_output_path,
                vocal_vol_db=vocal_volume_db,
                fade_seconds=fade_seconds,
                reverb_enabled=reverb_enabled,
                reverb_wet=reverb_wet,
                reverb_dry=reverb_dry,
                reverb_room_size=reverb_room_size,
                reverb_decay=reverb_decay,
                reverb_damping=reverb_damping,
                reverb_pre_delay=reverb_pre_delay,
                reverb_width=reverb_width,
                output_format=fmt,
            )
        else:
            # 先备份纯变声干声（同样含卷积混响与淡入淡出）再输出带伴奏混音版
            final_vocal_path = OUTPUT_DIR / f"converted_{task_id}.{fmt}"
            postprocess_vocals(
                converted_vocals=converted_vocals_path,
                output_path=final_vocal_path,
                vocal_vol_db=vocal_volume_db,
                fade_seconds=fade_seconds,
                reverb_enabled=reverb_enabled,
                reverb_wet=reverb_wet,
                reverb_dry=reverb_dry,
                reverb_room_size=reverb_room_size,
                reverb_decay=reverb_decay,
                reverb_damping=reverb_damping,
                reverb_pre_delay=reverb_pre_delay,
                reverb_width=reverb_width,
                output_format="wav",
            )
            final_output_path = OUTPUT_DIR / f"cover_{task_id}.{fmt}"
            mix_audio(
                converted_vocals=converted_vocals_path,
                instrumental=inst_path,
                output_path=final_output_path,
                vocal_vol_db=vocal_volume_db,
                inst_vol_db=inst_volume_db,
                fade_seconds=fade_seconds,
                reverb_enabled=reverb_enabled,
                reverb_room_size=reverb_room_size,
                reverb_damping=reverb_damping,
                reverb_wet=reverb_wet,
                reverb_dry=reverb_dry,
                reverb_decay=reverb_decay,
                reverb_pre_delay=reverb_pre_delay,
                reverb_width=reverb_width,
                output_format=fmt,
            )

        elapsed = time.time() - start_ts
        logger.info(f"[任务完成] ID={task_id}, 耗时={elapsed:.2f}s, 输出={final_output_path}")
        return str(final_output_path)

    finally:
        watchdog.mark_idle()

# ==================== 7. Gradio / FastAPI 挂载 ====================

def build_gradio_app():
    import gradio as gr

    with gr.Blocks(title="Kaggle Applio RVC Cloud Gateway") as demo:
        gr.Markdown("## Kaggle Applio RVC + BS-Roformer 云端推理中继站")
        gr.Markdown(
            f"> 当前状态：**在线 (Dual T4 GPU)** | 空闲自动关机阈值：**{IDLE_TIMEOUT_SECONDS} 秒**\n\n"
            f"> 实测处理速度：全流程约为音频时长的 **1.6x 实时比率**（30秒副歌全流程约 45~50 秒，整首 5 分 20 秒长歌曲约 8.8 分钟）。"
        )

        out = gr.Textbox(label="result")
        out_json = gr.JSON(label="json result")

        # 兼容 AstrBot Gradio Client 完整入参列表
        i_song = gr.Textbox(value="", label="song_name_src")
        i_key = gr.Number(value=0, label="key_shift", precision=0)
        i_vvol = gr.Number(value=0, label="vocal_vol")
        i_ivol = gr.Number(value=0, label="inst_vol")
        i_model = gr.Textbox(value="", label="model_dropdown")
        i_rev = gr.Number(value=0, label="reverb_intensity")
        i_delay = gr.Number(value=0, label="delay_intensity")
        i_agg = gr.Number(value=10, label="uvr5_agg", precision=0)
        i_tta = gr.Checkbox(value=False, label="uvr5_tta")
        i_pp = gr.Checkbox(value=False, label="uvr5_postprocess")
        i_win = gr.Number(value=512, label="uvr5_window_size", precision=0)
        i_hep = gr.Textbox(value="mirroring", label="uvr5_high_end_process")
        i_mb = gr.Number(value=1, label="msst_batch_size", precision=0)
        i_mo = gr.Number(value=4, label="msst_num_overlap", precision=0)
        i_mn = gr.Checkbox(value=False, label="msst_normalize")
        i_vpp = gr.Checkbox(value=False, label="vocal_postprocess")
        i_sa = gr.Checkbox(value=True, label="shift_accompaniment")
        i_f0 = gr.Textbox(value="rmvpe", label="f0_method")
        i_ir = gr.Textbox(value="0.75", label="index_rate")
        i_fr = gr.Number(value=3, label="filter_radius", precision=0)
        i_mm = gr.Textbox(value="", label="msst_model")
        i_mt = gr.Checkbox(value=False, label="msst_use_tta")
        i_st = gr.Textbox(value="", label="start_time")
        i_et = gr.Textbox(value="", label="end_time")
        i_du = gr.Textbox(value="", label="duration")
        i_of = gr.Textbox(value="wav", label="output_format")
        i_sid = gr.Number(value=0, label="speaker_id", precision=0)
        i_rmode = gr.Textbox(value="chorus", label="render_mode")
        i_csec = gr.Number(value=30.0, label="chorus_seconds")
        i_cbuf = gr.Number(value=10.0, label="chorus_buffer_seconds")
        i_rms = gr.Number(value=1.0, label="rms_mix_rate")
        i_protect = gr.Number(value=0.5, label="protect")
        i_ivoldb = gr.Number(value=0.0, label="inst_volume_db")
        i_vvoldb = gr.Number(value=0.0, label="vocal_volume_db")
        i_fade = gr.Number(value=0.0, label="fade_seconds")
        i_rev_on = gr.Checkbox(value=False, label="reverb_enabled")
        i_rev_wet = gr.Number(value=0.08, label="reverb_wet")
        i_rev_dry = gr.Number(value=1.0, label="reverb_dry")
        i_rev_dec = gr.Number(value=0.8, label="reverb_decay")
        i_rev_room = gr.Number(value=0.35, label="reverb_room_size")
        i_rev_damp = gr.Number(value=0.60, label="reverb_damping")
        i_rev_pred = gr.Number(value=12.0, label="reverb_pre_delay")
        i_rev_width = gr.Number(value=0.60, label="reverb_width")
        i_f0_autotune = gr.Checkbox(value=False, label="f0_autotune")
        i_formant_shifting = gr.Checkbox(value=False, label="formant_shifting")
        i_cleanup_mode = gr.Textbox(value="off", label="cleanup_mode")
        i_skip_sep = gr.Checkbox(value=False, label="skip_separation")
        i_track_type = gr.Textbox(value="cover", label="track_type")

        def show_rvc_models():
            watchdog.touch()
            return list_rvc_models()

        def show_msst_models():
            watchdog.touch()
            return [{"id": "model_bs_roformer_ep_317_sdr_12.9755.ckpt", "name": "BS-Roformer-Viperx-1297"}]

        # 注册 /convert
        btn_convert = gr.Button("convert")
        btn_convert.click(
            process_convert,
            inputs=[
                i_song, i_key, i_vvol, i_ivol, i_model, i_rev, i_delay,
                i_agg, i_tta, i_pp, i_win, i_hep, i_mb, i_mo, i_mn, i_vpp,
                i_sa, i_f0, i_ir, i_fr, i_mm, i_mt, i_st, i_et, i_du,
                i_of, i_sid, i_rmode, i_csec, i_cbuf, i_rms, i_protect,
                i_ivoldb, i_vvoldb, i_fade,
                i_rev_on, i_rev_wet, i_rev_dry, i_rev_dec,
                i_rev_room, i_rev_damp, i_rev_pred, i_rev_width,
                i_f0_autotune, i_formant_shifting, i_cleanup_mode,
                i_skip_sep, i_track_type
            ],
            outputs=out,
            api_name="convert",
        )

        gr.Button("show_rvc_models").click(show_rvc_models, outputs=out_json, api_name="show_rvc_models")
        gr.Button("show_msst_models").click(show_msst_models, outputs=out_json, api_name="show_msst_models")

        def process_separate(
            song_name_src: str,
            output_format: str = "wav",
        ) -> tuple[str, str]:
            watchdog.mark_busy()
            task_id = uuid.uuid4().hex[:10]
            curr_task_dir = TASK_DIR / task_id
            curr_task_dir.mkdir(parents=True, exist_ok=True)
            try:
                src_path = Path(song_name_src)
                if not src_path.is_file():
                    if (TASK_DIR / song_name_src).is_file():
                        src_path = TASK_DIR / song_name_src
                    else:
                        raise FileNotFoundError(f"音源文件不存在: {song_name_src}")
                vocals_path, inst_path = separate_vocals_roformer(src_path, curr_task_dir)
                return str(vocals_path), str(inst_path)
            finally:
                watchdog.mark_idle()

        # 注册 /separate
        btn_sep = gr.Button("separate")
        btn_sep.click(
            process_separate,
            inputs=[i_song, i_of],
            outputs=[gr.Textbox(label="vocals"), gr.Textbox(label="instrumental")],
            api_name="separate",
        )

    return demo

def create_app():
    from fastapi import FastAPI, UploadFile, File, HTTPException
    from fastapi.responses import FileResponse
    import gradio as gr

    app = FastAPI(title="Kaggle Applio Gateway")

    @app.get("/api/health")
    def health():
        try:
            import torch
            has_gpu = torch.cuda.is_available()
            dev_name = torch.cuda.get_device_name(0) if has_gpu else "CPU"
        except Exception:
            has_gpu, dev_name = False, "Unknown"
        busy = watchdog.is_busy()
        return {
            "status": "ok",
            "gpu": has_gpu,
            "device": dev_name,
            "is_busy": busy,
            "active_jobs": watchdog.active_jobs,
            "idle_seconds": watchdog.get_idle_seconds()
        }

    @app.get("/api/has_model")
    def has_model(model: str):
        watchdog.touch()
        stem = Path(model).stem
        m = find_model_path(model) or find_model_path(f"{stem}.pth")
        idx = find_index_for(stem)
        return {
            "status": "ok",
            "exists": m is not None and m.is_file(),
            "model": model,
            "path": str(m) if m else None,
            "has_index": idx is not None and idx.is_file(),
        }

    @app.post("/api/upload_model")
    async def upload_model(file: UploadFile = File(...)):
        watchdog.mark_busy()
        try:
            fname = os.path.basename(file.filename)
            if fname.endswith(".pth"):
                dest = WEIGHTS_DIR / fname
            elif fname.endswith(".index"):
                dest = INDEX_DIR / fname
            else:
                dest = ASSETS_DIR / fname
            with open(dest, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            logger.info(f"成功接收并保存音色资产: {fname} -> {dest}")
            return {"status": "ok", "filename": fname, "path": str(dest)}
        finally:
            watchdog.mark_idle()

    @app.post("/api/upload")
    async def upload_file(file: UploadFile = File(...)):
        watchdog.mark_busy()
        try:
            dest = TASK_DIR / f"{uuid.uuid4().hex}_{file.filename}"
            with open(dest, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            return {"status": "ok", "path": str(dest)}
        finally:
            watchdog.mark_idle()

    @app.post("/api/separate")
    async def api_separate(req: dict):
        watchdog.mark_busy()
        try:
            audio = req.get("audio_path") or req.get("song_name_src") or ""
            src_path = Path(audio)
            if not src_path.is_file():
                if (TASK_DIR / audio).is_file():
                    src_path = TASK_DIR / audio
                else:
                    raise HTTPException(status_code=404, detail=f"Audio file not found: {audio}")
            task_id = uuid.uuid4().hex[:10]
            curr_task_dir = TASK_DIR / task_id
            curr_task_dir.mkdir(parents=True, exist_ok=True)
            v_path, i_path = separate_vocals_roformer(src_path, curr_task_dir)
            return {
                "status": "ok",
                "vocals": str(v_path),
                "instrumental": str(i_path),
                "sep_model": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
            }
        finally:
            watchdog.mark_idle()

    @app.get("/api/download")
    def download_file(path: str):
        watchdog.touch()
        p = Path(path)
        if p.is_file():
            return FileResponse(str(p), filename=p.name)
        raise HTTPException(status_code=404, detail="File not found")

    @app.post("/api/shutdown")
    def shutdown():
        def _exit():
            time.sleep(0.5)
            try:
                subprocess.run(["pkill", "-9", "-f", "cloudflared"], check=False)
            except Exception:
                pass
            try:
                subprocess.run(["pkill", "-9", "-f", "ipykernel"], check=False)
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
        return {"status": "ok", "message": "Server is shutting down"}

    demo = build_gradio_app()
    app = gr.mount_gradio_app(app, demo, path="/")
    return app

if __name__ == "__main__":
    import uvicorn
    logger.info(f"[启动] Kaggle Applio Gateway 进程 (端口: {SERVER_PORT})...")
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, log_level="info")
