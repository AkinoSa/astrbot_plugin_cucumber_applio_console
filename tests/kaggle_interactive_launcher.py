# -*- coding: utf-8 -*-
import asyncio
import json
import os
import sys
import time
import uuid
import httpx
import websockets

KERNEL_SESSION_ID = int(os.getenv("KAGGLE_SESSION_ID", "0"))
WS_URL = os.getenv("KAGGLE_WS_URL", "")
ENDPOINT = os.getenv("RVC_KAGGLE_ENDPOINT", "https://rvc.yourdomain.com/")
if not ENDPOINT.endswith("/"):
    ENDPOINT += "/"

def log(msg):
    now_str = time.strftime("%H:%M:%S")
    print(f"[{now_str}] {msg}", flush=True)

async def exec_code(ws, code, desc=""):
    log(f"▶️ [执行] {desc}...")
    msg_id = uuid.uuid4().hex
    msg = {
        "header": {
            "msg_id": msg_id,
            "username": "user",
            "session": uuid.uuid4().hex,
            "msg_type": "execute_request",
            "version": "5.3"
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": code,
            "silent": False,
            "store_history": True,
            "user_expressions": {},
            "allow_stdin": False
        },
        "channel": "shell"
    }
    await ws.send(json.dumps(msg))
    while True:
        try:
            res_raw = await asyncio.wait_for(ws.recv(), timeout=600.0)
            res = json.loads(res_raw)
            mtype = res.get("header", {}).get("msg_type")
            content = res.get("content", {})
            if mtype == "stream":
                for line in content.get("text", "").splitlines():
                    if line.strip():
                        log(f"   {line.strip()}")
            elif mtype == "execute_reply":
                status = content.get("status")
                if status == "ok":
                    log(f"✅ {desc} 执行完成！")
                else:
                    log(f"⚠️ {desc} 状态: {status}")
                return status == "ok"
            elif mtype == "error":
                log(f"❌ {desc} 出错: {content.get('ename')}: {content.get('evalue')}")
                return False
        except asyncio.TimeoutError:
            log(f"❌ {desc} 执行超时！")
            return False

async def main():
    log("🚀 开始连接 Kaggle 云端 GPU 交互式实例...")
    async with websockets.connect(WS_URL, max_size=20*1024*1024) as ws:
        # 1. 配置参数
        c1 = """
import os
TUNNEL_TOKEN = os.getenv("CF_TUNNEL_TOKEN", "")
CUSTOM_DOMAIN = os.getenv("CUSTOM_DOMAIN", "https://rvc.yourdomain.com")
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "300"))
print(f"配置已加载: Token长度={len(TUNNEL_TOKEN)}, 空闲时限={IDLE_TIMEOUT_SECONDS}s")
"""
        await exec_code(ws, c1, "加载参数配置")

        # 2. 安装依赖
        c3 = """
import sys, os, subprocess
print('📦 正在安装依赖组件 (PyTorch, audio-separator, Gradio, FastAPI, Uvicorn)...')
subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '--upgrade', 'pip'], check=False)
subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'audio-separator[gpu]', 'fastapi', 'uvicorn', 'python-multipart', 'gradio>=4.38.0', 'httpx', 'tqdm'], check=False)

if not os.path.exists('/kaggle/working/cloudflared'):
    print('🌐 正在下载 Cloudflare Tunnel 客户端 (cloudflared)...')
    subprocess.run(['wget', '-q', '-O', '/kaggle/working/cloudflared', 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64'], check=True)
    subprocess.run(['chmod', '+x', '/kaggle/working/cloudflared'], check=True)
print('✅ 环境准备完毕！')
"""
        await exec_code(ws, c3, "安装依赖与 cloudflared")

        # 3. 准备模型权重
        c4 = """
import os, urllib.request
models_dir = '/kaggle/working/assets'
os.makedirs(models_dir, exist_ok=True)
os.makedirs('/kaggle/working/weights', exist_ok=True)
os.makedirs('/kaggle/working/indices', exist_ok=True)
os.makedirs('/kaggle/working/tasks', exist_ok=True)

hubert_path = os.path.join(models_dir, 'hubert_base.pt')
rmvpe_path = os.path.join(models_dir, 'rmvpe.pt')

if not os.path.exists(hubert_path):
    print('⬇️ 正在下载 Hubert 音色基底模型 (hubert_base.pt)...')
    urllib.request.urlretrieve('https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/hubert_base.pt', hubert_path)

if not os.path.exists(rmvpe_path):
    print('⬇️ 正在下载 RMVPE 伴奏人声音高提取模型 (rmvpe.pt)...')
    urllib.request.urlretrieve('https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt', rmvpe_path)

print('✅ 基础模型准备完成！')
"""
        await exec_code(ws, c4, "下载基础音色与高精度分离模型")

        # 4. 写入 kaggle_server.py
        server_py_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kaggle", "kaggle_server.py")
        with open(server_py_path, "r", encoding="utf-8") as f:
            server_py_content = f.read()
        
        c5 = f"with open('/kaggle/working/kaggle_server.py', 'w', encoding='utf-8') as f:\n    f.write({repr(server_py_content)})\nprint('✅ kaggle_server.py 部署完毕！大小:', len({repr(server_py_content)}))"
        await exec_code(ws, c5, "部署最新 kaggle_server.py (含浮点修复与看门狗)")

        # 5. 启动 Cloudflare Tunnel 与后台推理服务
        c6_7 = """
import subprocess, os, time

token = TUNNEL_TOKEN.strip()
print('🌐 启动 Cloudflare Tunnel...')
subprocess.Popen(['/kaggle/working/cloudflared', 'tunnel', 'run', '--token', token], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)

print('🚀 启动 FastAPI + Gradio 后台推理服务...')
os.environ['IDLE_TIMEOUT_SECONDS'] = str(IDLE_TIMEOUT_SECONDS)
server_proc = subprocess.Popen(['python', '/kaggle/working/kaggle_server.py'])
print(f'✅ 服务进程已启动 (PID: {server_proc.pid})，看门狗空闲阈值: {IDLE_TIMEOUT_SECONDS}秒')
"""
        await exec_code(ws, c6_7, "启动 Cloudflare Tunnel 与推理服务")

    log("🎉 云端启动指令全量下发完毕，正在验证公网隧道健康状态...")
    for i in range(30):
        try:
            r = httpx.get(f"{ENDPOINT}api/health", timeout=3.0)
            if r.status_code == 200 and r.json().get("status") == "ok":
                log(f"🟢 [连通成功] Kaggle 云端已成功连通！设备: {r.json().get('device')}")
                return True
        except Exception:
            pass
        time.sleep(2)
        log(f"   等待隧道握手... ({i+1}/30)")
    log("❌ 隧道连接等待超时")
    return False

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
