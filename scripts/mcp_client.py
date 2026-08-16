#!/usr/bin/env python3
"""
通用 MCP stdio 调用客户端：以客户端身份连接本地 Worker，调用任意工具。

用途：
  不依赖 Codex / Trae 等宿主，直接驱动 Vision / Document 两个 Worker，
  用于功能验证、基准测试与日常脚本化调用。

用法：
  # 查看工具列表
  python scripts/mcp_client.py --server run.py --list

  # 调用 Vision Worker 分析图片（任务结束后卸载模型，切换 Worker 时用）
  python scripts/mcp_client.py --server run.py --tool analyze_image ^
      --json "{\"image_path\": \"D:/x.png\"}" --unload true

  # 连续调用 Document Worker（保持模型驻留，用 --unload false）
  python scripts/mcp_client.py --server doc_worker.py --tool extract_requirements ^
      --json "{\"source_path\": \"D:/prd.md\"}" --unload false

说明：
  - unload_after_task 由 --unload 控制并注入工具参数；不指定时使用工具默认值（true）。
  - 协议基于 MCP stdio（换行分隔 JSON-RPC），与 scripts/mcp_smoke.py 同源。
"""

import argparse
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class McpClient:
    def __init__(self, server_py: Path, timeout: int = 300):
        self.server_py = Path(server_py)
        self.timeout = timeout
        self.proc = None
        self._reader_queue = queue.Queue()

    def _send(self, obj) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self, timeout: int = None) -> dict:
        timeout = timeout or self.timeout
        try:
            kind, val = self._reader_queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"等待 MCP 响应超时（{timeout}s）")
        if kind == "error":
            raise val
        if not val:
            raise RuntimeError("MCP 进程已退出（stdout 关闭）")
        return json.loads(val)

    def _start(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(self.server_py)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        def reader():
            try:
                while True:
                    line = self.proc.stdout.readline()
                    self._reader_queue.put(("line", line))
                    if not line:
                        break
            except Exception as exc:  # noqa: BLE001
                self._reader_queue.put(("error", exc))

        threading.Thread(target=reader, daemon=True).start()

    def initialize(self):
        self._send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-client", "version": "1.0.0"},
            },
        })
        resp = self._recv(30)
        assert "result" in resp, f"initialize 失败: {resp}"
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp["result"]

    def list_tools(self) -> list:
        self._send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = self._recv()
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        self._send({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        resp = self._recv()
        result = resp.get("result", {})
        if result.get("isError"):
            raise RuntimeError(f"工具 {name} 返回错误: {result.get('content', [])}")
        texts = [
            c.get("text", "")
            for c in result.get("content", [])
            if c.get("type") == "text"
        ]
        return {"text": "\n".join(texts), "raw": result}

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="run.py", help="Worker 入口，如 run.py / doc_worker.py（相对项目根）")
    ap.add_argument("--list", action="store_true", help="只列出工具，不调用")
    ap.add_argument("--tool", help="要调用的工具名")
    ap.add_argument("--json", help='工具参数 JSON 字符串，如 {"image_path": "D:/x.png"}')
    ap.add_argument("--unload", choices=["true", "false"], default=None,
                    help="注入 unload_after_task（true=任务结束即卸载模型；false=保持驻留，适合连续调用）")
    ap.add_argument("--timeout", type=int, default=300, help="单次响应超时（秒）")
    args = ap.parse_args()

    client = McpClient(Path(args.server), timeout=args.timeout)
    try:
        client._start()
        info = client.initialize()
        print(f"[MCP] 已连接 {info.get('serverInfo', {}).get('name', args.server)} "
              f"(protocol {info.get('protocolVersion', '?')})", file=sys.stderr)

        if args.list:
            for t in client.list_tools():
                print(t.get("name", "?"))
            return

        if not args.tool:
            ap.error("调用工具时需要 --tool；或使用 --list 查看工具列表")

        arguments = json.loads(args.json) if args.json else {}
        if args.unload is not None:
            arguments["unload_after_task"] = args.unload == "true"

        result = client.call_tool(args.tool, arguments)
        print(result["text"])
    finally:
        client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
