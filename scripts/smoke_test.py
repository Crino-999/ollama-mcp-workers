#!/usr/bin/env python3
"""
Qwen2.5-VL MCP Server 冒烟测试

不依赖任何 MCP 客户端，直接以标准 MCP stdio 协议与 run.py 通信：
1. 启动 run.py
2. initialize 握手
3. tools/list 检查 analyze_image / get_status 是否注册
4. 生成一张测试图并调用 analyze_image
5. 断言返回非空文本，确认整条链路（MCP -> Ollama -> Qwen2.5-VL）可用

用法:
    python scripts/smoke_test.py                              # 使用自动生成的测试图
    python scripts/smoke_test.py D:/path/a.png                # 使用指定图片
    python scripts/smoke_test.py D:/path/a.png "自定义提问"    # 指定图片和提问
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_PY = PROJECT_ROOT / "run.py"
TOOL_CALL_TIMEOUT = 120  # 与 run.py 的 OLLAMA_TIMEOUT 对齐

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def make_test_image() -> Path:
    """生成一张带文字的简单测试图，用于验证视觉链路。"""
    from PIL import Image, ImageDraw

    fd, path = tempfile.mkstemp(suffix=".png", prefix="mcp_vision_")
    import os

    os.close(fd)
    img = Image.new("RGB", (640, 320), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 280, 120], outline="red", width=4)
    draw.text((60, 70), "MCP TEST 123", fill="black")
    img.save(path)
    return Path(path)


def send(proc, obj) -> None:
    proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()


def recv_response(proc, rid: int, timeout: float):
    """同步读取一行 JSON-RPC 响应，直到 id 匹配或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == rid:
            return msg
    return None


def main() -> int:
    image_arg = sys.argv[1] if len(sys.argv) > 1 else None
    custom_prompt = sys.argv[2] if len(sys.argv) > 2 else "图片中有什么文字？只回答文字内容"
    image_path = Path(image_arg).resolve() if image_arg else make_test_image()
    print(f"[1/5] 使用测试图片: {image_path}")

    proc = subprocess.Popen(
        [sys.executable, str(RUN_PY)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )

    # 后台排空 stderr，避免日志写满管道导致阻塞
    def drain_stderr():
        for _ in proc.stderr:
            pass

    threading.Thread(target=drain_stderr, daemon=True).start()

    try:
        # 1. initialize 握手
        send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
        })
        init = recv_response(proc, 1, 15)
        assert init and "result" in init, f"initialize 失败: {init}"
        print(f"[2/5] initialize 成功, protocol={init['result'].get('protocolVersion')}")

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # 2. tools/list
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = recv_response(proc, 2, 15)
        assert tools and "result" in tools, f"tools/list 失败: {tools}"
        names = [t["name"] for t in tools["result"]["tools"]]
        assert "analyze_image" in names and "get_status" in names, f"缺少工具: {names}"
        print(f"[3/5] tools/list 成功, 已注册: {', '.join(names)}")

        # 3. tools/call analyze_image
        send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "analyze_image",
                "arguments": {
                    "image_path": str(image_path),
                    "custom_prompt": custom_prompt,
                },
            },
        })
        call = recv_response(proc, 3, TOOL_CALL_TIMEOUT)
        assert call and "result" in call, f"tools/call 失败: {call}"
        result = call["result"]
        assert result.get("isError") is not True, f"工具返回错误: {result}"
        text = "".join(
            c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
        )
        assert text.strip(), "analyze_image 返回空文本"
        print(f"[4/5] analyze_image 调用成功")
        print(f"[5/5] 模型回答: {text.strip()}")
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
