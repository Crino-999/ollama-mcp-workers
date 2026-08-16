#!/usr/bin/env python3
"""
Document Worker MCP Server - 本地文档工程 Worker（阶段一 MVP）

复用 run.py 的 FastMCP + Ollama 模式，纯文本模型。
阶段一只提供三个业务工具 + 一个状态查询：
- extract_requirements:   纯提取，不做推测（基线任务 A）
- decompose_requirements: 需求分解为结构化条目（任务 B）
- generate_test_cases:    根据需求/设计生成测试用例（任务 C）

设计原则：
1. 主代理只传 source_path（文件引用），Worker 自己读文件，主代理只收浓缩结果。
2. 输出强制 JSON schema（Ollama format=json + 服务端校验 + 失败重试）。
3. 长文档按 markdown 标题分块处理，块结果合并去重。
4. 温度按任务区分：抽取低、生成略高。
"""

import json
import logging
import os
import re
import time
import functools
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP
from src import model_lifecycle

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("doc-worker")

# ---------------------------------------------------------------------------
# 配置（均可通过环境变量覆盖）
# ---------------------------------------------------------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
DOC_MODEL = os.getenv("DOC_MODEL", "qwen3:4b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "300"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "8192"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "3000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "500"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(5 * 1024 * 1024)))
TEMP_EXTRACT = float(os.getenv("TEMP_EXTRACT", "0.2"))
TEMP_GENERATE = float(os.getenv("TEMP_GENERATE", "0.4"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))

SUPPORTED_EXT = {".md", ".markdown", ".txt", ".rst", ".html", ".htm", ".svg"}

mcp = FastMCP("doc-worker")


