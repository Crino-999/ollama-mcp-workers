#!/usr/bin/env python3
"""
手动验证脚本：Ollama 模型 graceful unload 与串行切换时延对比。

用法：
    python scripts/verify_model_unload.py unload --model qwen3:4b
    python scripts/verify_model_unload.py switch --model-a qwen3:4b --model-b qwen2.5vl:7b
    python scripts/verify_model_unload.py switch --with-unload   # 只测开启卸载的模式

说明：
- "unload" 模式：加载模型 -> 打印 ollama ps -> 调用 unload_model ->
  轮询 /api/ps 直到模型释放，分别打印"卸载请求耗时"与"完全释放耗时"。
- "switch" 模式：在 with_unload / without_unload 两种模式下测量
  "任务 A 结束 -> 任务 B 完成" 的切换时延（串行 Worker 场景的核心指标）。
  每轮开始前会先确保两个模型都已卸载，避免上一轮残留影响结果。
  可传入 --vision-image / --doc-file 使用真实负载（图片分析 / PRD 文本），
  否则使用短文本提示。
"""

import argparse
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import model_lifecycle  # noqa: E402

HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_PROMPT = "Reply with exactly: OK"
MAX_IMAGE_DIM = 1024
DOC_SAMPLE_CHARS = 3000


def _norm(name: str) -> str:
    return model_lifecycle._normalize_model_name(name)


