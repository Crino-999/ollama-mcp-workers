"""MCP stdio 冒烟测试：以客户端身份连接 doc_worker.py，验证 Codex 接入链路。

用法：python scripts/mcp_smoke.py [--tool generate_test_cases]
默认用 extract_requirements（最快）；可选 generate_test_cases 走完整生产路径。
"""

import argparse
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def send(proc, obj):
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def recv(proc, timeout=180):
    q = queue.Queue()

    def reader():
        try:
            q.put(("line", proc.stdout.readline()))
        except Exception as exc:  # noqa: BLE001
            q.put(("error", exc))

    threading.Thread(target=reader, daemon=True).start()
    try:
        kind, val = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"等待 MCP 响应超时（{timeout}s）")  # noqa: B904
    if kind == "error":
        raise val
    if not val:
        raise RuntimeError("MCP 进程已退出（stdout 关闭）")
    return json.loads(val)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tool", default="extract_requirements", choices=["extract_requirements", "generate_test_cases"])
    args = ap.parse_args()

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "doc_worker.py")],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-smoke", "version": "0.1"},
        },
    })
    init = recv(proc, timeout=30)
    print("initialize:", json.dumps(init, ensure_ascii=False)[:160])
    send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = recv(proc, timeout=30)
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    print("tools/list:", names)

    fixture = ROOT / "tests" / "fixtures" / "prd_vision_tool.md"
    send(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {
            "name": args.tool,
            "arguments": {"source_path": str(fixture)},
        },
    })
    result = recv(proc, timeout=180)
    content = result.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else ""
    is_error = result.get("result", {}).get("isError", False)
    print(f"tools/call({args.tool}): isError={is_error} 返回 {len(text)} 字符")
    if args.tool == "extract_requirements":
        data = json.loads(text)
        print("功能需求条数:", len(data.get("functional_requirements", [])))
        print("首条:", json.dumps(data["functional_requirements"][0], ensure_ascii=False)[:120])
    else:
        data = json.loads(text)
        print("测试用例数:", len(data.get("test_cases", [])))
        print("首条:", json.dumps(data["test_cases"][0], ensure_ascii=False)[:120])

    proc.terminate()
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
