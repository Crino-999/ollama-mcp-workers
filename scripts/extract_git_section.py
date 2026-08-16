"""从 git 历史中抽取指定章节的原文（只读操作）。

用法：
  python scripts/extract_git_section.py <commit> <仓库内文件路径> <起始正则> <结束正则> <输出文件>

示例：
  python scripts/extract_git_section.py <commit> "<仓库内文件路径>" "<起始正则>" "<结束正则>" <输出文件>
"""

import os
import re
import subprocess
import sys


def git_show(commit: str, path: str) -> list:
    # 目标仓库路径通过环境变量传入（仅本机使用，避免在公开仓库中硬编码个人路径）
    repo = os.getenv("BENCH_GIT_REPO", "")
    if not repo:
        sys.exit(
            "请设置环境变量 BENCH_GIT_REPO 指向要抽取的 git 仓库绝对路径，"
            "例如：$env:BENCH_GIT_REPO='D:/Projects/xxx'"
        )
    out = subprocess.run(
        ["git", "-C", repo, "show", f"{commit}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        sys.exit(f"git show 失败: {out.stderr[:300]}")
    return out.stdout.splitlines(keepends=True)


def main():
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    commit, path, start_re, end_re, out_file = sys.argv[1:]
    lines = git_show(commit, path)
    start_idx = None
    for i, line in enumerate(lines):
        if re.search(start_re, line):
            start_idx = i
            break
    if start_idx is None:
        sys.exit(f"未找到起始标记 {start_re!r}")
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if re.search(end_re, lines[i]):
            end_idx = i
            break
    section = "".join(lines[start_idx:end_idx])
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(section)
    print(f"已抽取 {commit} 的章节（{len(section)} 字符, {end_idx - start_idx} 行）-> {out_file}")


if __name__ == "__main__":
    main()
