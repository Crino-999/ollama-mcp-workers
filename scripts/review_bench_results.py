"""人工评审基准输出：python scripts/review_bench_results.py [extract|decompose|testcase|all]"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "bench_outputs"


def show_extract(name):
    d = json.loads((OUT / f"{name}_A_extract.json").read_text(encoding="utf-8"))
    r = d["result"]
    print(f"===== {name} A_extract =====")
    for it in r["functional_requirements"]:
        print(f"  {it['id']} [{it['source_section']}] {it['summary']}")
    print("  -- 非功能 --")
    for it in r["non_functional_requirements"]:
        print(f"  - {it}")
    print()


def show_decompose(name):
    d = json.loads((OUT / f"{name}_B_decompose.json").read_text(encoding="utf-8"))
    r = d["result"]
    print(f"===== {name} B_decompose =====")
    for it in r["requirements"]:
        print(f"  {it['id']} {it['title']} | 约束: {'; '.join(it['constraints']) or '-'} | 验收: {it['verification_method']}")
    print("  -- 待确认 --")
    for it in r["open_issues"]:
        print(f"  - {it}")
    print()


def show_testcase(name):
    d = json.loads((OUT / f"{name}_C_testcase.json").read_text(encoding="utf-8"))
    r = d["result"]
    print(f"===== {name} C_testcase =====")
    cats = {}
    for it in r["test_cases"]:
        cats[it["category"]] = cats.get(it["category"], 0) + 1
    print(f"  用例数: {len(r['test_cases'])}  分类: {cats}")
    for it in r["test_cases"][:14]:
        steps = " -> ".join(it["steps"][:3])
        print(f"  {it['id']} [{it['priority']}/{it['category']}] {it['title']} (需求:{it['requirement_ref']})")
        print(f"     前置: {'; '.join(it['preconditions']) or '-'}")
        print(f"     步骤: {steps}")
        print(f"     预期: {it['expected'][:80]}")
    print("  -- 覆盖缺口 --")
    for it in r["coverage_gaps"]:
        print(f"  - {it}")
    print()


if __name__ == "__main__":
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    if len(sys.argv) > 1:
        fixtures = [Path(sys.argv[1]).stem]
    else:
        fixtures = ["prd_bms", "prd_smart_thermostat", "prd_vision_tool"]
    for fx in fixtures:
        if which in ("all", "extract"):
            show_extract(fx)
        if which in ("all", "decompose"):
            show_decompose(fx)
        if which in ("all", "testcase"):
            show_testcase(fx)