def request_model_unload() -> None:
    """统一清理阶段：任务结束后主动请求 Ollama 尽快卸载文档模型（graceful unload）。"""
    try:
        result = model_lifecycle.unload_model(DOC_MODEL, host=OLLAMA_HOST)
        logger.info(
            "模型生命周期: %s -> %s (%dms)",
            DOC_MODEL,
            result.get("detail"),
            result.get("elapsed_ms", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("请求卸载文档模型 %s 失败（不影响任务结果）: %s", DOC_MODEL, exc)


def unload_after_task(func):
    """工具装饰器：读取工具参数 unload_after_task，决定任务结束后是否请求卸载本 Worker 模型。

    unload_after_task=True（默认）：任务无论成功/失败/内部报错，返回前统一请求
    Ollama 尽快卸载模型（适合任务完成、准备切换到另一个 Worker 时）。
    unload_after_task=False：保持模型驻留，交给 Ollama 默认 keep_alive 自动释放
    （适合连续多次调用本 Worker 的场景，避免反复加载）。
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        do_unload = kwargs.pop("unload_after_task", True)
        try:
            return func(*args, **kwargs)
        finally:
            if do_unload:
                request_model_unload()

    return wrapper


# ---------------------------------------------------------------------------
# 任务模板：system prompt + 输出结构 + 条目必填字段
# ---------------------------------------------------------------------------
TASKS = {
    "extract": {
        "system": """你是一名严谨的软件需求工程师，负责从产品需求文档（PRD）中提取功能需求。

规则：
1. 只提取文档中明确陈述的功能需求，禁止推测、补充、合并或改写语义。
2. 每条需求用一句话概括（50 字以内），并标注来源章节。
3. 性能、安全、兼容性、功耗等非功能需求单独归入 non_functional_requirements。
4. 某部分没有需求就输出空数组，不要编造。
5. 输出必须是合法 JSON，结构如下：
{
  "functional_requirements": [
    {"id": "FR-001", "summary": "一句话描述", "source_section": "章节名"}
  ],
  "non_functional_requirements": ["一条非功能需求"]
}""",
        "item_required": ["id", "summary", "source_section"],
    },
    "decompose": {
        "system": """你是一名软件需求工程师，负责把 PRD 中的需求分解为可开发、可验收的结构化需求条目。

规则：
1. 每个条目必须能在工程上独立实现和验收，粒度要小（一个功能点一条）。
2. 只能基于文档内容；缺失的信息标注 "TBD"，禁止编造。
3. 前置条件、输入、输出、约束按条目逐项列出，没有就写空数组。
4. 需求之间的依赖关系用依赖条目的 id 表示。
5. 对文档中含糊、冲突或缺失的信息，记录到 open_issues。
6. 输出必须是合法 JSON，结构如下：
{
  "requirements": [
    {
      "id": "REQ-001",
      "title": "短标题",
      "description": "一句话描述",
      "preconditions": ["前置条件"],
      "inputs": ["输入"],
      "outputs": ["输出"],
      "constraints": ["约束，如精度、时限"],
      "dependencies": ["依赖的条目 id"],
      "verification_method": "验收方式，如单元测试/评审/实测"
    }
  ],
  "open_issues": ["文档中含糊或缺失、需要人工确认的问题"]
}""",
        "item_required": [
            "id", "title", "description", "preconditions", "inputs",
            "outputs", "constraints", "dependencies", "verification_method",
        ],
    },
    "testcase": {
        "system": """你是一名测试设计工程师，根据需求/设计文档生成测试用例。

规则：
1. 覆盖正常路径、边界条件和异常/错误路径，优先做边界值分析。
2. 每个用例必须可执行：前置条件、操作步骤、预期结果明确。
3. 只能基于文档内容设计；无法确定的输入用 [TBD] 标注，不要编造。
4. 优先级：P0（阻塞发布）、P1（主要功能）、P2（一般/体验）。
5. category 只能是 normal / boundary / error 之一。
6. 对文档中无法覆盖到的点记录到 coverage_gaps。
7. 输出必须是合法 JSON，结构如下：
{
  "test_cases": [
    {
      "id": "TC-001",
      "title": "用例标题",
      "requirement_ref": "关联的需求 id 或章节",
      "priority": "P1",
      "category": "normal",
      "preconditions": ["前置条件"],
      "steps": ["操作步骤"],
      "expected": "预期结果"
    }
  ],
  "coverage_gaps": ["无法覆盖/需要补充信息的点"]
}""",
        "item_required": [
            "id", "title", "requirement_ref", "priority", "category",
            "preconditions", "steps", "expected",
        ],
    },
    "rewrite_note": {
        "system": """你是一名嵌入式学习笔记编辑，擅长把技术内容重写为规范的 HTML 学习章节（中文）。

输出规范：
1. 输出完整 HTML 片段：以 <h3> 或 <h4> 标题开头，正文用 <p>、<ul>、<ol>、<table> 组织。
2. 关键术语加粗 <b>，公式上下标用 <sub>/<sup>，数学符号用 Unicode（α β θ 等）。
3. 示意图必须用内联 SVG：<svg viewBox="0 0 W H" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, Microsoft YaHei, sans-serif">；
   网格线 stroke="#ccc"、主结构线 stroke="#0066cc"、文字 fill="#333"，图注放 <p class="fig-caption">。
4. 表格必须含表头行；所有数值、矢量顺序、公式、定义必须与输入严格核对，禁止编造。
5. 只输出最终 HTML 代码本身，不要 Markdown 代码围栏，不要计划、草稿、思考过程或任何解释文字。""",
    },
    "svg_diagram": {
        "system": """你是一名嵌入式硬件笔记绘图员，根据文字描述绘制内联 SVG 框图。

要求：
1. 只输出一个 <svg> 元素（可含 <defs>），不要 HTML 外壳、不要 Markdown 代码围栏、不要解释文字、不要任何计划或草稿——直接给出最终图。
2. 方框用 <rect>（fill="#e8f4f8" stroke="#2980b9" rx="6"），连线用 <line>，文字用 <text>（text-anchor="middle" 居中，字号 10-14）。
3. 信号流向用带箭头的线（可用 marker 或 <polygon> 画箭头）。
4. 框图必须覆盖描述中的全部模块与连接关系，标注信号名与引脚号，禁止遗漏或编造描述外的模块。
5. 只输出最终 <svg> 本身，不要计划、草稿、思考过程或任何解释文字。
6. viewBox 尺寸根据内容自定（建议 800x480 左右）。
7. 结构示例（仅示意骨架，内容必须按你的描述重画）：
<svg viewBox="0 0 800 480" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, Microsoft YaHei, sans-serif">
  <rect x="30" y="30" width="200" height="120" rx="6" fill="#e8f4f8" stroke="#2980b9"/>
  <text x="130" y="90" font-size="14" text-anchor="middle" font-weight="bold">模块A</text>
  <line x1="230" y1="90" x2="350" y2="90" stroke="#555" stroke-width="1.5"/>
  <polygon points="350,90 338,84 338,96" fill="#555"/>
  <rect x="360" y="30" width="200" height="120" rx="6" fill="#e8f4f8" stroke="#2980b9"/>
  <text x="460" y="90" font-size="14" text-anchor="middle" font-weight="bold">模块B</text>
</svg>""",
    },
}

MERGERS = {}


# ---------------------------------------------------------------------------
# 文件读取
# ---------------------------------------------------------------------------
def read_document(source_path: str) -> str:
    """读取本地文本/Markdown 文件，UTF-8 失败时回退 GB18030。"""
    p = Path(source_path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    if not p.is_file():
        raise ValueError(f"文件不存在: {p}")
    if p.suffix.lower() not in SUPPORTED_EXT:
        raise ValueError(f"不支持的文件类型 {p.suffix}，仅支持 {sorted(SUPPORTED_EXT)}")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"文件过大 ({p.stat().st_size} bytes)，超过上限 {MAX_FILE_BYTES}")
    for enc in ("utf-8", "gb18030"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return p.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 分块：按 markdown 标题切分，带重叠
# ---------------------------------------------------------------------------
def split_markdown(text: str, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    lines = text.splitlines(keepends=True)
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        is_heading = bool(re.match(r"^#{1,4}\s", line))
        if current and current_len + len(line) > max_chars:
            if is_heading:
                chunks.append("".join(current))
                current = [line]
                current_len = len(line)
            else:
                joined = "".join(current)
                chunks.append(joined)
                tail = joined[-overlap:] if len(joined) > overlap else joined
                current = [tail, line]
                current_len = len(tail) + len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


# ---------------------------------------------------------------------------
# Ollama 调用
# ---------------------------------------------------------------------------
def ollama_generate(system: str, prompt: str, temperature: float, json_mode: bool = True, think: Optional[bool] = None):
    """调用 Ollama /api/generate；json_mode=True 时强制 format=json 并校验，False 时返回原始文本。

    think 参数：本模型上思考模式有两大问题——JSON 任务答案会进 thinking 导致 response 为空；
    文本长任务思考可能耗尽输出预算后 response 仍为空。因此统一默认关闭思考。
    """
    payload = {
        "model": DOC_MODEL,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "temperature": temperature,
        "options": {
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
    }
    if json_mode:
        payload["format"] = "json"
        payload["think"] = False
    else:
        payload["think"] = False if think is None else think
    last_err = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            resp = requests.post(f"{OLLAMA_HOST}/api/generate", json=payload, timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "").strip()
            if not text:
                detail = (data.get("thinking") or "")[:120]
                raise ValueError(f"response 为空（thinking 字段: {detail!r}）")
            if json_mode:
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
                obj = json.loads(text)
                return obj, data
            # 文本模式：若模型仍包了代码围栏则去掉
            stripped = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            if stripped != text:
                text = re.sub(r"\s*```$", "", stripped)
            return text, data
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("Ollama 调用/解析失败(第 %d/%d 次): %s", attempt + 1, MAX_RETRIES + 1, exc)
            time.sleep(1.5)
    raise RuntimeError(f"模型调用失败: {last_err}")


def _task_temp(task_key: str) -> float:
    return TEMP_EXTRACT if task_key == "extract" else TEMP_GENERATE


# ---------------------------------------------------------------------------
# 合并与校验
# ---------------------------------------------------------------------------
def _dedupe(items, key):
    seen = set()
    out = []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def merge_extract(parts):
    fr, nfr = [], []
    for p in parts:
        fr += p.get("functional_requirements", []) or []
        nfr += p.get("non_functional_requirements", []) or []
    fr = _dedupe(fr, lambda x: (x.get("summary", ""), x.get("source_section", "")))
    nfr = _dedupe(nfr, lambda x: x)
    for i, item in enumerate(fr, 1):
        item["id"] = f"FR-{i:03d}"
    return {"functional_requirements": fr, "non_functional_requirements": nfr}


def merge_decompose(parts):
    reqs, issues = [], []
    for p in parts:
        reqs += p.get("requirements", []) or []
        issues += p.get("open_issues", []) or []
    reqs = _dedupe(reqs, lambda x: (x.get("title", ""), x.get("description", "")))
    issues = _dedupe(issues, lambda x: x)
    if len(parts) > 1:
        # 多分块时给每个块的 id 加块前缀，避免跨块重号；跨块依赖无法保留时记录到 open_issues
        valid_ids = set()
        renamed = []
        for ci, p in enumerate(parts, 1):
            for item in p.get("requirements", []) or []:
                old = item.get("id", "")
                new_id = f"C{ci}-{old}" if old else f"C{ci}-REQ"
                item["id"] = new_id
                valid_ids.add(new_id)
                renamed.append(item)
        for item in renamed:
            deps = [d for d in item.get("dependencies", []) if d in valid_ids]
            item["dependencies"] = deps
        reqs = _dedupe(renamed, lambda x: (x.get("title", ""), x.get("description", "")))
    else:
        for i, item in enumerate(reqs, 1):
            item["id"] = item.get("id") or f"REQ-{i:03d}"
    return {"requirements": reqs, "open_issues": issues}


def merge_testcase(parts):
    cases, gaps = [], []
    for p in parts:
        cases += p.get("test_cases", []) or []
        gaps += p.get("coverage_gaps", []) or []
    cases = _dedupe(cases, lambda x: (x.get("title", ""), x.get("expected", "")))
    gaps = _dedupe(gaps, lambda x: x)
    if len(parts) > 1:
        renamed = []
        for ci, p in enumerate(parts, 1):
            for item in p.get("test_cases", []) or []:
                old = item.get("id", "")
                item["id"] = f"C{ci}-{old}" if old else f"C{ci}-TC"
                renamed.append(item)
        cases = _dedupe(renamed, lambda x: (x.get("title", ""), x.get("expected", "")))
    else:
        for i, item in enumerate(cases, 1):
            item["id"] = item.get("id") or f"TC-{i:03d}"
    return {"test_cases": cases, "coverage_gaps": gaps}


MERGERS["extract"] = merge_extract
MERGERS["decompose"] = merge_decompose
MERGERS["testcase"] = merge_testcase


def validate_against_schema(obj, task_key):
    """轻量结构校验：顶层字段 + 条目必填字段。返回 (ok, errors)。"""
    errors = []
    arrays = {
        "extract": ["functional_requirements", "non_functional_requirements"],
        "decompose": ["requirements", "open_issues"],
        "testcase": ["test_cases", "coverage_gaps"],
    }[task_key]
    for field in arrays:
        if not isinstance(obj.get(field), list):
            errors.append(f"顶层字段 {field} 缺失或不是数组")
            return False, errors
    items_field = {
        "extract": "functional_requirements",
        "decompose": "requirements",
        "testcase": "test_cases",
    }[task_key]
    for i, item in enumerate(obj.get(items_field, [])):
        for f in TASKS[task_key]["item_required"]:
            if f not in item:
                errors.append(f"{items_field}[{i}] 缺少字段: {f}")
                return False, errors
    return True, errors


# ---------------------------------------------------------------------------
# 任务执行入口（供 MCP 工具与基准脚本共用）
# ---------------------------------------------------------------------------
def run_task_on_text(text: str, task_key: str, extra_instructions: Optional[str] = None):
    """执行任务；主条目为空时整体重试一次（qwen3 偶发返回空数组）。"""
    main_fields = {
        "extract": "functional_requirements",
        "decompose": "requirements",
        "testcase": "test_cases",
    }[task_key]

    for _ in range(2):
        chunks = split_markdown(text)
        parts = []
        stats = {"chunks": len(chunks), "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}
        t0 = time.time()
        for i, chunk in enumerate(chunks, 1):
            prompt = f"以下是文档的第 {i}/{len(chunks)} 部分：\n\n{chunk}"
            if extra_instructions:
                prompt += f"\n\n附加要求：{extra_instructions}"
            obj, meta = ollama_generate(TASKS[task_key]["system"], prompt, _task_temp(task_key))
            parts.append(obj)
            stats["prompt_tokens"] += int(meta.get("prompt_eval_count", 0) or 0)
            stats["completion_tokens"] += int(meta.get("eval_count", 0) or 0)
        merged = MERGERS[task_key](parts)
        stats["seconds"] = time.time() - t0
        if merged.get(main_fields):
            return merged, stats
        logger.warning("任务 %s 返回空结果（主条目为空），重试一次", task_key)
    raise RuntimeError(f"任务 {task_key} 连续两次返回空结果")


def run_text_task(text: str, task_key: str, extra_instructions: Optional[str] = None):
    """文本生成任务（HTML 重写 / SVG 绘图）：单次生成，不做 JSON 合并。"""
    max_len = CHUNK_CHARS * 3
    if len(text) > max_len:
        logger.warning("输入过长（%d 字符），已截断至 %d", len(text), max_len)
        text = text[:max_len]
    prompt = f"以下是待处理的原始内容：\n\n{text}"
    if extra_instructions:
        prompt += f"\n\n附加要求：{extra_instructions}"
    t0 = time.time()
    raw, meta = ollama_generate(TASKS[task_key]["system"], prompt, TEMP_GENERATE, json_mode=False, think=False)
    stats = {
        "chunks": 1,
        "prompt_tokens": int(meta.get("prompt_eval_count", 0) or 0),
        "completion_tokens": int(meta.get("eval_count", 0) or 0),
        "seconds": time.time() - t0,
    }
    return raw.strip(), stats


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------
def _fmt_bool(value) -> str:
    return "是" if value else "否"


def format_output(obj, task_key: str, output_format: str) -> str:
    output_format = (output_format or "json").lower()
    if output_format == "json":
        return json.dumps(obj, ensure_ascii=False, indent=2)
    if output_format == "markdown":
        lines = []
        if task_key == "extract":
            lines.append("## 功能需求")
            for it in obj["functional_requirements"]:
                lines.append(f"- **{it['id']}** {it['summary']}（来源：{it['source_section']}）")
            lines.append("")
            lines.append("## 非功能需求")
            for it in obj["non_functional_requirements"]:
                lines.append(f"- {it}")
        elif task_key == "decompose":
            for it in obj["requirements"]:
                lines.append(f"### {it['id']} {it['title']}")
                lines.append(f"- 描述：{it['description']}")
                lines.append(f"- 前置条件：{', '.join(it['preconditions']) if it['preconditions'] else '无'}")
                lines.append(f"- 输入：{', '.join(it['inputs']) if it['inputs'] else '无'}")
                lines.append(f"- 输出：{', '.join(it['outputs']) if it['outputs'] else '无'}")
                lines.append(f"- 约束：{', '.join(it['constraints']) if it['constraints'] else '无'}")
                lines.append(f"- 依赖：{', '.join(it['dependencies']) if it['dependencies'] else '无'}")
                lines.append(f"- 验收方式：{it['verification_method']}")
                lines.append("")
            if obj["open_issues"]:
                lines.append("## 待确认问题")
                for x in obj["open_issues"]:
                    lines.append(f"- {x}")
        elif task_key == "testcase":
            for it in obj["test_cases"]:
                lines.append(f"### {it['id']} {it['title']}（{it['priority']} / {it['category']}）")
                lines.append(f"- 关联需求：{it['requirement_ref']}")
                lines.append(f"- 前置条件：{', '.join(it['preconditions']) if it['preconditions'] else '无'}")
                for n, step in enumerate(it["steps"], 1):
                    lines.append(f"- 步骤{n}：{step}")
                lines.append(f"- 预期：{it['expected']}")
                lines.append("")
            if obj["coverage_gaps"]:
                lines.append("## 覆盖缺口")
                for x in obj["coverage_gaps"]:
                    lines.append(f"- {x}")
        return "\n".join(lines).strip()
    if output_format == "table":
        header, rows = None, []
        if task_key == "extract":
            header = "| ID | 需求 | 来源 |"
            rows = [f"| {it['id']} | {it['summary']} | {it['source_section']} |" for it in obj["functional_requirements"]]
        elif task_key == "decompose":
            header = "| ID | 标题 | 约束 | 验收方式 |"
            rows = [
                f"| {it['id']} | {it['title']} | "
                f"{'; '.join(it['constraints']) if it['constraints'] else '-'} | "
                f"{it['verification_method']} |"
                for it in obj["requirements"]
            ]
        elif task_key == "testcase":
            header = "| ID | 用例 | 优先级 | 类型 | 需求 | 预期 |"
            rows = [
                f"| {it['id']} | {it['title']} | {it['priority']} | {it['category']} | "
                f"{it['requirement_ref']} | {it['expected']} |"
                for it in obj["test_cases"]
            ]
        sep = "|" + "---|" * (header.count("|") - 1)
        return "\n".join([header, sep] + rows)
    raise ValueError(f"output_format 仅支持 json / markdown / table，收到: {output_format}")


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------
@mcp.tool()
def get_status() -> str:
    """查询 Document Worker 状态（模型、Ollama 连通性）。"""
    info = {"ok": False, "doc_model": DOC_MODEL, "ollama_host": OLLAMA_HOST}
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/version", timeout=10)
        r.raise_for_status()
        info["ollama_version"] = r.json().get("version")
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
@unload_after_task
def extract_requirements(source_path: str, unload_after_task: bool = True) -> str:
    """从 Markdown PRD 中提取功能需求与非功能需求（纯提取，不做推测）。参数 source_path 为本地文件路径；unload_after_task=True（默认）=任务结束后立即请求 Ollama 卸载模型，False=保持驻留交给 keep_alive 自动释放。"""
    merged, _ = run_task_on_text(read_document(source_path), "extract")
    return json.dumps(merged, ensure_ascii=False, indent=2)


@mcp.tool()
@unload_after_task
def decompose_requirements(source_path: str, output_format: str = "json", extra_instructions: Optional[str] = None, unload_after_task: bool = True) -> str:
    """将 PRD 需求分解为可开发可验收的结构化需求条目。output_format: json / markdown / table；unload_after_task=True（默认）=任务结束后立即请求 Ollama 卸载模型，False=保持驻留交给 keep_alive 自动释放。"""
    merged, _ = run_task_on_text(read_document(source_path), "decompose", extra_instructions)
    return format_output(merged, "decompose", output_format)


@mcp.tool()
@unload_after_task
def generate_test_cases(source_path: str, requirement_id: Optional[str] = None, output_format: str = "json", extra_instructions: Optional[str] = None, unload_after_task: bool = True) -> str:
    """根据需求/设计文档生成测试用例（覆盖正常/边界/异常）。requirement_id 可选：只针对指定需求条目。output_format: json / markdown / table；unload_after_task=True（默认）=任务结束后立即请求 Ollama 卸载模型，False=保持驻留交给 keep_alive 自动释放。"""
    extra = extra_instructions or ""
    if requirement_id:
        extra = f"只针对需求条目 {requirement_id} 生成测试用例，其余需求不要生成。" + extra
    merged, _ = run_task_on_text(read_document(source_path), "testcase", extra or None)
    return format_output(merged, "testcase", output_format)

if __name__ == "__main__":
    mcp.run()
