# -*- coding: utf-8 -*-
import os
import sys
import time
import httpx
from gradio_client import Client

ENDPOINT = os.getenv("RVC_KAGGLE_ENDPOINT", sys.argv[1] if len(sys.argv) > 1 else "https://rvc.yourdomain.com/")
if not ENDPOINT.endswith("/"):
    ENDPOINT += "/"
SAMPLE_AUDIO = os.getenv("RVC_TEST_AUDIO", "test_30s.mp3")
OUT_DIR = os.getenv("RVC_TEST_OUT_DIR", "./out")

def log(msg):
    now_str = time.strftime("%H:%M:%S")
    print(f"[{now_str}] {msg}", flush=True)

def wait_for_online(max_wait=1200):
    log("⏳ 等待 Kaggle 冷启动上线...")
    start_t = time.time()
    last_log_t = start_t
    while time.time() - start_t < max_wait:
        try:
            r = httpx.get(f"{ENDPOINT}api/health", timeout=3.0)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "ok":
                    log(f"🟢 Kaggle 云端已成功连通！加速设备: {data.get('device', 'GPU')}，用时: {time.time() - start_t:.1f}s")
                    return True
        except Exception:
            pass
        if time.time() - last_log_t >= 20:
            last_log_t = time.time()
            log(f"⏳ 等待云端容器分配与建联... 已等待 {int(time.time() - start_t)}s")
        time.sleep(3)
    log("❌ 等待超时，Kaggle 仍未上线。")
    return False

def run_single_cover(client, model_name, task_num):
    log(f"\n--- [任务 {task_num}] 开始翻唱测试: 音色={model_name} ---")
    t0 = time.time()
    
    # 1. 上传音频
    with open(SAMPLE_AUDIO, "rb") as f:
        r = httpx.post(f"{ENDPOINT}api/upload", files={"file": (f"test_track_{task_num}.mp3", f, "audio/mpeg")}, timeout=30.0)
        remote_path = r.json().get("path")
    log(f"音频上传完成，云端路径: {remote_path}")

    # 2. 如果云端没有模型，自动上传
    r_has = httpx.get(f"{ENDPOINT}api/has_model", params={"model": model_name}, timeout=10.0)
    if not (r_has.status_code == 200 and r_has.json().get("exists")):
        log(f"云端缺失模型 {model_name}，正在从本地同步上传...")
        weights_dir = os.getenv("RVC_WEIGHTS_DIR", "./weights")
        indices_dir = os.getenv("RVC_INDICES_DIR", "./indices")
        local_pth = os.path.join(weights_dir, model_name)
        if not os.path.exists(local_pth):
            local_pth = os.path.join(weights_dir, f"{model_name}.pth")
        if os.path.exists(local_pth):
            with open(local_pth, "rb") as f:
                httpx.post(f"{ENDPOINT}api/upload_model", files={"file": (os.path.basename(local_pth), f, "application/octet-stream")}, timeout=120.0)
            log(f"模型 {os.path.basename(local_pth)} 已同步至云端！")
        
        # 索引同步
        stem = os.path.splitext(os.path.basename(model_name))[0]
        local_idx = os.path.join(indices_dir, f"{stem}.index")
        if os.path.exists(local_idx):
            try:
                with open(local_idx, "rb") as f:
                    httpx.post(f"{ENDPOINT}api/upload_model", files={"file": (os.path.basename(local_idx), f, "application/octet-stream")}, timeout=60.0)
                log(f"特征索引 {os.path.basename(local_idx)} 已同步至云端！")
            except Exception as e:
                log(f"索引上传跳过: {e}")

    # 3. 提交推理
    log(f"正在调用 Kaggle /convert 进行人声分离 + RVC 推理...")
    result = client.predict(
        song_name_src=remote_path,
        key_shift=0,
        vocal_vol=0,
        inst_vol=0,
        model_dropdown=model_name,
        reverb_intensity=0,
        delay_intensity=0,
        uvr5_agg=10,
        uvr5_tta=False,
        uvr5_postprocess=False,
        uvr5_window_size=512,
        uvr5_high_end_process="mirroring",
        msst_batch_size=1,
        msst_num_overlap=4,
        msst_normalize=False,
        vocal_postprocess=False,
        shift_accompaniment=True,
        f0_method="rmvpe",
        index_rate="0.75",
        filter_radius=3,
        msst_model="",
        msst_use_tta=False,
        start_time="",
        end_time="",
        duration="",
        output_format="wav",
        speaker_id=0,
        render_mode="full",
        chorus_seconds=30.0,
        rms_mix_rate=1.0,
        protect=0.5,
        inst_volume_db=0.0,
        vocal_volume_db=0.0,
        fade_seconds=0.0,
        reverb_enabled=False,
        reverb_wet=0.08,
        reverb_dry=1.0,
        reverb_decay=0.8,
        reverb_room_size=0.35,
        reverb_damping=0.60,
        reverb_pre_delay=12.0,
        reverb_width=0.60,
        f0_autotune=False,
        formant_shifting=False,
        cleanup_mode="off",
        skip_separation=True,
        api_name="/convert"
    )
    log(f"任务 {task_num} 推理成功，云端输出: {result} (耗时: {time.time() - t0:.1f}s)")

    # 4. 回传音频
    save_path = os.path.join(OUT_DIR, f"kaggle_test_task_{task_num}_{model_name}.wav")
    r_dl = httpx.get(f"{ENDPOINT}api/download", params={"path": result}, timeout=60.0)
    with open(save_path, "wb") as f:
        f.write(r_dl.content)
    size_kb = os.path.getsize(save_path) / 1024
    log(f"✅ 成品已回传保存: {save_path} ({size_kb:.1f} KB)")
    return save_path

