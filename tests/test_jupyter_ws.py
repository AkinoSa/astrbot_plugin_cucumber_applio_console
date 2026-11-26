import asyncio
import json
import uuid
import sys
import websockets

import os
ws_url = os.getenv("KAGGLE_WS_URL", "")

async def test_exec():
    print("Connecting to Jupyter kernel WS...")
    async with websockets.connect(ws_url) as ws:
        msg_id = uuid.uuid4().hex
        code_to_run = (
            "import torch\n"
            "print('PyTorch CUDA Available:', torch.cuda.is_available())\n"
            "print('Device Name:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'None')\n"
        )
        msg = {
            "header": {
                "msg_id": msg_id,
                "username": "username",
                "session": uuid.uuid4().hex,
                "msg_type": "execute_request",
                "version": "5.3"
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code_to_run,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False
            },
            "channel": "shell"
        }
        await ws.send(json.dumps(msg))
        print("Sent execute_request, waiting for output...")
        
        while True:
            try:
                res_raw = await asyncio.wait_for(ws.recv(), timeout=20.0)
                res = json.loads(res_raw)
                msg_type = res.get("header", {}).get("msg_type")
                content = res.get("content", {})
                if msg_type == "stream":
                    print("[STREAM OUT]:", content.get("text", "").strip())
                elif msg_type == "execute_reply":
                    print("[EXECUTE REPLY STATUS]:", content.get("status"))
                    break
                elif msg_type == "error":
                    print("[ERROR]:", content.get("ename"), content.get("evalue"))
                    break
            except asyncio.TimeoutError:
                print("Timeout waiting for message")
                break

asyncio.run(test_exec())
