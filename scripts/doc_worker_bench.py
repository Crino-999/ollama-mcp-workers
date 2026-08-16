#!/usr/bin/env python3
"""
doc_worker_bench.py - qwen3:4b 文档 Worker 基准测试（A/B/C 三任务）

用法：
  python scripts/doc_worker_bench.py                       # 遍历 tests/fixtures/*.md
  python scripts/doc_worker_bench.py --fixture tests/fixtures/prd_xxx.md

每个 fixture 依次执行：
  A_extract  纯提取（基线）
  B_decompose 需求分解
  C_testcase  测试用例生成

输出：
  tests/bench_outputs/<fixture>_<task>.json（含结果、耗时、token 统计、schema 校验）
  终端打印汇总表。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doc_worker import run_task_on_text, read_document, validate_against_schema  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures"
OUT_DIR = ROOT / "tests" / "bench_outputs"
TASKS_TO_RUN = [("A_extract", "extract"), ("B_decompose", "decompose"), ("C_testcase", "testcase")]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", type=Path, default=None, help="指定单个 fixture 文件")
    ap.add_argument("--task", choices=["extract", "decompose", "testcase"], default=None,
                    help="只跑指定任务（默认三个都跑）")
    args = ap.parse_args()

    fixtures = [args.fixture] if args.fixture else sorted(FIXTURES_DIR.glob("*.md"))
    if not fixtures:
        sys.exit(f"未找到 fixture 文件: {FIXTURES_DIR}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    for fx in fixtures:
        text = read_document(str(fx))
        tasks = [(label, key) for label, key in TASKS_TO_RUN if key == args.task] if args.task else TASKS_TO_RUN
        for label, key in tasks:
            ok, errors, merged, stats = False, [], None, {}
            try:
                merged, stats = run_task_on_text(text, key)
                ok, errors = validate_against_schema(merged, key)
            except Exception as exc:  # noqa: BLE001
                errors = [f"{type(exc).__name__}: {exc}"]
            out_file = OUT_DIR / f"{fx.stem}_{label}.json"
            out_file.write_text(
                json.dumps({"ok": ok, "errors": errors, "stats": stats, "result": merged},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            rows.append({
                "fixture": fx.name,
                "task": label,
                "schema_ok": ok,
                "chunks": stats.get("chunks", "?"),
                "secs": round(stats.get("seconds", 0.0), 1),
                "in_tok": stats.get("prompt_tokens", 0),
                "out_tok": stats.get("completion_tokens", 0),
                "errors": "; ".join(errors)[:70],
            })

    print(f"\n{'fixture':<28} {'task':<12} {'schema':<7} {'chunk':<5} {'secs':<7} {'in_tok':<8} {'out_tok':<8} errors")
    print("-" * 100)
    for r in rows:
        print(f"{r['fixture']:<28} {r['task']:<12} {str(r['schema_ok']):<7} {r['chunks']:<5} "
              f"{r['secs']:<7} {r['in_tok']:<8} {r['out_tok']:<8} {r['errors'][:60]}")
    print(f"\n输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