def _image_b64(path: Path) -> str:
    """与 run.py 一致的预处理：RGB、最大边 1024、JPEG q85、Base64。"""
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_IMAGE_DIM:
        if w > h:
            img = img.resize((MAX_IMAGE_DIM, int(h * MAX_IMAGE_DIM / w)), Image.LANCZOS)
        else:
            img = img.resize((int(w * MAX_IMAGE_DIM / h), MAX_IMAGE_DIM), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _task_call(model: str, timeout: int, kind: str = "text", vision_image: str = None, doc_file: str = None) -> dict:
    """执行一次任务调用并返回 Ollama 响应（含 load_duration / total_duration）。

    vision 用图片分析，doc 用文件文本，默认短文本。num_predict 限长以降低
    推理噪声，让"切换时延"成为主要被测对象。
    """
    payload = {"model": model, "stream": False, "options": {"num_predict": 512}}
    if kind == "vision" and vision_image:
        payload["images"] = [_image_b64(Path(vision_image))]
        payload["prompt"] = "请用中文简要描述这张图片的内容（不超过100字）。"
    elif kind == "doc" and doc_file:
        text = Path(doc_file).read_text(encoding="utf-8", errors="replace")
        payload["prompt"] = (
            "请从以下文档中提取功能需求，用中文一句话概括每条需求（最多列5条）：\n\n"
            + text[:DOC_SAMPLE_CHARS]
        )
    else:
        payload["prompt"] = DEFAULT_PROMPT
    resp = requests.post(f"{HOST}/api/generate", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _fmt_metrics(wall_s: float, data: dict) -> str:
    load_s = (data.get("load_duration") or 0) / 1e9
    total_s = (data.get("total_duration") or 0) / 1e9
    head_s = max(0.0, wall_s - total_s)
    return (
        f"墙钟 {wall_s:.1f}s | 调度等待(旧模型释放等) {head_s:.1f}s "
        f"| 新模型加载 {load_s:.1f}s | 推理 {max(0.0, total_s - load_s):.1f}s"
    )


def _ps_names():
    loaded = model_lifecycle.list_loaded_models(host=HOST)
    return loaded or []


def _wait_released(model: str, max_wait: int):
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_wait:
        if not any(_norm(n) == _norm(model) for n in _ps_names()):
            return True, time.monotonic() - t0
        time.sleep(0.5)
    return False, time.monotonic() - t0


def _ensure_unloaded(models, max_wait: int) -> None:
    for m in models:
        model_lifecycle.unload_model(m, host=HOST)
    for m in models:
        _wait_released(m, max_wait=max_wait)


def cmd_unload(args) -> int:
    model = args.model
    print(f"[1] 当前驻留模型: {_ps_names() or '无'}")
    print(f"[2] 加载模型 {model}（执行一次短推理）...")
    t0 = time.monotonic()
    _generate(model, args.prompt, args.timeout)
    print(f"    加载+推理耗时: {time.monotonic() - t0:.2f}s")
    print(f"[3] 加载后 ollama ps: {_ps_names()}")

    print(f"[4] 调用 unload_model({model!r}) ...")
    result = model_lifecycle.unload_model(model, host=HOST)
    print(f"    返回: {json.dumps(result, ensure_ascii=False)}")

    ok, released_s = _wait_released(model, max_wait=args.max_wait)
    print(f"[5] 模型完全释放: {'是' if ok else '否（超过等待上限，Ollama 可能在后台稍后释放）'}")
    print(f"    完全释放耗时（自卸载请求发出起）: {released_s:.2f}s")
    print(f"[6] 释放后 ollama ps: {_ps_names() or '无'}")
    return 0 if ok else 1


def _full_cycle(args, with_unload: bool) -> dict:
    """模拟 Vision -> Document -> Vision 完整串行链（每轮前清空驻留）。"""
    vision, doc = args.model_b, args.model_a
    print(f"  模式: {'开启 unload（优化后）' if with_unload else '关闭 unload（优化前/默认驻留）'}")
    print(f"  [准备] 确保 {vision} / {doc} 均已卸载 ...")
    _ensure_unloaded([vision, doc], max_wait=args.max_wait)

    print(f"  [V1] 任务 Vision（{vision}）开始 ...")
    _task_call(vision, args.timeout, kind="vision", vision_image=args.vision_image)
    print(f"       V1 完成")

    if with_unload:
        result = model_lifecycle.unload_model(vision, host=HOST)
        print(f"      已请求卸载 Vision: {result.get('detail')}（{result.get('elapsed_ms')}ms）")
    t_switch = time.monotonic()
    print(f"  [D] 任务 Document（{doc}）开始 ...")
    d_data = _task_call(doc, args.timeout, kind="doc", doc_file=args.doc_file)
    vd_s = time.monotonic() - t_switch
    print(f"       D 完成；Vision结束 -> Document完成: {_fmt_metrics(vd_s, d_data)}")

    if with_unload:
        result = model_lifecycle.unload_model(doc, host=HOST)
        print(f"      已请求卸载 Document: {result.get('detail')}（{result.get('elapsed_ms')}ms）")
    t_switch = time.monotonic()
    print(f"  [V2] 任务 Vision（{vision}）再次开始 ...")
    v_data = _task_call(vision, args.timeout, kind="vision", vision_image=args.vision_image)
    dv_s = time.monotonic() - t_switch
    print(f"       V2 完成；Document结束 -> Vision完成: {_fmt_metrics(dv_s, v_data)}")

    model_lifecycle.unload_model(vision, host=HOST)
    _wait_released(vision, max_wait=args.max_wait)
    return {"vd": vd_s, "dv": dv_s, "total": vd_s + dv_s}


def cmd_switch(args) -> int:
    if args.with_unload:
        results = [("开启 unload", _full_cycle(args, with_unload=True))]
    else:
        results = [
            ("关闭 unload", _full_cycle(args, with_unload=False)),
            ("开启 unload", _full_cycle(args, with_unload=True)),
        ]
    print("\n===== 对比汇总 =====")
    base = None
    for label, d in results:
        print(
            f"{label}: V->D {d['vd']:.1f}s | D->V {d['dv']:.1f}s | 合计 {d['total']:.1f}s"
        )
        if label == "关闭 unload":
            base = d
    if base is not None and len(results) == 2:
        delta = base["total"] - results[1][1]["total"]
        print(f"切换总时延下降: {delta:.2f}s（{delta / base['total'] * 100:.0f}%）" if base["total"] > 0 else "无法计算")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ollama 模型 graceful unload 验证")
    sub = parser.add_subparsers(dest="command", required=True)

    p_unload = sub.add_parser("unload", help="验证单个模型的主动卸载")
    p_unload.add_argument("--model", default=os.getenv("DOC_MODEL", "qwen3:4b"))
    p_unload.add_argument("--prompt", default=DEFAULT_PROMPT)
    p_unload.add_argument("--timeout", type=int, default=300, help="推理请求超时（秒）")
    p_unload.add_argument("--max-wait", type=int, default=30, help="等待完全释放的最大秒数")
    p_unload.set_defaults(func=cmd_unload)

    p_switch = sub.add_parser("switch", help="测量并对比 Vision/Document 串行切换时延")
    p_switch.add_argument("--model-a", default=os.getenv("DOC_MODEL", "qwen3:4b"))
    p_switch.add_argument("--model-b", default=os.getenv("VISION_MODEL", "qwen2.5vl:7b"))
    p_switch.add_argument("--prompt", default=DEFAULT_PROMPT)
    p_switch.add_argument("--timeout", type=int, default=600, help="推理请求超时（秒）")
    p_switch.add_argument("--max-wait", type=int, default=60, help="等待模型完全释放的最大秒数")
    p_switch.add_argument("--vision-image", default=None, help="Vision 任务的测试图片路径（默认用短文本）")
    p_switch.add_argument("--doc-file", default=None, help="Document 任务的测试文档路径（默认用短文本）")
    p_switch.add_argument(
        "--with-unload",
        action="store_true",
        help="只测量开启 unload 的模式（跳过关闭模式）",
    )
    p_switch.set_defaults(func=cmd_switch)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
