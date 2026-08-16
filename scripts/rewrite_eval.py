"""HTML 重写 / SVG 绘图评测：调用本地 Worker 生成并做基础检查。

用法：
  python scripts/rewrite_eval.py --input <文件> --task rewrite_note --instructions <文件> --out <输出.html>
  python scripts/rewrite_eval.py --input <文件> --task svg_diagram --instructions <文件> --out <输出.svg>
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doc_worker import run_text_task, read_document  # noqa: E402


def check_rewrite_note(text: str) -> list:
    issues = []
    topics = ["伏秒平衡", "扇区判断", "作用时间", "七段式", "过调制", "CCR", "五段式", "马鞍波"]
    for t in topics:
        if t not in text:
            issues.append(f"缺少子主题: {t}")
    if not re.search(r"T[₀0]\s*[=＝]\s*T[Ss]\s*(?:−|-|–)\s*T[Xx]", text) and "T0 = Ts" not in text:
        issues.append("T0 定义缺失（期望 T0 = Ts − Tx − Ty 形式）")
    if "<table" not in text:
        issues.append("缺少表格（期望六扇区查表）")
    svg_open = len(re.findall(r"<svg", text))
    svg_close = len(re.findall(r"</svg>", text))
    if svg_open == 0:
        issues.append("没有 SVG 图")
    if svg_open != svg_close:
        issues.append(f"SVG 标签不平衡: <svg>{svg_open} vs </svg>{svg_close}")
    for tag in ("rect", "line", "text", "polygon"):
        if not re.search(rf"<{tag}[ >/]", text):
            issues.append(f"SVG 缺少基本元素 <{tag}>")
    return issues


def check_svg(text: str) -> list:
    issues = []
    if not text.strip().startswith("<svg"):
        issues.append("输出不以 <svg 开头")
    if not text.rstrip().endswith("</svg>"):
        issues.append("输出不以 </svg> 结尾")
    for tag in ("rect", "text", "line"):
        if not re.search(rf"<{tag}[ >/]", text):
            issues.append(f"SVG 缺少基本元素 <{tag}>")
    blocks = len(re.findall(r"<rect", text))
    if blocks < 3:
        issues.append(f"方框过少（rect={blocks}，期望至少 3 个模块）")
    if "arrow" not in text and "<marker" not in text and "polygon" not in text:
        issues.append("未检测到箭头（marker/polygon）")
    return issues


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--task", choices=["rewrite_note", "svg_diagram"], required=True)
    ap.add_argument("--instructions", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    src = read_document(str(args.input))
    extra = read_document(str(args.instructions)) if args.instructions else None
    raw, stats = run_text_task(src, args.task, extra)
    args.out.write_text(raw, encoding="utf-8")

    check = check_rewrite_note if args.task == "rewrite_note" else check_svg
    issues = check(raw)
    print(f"task={args.task} chars={len(raw)} svg_count={raw.count('<svg')} secs={stats['seconds']:.1f} "
          f"in_tok={stats['prompt_tokens']} out_tok={stats['completion_tokens']}")
    print(f"输出已保存: {args.out}")
    if issues:
        print("检查未通过:")
        for x in issues:
            print(f"  - {x}")
    else:
        print("基础检查全部通过")


if __name__ == "__main__":
    main()