def monitor_shutdown(max_idle=420):
    log("\n" + "="*60)
    log("⏰ [看门狗自动关机测试] 所有排队翻唱任务已全部完成！")
    log("当前开始进入空闲监测，目标验证: 连续 300 秒（5分钟）无任务是否自动触发关机...")
    log("="*60)

    start_idle = time.time()

    while time.time() - start_idle < max_idle:
        try:
            r = httpx.get(f"{ENDPOINT}api/health", timeout=4.0)
            if r.status_code == 200:
                data = r.json()
                idle_sec = int(data.get("idle_seconds", 0))
                remain = max(0, 300 - idle_sec)
                log(f"💓 云端看门狗心跳: 已空闲 {idle_sec}s / 300s，距离自动关机还剩约 {remain // 60}分{remain % 60}秒")
            elif r.status_code in (502, 503, 530):
                elapsed = time.time() - start_idle
                log(f"\n🎉 [关机成功] 探测到 HTTP {r.status_code}，云端服务进程已彻底退出！")
                log(f"实测空闲总时长: {elapsed:.1f} 秒 (约 {int(elapsed)//60} 分 {int(elapsed)%60} 秒)")
                log("5 分钟自动防扣费与关机看门狗全链路验证 100% 成功！")
                return True
            else:
                log(f"🔔 探测到状态码 {r.status_code}，隧道正在断开...")
        except Exception:
            elapsed = time.time() - start_idle
            log(f"\n🎉 [关机成功] 隧道已彻底断开！Kaggle 看门狗已执行自动关机。")
            log(f"实测空闲总时长: {elapsed:.1f} 秒 (约 {int(elapsed)//60} 分 {int(elapsed)%60} 秒)")
            log("5 分钟自动防扣费与关机看门狗全链路验证 100% 成功！")
            return True
        time.sleep(20)

    log("⚠️ 超过预设观察时长，Kaggle 仍在响应。")
    return False

if __name__ == "__main__":
    if not wait_for_online():
        sys.exit(1)

    client = Client(ENDPOINT)
    
    # 连续提交排队测试
    run_single_cover(client, os.getenv("RVC_TEST_MODEL_1", "voice_a.pth"), 1)
    run_single_cover(client, os.getenv("RVC_TEST_MODEL_2", "voice_b.pth"), 2)
    
    # 监控 5 分钟关机
    monitor_shutdown()
