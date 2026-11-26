# -*- coding: utf-8 -*-
"""CUSTOM-6：AstrBot 原生插件页面后端 API。

页面通过 AstrBot bridge 调 ``/api/plug/astrbot_plugin_cucumber_applio_console/page/*``，
本模块负责：配置读写、模型列表、网关请求、任务记录、RVC 控制台转换。

设计原则：
- 只读复用现有插件实例与 Gateway，不复制推理逻辑；
- 不暴露任意文件读取/命令执行；上传只落在 Gateway 的 uploads 目录；
- 所有返回统一 ``{"status": "ok"|"error", "data"/"message": ...}``。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from quart import request

PLUGIN_NAME = "astrbot_plugin_cucumber_applio_console"
API_BASE = f"/{PLUGIN_NAME}/page"

# CUSTOM-9 通用化：所有宿主机路径不再写死。
#   插件数据目录 → AstrBot 插件数据路径（自动），可用环境变量 CHORUS_PLUGIN_DATA 覆盖
#   网关工作目录 → 插件配置 `gateway_work_dir`，可用 CHORUS_GATEWAY_WORK 覆盖
#   AstrBot 日志 → AstrBot 数据目录上一级的 info.log，可用 CHORUS_PLUGIN_LOG 覆盖


def _default_plugin_data_dir() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:  # noqa: BLE001 — 脱离 AstrBot 时兜底
        return os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)


def _default_log_file() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return os.path.join(os.path.dirname(get_astrbot_data_path().rstrip("/")), "info.log")
    except Exception:  # noqa: BLE001
        return ""


def _data_dir() -> Path:
    return Path(os.environ.get("CHORUS_PLUGIN_DATA") or _default_plugin_data_dir())


def _gateway_work(plugin) -> Path:
    env = os.environ.get("CHORUS_GATEWAY_WORK")
    if env:
        return Path(env)
    try:
        gw = ""
        if hasattr(plugin, "_gateway_work_dir") and callable(plugin._gateway_work_dir):
            gw = plugin._gateway_work_dir()
        if not gw and hasattr(plugin, "config") and isinstance(plugin.config, dict):
            gw = str(plugin.config.get("gateway_work_dir", "") or "").strip()
            if not gw and isinstance(plugin.config.get("advanced"), dict):
                gw = str(plugin.config["advanced"].get("gateway_work_dir", "") or "").strip()
    except Exception:  # noqa: BLE001
        gw = ""
    return Path(gw) if gw else Path()


def _upload_dir(plugin) -> Path:
    return _gateway_work(plugin) / "uploads"


def _out_dir(plugin) -> Path:
    return _gateway_work(plugin) / "out"


def _task_log_dir(plugin) -> Path:
    return _gateway_work(plugin) / "logs"


def _log_file() -> Path:
    env = os.environ.get("CHORUS_PLUGIN_LOG")
    if env:
        return Path(env)
    p = _default_log_file()
    return Path(p) if p else Path("/dev/null")

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_RESULT_BYTES = 50 * 1024 * 1024
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus"}
JOB_KEEP = 60
UPLOAD_KEEP = 40

# 只在插件初始化时读取的配置：改完需要重载插件才生效
RELOAD_REQUIRED = {
    # 启动时读取的键全部在此标注（WebUI 保存后显示「需重载插件生效」）
    "enable_rvc", "chorus_seconds", "render_mode", "inference_timeout",
    "default_key_shift", "timeout", "music_api_timeout", "rvc_base_url",
    "rvc_models_keywords", "rvc_models_dir", "source_order",
    "auto_pick_song", "auto_pick_fallback_menu", "auto_pick_model",
    "auto_pick_min_score", "auto_pick_min_margin",
    "enable_smart_cover", "enable_enhanced_context", "enable_confirm_mechanism",
    "enable_batch_cover", "enable_preference_learning", "enable_inject_context",
    "enable_llm_success_notify", "enable_config_report",
    "notify_progress", "notify_retry",
    "enable_auto_key_shift", "male_to_female_shift", "female_to_male_shift",
    "gender_detection_timeout", "enable_auto_key_shift_debug",
    "artist_gender_map", "model_gender_map", "notify_persona_id",
    "enable_send_file", "enable_fade",
    "enable_local_audio_cover", "max_local_audio_size_mb", "local_audio_cache_ttl",
    "local_audio_trigger_keywords", "local_audio_auto_trigger",
    "local_audio_default_model", "local_audio_default_shift",
    "netease_quality", "music_api_timeout", "netease_cookie",
    "netease_meting_base", "netease_api_base", "netease_api_token",
    "alert_admin_on_cookie_expired", "cookie_alert_interval_hours",
    "enable_task_tracking", "preference_storage_path",
    "qq_official_cards", "qq_official_buttons",
}

SENSITIVE_KEYS = {
    "netease_cookie", "netease_api_token",
    "kaggle_api_token", "cloudflare_tunnel_token",
    "sources.netease_cookie", "sources.netease_api_token",
    "architecture.kaggle_api_token", "architecture.cloudflare_tunnel_token",
}

# Gateway /convert 中当前网关明确不生效的参数（页面要如实标注）
IGNORED_BY_GATEWAY = {
    "vocal_vol", "inst_vol", "reverb_intensity", "delay_intensity",
    "uvr5_agg", "uvr5_tta", "uvr5_postprocess", "uvr5_window_size",
    "uvr5_high_end_process", "msst_batch_size", "msst_num_overlap",
    "msst_normalize", "msst_use_tta", "vocal_postprocess",
    "shift_accompaniment", "inst_volume_db", "filter_radius",
}

_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{4,64}$")


def _ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data if data is not None else {}}


def _err(message: str) -> dict:
    return {"status": "error", "message": str(message)}


def _now() -> float:
    return time.time()


def _fmt_bytes(num: int | float | None) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"


def _arg(name: str, default: str = "") -> str:
    """安全读取 query 参数（无请求上下文时返回默认值）。"""
    try:
        value = request.args.get(name)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else str(value)


def _num(payload: dict, key: str, default: float, low: float, high: float):
    """安全取数值：空值/非法值一律回退默认，避免空表单把接口打成 500。"""
    raw = (payload or {}).get(key, None)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return max(low, min(high, value))


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class PageApi:
    """插件页面 API：全部方法在 AstrBot 请求上下文中被调用。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._jobs: dict[str, dict] = {}
        self._models_cache: dict[str, Any] = {"at": 0.0, "payload": None}
        self._kaggle_api_lock: asyncio.Lock | None = None
        self._load_jobs()

    # ---------------- 注册 ----------------
    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/overview", self.overview, ["GET"], "Applio 控制台 概览"),
            ("/health", self.health, ["GET"], "Applio 控制台 健康检查"),
            ("/tasks", self.tasks, ["GET"], "Applio 控制台 任务列表"),
            ("/task_audio", self.task_audio, ["GET"], "Applio 控制台 任务音频"),
            ("/active_tasks", self.active_tasks, ["GET"], "Applio 控制台 当前任务"),
            ("/models", self.models, ["GET"], "Applio 控制台 模型列表"),
            ("/models/refresh", self.refresh_models, ["POST"], "Applio 控制台 刷新模型"),
            ("/models/default", self.set_default_model, ["POST"], "Applio 控制台 默认音色"),
            ("/models/alias", self.set_alias, ["POST"], "Applio 控制台 模型别名"),
            ("/models/gender", self.set_gender, ["POST"], "Applio 控制台 模型性别"),
            ("/config", self.get_config, ["GET"], "Applio 控制台 配置读取"),
            ("/config/save", self.save_config, ["POST"], "Applio 控制台 配置保存"),
            ("/rvc/sep_models", self.sep_models, ["GET"], "Applio 控制台 分离模型列表"),
            ("/rvc/upload", self.upload_audio, ["POST"], "Applio 控制台 上传音频"),
            ("/rvc/convert", self.start_convert, ["POST"], "Applio 控制台 提交转换"),
            ("/rvc/separate", self.start_separate, ["POST"], "Applio 控制台 提交分离"),
            ("/rvc/clip", self.start_clip, ["POST"], "Applio 控制台 提交切段"),
            ("/rvc/status", self.job_status, ["GET"], "Applio 控制台 转换状态"),
            ("/rvc/result", self.job_result, ["GET"], "Applio 控制台 转换结果"),
            ("/tts/providers", self.tts_providers, ["GET"], "Applio 控制台 TTS 服务商列表"),
            ("/tts/generate", self.tts_generate, ["POST"], "Applio 控制台 TTS 生成"),
            ("/tts/tasks", self.tts_tasks, ["GET"], "Applio 控制台 TTS 任务列表"),
            ("/tts/audio", self.tts_audio, ["GET"], "Applio 控制台 TTS 音频回放"),
            ("/kaggle/status", self.kaggle_status, ["GET"], "Applio 控制台 Kaggle 状态"),
            ("/kaggle/cold_start", self.kaggle_cold_start, ["POST"], "Applio 控制台 Kaggle 远程开机"),
            ("/kaggle/shutdown", self.kaggle_shutdown, ["POST"], "Applio 控制台 Kaggle 远程关机"),
            ("/diagnostics", self.diagnostics, ["GET"], "Applio 控制台 诊断"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{API_BASE}{path}", handler, methods, desc)

    # ---------------- 内部工具 ----------------
    @property
    def cfg(self):
        return self.plugin.config

    def _gateway_base(self) -> str:
        return str(self.cfg.get("rvc_base_url", "http://127.0.0.1:3333/") or "").strip()

    def _model_entries(self) -> list[dict]:
        """解析 rvc_models_keywords（"file.pth|||别名"）为结构化条目。"""
        out = []
        for raw in list(self.plugin.rvc_models_keywords or []):
            if not isinstance(raw, str):
                continue
            parts = raw.split("|||", 1)
            filename = parts[0].strip()
            if filename and not filename.lower().endswith(".pth"):
                filename += ".pth"  # 兼容无后缀的历史条目
            alias = parts[1].strip() if len(parts) > 1 else ""
            if not filename:
                continue
            out.append({
                "filename": filename,
                "name": os.path.splitext(filename)[0],
                "alias": alias,
                "display": alias or os.path.splitext(filename)[0],
            })
        return out

    def _gender_of(self, name: str) -> str:
        base = os.path.splitext(str(name))[0].lower()
        try:
            return self.plugin.model_gender_map.get(base, "unknown")
        except Exception:
            return "unknown"

    async def _gateway_model_info(self, use_cache: bool = True, timeout: float = 25.0) -> dict:
        if use_cache and self._models_cache["payload"] and _now() - self._models_cache["at"] < 30:
            return self._models_cache["payload"]
        base = self._gateway_base()
        payload: dict[str, Any] = {"ok": False, "models": [], "error": "", "dir": ""}
        if not base:
            payload["error"] = "未配置 Gateway 地址"
            return payload
        try:
            async with self.plugin._get_gradio_client(base) as client:  # noqa: SLF001
                raw = await asyncio.wait_for(
                    asyncio.to_thread(client.predict, models_dir="", api_name="/model_info"),
                    timeout=timeout,
                )
        except Exception as exc:  # noqa: BLE001
            payload["error"] = f"{type(exc).__name__}: {exc}"
            return payload
        models: list[str] = []
        model_dir = ""
        if isinstance(raw, dict):
            model_dir = str(raw.get("dir") or "")
            for item in raw.get("models") or []:
                if isinstance(item, str):
                    models.append(item)
                elif isinstance(item, dict):
                    models.append(str(item.get("name") or item.get("filename") or ""))
        elif isinstance(raw, list):
            models = [str(x) for x in raw if isinstance(x, str)]
        payload.update({
            "ok": True,
            "models": [m for m in models if m],
            "dir": model_dir,
            # 每个模型的 index 匹配情况({模型文件名: 是否有配对 index})
            "index_matches": (raw or {}).get("index_matches", {}) if isinstance(raw, dict) else {},
            "warning": (raw or {}).get("warning", "") if isinstance(raw, dict) else "",
        })
        self._models_cache = {"at": _now(), "payload": payload}
        return payload

    def _load_cover_history(self) -> dict:
        data = _read_json(_data_dir() / "cover_history.json", {})
        return data if isinstance(data, dict) else {}

    def _read_task_records(self, limit: int = 80) -> list[dict]:
        """读 Gateway 落盘的 task 记录（成功任务）。失败任务记录不存在。"""
        if not _task_log_dir(self.plugin).is_dir():
            return []
        files = sorted(
            _task_log_dir(self.plugin).glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        history = self._load_cover_history()
        rows = []
        for path in files:
            rec = _read_json(path, None)
            if not isinstance(rec, dict):
                continue
            task_id = rec.get("task_id") or path.stem
            if str(rec.get("kind") or "") == "tts":
                continue  # CUSTOM-7.4：TTS 任务不入 RVC 历史列表
            # 历史键可能与任务号带后缀（如混响产物 `<task>_reverb`），按前缀匹配
            hist = history.get(task_id) or next(
                (v for k, v in history.items() if k.startswith(task_id + "_")), {})

            out_name = Path(str(rec.get("host_output") or rec.get("output") or "")).name
            webui_job = None
            for j in self._jobs.values():
                if j.get("gateway_task_id") == task_id or (out_name and j.get("output_name") == out_name):
                    webui_job = j
                    break

            user = str(hist.get("user") or rec.get("user") or "")
            user_name = str(hist.get("user_name") or rec.get("user_name") or "")
            song = str(rec.get("song") or hist.get("song") or Path(str(rec.get("source") or "")).stem or "")
            artist = str(rec.get("artist") or hist.get("artist") or "")
            model_name = rec.get("model") or ""

            if hist.get("song") or rec.get("song"):
                song = str(hist.get("song") or rec.get("song"))
                artist = str(hist.get("artist") or rec.get("artist") or "")
                if hist.get("model") or rec.get("model"):
                    model_name = str(hist.get("model") or rec.get("model"))
            elif webui_job:
                user = "WebUI"
                user_name = "WebUI 控制台"
                if webui_job.get("input_name"):
                    song = webui_job["input_name"]
                    artist = "本地上传"
                if webui_job.get("model"):
                    model_name = webui_job["model"]

            out_str = str(rec.get("host_output") or rec.get("output") or "")
            is_cloud = (
                "kaggle" in out_str.lower()
                or "kaggle" in str(out_name).lower()
                or str(rec.get("engine") or "").strip() in ("云端", "云端 (Kaggle)")
                or (webui_job and "kaggle" in str(webui_job.get("engine", "")).lower())
            )
            if is_cloud:
                engine = "云端"
            elif (hist.get("engine") and "回退" in str(hist.get("engine"))) or (rec.get("engine") and "回退" in str(rec.get("engine"))):
                engine = "本地 (云端离线回退)"
            else:
                engine = "本地"

            timings = rec.get("timings") or {}
            elapsed = rec.get("elapsed")
            if elapsed is None and timings:
                elapsed = round(sum(v for k, v in timings.items() if isinstance(v, (int, float)) and k != "queue_wait"), 1)
            if elapsed is None and hist.get("elapsed"):
                elapsed = hist.get("elapsed")
            if elapsed is None and webui_job:
                elapsed = webui_job.get("elapsed")

            sep_model = str(rec.get("sep_model") or hist.get("sep_model") or "").strip()
            if is_cloud and (not sep_model or "3_hp" in sep_model.lower()):
                sep_model = "BS-Roformer (ViperX)"

            row = {
                "id": task_id,
                "source": "gateway",
                "model": model_name,
                "song": song,
                "artist": artist,
                "user": user,
                "user_name": user_name,
                "pitch": rec.get("pitch"),
                "group_id": str(hist.get("group_id") or ""),
                "group_name": str(hist.get("group_name") or ""),
                "output": out_str,
                "output_name": out_name,
                "duration": rec.get("output_duration") or (webui_job.get("duration") if webui_job else None),
                "requested": rec.get("chorus_seconds_requested"),
                "clip": rec.get("clip_length"),
                "mode": rec.get("render_mode"),
                "created_at": rec.get("created_at") or path.stat().st_mtime,
                "status": "succeeded",
                "error": "",
                "engine": engine,
                "sep_model": sep_model,
                "elapsed": elapsed,
                "timings": timings,
            }
            rows.append(row)
        return rows

    def _jobs_file(self) -> Path:
        return _data_dir() / "webui_jobs.json"

    def _load_jobs(self) -> None:
        data = _read_json(self._jobs_file(), {})
        if isinstance(data, dict):
            self._jobs = {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save_jobs(self) -> None:
        try:
            items = sorted(self._jobs.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True)
            self._jobs = dict(items[:JOB_KEEP])
            _write_json_atomic(self._jobs_file(), self._jobs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WebUI] 保存任务记录失败: %s", exc)

    def _job_public(self, job: dict) -> dict:
        data = {k: v for k, v in job.items() if k != "output_path"}
        data["outputs"] = [
            {k: v for k, v in (item or {}).items() if k != "path"}
            for item in (job.get("outputs") or [])
        ]
        data["has_output"] = bool(job.get("output_path") or job.get("outputs"))
        data["elapsed"] = (
            round(_now() - float(job.get("started_at") or job.get("created_at") or _now()), 1)
            if job.get("status") in ("running", "pending") else job.get("elapsed", 0)
        )
        return data

    def _safe_job(self, job_id: str) -> dict | None:
        if not _ID_RE.match(str(job_id or "")):
            return None
        job = self._jobs.get(job_id)
        return job if isinstance(job, dict) else None

    # ---------------- 基础状态 ----------------
    async def health(self, timeout: float = 5.0) -> dict:
        base = self._gateway_base()
        result = {"ok": False, "base": base, "latency_ms": None, "error": ""}
        if not base:
            result["error"] = "未配置 Gateway 地址"
            return _ok(result)
        started = _now()
        try:
            import httpx

            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as cli:
                resp = await cli.get(base)
            result["ok"] = resp.status_code < 500
            result["status_code"] = resp.status_code
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_ms"] = int((_now() - started) * 1000)
        return _ok(result)

    async def _fetch_kaggle_api_info(self) -> dict:
        """异步获取 Kaggle 配额与运行状态（60s 缓存 + 单飞锁 + 放到工作线程，避免阻塞事件循环）。"""
        now = time.time()
        cached = getattr(self, "_kaggle_api_cache", None)
        if cached and (now - cached.get("at", 0) < 60):
            return cached.get("data", {})
        if self._kaggle_api_lock is None:
            self._kaggle_api_lock = asyncio.Lock()
        async with self._kaggle_api_lock:
            now = time.time()
            cached = getattr(self, "_kaggle_api_cache", None)
            if cached and (now - cached.get("at", 0) < 60):
                return cached.get("data", {})
            return await asyncio.to_thread(self._fetch_kaggle_api_info_sync)

    def _fetch_kaggle_api_info_sync(self) -> dict:
        """同步调用 Kaggle CLI 查询配额与状态（仅在工作线程中调用）。"""
        import subprocess

        now = time.time()
        arch = self.cfg.get("architecture") if isinstance(self.cfg, dict) else {}
        token = str(getattr(self.plugin, "kaggle_api_token", "") or self._get_cfg("kaggle_api_token") or arch.get("kaggle_api_token") or "").strip()
        if not token:
            return {}

        info = {}
        try:
            env = os.environ.copy()
            if token.startswith("{"):
                try:
                    t_json = json.loads(token)
                    env["KAGGLE_USERNAME"] = t_json.get("username", "")
                    env["KAGGLE_KEY"] = t_json.get("key", "")
                except Exception:
                    env["KAGGLE_API_TOKEN"] = token
            else:
                env["KAGGLE_API_TOKEN"] = token

            import sys as _sys
            kaggle_bin = os.path.join(os.path.dirname(_sys.executable), "kaggle")
            if not os.path.exists(kaggle_bin):
                kaggle_bin = shutil.which("kaggle") or "kaggle"

            # 1. 查询配额
            q_res = subprocess.run([kaggle_bin, "quota"], env=env, capture_output=True, text=True, timeout=12)
            if q_res.returncode == 0 and q_res.stdout:
                for line in q_res.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and parts[0].upper() == "GPU":
                        try:
                            used = float(parts[1].replace("h", ""))
                            rem = float(parts[2].replace("h", ""))
                            total = float(parts[3].replace("h", ""))
                            pct = int((rem / total) * 100) if total > 0 else 0
                            info["gpu_quota_used"] = round(used, 2)
                            info["gpu_quota_total"] = round(total, 2)
                            info["gpu_quota_remaining"] = round(rem, 2)
                            info["gpu_quota_pct"] = pct
                            info["gpu_quota_text"] = f"{rem:.1f}h / {total:.0f}h ({pct}%)"
                            if len(parts) >= 5:
                                info["quota_refresh_time"] = parts[4]
                        except Exception:
                            pass

            # 2. 探测指定或最近 kernel 的状态（精确匹配 slug 优先，再回退子串）
            slug = str(getattr(self.plugin, "kaggle_notebook_slug", "") or self._get_cfg("kaggle_notebook_slug") or arch.get("kaggle_notebook_slug") or "applio").strip()
            k_res = subprocess.run([kaggle_bin, "kernels", "list", "--mine"], env=env, capture_output=True, text=True, timeout=12)
            target_ref = None
            partial_ref = None
            first_ref = None
            if k_res.returncode == 0 and k_res.stdout:
                for line in k_res.stdout.strip().splitlines():
                    line = line.strip()
                    if line and not line.startswith("ref") and not line.startswith("-") and "/" in line:
                        first_item = line.split()[0]
                        if not first_ref:
                            first_ref = first_item
                        item_slug = first_item.split("/", 1)[-1].lower()
                        if slug and item_slug == slug.lower():
                            target_ref = first_item
                            break
                        if slug and not partial_ref and slug.lower() in first_item.lower():
                            partial_ref = first_item
            target_ref = target_ref or partial_ref or first_ref

            if target_ref:
                info["kernel_ref"] = target_ref
                s_res = subprocess.run([kaggle_bin, "kernels", "status", target_ref], env=env, capture_output=True, text=True, timeout=8)
                if s_res.returncode == 0 and s_res.stdout:
                    out_s = s_res.stdout.strip()
                    if '"' in out_s:
                        st = out_s.split('"')[1]
                        st = st.replace("KernelWorkerStatus.", "")
                        info["kernel_status"] = st
                    else:
                        info["kernel_status"] = out_s

            self._kaggle_api_cache = {"at": now, "data": info}
        except BaseException as e:
            logger.debug(f"[KaggleAPI] 查询 Kaggle 官方配额失败: {e}")
            info = (self._kaggle_api_cache or {}).get("data") or {}

        return info

    def _kick_kaggle_api_refresh(self) -> None:
        """首屏快开时后台异步预热 Kaggle 配额与状态，不阻塞页面首屏响应。"""
        task = getattr(self, "_kaggle_bg_refresh_task", None)
        if task is not None and not task.done():
            return
        try:
            self._kaggle_bg_refresh_task = asyncio.create_task(self._fetch_kaggle_api_info())
        except Exception:
            pass

    async def _probe_kaggle_status(self, fast: bool = False) -> dict:
        """探测 Kaggle 云端端点健康状态与运行指标。"""
        arch = self.cfg.get("architecture") if isinstance(self.cfg, dict) else {}
        endpoint = str(getattr(self.plugin, "kaggle_endpoint", "") or self._get_cfg("architecture.kaggle_endpoint") or self._get_cfg("kaggle_endpoint") or arch.get("kaggle_endpoint") or "").strip()
        exec_mode = str(getattr(self.plugin, "execution_mode", "") or self._get_cfg("architecture.execution_mode") or self._get_cfg("execution_mode") or arch.get("execution_mode") or "local_full")
        token = str(getattr(self.plugin, "kaggle_api_token", "") or self._get_cfg("kaggle_api_token") or arch.get("kaggle_api_token") or "").strip()
        has_token = bool(token)
        if fast:
            cached = getattr(self, "_kaggle_api_cache", None) or {}
            api_info = dict(cached.get("data") or {})
            if has_token and exec_mode == "kaggle_hybrid" and (time.time() - cached.get("at", 0) >= 60):
                self._kick_kaggle_api_refresh()
        else:
            api_info = await self._fetch_kaggle_api_info()
        quota_text = api_info.get("gpu_quota_text")
        quota_tip = f"每周 GPU 剩余: {quota_text} (看门狗 10 分钟无任务自动关机防扣费)" if quota_text else "10 分钟无任务自动关机"

        now = time.time()
        shutting_since = getattr(self.plugin, "_kaggle_shutting_since", 0)
        is_shutting = bool(shutting_since and (now - shutting_since < 45))
        if shutting_since and (now - shutting_since >= 45):
            self.plugin._kaggle_shutting_since = 0

        starting_since = getattr(self.plugin, "_kaggle_starting_since", 0)
        bg_watch_active = bool(getattr(self.plugin, "_kaggle_bg_watch_active", False))
        starting_window = 1200 if bg_watch_active else 360
        is_starting = False
        starting_elapsed = 0
        if not is_shutting and starting_since and (now - starting_since < starting_window):
            is_starting = True
            starting_elapsed = int(now - starting_since)
        elif starting_since and (now - starting_since >= starting_window):
            self.plugin._kaggle_starting_since = 0

        if not endpoint:
            return {
                "configured": False,
                "online": False,
                "has_token": has_token,
                "is_starting": is_starting,
                "starting_elapsed": starting_elapsed,
                "status_text": "未配置端点",
                "exec_mode": exec_mode,
                "tip": "请在插件设置中填入 Kaggle 隧道 URL",
                "quota_tip": quota_tip,
                "quota_info": api_info,
            }

        target = endpoint.rstrip("/") + "/"
        import httpx
        try:
            async with httpx.AsyncClient(timeout=(3.5 if fast else 6.0)) as client:
                resp = await client.get(f"{target}api/health")
                if resp.status_code == 200:
                    data = resp.json()
                    is_busy = bool(data.get("is_busy")) or int(data.get("active_jobs", 0)) > 0
                    idle_sec = 0 if is_busy else int(data.get("idle_seconds", 0))
                    watchdog_timeout = 600
                    countdown = max(0, watchdog_timeout - idle_sec)
                    if is_busy:
                        countdown_text = "任务推理中（倒计时暂停）"
                    elif countdown > 0:
                        countdown_text = f"{countdown // 60}分{countdown % 60}秒后自动关机"
                    else:
                        countdown_text = "即将自动关机"
                    self.plugin._kaggle_starting_since = 0
                    self.plugin._kaggle_shutting_since = 0
                    return {
                        "configured": True,
                        "online": True,
                        "has_token": has_token,
                        "is_starting": False,
                        "starting_elapsed": 0,
                        "is_busy": is_busy,
                        "active_jobs": int(data.get("active_jobs", 0)),
                        "status_text": "在线推理中" if is_busy else "在线就绪",
                        "gpu": data.get("gpu", True),
                        "device": data.get("device", "GPU 加速"),
                        "idle_seconds": idle_sec,
                        "countdown_seconds": countdown,
                        "countdown_text": countdown_text,
                        "endpoint": endpoint,
                        "exec_mode": exec_mode,
                        "quota_tip": quota_tip,
                        "quota_info": api_info,
                    }
        except Exception:
            pass

        k_stat = str(api_info.get("kernel_status") or "").upper()
        if not is_shutting and k_stat in ("QUEUED", "RUNNING") and not is_starting:
            is_starting = True
        if is_starting:
            off_text = "云端开机启动中"
            if starting_elapsed > 0:
                tip_text = f"Kaggle 远程冷启动指令已于 {starting_elapsed} 秒前下发，云端实例正在启动与加载模型，预计 2~4 分钟就绪（请勿重复点击开机）..."
            else:
                tip_text = "Kaggle 云端实例已处于启动/运行队列中，正在等待隧道与服务就绪（请勿重复点击开机）..."
        elif k_stat in ("COMPLETE", "STOPPED", "CANCELLED"):
            off_text = "休眠关机 (官方 API 确认已停机)"
            tip_text = f"Kaggle 官方 API 已核验实例彻底关机停止计费。本周 GPU 剩余 {quota_text or '充裕'}。翻唱任务自动由本地网关无缝兜底。"
        else:
            off_text = "休眠关机"
            tip_text = f"Kaggle 当前处于休眠关机状态以节省您的 GPU 时长。本周剩余 {quota_text or '充足'}；当前翻唱将自动由本地设备兜底。"

        return {
            "configured": True,
            "online": False,
            "has_token": has_token,
            "is_starting": is_starting,
            "starting_elapsed": starting_elapsed,
            "status_text": off_text,
            "endpoint": endpoint,
            "exec_mode": exec_mode,
            "tip": tip_text,
            "quota_tip": quota_tip,
            "quota_info": api_info,
        }

    async def kaggle_status(self) -> dict:
        fast = str(_arg("fast") or "").strip().lower() in ("1", "true", "yes")
        return _ok(await self._probe_kaggle_status(fast=fast))

    async def kaggle_cold_start(self) -> dict:
        """通过 Kaggle 官方 API/CLI 远程触发云端 GPU 实例开机（免手动开网页 Run All）。"""
        now = time.time()
        starting_window = 1200 if getattr(self.plugin, "_kaggle_bg_watch_active", False) else 360
        if getattr(self.plugin, "_kaggle_starting_since", 0) and (now - self.plugin._kaggle_starting_since < starting_window):
            elapsed = int(now - self.plugin._kaggle_starting_since)
            return _ok({"message": f"云端开机已在进行中（已等待 {elapsed} 秒），请耐心等待建联，无需重复点击。"})
        self.plugin._kaggle_shutting_since = 0
        ok, msg = await self.plugin.cold_start_kaggle()
        if not ok:
            return _err(msg)
        self.plugin._kaggle_starting_since = getattr(self.plugin, "_kaggle_starting_since", 0) or time.time()
        self._kaggle_api_cache = None
        return _ok({"message": msg if "复用" in msg or "沿用" in msg else "Kaggle 远程冷启动指令已成功下发！云端实例正在启动与加载服务，预计 2~4 分钟就绪。"})

    async def kaggle_shutdown(self) -> dict:
        """向云端 Kaggle 发送远程关机信号，终止 GPU 容器运行并停止计费。"""
        self.plugin._kaggle_starting_since = 0
        self.plugin._kaggle_shutting_since = time.time()
        arch = self.cfg.get("architecture") if isinstance(self.cfg, dict) else {}
        endpoint = str(getattr(self.plugin, "kaggle_endpoint", "") or self._get_cfg("architecture.kaggle_endpoint") or self._get_cfg("kaggle_endpoint") or arch.get("kaggle_endpoint") or "").strip()
        if not endpoint:
            return _err("未配置 Kaggle 云端端点地址")
        target = endpoint.rstrip("/") + "/"
        import httpx
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(f"{target}api/shutdown")
                if resp.status_code == 200:
                    self._kaggle_api_cache = None
                    return _ok({"message": "已成功向云端实例发送关机指令，Kaggle 实例正在停止运行！"})
                if resp.status_code in (502, 503, 530):
                    self._kaggle_api_cache = None
                    return _ok({"message": "云端已断开连接（当前处于休眠关机状态）。"})
                return _err(f"云端返回状态码: {resp.status_code}")
        except Exception as e:
            err_str = str(e)
            if "530" in err_str or "Connect" in err_str or "unreachable" in err_str.lower():
                self._kaggle_api_cache = None
                return _ok({"message": "云端已断开连接（当前处于休眠关机状态）。"})
            return _err(f"向云端发送关机信号失败: {e}")

    async def overview(self) -> dict:
        # 并发探测 Gateway、模型列表与 Kaggle 状态（fast=True 不阻塞 Kaggle CLI 子进程）
        health_res, info_res, kg_res = await asyncio.gather(
            self.health(timeout=3.5),
            self._gateway_model_info(timeout=8.0),
            self._probe_kaggle_status(fast=True),
            return_exceptions=True,
        )
        health = (
            health_res.get("data", {})
            if isinstance(health_res, dict)
            else {"ok": False, "base": self._gateway_base(), "latency_ms": None, "error": str(health_res)}
        )
        info = (
            info_res
            if isinstance(info_res, dict)
            else {"ok": False, "models": [], "error": str(info_res), "dir": ""}
        )
        recent_all = self._read_task_records(limit=200)
        tasks = recent_all[:8]
        jobs = sorted(self._jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)[:6]
        today = time.strftime("%Y-%m-%d")
        today_count = sum(
            1 for t in recent_all
            if time.strftime("%Y-%m-%d", time.localtime(t.get("created_at") or 0)) == today
        )
        default = str(self.cfg.get("rvc_default_model", "") or "")
        alias = ""
        for item in self._model_entries():
            if item["name"] == os.path.splitext(default)[0]:
                alias = item["alias"]
                break

        arch = self.cfg.get("architecture") if isinstance(self.cfg, dict) else {}
        exec_mode = str(getattr(self.plugin, "execution_mode", "") or self._get_cfg("architecture.execution_mode") or self._get_cfg("execution_mode") or arch.get("execution_mode") or "local_full")
        kaggle_ep = str(getattr(self.plugin, "kaggle_endpoint", "") or self._get_cfg("architecture.kaggle_endpoint") or self._get_cfg("kaggle_endpoint") or arch.get("kaggle_endpoint") or "").strip()
        kg_status = kg_res if isinstance(kg_res, dict) else {"configured": bool(kaggle_ep), "online": False, "exec_mode": exec_mode}

        return _ok({
            "plugin": {"name": PLUGIN_NAME, "loaded": True, "version": self._plugin_version()},
            "gateway": health,
            "config": {
                "execution_mode": exec_mode,
                "kaggle_endpoint": kaggle_ep,
                "architecture": {
                    "execution_mode": exec_mode,
                    "kaggle_endpoint": kaggle_ep,
                },
            },
            "kaggle": kg_status,
            "models": {
                "gateway_ok": info["ok"],
                "gateway_count": len(info["models"]),
                "configured_count": len(self._model_entries()),
                "dir": info["dir"],
                "error": info["error"],
            },
            "default_model": {"value": default, "alias": alias,
                              "gender": self._gender_of(default)},
            "today": {"count": today_count, "date": today},
            "recent_tasks": tasks,
            "recent_jobs": [self._job_public(j) for j in jobs],
            "paths": {
                "gateway_work": str(_gateway_work(self.plugin) or "(未配置)"),
                "tasks_available": _task_log_dir(self.plugin).is_dir(),
                "outputs_available": _out_dir(self.plugin).is_dir(),
            },
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _plugin_version(self) -> str:
        try:
            meta = Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8")
            m = re.search(r"^version:\s*(\S+)", meta, re.M)
            return m.group(1) if m else ""
        except Exception:
            return ""

    # ---------------- 任务 ----------------
    async def tasks(self) -> dict:
        try:
            limit = max(1, min(200, int(_arg("limit", "60") or 60)))
        except Exception:
            limit = 60
        rows = self._read_task_records(limit=limit)

        logged_outputs = {r.get("output_name") for r in rows if r.get("output_name")}
        logged_ids = {r.get("id") for r in rows if r.get("id")}

        for j in self._jobs.values():
            if j.get("kind") == "tts":
                continue
            out_name = j.get("output_name") or (Path(j.get("output_path")).name if j.get("output_path") else "")
            gw_id = j.get("gateway_task_id") or (out_name.split("_")[0].split(".")[0] if out_name else "")

            # 若该 WebUI 任务已在网关日志中收录，则跳过，杜绝重复出现两条（一条有音频一条无音频）
            if (out_name and out_name in logged_outputs) or (gw_id and gw_id in logged_ids) or (j.get("id") in logged_ids):
                continue

            job_row = dict(self._job_public(j), user="WebUI", user_name="WebUI 控制台")
            if not job_row.get("song"):
                job_row["song"] = j.get("input_name") or "-"
            if not job_row.get("output_name") and out_name:
                job_row["output_name"] = out_name
            if not job_row.get("engine"):
                job_row["engine"] = "本地"
            rows.append(job_row)

        rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        return _ok({
            "items": rows[:limit],
            "total": len(rows),
            "sources": {
                "gateway_records": _task_log_dir(self.plugin).is_dir(),
                "webui_jobs": bool(self._jobs),
            },
        })

    def _infer_gateway_stage(self, started_at: float) -> str:
        """按 Gateway tasks 目录里的中间文件推断当前处于哪一步（串行任务取最新目录）。"""
        root = _gateway_work(self.plugin) / "tasks"
        if not root.is_dir():
            return ""
        dirs = [d for d in root.iterdir() if d.is_dir()]
        if not dirs:
            return ""
        latest = max(dirs, key=lambda d: d.stat().st_mtime)
        if started_at and latest.stat().st_mtime < started_at - 5:
            return ""
        sep_dir = latest / "sep"
        if sep_dir.is_dir():
            if list(sep_dir.rglob("*_Vocals.wav")):
                return "变声推理"
            return "人声分离"
        if (latest / "clip.wav").is_file():
            return "准备音源"
        return ""

    async def active_tasks(self) -> dict:
        """当前任务 + 排队情况（来自插件内存队列）。"""
        try:
            snap = self.plugin._queue_snapshot()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取队列失败: {exc}")
        snap["queue_enabled"] = int(snap.get("limit") or 0) > 0
        # CUSTOM-8.1：注册表里「已接收/排队」但还没进推理槽的任务也纳入排队可见性
        reg_waiting = []
        try:
            queued_ids = {w.get("registry_id") for w in snap.get("waiting") or []}
            active_id = (snap.get("active") or {}).get("registry_id")
            for t in self.plugin.tasks.all_tasks():  # noqa: SLF001
                if t.status not in ("received", "queued"):
                    continue
                if t.task_id in queued_ids or t.task_id == active_id:
                    continue
                reg_waiting.append({
                    "id": t.task_id, "type": t.task_type,
                    "label": t.request_text[:40], "user": t.user_name or t.user_id,
                    "waited": round(_now() - t.created_at, 1),
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("[WebUI] 注册表等待任务读取失败: %s", exc)
        snap["registry_waiting"] = reg_waiting
        is_hybrid = getattr(self.plugin, "execution_mode", "local_full") == "kaggle_hybrid"
        has_ep = bool(getattr(self.plugin, "kaggle_endpoint", ""))
        default_engine = "云端" if (is_hybrid and has_ep) else "本地"

        active = snap.get("active")
        if active:
            if not active.get("engine"):
                active["engine"] = default_engine
            stage_str = str(active.get("stage") or "")
            if any(k in stage_str for k in ("连接", "唤醒", "冷启动", "就绪")):
                active["step"] = 1
            elif any(k in stage_str for k in ("准备音源", "取歌", "音源准备", "上传")):
                active["step"] = 2
            elif "分离" in stage_str:
                active["step"] = 3
            elif any(k in stage_str for k in ("变声", "推理")):
                active["step"] = 4
            elif any(k in stage_str for k in ("发送", "拉取")):
                active["step"] = 5
            elif active.get("at"):
                stage = self._infer_gateway_stage(float(active.get("at") or 0))
                if stage:
                    active["stage"] = stage
                    active["step"] = {"准备音源": 2, "人声分离": 3, "变声推理": 4}.get(stage, 3)
                    active["stage_from"] = "gateway"
                elif active.get("user") in ("WebUI", "WebUI 控制台"):
                    active["step"] = 4

        for w in snap.get("waiting") or []:
            if not w.get("engine"):
                w["engine"] = default_engine
        for r in reg_waiting:
            if not r.get("engine"):
                r["engine"] = default_engine if r.get("type") == "cover" else "本地"
        return _ok(snap)

    async def task_audio(self) -> dict:
        """按任务号返回成品音频（仅允许 gateway out 目录内的文件）。"""
        task_id = _arg("id").strip()
        if not _ID_RE.match(task_id):
            return _err("非法任务号")
        path = None
        duration = None
        rec = _read_json(_task_log_dir(self.plugin) / f"{task_id}.json", None)
        if isinstance(rec, dict):
            path = Path(str(rec.get("host_output") or rec.get("output") or ""))
            duration = rec.get("output_duration")
        else:
            # 兼容通过 WebUI job_id 查询任务音频
            job = self._jobs.get(task_id)
            if isinstance(job, dict):
                gw_id = job.get("gateway_task_id")
                if gw_id:
                    gw_rec = _read_json(_task_log_dir(self.plugin) / f"{gw_id}.json", None)
                    if isinstance(gw_rec, dict):
                        path = Path(str(gw_rec.get("host_output") or gw_rec.get("output") or ""))
                        duration = gw_rec.get("output_duration")
                if not path or not path.is_file():
                    p = str(job.get("output_path") or "")
                    if not p and job.get("outputs"):
                        p = str(job["outputs"][0].get("path") or "")
                    if not p and job.get("output_name"):
                        p = str(_out_dir(self.plugin) / str(job["output_name"]))
                    if p:
                        path = Path(p)
                        duration = job.get("duration")

        if not path:
            return _err("找不到该任务记录（可能已被清理）")
        try:
            path.resolve().relative_to(_out_dir(self.plugin).resolve())
        except Exception:
            return _err("结果路径不在允许目录内")
        if not path.is_file():
            return _err("成品文件已被清理")
        size = path.stat().st_size
        if size > MAX_RESULT_BYTES:
            return _err(f"结果过大（{_fmt_bytes(size)}），请到服务器查看")
        return _ok({
            "job_id": task_id,
            "name": path.name,
            "size": size,
            "size_text": _fmt_bytes(size),
            "duration": duration,
            "mime": "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg",
            "audio_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        })

    # ---------------- 模型 ----------------
    async def models(self) -> dict:
        info = await self._gateway_model_info(use_cache=False)
        configured = {m["filename"]: m for m in self._model_entries()}
        rows = []
        idx_matches = info.get("index_matches") or {}
        for name in info["models"]:
            base = os.path.splitext(name)[0]
            hit = configured.get(name) or configured.get(base + ".pth")
            rows.append({
                "filename": name,
                "name": base,
                "alias": (hit or {}).get("alias", ""),
                "gender": self._gender_of(name),
                "in_config": bool(hit),
                "has_index": idx_matches.get(name),
            })
        for filename, item in configured.items():
            if not any(r["filename"] == filename for r in rows):
                rows.append({
                    "filename": filename,
                    "name": item["name"],
                    "alias": item["alias"],
                    "gender": self._gender_of(filename),
                    "in_config": True,
                    "missing_in_gateway": True,
                })
        default = str(self.cfg.get("rvc_default_model", "") or "")
        return _ok({
            "gateway_ok": info["ok"],
            "gateway_error": info["error"],
            "dir": info["dir"],
            "warning": info.get("warning", ""),
            "models": rows,
            "default": default,
            "default_missing": bool(default) and not any(
                os.path.splitext(r["filename"])[0] == os.path.splitext(default)[0] or
                r["filename"] == default for r in rows),
        })

    async def refresh_models(self) -> dict:
        try:
            result = await self.plugin._update_models_from_api()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"刷新失败: {exc}")
        self._models_cache = {"at": 0.0, "payload": None}
        if not isinstance(result, dict):
            return _err("刷新返回格式异常")
        if not result.get("ok"):
            return _err(result.get("reason") or "刷新失败")
        return _ok({
            "models": self._model_entries(),
            "missing_default": result.get("missing_default", ""),
            "default": str(self.cfg.get("rvc_default_model", "") or ""),
        })

    async def set_default_model(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        name = str(payload.get("model") or "").strip()
        if not name:
            return _err("缺少 model")
        entries = {e["name"]: e for e in self._model_entries()}
        key = os.path.splitext(name)[0]
        if key not in entries:
            return _err(f"模型 {name} 不在已配置列表中，先刷新模型")
        self.cfg["rvc_default_model"] = key
        self.cfg.save_config()
        try:
            self.plugin.rvc_default_model = key
        except Exception:
            pass
        logger.info("[WebUI] 默认音色改为 %s", key)
        return _ok({"default": key})

    async def set_alias(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        model = str(payload.get("model") or "").strip()
        alias = str(payload.get("alias") or "").strip()[:40]
        if not model:
            return _err("缺少 model")
        key = os.path.splitext(model)[0]
        items = list(self.plugin.rvc_models_keywords or [])
        found = False
        for i, raw in enumerate(items):
            if not isinstance(raw, str):
                continue
            parts = raw.split("|||", 1)
            if os.path.splitext(parts[0].strip())[0] == key:
                items[i] = f"{parts[0].strip()}|||{alias}"
                found = True
                break
        if not found:
            items.append(f"{key}.pth|||{alias}")
        self.plugin.rvc_models_keywords = items
        self.cfg["rvc_models_keywords"] = items
        self.cfg.save_config()
        return _ok({"model": key, "alias": alias})

    async def set_gender(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        model = str(payload.get("model") or "").strip()
        gender = str(payload.get("gender") or "").strip().lower()
        if not model:
            return _err("缺少 model")
        if gender not in ("male", "female", "unknown"):
            return _err("gender 只能是 male/female/unknown")
        key = os.path.splitext(model)[0]
        try:
            self.plugin._write_model_gender(key, gender)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"写入失败: {exc}")
        return _ok({"model": key, "gender": gender})

    # ---------------- 配置 ----------------
    def _schema(self) -> dict:
        path = Path(__file__).with_name("_conf_schema.json")
        data = _read_json(path, {})
        return data if isinstance(data, dict) else {}

    def _flatten_schema(self, schema: dict, prefix: str = "", group: str = "") -> list[dict]:
        rows = []
        for key, meta in schema.items():
            if not isinstance(meta, dict):
                continue
            path = f"{prefix}{key}"
            mtype = meta.get("type")
            if mtype == "object" and isinstance(meta.get("items"), dict):
                rows.extend(self._flatten_schema(meta["items"], path + ".", str(meta.get("description") or key)))
                continue
            rows.append({
                "key": path,
                "group": group,
                "type": mtype or "string",
                "label": meta.get("description") or key,
                "hint": meta.get("hint") or "",
                "default": meta.get("default"),
                "options": meta.get("options"),
                "labels": meta.get("labels"),
                "minimum": meta.get("minimum"),
                "maximum": meta.get("maximum"),
                "invisible": bool(meta.get("invisible")),
                "sensitive": False,
            })
        return rows

    def _get_cfg(self, key: str, default=None):
        try:
            if key in self.cfg:
                return self.cfg[key]
            if "." in key:
                parts = key.split(".")
                curr = self.cfg
                for p in parts:
                    if not isinstance(curr, dict) or p not in curr:
                        break
                    curr = curr[p]
                else:
                    return curr
            return default
        except Exception:
            return default

    async def get_config(self) -> dict:
        fields = [f for f in self._flatten_schema(self._schema()) if not f["invisible"]]
        values = {}
        for f in fields:
            if f["key"] in ("basic.llm_provider_id", "llm_provider_id"):
                opts = [""]
                lbls = ["默认跟随 AstrBot 全局 / 当前会话模型"]
                try:
                    ctx = getattr(self.plugin, "context", None)
                    if ctx and hasattr(ctx, "get_all_providers"):
                        all_provs = ctx.get_all_providers() or []
                        for p in all_provs:
                            pid = ""
                            pname = ""
                            if hasattr(p, "meta"):
                                pid = getattr(p.meta, "id", "") or ""
                                model_name = getattr(p.meta, "model", "") or ""
                                pname = getattr(p.meta, "name", "") or model_name or pid
                                if model_name and model_name != pname:
                                    pname = f"{pname} ({model_name})"
                            elif isinstance(p, dict):
                                pid = p.get("id") or ""
                                pname = p.get("name") or p.get("model") or pid
                            if pid and pid not in opts:
                                opts.append(pid)
                                lbls.append(f"{pname} [ID: {pid}]")
                except Exception as ex:
                    logger.debug(f"[PageApi] 获取 providers 列表异常: {ex}")
                if len(opts) <= 1:
                    try:
                        cfg_file = None
                        try:
                            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                            cand = Path(get_astrbot_data_path()) / "cmd_config.json"
                            if cand.is_file():
                                cfg_file = cand
                        except Exception:
                            pass
                        if not cfg_file:
                            for cand_str in (
                                "data/cmd_config.json",
                                "../cmd_config.json",
                            ):
                                cp = Path(cand_str)
                                if cp.is_file():
                                    cfg_file = cp
                                    break
                        if cfg_file:
                            import json as _json
                            with open(cfg_file, "r", encoding="utf-8-sig") as cf:
                                cdata = _json.load(cf) or {}
                            for p in cdata.get("provider", []):
                                if isinstance(p, dict) and p.get("enable") and p.get("id"):
                                    pid = str(p.get("id"))
                                    pmodel = str(p.get("model") or pid)
                                    ptype = str(p.get("provider_type") or "")
                                    if ptype in ("embedding", "text_to_speech", "speech_to_text"):
                                        continue
                                    if pid not in opts:
                                        opts.append(pid)
                                        lbls.append(f"{pmodel} [ID: {pid}]")
                    except Exception as ex2:
                        logger.debug(f"[PageApi] 从 cmd_config.json 获取 providers 异常: {ex2}")
                f["options"] = opts
                f["labels"] = lbls
            elif f["key"] in ("tts.tts_provider_name", "tts_provider_name"):
                opts = [""]
                lbls = ["默认跟随当前使用的 TTS 服务商（自动回退第 1 个）"]
                try:
                    ctx = getattr(self.plugin, "context", None)
                    pm = getattr(ctx, "provider_manager", None) if ctx else None
                    for p in getattr(pm, "tts_provider_insts", None) or []:
                        pid = self._provider_id(p)
                        if not pid or pid in opts:
                            continue
                        cls_name = type(p).__name__
                        model_desc = str(getattr(p, "model_name", "") or getattr(p, "voice", "") or "").strip()
                        extra = " · ".join(x for x in (cls_name, model_desc) if x)
                        opts.append(pid)
                        lbls.append(f"{pid} ({extra})" if extra else pid)
                except Exception as ex:
                    logger.debug(f"[PageApi] 获取 TTS providers 列表异常: {ex}")
                if len(opts) <= 1:
                    try:
                        cfg_file = None
                        try:
                            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                            cand = Path(get_astrbot_data_path()) / "cmd_config.json"
                            if cand.is_file():
                                cfg_file = cand
                        except Exception:
                            pass
                        if not cfg_file:
                            for cand_str in ("data/cmd_config.json", "../cmd_config.json"):
                                cp = Path(cand_str)
                                if cp.is_file():
                                    cfg_file = cp
                                    break
                        if cfg_file:
                            import json as _json
                            with open(cfg_file, "r", encoding="utf-8-sig") as cf:
                                cdata = _json.load(cf) or {}
                            for p in cdata.get("provider", []):
                                if isinstance(p, dict) and p.get("enable") and p.get("id"):
                                    if str(p.get("provider_type") or "") != "text_to_speech":
                                        continue
                                    pid = str(p.get("id"))
                                    if pid not in opts:
                                        pmodel = str(p.get("model") or p.get("type") or "").strip()
                                        opts.append(pid)
                                        lbls.append(f"{pid} ({pmodel})" if pmodel and pmodel != pid else pid)
                    except Exception as ex2:
                        logger.debug(f"[PageApi] 从 cmd_config.json 获取 TTS providers 异常: {ex2}")
                cur_val = str(self._get_cfg(f["key"], "") or "").strip()
                if cur_val and cur_val not in opts:
                    opts.append(cur_val)
                    lbls.append(f"{cur_val} (当前配置·未在运行列表发现)")
                f["options"] = opts
                f["labels"] = lbls
            values[f["key"]] = self._get_cfg(f["key"], f.get("default"))
        return _ok({
            "fields": fields,
            "values": values,
            "sensitive_configured": {},
            "reload_required": sorted(RELOAD_REQUIRED),
            "gateway_ignored": sorted(IGNORED_BY_GATEWAY),
        })

    async def save_config(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            return _err("values 必须是对象")

        # 校验：若尝试切换为混合云模式，必须至少配置了 Kaggle API Token 或 Kaggle 隧道公网端点之一
        target_mode = values.get("architecture.execution_mode") or values.get("execution_mode")
        if target_mode == "kaggle_hybrid":
            arch = self.cfg.get("architecture") if isinstance(self.cfg, dict) else {}
            token = str(values.get("architecture.kaggle_api_token") or getattr(self.plugin, "kaggle_api_token", "") or self._get_cfg("kaggle_api_token") or arch.get("kaggle_api_token") or "").strip()
            endpoint = str(values.get("architecture.kaggle_endpoint") or getattr(self.plugin, "kaggle_endpoint", "") or self._get_cfg("architecture.kaggle_endpoint") or self._get_cfg("kaggle_endpoint") or arch.get("kaggle_endpoint") or "").strip()
            if not token and not endpoint:
                return _err("未检测到 Kaggle 配置！切换为混合云模式需至少配置 Kaggle 隧道端点地址（固定/临时均可）或 Kaggle API Token。")

        fields = {f["key"]: f for f in self._flatten_schema(self._schema())}
        # 对象节点键（如 tuning.gender.model_genders，由音色列表编辑器整组提交）
        node_paths = set()

        def _collect_nodes(schema, prefix=""):
            for key, meta in schema.items():
                if not isinstance(meta, dict):
                    continue
                path = f"{prefix}{key}"
                if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
                    node_paths.add(path)
                    _collect_nodes(meta["items"], path + ".")
                elif meta.get("type") in ("dict", "object"):
                    # dict/object 叶子（如 model_genders_extra）也按整组提交
                    node_paths.add(path)
        _collect_nodes(self._schema())

        changed, needs_reload, errors = [], [], []
        old_values = {}
        for key, raw in values.items():
            meta = fields.get(key)
            if meta is None:
                if key in node_paths:
                    try:
                        value = raw if isinstance(raw, dict) else json.loads(str(raw))
                    except Exception:  # noqa: BLE001
                        errors.append(f"{key}: 值必须是对象")
                        continue
                    old = self._get_cfg(key)
                    if old == value:
                        continue
                    old_values[key] = old
                    try:
                        self.cfg[key] = value
                        if "." in key:
                            parts = key.split(".")
                            curr = self.cfg
                            for p in parts[:-1]:
                                if p not in curr or not isinstance(curr[p], dict):
                                    curr[p] = {}
                                curr = curr[p]
                            curr[parts[-1]] = value
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{key}: 写入失败 {exc}")
                        continue
                    changed.append(key)
                    continue
                errors.append(f"{key}: 未知配置项")
                continue
            if meta["sensitive"] and not str(raw or "").strip():
                continue  # 留空表示不修改
            try:
                value = self._coerce(meta, raw)
            except ValueError as exc:
                errors.append(f"{meta['label']}: {exc}")
                continue
            old = self._get_cfg(key, meta.get("default"))
            if old == value:
                continue
            old_values[key] = old
            try:
                self.cfg[key] = value
                if "." in key:
                    parts = key.split(".")
                    curr = self.cfg
                    for p in parts[:-1]:
                        if p not in curr or not isinstance(curr[p], dict):
                            curr[p] = {}
                        curr = curr[p]
                    curr[parts[-1]] = value
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{meta['label']}: 写入失败 {exc}")
                continue
            changed.append(key)
            if key in RELOAD_REQUIRED:
                needs_reload.append(key)
        if errors and not changed:
            return _err("；".join(errors[:5]))
        if changed:
            try:
                self.cfg.save_config()
            except Exception as exc:  # noqa: BLE001
                return _err(f"保存失败: {exc}")
            try:
                self.plugin.apply_live_config(changed)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                logger.debug("[WebUI] 运行时同步失败: %s", exc)
            logger.info("[WebUI] 配置已更新: %s", ", ".join(changed))
        return _ok({
            "changed": changed,
            "old": old_values,
            "needs_reload": sorted(set(needs_reload)),
            "errors": errors,
        })

    def _coerce(self, meta: dict, raw):
        mtype = meta["type"]
        if mtype == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if mtype == "int":
            value = int(float(raw))
            if meta.get("options") and value not in meta["options"]:
                if str(value) not in [str(o) for o in meta["options"]]:
                    raise ValueError(f"必须在可选值列表中: {meta['options']}")
            lo, hi = meta.get("minimum"), meta.get("maximum")
            if lo is not None and value < lo:
                raise ValueError(f"不能小于 {lo}")
            if hi is not None and value > hi:
                raise ValueError(f"不能大于 {hi}")
            return value
        if mtype == "float":
            value = float(raw)
            lo, hi = meta.get("minimum"), meta.get("maximum")
            if lo is not None and value < lo:
                raise ValueError(f"不能小于 {lo}")
            if hi is not None and value > hi:
                raise ValueError(f"不能大于 {hi}")
            return value
        if mtype == "list":
            if isinstance(raw, list):
                return [str(x) for x in raw]
            text = str(raw).replace("，", ",")
            return [x.strip() for x in re.split(r"[,\n]", text) if x.strip()]
        if mtype == "object":
            if isinstance(raw, dict):
                return raw
            return json.loads(str(raw))
        if mtype == "dict":
            if isinstance(raw, dict):
                return raw
            return json.loads(str(raw)) if str(raw).strip() else {}
        text = str(raw)
        options = meta.get("options")
        if options and text not in options:
            raise ValueError(f"只能是 {'/'.join(map(str, options))}")
        return text

    # ---------------- Applio 控制台 ----------------
    async def upload_audio(self) -> dict:
        """接收音频：优先 multipart，其次 JSON base64。"""
        filename = ""
        size = 0
        upload_id = uuid.uuid4().hex[:24]
        dest = None

        upload = None
        try:
            raw_files = request.files
            if inspect.isawaitable(raw_files):
                raw_files = await raw_files
            upload = raw_files.get("file") if hasattr(raw_files, "get") else None
        except Exception as exc:
            logger.warning("[WebUI] 获取 request.files 异常: %s", exc)
            upload = None

        if upload is not None:
            filename = str(getattr(upload, "filename", "") or "")
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_AUDIO_EXT:
                return _err(f"不支持的格式 {ext or '(无扩展名)'}，仅支持 {', '.join(sorted(ALLOWED_AUDIO_EXT))}")
            _upload_dir(self.plugin).mkdir(parents=True, exist_ok=True)
            dest = _upload_dir(self.plugin) / f"{upload_id}{ext}"
            try:
                if hasattr(upload, "save"):
                    save_res = upload.save(str(dest))
                    if inspect.isawaitable(save_res):
                        await asyncio.wait_for(save_res, timeout=120)
                elif hasattr(upload, "read"):
                    read_res = upload.read()
                    data = await read_res if inspect.isawaitable(read_res) else read_res
                    dest.write_bytes(data)
                size = dest.stat().st_size
                if size > MAX_UPLOAD_BYTES:
                    dest.unlink(missing_ok=True)
                    return _err(f"音频文件超过 {MAX_UPLOAD_BYTES // (1024*1024)}MB 限制")
            except Exception as exc:  # noqa: BLE001
                try:
                    if dest is not None:
                        dest.unlink(missing_ok=True)
                except OSError:
                    pass
                return _err(f"保存失败: {exc}")
        else:
            payload = await request.get_json(silent=True) or {}
            filename = str(payload.get("name") or "").strip()
            b64 = str(payload.get("data_base64") or "").strip()
            if not filename or not b64:
                return _err("缺少文件数据（name / data_base64）")
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_AUDIO_EXT:
                return _err(f"不支持的格式 {ext or '(无扩展名)'}，仅支持 {', '.join(sorted(ALLOWED_AUDIO_EXT))}")
            try:
                raw_bytes = base64.b64decode(b64, validate=True)
            except Exception:
                return _err("base64 数据无法解码")
            if len(raw_bytes) > MAX_UPLOAD_BYTES:
                return _err(f"文件过大（上限 {_fmt_bytes(MAX_UPLOAD_BYTES)}）")
            _upload_dir(self.plugin).mkdir(parents=True, exist_ok=True)
            dest = _upload_dir(self.plugin) / f"{upload_id}{ext}"
            dest.write_bytes(raw_bytes)
            size = len(raw_bytes)

        self._prune_uploads()
        return _ok({
            "upload_id": upload_id,
            "name": filename,
            "size": size,
            "size_text": _fmt_bytes(size),
            "container_path": f"/work/uploads/{upload_id}{ext}",
        })

    def _prune_uploads(self) -> None:
        try:
            items = sorted(_upload_dir(self.plugin).glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in items[UPLOAD_KEEP:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    async def sep_models(self) -> dict:
        """读取网关可用分离模型（/show_msst_models）。"""
        try:
            base = self._gateway_base()
            async with self.plugin._get_gradio_client(base) as client:  # noqa: SLF001
                raw = await asyncio.wait_for(
                    asyncio.to_thread(client.predict, api_name="/show_msst_models"),
                    timeout=20,
                )
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取分离模型失败: {exc}")
        models = []
        for item in raw or []:
            if isinstance(item, dict) and item.get("id"):
                models.append({"id": item["id"], "name": item.get("name") or item["id"]})
            elif isinstance(item, str):
                models.append({"id": item, "name": os.path.splitext(item)[0]})
        return _ok({"models": models, "default": models[0]["id"] if models else ""})

    async def start_separate(self) -> dict:
        """提交仅分离任务（人声/伴奏），不跑 RVC。"""
        payload = await request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id") or "").strip()
        if not _ID_RE.match(upload_id):
            return _err("缺少或非法的 upload_id")
        matches = list(_upload_dir(self.plugin).glob(f"{upload_id}.*")) if _upload_dir(self.plugin).is_dir() else []
        if not matches:
            return _err("上传文件已失效，请重新上传")
        src = matches[0]
        mode = str(payload.get("mode") or "both").strip().lower()
        if mode not in ("both", "vocals", "instrumental"):
            return _err("处理方式只能是 both / vocals / instrumental")
        fmt = str(payload.get("output_format") or "wav").strip().lower()
        if fmt not in ("wav", "flac", "mp3", "m4a"):
            return _err("不支持的导出格式")
        if hasattr(self.plugin, "_cover_queue"):
            q_len = len(self.plugin._cover_queue)
            q_limit = int(getattr(self.plugin, "cover_queue_limit", 3))
            if q_len >= q_limit:
                return _err(f"当前排队已满（上限 {q_limit} 人），请等待已有任务完成")
        else:
            running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
            if running:
                return _err(f"已有任务进行中（{running[0]['id'][:8]}），请等待完成")
        job_id = uuid.uuid4().hex[:16]
        input_name = str(payload.get("input_name") or "").strip() or src.name
        params = {
            "song_name_src": f"/work/uploads/{src.name}",
            "msst_model": str(payload.get("model") or "").strip(),
            "output_format": fmt,
            "mode": mode,
        }
        is_hybrid = getattr(self.plugin, "execution_mode", "local_full") == "kaggle_hybrid"
        has_ep = bool(getattr(self.plugin, "kaggle_endpoint", ""))
        default_engine = "云端" if (is_hybrid and has_ep) else "本地"
        req_engine = str(payload.get("engine") or "").strip().lower()
        engine_name = "云端" if req_engine == "kaggle" else ("本地" if req_engine == "local" else default_engine)
        if engine_name == "云端":
            arch = self.cfg.get("architecture") if isinstance(self.cfg, dict) else {}
            token = str(getattr(self.plugin, "kaggle_api_token", "") or self._get_cfg("kaggle_api_token") or arch.get("kaggle_api_token") or "").strip()
            endpoint = str(getattr(self.plugin, "kaggle_endpoint", "") or self._get_cfg("architecture.kaggle_endpoint") or self._get_cfg("kaggle_endpoint") or arch.get("kaggle_endpoint") or "").strip()
            if not token and not endpoint:
                return _err("未配置 Kaggle 端点地址或 API Token，无法使用云端算力。请先在「设置」中配置。")
        job = {
            "id": job_id,
            "kind": "separate",
            "created_at": _now(),
            "started_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "已提交任务",
            "model": params["msst_model"] or "default",
            "input_name": input_name,
            "mode": mode,
            "format": fmt,
            "output_path": "",
            "error": "",
            "engine": engine_name,
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_job(job_id, params))
        return _ok({"job_id": job_id})

    async def start_clip(self) -> dict:
        """提交切段任务：只裁出目标时长的片段，不做分离/变声。"""
        payload = await request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id") or "").strip()
        if not _ID_RE.match(upload_id):
            return _err("缺少或非法的 upload_id")
        matches = list(_upload_dir(self.plugin).glob(f"{upload_id}.*")) if _upload_dir(self.plugin).is_dir() else []
        if not matches:
            return _err("上传文件已失效，请重新上传")
        src = matches[0]
        fmt = str(payload.get("output_format") or "wav").strip().lower()
        if fmt not in ("wav", "flac", "mp3", "m4a"):
            fmt = "wav"
        seconds = int(_num(payload, "seconds", 25, 10, 200))
        if hasattr(self.plugin, "_cover_queue"):
            q_len = len(self.plugin._cover_queue)
            q_limit = int(getattr(self.plugin, "cover_queue_limit", 3))
            if q_len >= q_limit:
                return _err(f"当前排队已满（上限 {q_limit} 人），请等待已有任务完成")
        else:
            running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
            if running:
                return _err(f"已有任务进行中（{running[0]['id'][:8]}），请等待完成")
        job_id = uuid.uuid4().hex[:16]
        params = {
            "song_name_src": f"/work/uploads/{src.name}",
            "seconds": seconds,
            "output_format": fmt,
        }
        job = {
            "id": job_id, "kind": "clip",
            "created_at": _now(), "started_at": _now(), "updated_at": _now(),
            "status": "running", "stage": "已提交到 Gateway",
            "model": "-", "input_name": src.name,
            "seconds": seconds, "format": fmt,
            "output_path": "", "error": "",
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_job(job_id, params))
        return _ok({"job_id": job_id})

    async def start_convert(self) -> dict:
        payload = await request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id") or "").strip()
        if not _ID_RE.match(upload_id):
            return _err("缺少或非法的 upload_id")
        matches = list(_upload_dir(self.plugin).glob(f"{upload_id}.*")) if _upload_dir(self.plugin).is_dir() else []
        if not matches:
            return _err("上传文件已失效，请重新上传")
        src = matches[0]
        model = str(payload.get("model") or "").strip()
        entries_by_name = {e["name"]: e for e in self._model_entries()}
        entries_by_lower = {e["name"].lower(): e for e in self._model_entries()}
        stem = os.path.splitext(model)[0]
        if not model or (stem not in entries_by_name and stem.lower() not in entries_by_lower):
            return _err("请选择已配置的音色")
        entry = entries_by_name.get(stem) or entries_by_lower.get(stem.lower())
        model = entry["filename"]

        if hasattr(self.plugin, "_cover_queue"):
            q_len = len(self.plugin._cover_queue)
            q_limit = int(getattr(self.plugin, "cover_queue_limit", 3))
            if q_len >= q_limit:
                return _err(f"当前排队已满（上限 {q_limit} 人），请等待已有任务完成")
        else:
            running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
            if running:
                return _err(f"已有转换进行中（{running[0]['id'][:8]}），请等待完成")

        fmt = str(payload.get("output_format") or "wav").strip().lower()
        if fmt not in ("wav", "mp3", "flac", "m4a"):
            fmt = "wav"
        rmode = str(payload.get("render_mode") or "full").strip().lower()
        if rmode not in ("full", "chorus", "custom"):
            rmode = "full"
        f0 = str(payload.get("f0_method") or "rmvpe").strip() or "rmvpe"
        idx_rate = round(_num(payload, "index_rate", 0.75, 0.0, 1.0), 2)
        protect_val = round(_num(payload, "protect", 0.5, 0.0, 0.5), 2)
        sep_mode = str(payload.get("sep_mode") or "").strip().lower()
        skip_sep = (sep_mode == "skip") or (payload.get("skip_separation") in (True, "true", "1", 1, "yes", "on"))
        track_type = "cover" if sep_mode == "mix" else "vocals"
        raw_clean = str(payload.get("cleanup_mode") or "off").strip().lower()
        clean_map = {"off": "off", "light": "light", "low": "light", "medium": "light", "speech": "speech", "high": "speech"}
        cleanup_val = clean_map.get(raw_clean, "off")
        params = {
            "song_name_src": f"/work/uploads/{src.name}",
            "model_dropdown": model,
            # Gateway 的 index_rate 是 Textbox(str)，必须按字符串传
            "key_shift": int(_num(payload, "key_shift", 0, -24, 24)),
            "f0_method": f0,
            "index_rate": str(idx_rate),
            "rms_mix_rate": round(_num(payload, "rms_mix_rate", 1.0, 0.0, 1.0), 2),
            "protect": protect_val,
            "vocal_volume_db": round(_num(payload, "vocal_volume_db", 0.0, -12.0, 12.0), 1),
            "output_format": fmt,
            "render_mode": rmode,
            "chorus_seconds": int(_num(payload, "chorus_seconds", 25, 10, 200)),
            "chorus_buffer_seconds": round(_num(payload, "chorus_buffer_seconds", 10.0, 0.0, 30.0), 1),
            "fade_seconds": round(_num(payload, "fade_seconds", 0.5, 0.0, 5.0), 2),
            "speaker_id": int(_num(payload, "speaker_id", 0, 0, 20)),
            "vocal_vol": 0,
            "inst_vol": 0,
            "shift_accompaniment": False,
            "skip_separation": skip_sep,
            "track_type": track_type,
        }
        if payload.get("f0_autotune"):
            params["f0_autotune"] = True
        if payload.get("formant_shifting"):
            params["formant_shifting"] = True
        params["cleanup_mode"] = cleanup_val
        for key in ("start_time", "end_time", "duration"):
            value = str(payload.get(key) or "").strip()
            if value:
                params[key] = value
        # CUSTOM-7：本次转换的人声混响试听参数（只对本次请求生效，不写配置）
        if payload.get("reverb_enabled") in (True, "true", "1", "on"):
            params.update({
                "reverb_enabled": True,
                "reverb_wet": round(_num(payload, "reverb_wet", 0.08, 0.0, 0.5), 2),
                "reverb_dry": round(_num(payload, "reverb_dry", 1.0, 0.5, 1.0), 2),
                "reverb_room_size": round(_num(payload, "reverb_room_size", 0.35, 0.0, 1.0), 2),
                "reverb_decay": round(_num(payload, "reverb_decay", 0.8, 0.2, 4.0), 2),
                "reverb_damping": round(_num(payload, "reverb_damping", 0.60, 0.0, 1.0), 2),
                "reverb_pre_delay": round(_num(payload, "reverb_pre_delay", 12.0, 0.0, 100.0), 1),
                "reverb_width": round(_num(payload, "reverb_width", 0.60, 0.0, 1.0), 2),
            })
        if params["key_shift"] < -24 or params["key_shift"] > 24:
            return _err("变调范围只能是 -24 ~ +24")
        if not 0 <= idx_rate <= 1:
            return _err("index_rate 必须在 0~1")
        if not 0 <= protect_val <= 0.5:
            return _err("protect 必须在 0~0.5")

        job_id = uuid.uuid4().hex[:16]
        input_name = str(payload.get("input_name") or "").strip() or src.name
        is_hybrid = getattr(self.plugin, "execution_mode", "local_full") == "kaggle_hybrid"
        has_ep = bool(getattr(self.plugin, "kaggle_endpoint", ""))
        default_engine = "云端" if (is_hybrid and has_ep) else "本地"
        req_engine = str(payload.get("engine") or "").strip().lower()
        engine_name = "云端" if req_engine == "kaggle" else ("本地" if req_engine == "local" else default_engine)
        if engine_name == "云端":
            arch = self.cfg.get("architecture") if isinstance(self.cfg, dict) else {}
            token = str(getattr(self.plugin, "kaggle_api_token", "") or self._get_cfg("kaggle_api_token") or arch.get("kaggle_api_token") or "").strip()
            endpoint = str(getattr(self.plugin, "kaggle_endpoint", "") or self._get_cfg("architecture.kaggle_endpoint") or self._get_cfg("kaggle_endpoint") or arch.get("kaggle_endpoint") or "").strip()
            if not token and not endpoint:
                return _err("未配置 Kaggle 端点地址或 API Token，无法使用云端算力。请先在「设置」中配置。")
        job = {
            "id": job_id,
            "kind": "convert",
            "created_at": _now(),
            "started_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "已提交任务",
            "model": os.path.splitext(model)[0],
            "input_name": input_name,
            "params": {k: v for k, v in params.items() if k not in ("song_name_src",)},
            "output_path": "",
            "error": "",
            "engine": engine_name,
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_job(job_id, params))
        return _ok({"job_id": job_id})

    # ---------------- TTS 控制台（CUSTOM-7） ----------------
    @staticmethod
    def _provider_id(provider: Any) -> str:
        try:
            meta = provider.meta() if hasattr(provider, "meta") else None
        except Exception:
            meta = None
        if isinstance(meta, dict):
            return str(meta.get("id") or "")
        return str(getattr(meta, "id", "") or "")

    async def tts_providers(self) -> dict:
        """列出 AstrBot 已配置的 TTS 服务商（供控制台下拉，不暴露任何 Key）。"""
        pm = getattr(self.plugin.context, "provider_manager", None)
        rows = []
        for p in getattr(pm, "tts_provider_insts", None) or []:
            pid = self._provider_id(p)
            rows.append({
                "id": pid,
                "class": type(p).__name__,
                "model": str(getattr(p, "model_name", "") or getattr(p, "voice", "") or ""),
            })
        using = ""
        try:
            from astrbot.core.provider.entities import ProviderType

            current = pm.get_using_provider(ProviderType.TEXT_TO_SPEECH) if pm else None
            using = self._provider_id(current) if current else ""
        except Exception:
            pass
        return _ok({
            "providers": rows,
            "configured": str(self.cfg.get("tts_provider_name", "") or ""),
            "using": using,
            "tts_enable": bool(self.cfg.get("tts_enable", False)),
            "tts_use_original_voice": bool(self.cfg.get("tts_use_original_voice", False)),
            "tts_model": str(self.cfg.get("tts_rvc_model", "") or ""),
            "gateway_models": [m["filename"] for m in self._model_entries()],
        })

    async def tts_generate(self) -> dict:
        """提交 TTS 生成任务：文本 → TTS Provider → （可选）网关换声 → 预览。"""
        payload = await request.get_json(silent=True) or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return _err("请输入要朗读的文本")
        try:
            max_chars = int(self.cfg.get("tts_max_text_chars", 200) or 200)
        except (TypeError, ValueError):
            max_chars = 200
        text = text[:min(max_chars, 500)]
        provider_id = str(payload.get("provider_id") or "").strip()[:80]
        raw_model = str(payload.get("model") or "").strip()
        skip_rvc_raw = payload.get("skip_rvc")
        if raw_model == "__raw__":
            skip_rvc = True
            model = ""
        elif skip_rvc_raw is not None:
            skip_rvc = bool(skip_rvc_raw)
            model = "" if skip_rvc else raw_model
        else:
            skip_rvc = (not raw_model) and bool(self.cfg.get("tts_use_original_voice", False))
            model = raw_model
        if model and not skip_rvc:
            entries = {os.path.splitext(e["filename"])[0] for e in self._model_entries()}
            model = model if model.endswith(".pth") else f"{model}.pth"
            if os.path.splitext(model)[0] not in entries:
                return _err(f"音色 {model} 不在已配置模型列表中")
        reverb_raw = payload.get("reverb")
        reverb_override = None if reverb_raw in (None, "", "default") else bool(reverb_raw)
        pitch = int(_num(payload, "pitch", 0, -24, 24))
        fmt = str(payload.get("output_format") or "").strip().lower()
        if fmt and fmt not in ("wav", "mp3", "flac", "m4a"):
            return _err("不支持的输出格式")
        fmt_override = fmt or ""

        if hasattr(self.plugin, "_tts_queue"):
            q_len = len(self.plugin._tts_queue)
            q_limit = int(getattr(self.plugin, "tts_queue_limit", 3))
            if q_len >= q_limit:
                return _err(f"当前 TTS 排队已满（上限 {q_limit} 人），请等待已有任务完成")
        else:
            running = [j for j in self._jobs.values() if j.get("status") in ("pending", "running")]
            if running:
                return _err(f"已有任务进行中（{running[0]['id'][:8]}），请等待完成")

        job_id = uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "kind": "tts",
            "created_at": _now(),
            "started_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "TTS 生成原始语音",
            "model": "TTS原音色" if skip_rvc else (os.path.splitext(model)[0] if model else str(self.cfg.get("tts_rvc_model", "") or "") or "default"),
            "text": text[:120] + ("…" if len(text) > 120 else ""),
            "output_path": "",
            "error": "",
        }
        self._jobs[job_id] = job
        self._save_jobs()
        asyncio.create_task(self._run_tts_job(job_id, {
            "text": text,
            "provider_id": provider_id,
            "model": model,
            "skip_rvc": skip_rvc,
            "reverb_override": reverb_override,
            "pitch": pitch,
            "output_format": fmt_override,
        }))
        return _ok({"job_id": job_id})

    async def _run_tts_job(self, job_id: str, params: dict) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        started = _now()
        acquired_tts = False
        try:
            if hasattr(self.plugin, "_acquire_tts_slot"):
                umo = f"webui_tts_{job_id[:8]}"
                label = f"WebUI TTS: {params.get('text', '')[:20]}"
                meta = {
                    "user": "WebUI",
                    "user_name": "WebUI 控制台",
                    "text": params.get("text", "")[:40],
                    "model": params.get("model", ""),
                    "webui_job_id": job_id,
                }
                job["stage"] = "排队中"
                job["status"] = "pending"
                job["updated_at"] = _now()
                self._save_jobs()
                acquired = await self.plugin._acquire_tts_slot(umo, label, meta=meta)
                if not acquired:
                    job["status"] = "failed"
                    job["error"] = "当前 TTS 任务排队已满，请稍后再试"
                    job["stage"] = "排队已满"
                    job["updated_at"] = _now()
                    self._save_jobs()
                    return
                acquired_tts = True
                started = _now()
                job["started_at"] = started
                job["status"] = "running"
                if hasattr(self.plugin, "_set_tts_stage"):
                    self.plugin._set_tts_stage("WebUI TTS 生成与变声")
            tts = getattr(self.plugin, "tts", None)
            if tts is None:
                raise RuntimeError("TTS 模块未初始化")
            raw_path, out_path, info = await asyncio.wait_for(
                tts.convert(
                    params["text"], None,
                    provider_id=params.get("provider_id") or "",
                    model=params.get("model") or "",
                    reverb_override=params.get("reverb_override"),
                    pitch_override=params.get("pitch"),
                    format_override=params.get("output_format") or "",
                    skip_rvc=params.get("skip_rvc"),
                ),
                timeout=max(120, float(getattr(self.plugin, "tts_timeout", 300) or 300) + 60),
            )
            # 原始音频复制到成品目录供页面回放（保持与 /rvc/result 同一安全边界）
            outputs = []
            raw_copy = None
            raw_duration = None
            try:
                raw_name = f"{job_id}_tts_raw{os.path.splitext(raw_path)[1].lower() or '.wav'}"
                raw_copy = _out_dir(self.plugin) / raw_name
                shutil.copyfile(raw_path, raw_copy)
                os.chmod(raw_copy, 0o644)
                raw_duration = await self._probe_duration_async(str(raw_copy))
            except Exception as exc:  # noqa: BLE001
                raw_copy = None
                logger.warning("[WebUI] 原始 TTS 音频复制失败（只影响预览）: %s", exc)

            is_raw_only = bool(info.get("raw_only")) or (os.path.abspath(out_path) == os.path.abspath(raw_path))
            if is_raw_only and raw_copy is not None and raw_copy.is_file():
                out_path = str(raw_copy)
                outputs.append({
                    "label": "raw",
                    "label_text": "TTS 原音色" if info.get("raw_only") else "原始 TTS（未转换兜底）",
                    "path": out_path,
                    "name": raw_copy.name,
                    "size": raw_copy.stat().st_size,
                    "duration": raw_duration,
                })
            else:
                if raw_copy is not None and raw_copy.is_file():
                    outputs.append({
                        "label": "raw", "label_text": "原始 TTS",
                        "path": str(raw_copy), "name": raw_copy.name,
                        "size": raw_copy.stat().st_size,
                        "duration": raw_duration,
                    })
                outputs.append({
                    "label": "output", "label_text": "RVC 转换后",
                    "path": out_path, "name": Path(out_path).name,
                    "size": Path(out_path).stat().st_size,
                    "duration": info.get("output_duration") or await self._probe_duration_async(out_path),
                })
            job["output_path"] = out_path
            job["output_name"] = Path(out_path).name
            job["outputs"] = outputs
            job["status"] = "succeeded"
            job["stage"] = "完成"
            job["model_used"] = info.get("model") or ""
            job["reverb"] = info.get("reverb") or {}
            # 控制台任务也进 TTS 历史（触发用户标为 WebUI，文本即朗读内容）
            try:
                self.plugin._record_tts_history(  # noqa: SLF001
                    task_id=job_id,
                    user="WebUI",
                    user_name="WebUI 控制台",
                    request_text=params["text"],
                    speak_text=params["text"],
                    model=str(info.get("model") or ""),
                    output_path=out_path,
                    duration=info.get("output_duration"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[WebUI] 记录 TTS 历史失败: %s", exc)
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = "TTS 转换超时"
            job["stage"] = "超时"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = str(exc)
            job["stage"] = "失败"
            logger.warning("[WebUI] TTS 生成失败 %s: %s", job_id, exc)
        finally:
            if acquired_tts and hasattr(self.plugin, "_release_tts_slot"):
                self.plugin._release_tts_slot()
            job["elapsed"] = round(_now() - started, 1)
            job["updated_at"] = _now()
            self._save_jobs()

    async def _run_job(self, job_id: str, params: dict) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        started = _now()
        kind = job.get("kind") or "convert"
        timeout = float(self.cfg.get("inference_timeout", 600) or 600) + 60
        acquired_slot = False
        try:
            # 接入排队系统：与 QQ 翻唱任务共享槽位与排队
            if hasattr(self.plugin, "_acquire_cover_slot"):
                umo = f"webui_{job_id[:8]}"
                song_name = job.get("input_name", "本地音频")
                model_name = job.get("model", "")
                label = f"WebUI {job.get('kind', '转换')}: {song_name}"
                meta = {
                    "user": "WebUI",
                    "user_name": "WebUI 控制台",
                    "song": song_name,
                    "artist": "本地上传",
                    "model": model_name,
                    "webui_job_id": job_id,
                    "engine": job.get("engine", "本地"),
                }
                job["stage"] = "排队中（等待当前任务完成）"
                job["status"] = "pending"
                job["updated_at"] = _now()
                self._save_jobs()

                acquired = await self.plugin._acquire_cover_slot(umo, label, meta=meta)
                if not acquired:
                    job["status"] = "failed"
                    job["error"] = "当前任务排队已满，请稍后再试"
                    job["stage"] = "排队已满"
                    job["updated_at"] = _now()
                    self._save_jobs()
                    return
                acquired_slot = True
                started = _now()
                job["started_at"] = started
                job["status"] = "running"

            if job.get("engine") == "云端":
                await self._run_kaggle_job(job, params)
                return

            base = self._gateway_base()
            async with self.plugin._get_gradio_client(base) as client:  # noqa: SLF001
                if kind == "separate":
                    job["stage"] = "Gateway 正在分离人声/伴奏"
                    if hasattr(self.plugin, "_set_cover_stage"):
                        self.plugin._set_cover_stage("WebUI 人声分离中")
                elif kind == "clip":
                    job["stage"] = "Gateway 正在切段"
                    if hasattr(self.plugin, "_set_cover_stage"):
                        self.plugin._set_cover_stage("WebUI 音频切段中")
                else:
                    job["stage"] = "Gateway 正在变声推理"
                    if hasattr(self.plugin, "_set_cover_stage"):
                        self.plugin._set_cover_stage("WebUI 变声推理中")
                job["updated_at"] = _now()
                self._save_jobs()
                if kind == "separate":
                    raw = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, **params, api_name="/separate"),
                        timeout=timeout,
                    )
                    self._finish_separate(job, raw)
                elif kind == "clip":
                    raw = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, **params, api_name="/clip"),
                        timeout=timeout,
                    )
                    self._finish_clip(job, raw)
                else:
                    param_names = self.plugin._client_api_parameter_names(client, "/convert")
                    local_params = {
                        k: v for k, v in params.items()
                        if not param_names or k in param_names
                    }
                    result = await asyncio.wait_for(
                        asyncio.to_thread(client.predict, **local_params, api_name="/convert"),
                        timeout=timeout,
                    )
                    await self._finish_convert_async(job, result)
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = "推理超时"
            job["stage"] = "超时"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["stage"] = "失败"
            logger.warning("[WebUI] 转换失败 %s: %s", job_id, exc)
        finally:
            if acquired_slot and hasattr(self.plugin, "_release_cover_slot"):
                self.plugin._release_cover_slot()
            job["elapsed"] = round(_now() - started, 1)
            job["updated_at"] = _now()
            self._save_jobs()

    async def _run_kaggle_job(self, job: dict, params: dict) -> None:
        import httpx
        import urllib.parse

        kind = job.get("kind") or "convert"
        job_id = job.get("id") or ""
        kaggle_url = getattr(self.plugin, "kaggle_endpoint", "").rstrip("/") + "/"
        if not kaggle_url.startswith("http"):
            job["status"] = "failed"
            job["error"] = "未配置 Kaggle 隧道公网端点 (kaggle_endpoint)"
            job["stage"] = "失败"
            self._save_jobs()
            return

        try:
            # 1. 检测云端在线状态；若离线尝试自动开机唤醒
            is_online = await self.plugin._is_kaggle_healthy(kaggle_url)
            if not is_online:
                job["stage"] = "云端实例离线，正在调用 Kaggle 官方 API 唤醒中..."
                self._save_jobs()
                woke = await self.plugin._cold_start_kaggle()
                if not woke:
                    job["status"] = "failed"
                    job["error"] = "云端实例离线且远程唤醒失败，请检查 Kaggle API 配置或手动开机"
                    job["stage"] = "失败"
                    self._save_jobs()
                    return
                # 轮询等待云端服务就绪（最多等待 180 秒）
                ready = False
                for _ in range(36):
                    await asyncio.sleep(5)
                    if await self.plugin._is_kaggle_healthy(kaggle_url):
                        ready = True
                        break
                if not ready:
                    job["status"] = "failed"
                    job["error"] = "等待云端实例启动超时 (180s)，请稍后在控制台重试"
                    job["stage"] = "失败"
                    self._save_jobs()
                    return

            # 2. 定位本地待处理音源
            src_param = str(params.get("song_name_src") or "")
            src_name = Path(src_param).name
            local_src = _upload_dir(self.plugin) / src_name
            if not local_src.is_file():
                alt_src = self.plugin.work_dir / "uploads" / src_name
                if alt_src.is_file():
                    local_src = alt_src
                else:
                    job["status"] = "failed"
                    job["error"] = f"待处理本地音频文件不存在: {src_name}"
                    job["stage"] = "失败"
                    self._save_jobs()
                    return

            # 3. 上传本地音源至 Kaggle 云端
            job["stage"] = "正在向 Kaggle 云端上传音源文件..."
            if hasattr(self.plugin, "_set_cover_stage"):
                self.plugin._set_cover_stage("WebUI 向云端上传音源中")
            self._save_jobs()

            remote_src_path = ""
            async with httpx.AsyncClient(timeout=120.0) as http_client:
                with open(local_src, "rb") as f:
                    files = {"file": (local_src.name, f, "application/octet-stream")}
                    resp = await http_client.post(f"{kaggle_url}api/upload", files=files)
                    resp.raise_for_status()
                    data = resp.json()
                    remote_src_path = str(data.get("path") or "")

            if not remote_src_path:
                job["status"] = "failed"
                job["error"] = "上传音源至 Kaggle 云端失败，未返回远程路径"
                job["stage"] = "失败"
                self._save_jobs()
                return

            out_dir = self.plugin._gateway_out_dir()
            if not out_dir:
                out_dir = str(self.plugin.work_dir / "outputs")
            os.makedirs(out_dir, exist_ok=True)

            # 4. 执行云端任务
            if kind == "separate":
                job["stage"] = "Kaggle 云端 GPU (BS-Roformer 1297) 正在分离人声/伴奏..."
                if hasattr(self.plugin, "_set_cover_stage"):
                    self.plugin._set_cover_stage("WebUI 云端人声分离中")
                self._save_jobs()

                # 调用 Kaggle 分离接口
                sep_model_name = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
                remote_vocals, remote_inst = "", ""
                async with httpx.AsyncClient(timeout=600.0) as http_client:
                    resp = await http_client.post(
                        f"{kaggle_url}api/separate",
                        json={"audio_path": remote_src_path, "model": sep_model_name},
                    )
                    if resp.status_code == 200:
                        sep_res = resp.json()
                        remote_vocals = sep_res.get("vocals") or ""
                        remote_inst = sep_res.get("instrumental") or ""
                    else:
                        # 尝试通过 Gradio client 调用 /separate
                        async with self.plugin._get_gradio_client(kaggle_url) as client:
                            raw_sep = await self.plugin._async_predict(
                                client,
                                remote_src_path,
                                "wav",
                                api_name="/separate",
                                timeout=600.0,
                            )
                            if isinstance(raw_sep, (list, tuple)) and len(raw_sep) >= 2:
                                remote_vocals, remote_inst = str(raw_sep[0]), str(raw_sep[1])

                if not remote_vocals or not remote_inst:
                    job["status"] = "failed"
                    job["error"] = "云端人声分离未返回有效音轨路径"
                    job["stage"] = "失败"
                    self._save_jobs()
                    return

                job["stage"] = "正在从云端拉取分离音轨..."
                self._save_jobs()

                # 下载 vocals 和 instrumental
                task_uid = uuid.uuid4().hex[:8]
                local_vocals = os.path.join(out_dir, f"kaggle_{task_uid}_vocals.wav")
                local_inst = os.path.join(out_dir, f"kaggle_{task_uid}_instrumental.wav")

                async with httpx.AsyncClient(timeout=180.0) as http_client:
                    for rem_p, loc_p in ((remote_vocals, local_vocals), (remote_inst, local_inst)):
                        dl_url = f"{kaggle_url}api/download?path={urllib.parse.quote(str(rem_p))}"
                        r = await http_client.get(dl_url)
                        if r.status_code != 200:
                            fb_url = f"{kaggle_url}gradio_api/file={urllib.parse.quote(str(rem_p))}"
                            r = await http_client.get(fb_url)
                        r.raise_for_status()
                        with open(loc_p, "wb") as f_out:
                            f_out.write(r.content)

                outputs_dict = {}
                mode = job.get("mode") or "both"
                if mode in ("both", "vocals"):
                    outputs_dict["vocals"] = {
                        "host_path": local_vocals,
                        "name": Path(local_vocals).name,
                        "size": Path(local_vocals).stat().st_size,
                        "duration": await self._probe_duration_async(local_vocals),
                    }
                if mode in ("both", "instrumental"):
                    outputs_dict["instrumental"] = {
                        "host_path": local_inst,
                        "name": Path(local_inst).name,
                        "size": Path(local_inst).stat().st_size,
                        "duration": await self._probe_duration_async(local_inst),
                    }
                outputs_dict["sep_model"] = "BS-Roformer 1297 (ViperX)"
                outputs_dict["format"] = params.get("output_format", "wav")
                self._finish_separate(job, outputs_dict)

            else:
                # 变声转换 (kind == "convert")
                skip_sep = bool(params.get("skip_separation", False))
                job["stage"] = "Kaggle 云端 GPU 正在进行 Applio 纯人声变声推理..." if skip_sep else "Kaggle 云端 GPU 正在进行人声分离与 Applio 推理..."
                if hasattr(self.plugin, "_set_cover_stage"):
                    self.plugin._set_cover_stage("WebUI 云端变声推理中")
                self._save_jobs()

                model_name = str(params.get("model_dropdown") or "").strip()
                # 验证云端模型是否存在
                await self.plugin._ensure_model_on_kaggle(kaggle_url, model_name)

                cloud_kwargs = {
                    "song_name_src": remote_src_path,
                    "key_shift": int(params.get("key_shift", 0)),
                    "vocal_vol": 0,
                    "inst_vol": 0,
                    "model_dropdown": model_name,
                    "reverb_intensity": 0.0,
                    "delay_intensity": 0.0,
                    "uvr5_agg": 10,
                    "uvr5_tta": False,
                    "uvr5_postprocess": False,
                    "uvr5_window_size": 512,
                    "uvr5_high_end_process": "mirroring",
                    "msst_batch_size": 1,
                    "msst_num_overlap": 4,
                    "msst_normalize": False,
                    "vocal_postprocess": False,
                    "shift_accompaniment": False,
                    "f0_method": str(params.get("f0_method", "rmvpe") or "rmvpe"),
                    "index_rate": float(params.get("index_rate", 0.75)),
                    "filter_radius": 3,
                    "msst_model": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
                    "msst_use_tta": False,
                    "start_time": str(params.get("start_time") or ""),
                    "end_time": str(params.get("end_time") or ""),
                    "duration": str(params.get("duration") or ""),
                    "output_format": str(params.get("output_format", "wav")),
                    "speaker_id": int(params.get("speaker_id", 0)),
                    "render_mode": str(params.get("render_mode", "full")),
                    "chorus_seconds": float(params.get("chorus_seconds", 25.0)),
                    "chorus_buffer_seconds": float(params.get("chorus_buffer_seconds", 10.0)),
                    "rms_mix_rate": float(params.get("rms_mix_rate", 1.0)),
                    "protect": float(params.get("protect", 0.5)),
                    "inst_volume_db": 0.0,
                    "vocal_volume_db": float(params.get("vocal_volume_db", 0.0)),
                    "fade_seconds": float(params.get("fade_seconds", 0.5)),
                    "reverb_enabled": bool(params.get("reverb_enabled", False)),
                    "reverb_wet": float(params.get("reverb_wet", 0.08)),
                    "reverb_dry": float(params.get("reverb_dry", 1.0)),
                    "reverb_decay": float(params.get("reverb_decay", 0.8)),
                    "reverb_room_size": float(params.get("reverb_room_size", 0.35)),
                    "reverb_damping": float(params.get("reverb_damping", 0.60)),
                    "reverb_pre_delay": float(params.get("reverb_pre_delay", 12.0)),
                    "reverb_width": float(params.get("reverb_width", 0.60)),
                    "f0_autotune": bool(params.get("f0_autotune", False)),
                    "formant_shifting": bool(params.get("formant_shifting", False)),
                    "cleanup_mode": str(params.get("cleanup_mode", "off")),
                    "skip_separation": skip_sep,
                    "track_type": str(params.get("track_type") or "vocals"),
                }

                timeout = float(self.cfg.get("inference_timeout", 600) or 600) + 60
                async with self.plugin._get_gradio_client(kaggle_url) as client:
                    param_names = self.plugin._client_api_parameter_names(client, "/convert")
                    filtered_kwargs = {
                        k: v for k, v in cloud_kwargs.items()
                        if not param_names or k in param_names
                    }
                    res_cloud = await self.plugin._async_predict(
                        client,
                        **filtered_kwargs,
                        api_name="/convert",
                        timeout=timeout,
                    )

                res_str = str(res_cloud or "").strip()
                if not res_str:
                    job["status"] = "failed"
                    job["error"] = "Kaggle 云端变声推理未返回有效路径"
                    job["stage"] = "失败"
                    self._save_jobs()
                    return

                job["stage"] = "正在从云端拉取成品音频..."
                self._save_jobs()

                task_uid = uuid.uuid4().hex[:8]
                fname = Path(res_str).name or f"converted_{task_uid}.wav"
                local_dest = os.path.join(out_dir, f"kaggle_{task_uid}_{fname}")

                async with httpx.AsyncClient(timeout=180.0) as http_client:
                    dl_url = f"{kaggle_url}api/download?path={urllib.parse.quote(res_str)}"
                    r = await http_client.get(dl_url)
                    if r.status_code != 200:
                        fb_url = f"{kaggle_url}gradio_api/file={urllib.parse.quote(res_str)}"
                        r = await http_client.get(fb_url)
                    r.raise_for_status()
                    with open(local_dest, "wb") as f_out:
                        f_out.write(r.content)

                await self._finish_convert_async(job, local_dest)
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = "云端推理超时"
            job["stage"] = "超时"
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["stage"] = "失败"
            logger.warning("[WebUI] 云端任务失败 %s: %s", job_id, exc)

    def _finish_convert(self, job: dict, result, duration: float | None = None) -> None:
        path = ""
        if isinstance(result, (list, tuple)) and result:
            path = str(result[0] or "")
        elif isinstance(result, str):
            path = result
        if not path or not Path(path).is_file():
            job["status"] = "failed"
            job["error"] = f"Gateway 未返回有效文件: {path or '空'}"
            job["stage"] = "失败"
            return
        job["output_path"] = path
        job["output_name"] = Path(path).name
        gw_task_id = Path(path).name.split("_")[0].split(".")[0]
        if gw_task_id:
            job["gateway_task_id"] = gw_task_id
        job["status"] = "succeeded"
        job["stage"] = "完成"
        job["size"] = Path(path).stat().st_size
        job["duration"] = duration if duration is not None else self._probe_duration(path)
        job["outputs"] = [{
            "label": "output", "label_text": "翻唱成品",
            "path": path, "name": Path(path).name,
            "size": job["size"], "duration": job["duration"],
        }]

    async def _finish_convert_async(self, job: dict, result) -> None:
        path = ""
        if isinstance(result, (list, tuple)) and result:
            path = str(result[0] or "")
        elif isinstance(result, str):
            path = result
        duration = await self._probe_duration_async(path) if (path and Path(path).is_file()) else None
        self._finish_convert(job, result, duration=duration)

    def _finish_clip(self, job: dict, raw) -> None:
        data = raw if isinstance(raw, dict) else {}
        item = data.get("file")
        if not isinstance(item, dict):
            job["status"] = "failed"
            job["error"] = "切段未返回有效文件"
            job["stage"] = "失败"
            return
        path = str(item.get("host_path") or "")
        if not path or not Path(path).is_file():
            job["status"] = "failed"
            job["error"] = f"切段结果不存在: {path or '空'}"
            job["stage"] = "失败"
            return
        job["outputs"] = [{
            "label": "clip", "label_text": "切段结果", "path": path,
            "name": item.get("name") or Path(path).name,
            "size": item.get("size") or Path(path).stat().st_size,
            "duration": item.get("duration"),
        }]
        job["status"] = "succeeded"
        job["stage"] = "完成"
        job["clip_range"] = [data.get("start"), data.get("length")]
        job["clip_source"] = data.get("clip_source")

    def _finish_separate(self, job: dict, raw) -> None:
        data = raw if isinstance(raw, dict) else {}
        outputs = []
        for label, text in (("vocals", "人声"), ("instrumental", "伴奏")):
            item = data.get(label)
            if not isinstance(item, dict):
                continue
            path = str(item.get("host_path") or "")
            if not path or not Path(path).is_file():
                continue
            outputs.append({
                "label": label, "label_text": text, "path": path,
                "name": item.get("name") or Path(path).name,
                "size": item.get("size") or Path(path).stat().st_size,
                "duration": item.get("duration"),
            })
        if not outputs:
            job["status"] = "failed"
            job["error"] = "分离未返回有效文件"
            job["stage"] = "失败"
            return
        job["outputs"] = outputs
        job["status"] = "succeeded"
        job["stage"] = "完成"
        job["sep_model"] = data.get("sep_model")
        job["format"] = data.get("format")

    @classmethod
    async def _probe_duration_async(cls, path: str) -> float | None:
        return await asyncio.to_thread(cls._probe_duration, path)

    @staticmethod
    def _probe_duration(path: str) -> float | None:
        import subprocess

        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            return round(float(out), 2) if out else None
        except Exception:
            return None

    async def job_status(self) -> dict:
        job_id = _arg("job_id").strip()
        job = self._safe_job(job_id)
        if job is None:
            if _ID_RE.match(job_id):
                return _ok({"id": job_id, "status": "unknown", "stage": "记录已过期"})
            return _err("非法 job_id")
        return _ok(self._job_public(job))

    async def job_result(self) -> dict:
        job_id = _arg("job_id").strip()
        job = self._safe_job(job_id)
        if job is None:
            return _err("任务不存在或记录已过期")
        if job.get("status") != "succeeded":
            return _err("任务尚未成功完成")
        outputs = list(job.get("outputs") or [])
        if not outputs and job.get("output_path"):
            outputs = [{
                "label": "output", "label_text": "翻唱成品",
                "path": job.get("output_path"), "name": job.get("output_name"),
                "size": job.get("size"), "duration": job.get("duration"),
            }]
        files = []
        for item in outputs:
            path = Path(str(item.get("path") or ""))
            try:
                path.resolve().relative_to(_out_dir(self.plugin).resolve())
            except Exception:
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            entry = {
                "label": item.get("label") or "output",
                "label_text": item.get("label_text") or "输出",
                "name": path.name,
                "size": size,
                "size_text": _fmt_bytes(size),
                "duration": item.get("duration"),
                "mime": "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg",
            }
            if size > MAX_RESULT_BYTES:
                entry["error"] = f"结果过大（{_fmt_bytes(size)}），请到服务器查看"
            else:
                entry["audio_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
            files.append(entry)
        if not files:
            return _err("结果文件已被清理或在允许目录之外")
        return _ok({"job_id": job_id, "kind": job.get("kind") or "convert", "outputs": files})

    def _load_tts_history(self) -> dict:
        data = _read_json(_data_dir() / "tts_history.json", {})
        return data if isinstance(data, dict) else {}

    async def tts_tasks(self) -> dict:
        """TTS 当前队列 + 历史任务（含触发用户与文本，列表里文本过长会截断）。"""
        try:
            snap = self.plugin._tts_queue_snapshot()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return _err(f"读取队列失败: {exc}")
        rows = []
        for item in self._load_tts_history().values():
            path = str(item.get("output") or "")
            playable = bool(path) and Path(path).resolve().as_posix().startswith(
                _out_dir(self.plugin).resolve().as_posix()) and Path(path).is_file()
            rows.append({
                "id": str(item.get("task_id") or ""),
                "user": str(item.get("user") or ""),
                "user_name": str(item.get("user_name") or ""),
                "group_id": str(item.get("group_id") or ""),
                "group_name": str(item.get("group_name") or ""),
                "request": str(item.get("request") or "")[:60],
                "speak": str(item.get("speak") or "")[:60],
                "model": str(item.get("model") or ""),
                "output_name": Path(path).name if path else "",
                "duration": item.get("duration"),
                "created_at": item.get("created_at") or 0,
                "status": "succeeded",
                "playable": playable,
            })
        rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        try:
            limit = max(1, min(200, int(_arg("limit", "60") or 60)))
        except Exception:  # noqa: BLE001
            limit = 60
        return _ok({
            "active": snap.get("active"),
            "waiting": snap.get("waiting") or [],
            "queue_limit": snap.get("limit", 3),
            "items": rows[:limit],
            "total": len(rows),
        })

    async def tts_audio(self) -> dict:
        """按任务号返回 TTS 音频（仅允许网关 out 目录内的成品）。"""
        task_id = _arg("id").strip()
        if not _ID_RE.match(task_id):
            return _err("非法任务号")
        item = self._load_tts_history().get(task_id)
        if not isinstance(item, dict):
            return _err("找不到该 TTS 任务记录")
        path = Path(str(item.get("output") or ""))
        try:
            path.resolve().relative_to(_out_dir(self.plugin).resolve())
        except Exception:  # noqa: BLE001
            return _err("结果路径不在允许目录内")
        if not path.is_file():
            return _err("音频已被清理")
        size = path.stat().st_size
        if size > MAX_RESULT_BYTES:
            return _err(f"音频过大（{_fmt_bytes(size)}），请到服务器查看")
        return _ok({
            "id": task_id,
            "name": path.name,
            "speak": str(item.get("speak") or ""),
            "user": str(item.get("user") or ""),
            "group_id": str(item.get("group_id") or ""),
            "group_name": str(item.get("group_name") or ""),
            "duration": item.get("duration"),
            "mime": "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg",
            "audio_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        })

    # ---------------- 诊断 ----------------
    async def diagnostics(self) -> dict:
        health = (await self.health())["data"]
        info = await self._gateway_model_info(use_cache=False)
        entries = self._model_entries()
        default = str(self.cfg.get("rvc_default_model", "") or "")
        default_key = os.path.splitext(default)[0]
        checks = []

        def add(name: str, ok: bool, detail: str, level: str = "error"):
            checks.append({"name": name, "ok": ok, "detail": detail, "level": level})

        add("Gateway 连通", bool(health.get("ok")),
            f"{health.get('base')} · {health.get('latency_ms')}ms" if health.get("ok")
            else (health.get("error") or "不可达"))
        add("Gateway 模型列表", bool(info["ok"]), info["error"] or f"{len(info['models'])} 个模型")
        add("插件已配置音色", bool(entries), f"{len(entries)} 个")
        add("默认音色已设置", bool(default), default or "未设置（会弹音色菜单）", "warning")
        add("默认音色在配置列表", bool(default_key) and any(e["name"] == default_key for e in entries),
            default_key or "-")
        if info["ok"] and default_key:
            add("默认音色在 Gateway 列表",
                any(os.path.splitext(m)[0] == default_key for m in info["models"]),
                default_key)
        add("默认音色性别已知", self._gender_of(default) in ("male", "female"),
            self._gender_of(default), "warning")
        try:
            timeout = int(self.cfg.get("inference_timeout", 0) or 0)
        except Exception:
            timeout = 0
        add("推理超时足够", timeout >= 300, f"{timeout}s（建议 ≥600）", "warning")
        add("任务记录目录可读", _task_log_dir(self.plugin).is_dir(), str(_task_log_dir(self.plugin)), "warning")
        add("成品目录可读", _out_dir(self.plugin).is_dir(), str(_out_dir(self.plugin)), "warning")

        errors = self._recent_errors(limit=40)
        return _ok({
            "health": health,
            "checks": checks,
            "errors": errors,
            "gateway_models": info["models"],
            "paths": {
                "gateway_work": str(_gateway_work(self.plugin) or "(未配置)"),
                "upload": str(_upload_dir(self.plugin)),
                "output": str(_out_dir(self.plugin)),
                "task_logs": str(_task_log_dir(self.plugin)),
                "plugin_data": str(_data_dir()),
            },
            "version": {"plugin": self._plugin_version()},
        })

    def _recent_errors(self, limit: int = 40) -> list[dict]:
        """从 AstrBot 日志里截取本插件相关的错误行（只读、有上限）。"""
        if not _log_file().is_file():
            return []
        rows: list[dict] = []
        try:
            with open(_log_file(), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 400 * 1024))
                tail = f.read().decode("utf-8", errors="ignore")
        except Exception:
            return []
        lines = tail.splitlines()
        for i, line in enumerate(lines):
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
            if PLUGIN_NAME not in clean:
                continue
            if not re.search(r"\[ERRO\]|\[WARN\]|Traceback|异常", clean):
                continue
            ctx = ""
            if i + 1 < len(lines):
                nxt = re.sub(r"\x1b\[[0-9;]*m", "", lines[i + 1]).strip()
                if nxt and PLUGIN_NAME not in nxt:
                    ctx = nxt[:160]
            ts = ""
            m = re.match(r"\[(\d\d:\d\d:\d\d)", clean)
            if m:
                ts = m.group(1)
            rows.append({"text": clean.strip()[:400], "context": ctx, "time": ts})
        return rows[-limit:][::-1]
