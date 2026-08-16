# ollama-mcp-workers

基于 Ollama 的本地混合分层 MCP Worker 套件：**Vision Worker**（qwen2.5vl:7b）负责图像理解，**Document Worker**（qwen3:4b）负责文档工程任务，由云端主脑（如 DeepSeek）统一调度。

> 核心理念：云端强模型负责思考与终审，本地模型负责廉价批量执行，MCP 负责能力接入。任务落到最合适的执行节点，而不是"什么模型强就全用它"。

本项目不仅是可运行的代码，更是一套完整的工程方法论记录——完整开发过程见 [docs/DEVELOPMENT_RECORD.md](docs/DEVELOPMENT_RECORD.md)。

## 架构

```
Codex（编排层）── DeepSeek 主脑（思考 / 决策 / 终审）
│
├── 原生能力：PDF、文件读写、搜索、Git（不重复造轮子）
│
├── Vision Worker（MCP）── qwen2.5vl:7b（Ollama）
│   ├── analyze_image                单图分析
│   ├── analyze_image_detailed       大图自动分区识别
│   ├── analyze_clipboard            剪贴板直达（免路径）
│   ├── analyze_latest_screenshot    最新截图直达
│   ├── capture_screen               整屏捕获
│   └── get_status                   状态查询
│
├── Document Worker（MCP）── qwen3:4b（Ollama）
│   ├── extract_requirements         需求提取（L1 全本地）
│   ├── generate_test_cases          测试用例生成（L1）
│   ├── decompose_requirements       需求分解（L2：本地生成 + 云端审核）
│   └── get_status                   状态查询
│
└── 资源调度：unload_after_task 参数 + AGENTS.md 约定（共享 6GB 显存）
```

## 特性

- 🖼️ **Vision Worker**：单图分析、大图自动分区识别、剪贴板 / 最新截图 / 整屏直达，无需手动保存文件或输入路径；
- 📄 **Document Worker**：需求提取、需求分解、测试用例生成，输出强制 JSON schema，长文档自动分块合并；
- 🔌 **MCP 标准协议**：跨客户端接入（Codex / Trae / Claude Code / VSCode Copilot / Cursor），配置模板见 [config/](config/README.md)；
- 📁 **数据本地化**：图像与文档分析全部在本地完成，隐私可控；
- ⚙️ **显存生命周期管理**：两个 Worker 共用一块 6GB 显存，`unload_after_task` 参数由主代理按次决定模型驻留 / 卸载；
- 🧪 **可复跑的验证体系**：冒烟测试、基准脚本、fixtures 与基准产物一应俱全，不依赖任何客户端即可验证链路。

## 前置要求

- Windows / Linux / macOS（本机验证环境：Windows + Python 3.12）
- Python 3.10+
- [Ollama](https://ollama.com/)（本机验证版本 0.32.3）
- GPU 建议 ≥ 6GB 显存（两个模型可独立驻留；不满足时 Worker 会自动退化为 CPU 推理，速度明显下降）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 注：`fastmcp==0.1.0` 为本项目验证过的版本（旧版 API：`@mcp.tool()` + `mcp.run()`）。升级 fastmcp 大版本前请先跑冒烟测试确认兼容。

### 2. 拉取模型

```bash
ollama pull qwen2.5vl:7b   # Vision Worker
ollama pull qwen3:4b       # Document Worker
```

### 3. 配置环境变量（可选）

复制 `.env.example` 为 `.env` 并按需修改。默认配置即可在本机运行：

```bash
OLLAMA_HOST=http://127.0.0.1:11434
VISION_MODEL=qwen2.5vl:7b
DOC_MODEL=qwen3:4b
```

### 4. 验证链路（不依赖任何客户端）

```bash
# 查看两个 Worker 的工具列表
python scripts/mcp_client.py --server run.py --list
python scripts/mcp_client.py --server doc_worker.py --list

# 完整冒烟测试（会真实调用本地模型）
python scripts/smoke_test.py                      # 视觉链路
python scripts/mcp_smoke.py                       # 文档链路
```

### 5. 接入客户端

各平台（Codex / Trae / Claude Code / VSCode Copilot / Cursor）的可拷贝配置与实测状态见 [config/README.md](config/README.md)。

以 Codex 为例，把 [config/codex.toml](config/codex.toml) 中的配置追加到 `~/.codex/config.toml`（替换 `<python>` 与 `<项目根目录>` 占位符），重启 Codex，对话中输入 `/mcp` 确认两个服务器已连接。

## 工具一览

### Vision Worker（run.py）

| 工具 | 说明 |
|------|------|
| `analyze_image` | 分析本地图片文件 |
| `analyze_image_detailed` | 大图 / 密集图自动分区识别（空白投影 + 自适应网格 + 重叠） |
| `analyze_clipboard` | 分析剪贴板图片；无图时可按参数回退最新截图或整屏 |
| `analyze_latest_screenshot` | 自动定位并分析最新截图 |
| `capture_screen` | 截取整屏并立即分析 |
| `get_status` | 服务器与 Ollama 状态 |

所有分析工具均接受 `unload_after_task` 参数（默认 `true`）。

### Document Worker（doc_worker.py）

| 工具 | 层级 | 说明 |
|------|:---:|------|
| `extract_requirements` | L1 | 从 PRD 提取功能 / 非功能需求，纯提取不做推测 |
| `generate_test_cases` | L1 | 生成测试用例（正常 / 边界 / 异常），支持按需求定向生成 |
| `decompose_requirements` | L2 | 需求分解为可开发可验收的结构化条目，供云端主脑审核 |
| `get_status` | 诊断 | 模型与 Ollama 状态 |

`output_format` 支持 `json` / `markdown` / `table`。

## 三层使用模式

| 层 | 模式 | 适用 |
|----|------|------|
| L1 | 全本地（不消耗云端 token） | 需求提取、测试用例生成 |
| L2 | 本地生成 + DeepSeek 审核 | 需求分解（审核协议：只返回 issues + required_repairs） |
| L3 | 直接交给云端 | HTML+SVG 长文创作、复杂设计、跨模块推理（4B 已实测不适合） |

使用规则：

1. **传文件路径，不传正文**——Worker 自己读文件，主代理只收浓缩结果；
2. **结果必审**——L1 快速扫一眼，L2 按审核协议执行；
3. 超时预期：单文档单任务 12~77s（8K 上下文纯 GPU），长文档线性变慢；
4. Worker 内置空结果自动重试，仍失败时主代理重试或改全云端。

## 显存调度约定

两个 Worker 共用一块 6GB 显存，切换模型需要先释放再加载。调用工具时按场景传 `unload_after_task`：

- 预计连续调用同一个 Worker：`unload_after_task=false`，保持模型驻留；
- 本次调用后切换 Worker 或长时间不用：`unload_after_task=true`（默认值）。

Ollama 自身 `keep_alive`（默认 5 分钟）负责"长时间空闲"的兜底释放。详见 [AGENTS.md](AGENTS.md)。

## 基准结论（qwen3:4b 首轮）

三份模拟 PRD 的 A（提取）/ B（分解）/ C（测试用例）三任务全部 Schema 合法；真实长文档（3~4 块分块合并）无内容丢失。能力边界明确：

- ✅ 结构化提取、需求分解（需审核）、测试用例生成（最强项）；
- ❌ 长篇 HTML+SVG 技术创作（规划泄漏、长文截断）。

详细数据与方法见 [docs/doc-worker-benchmark.md](docs/doc-worker-benchmark.md)。

## 项目结构

```
├── run.py                    # Vision Worker MCP 入口
├── doc_worker.py             # Document Worker MCP 入口
├── src/
│   ├── vision_tiler.py       # 大图自动分区识别
│   └── model_lifecycle.py    # Ollama 模型优雅卸载
├── config/                   # 各平台 MCP 配置模板（含实测标注）
├── docs/
│   ├── DEVELOPMENT_RECORD.md # 完整开发过程记录（重点）
│   ├── roadmap.md            # 早期路线图与选型过程
│   ├── doc-worker-usage.md   # Document Worker 使用指南
│   └── doc-worker-benchmark.md
├── scripts/                  # 冒烟测试 / 基准 / 通用 MCP 客户端
└── tests/
    ├── fixtures/             # 模拟 PRD 与任务输入
    └── bench_outputs/        # 基准产物（合成数据）
```

## 相关文档

- [开发过程记录](docs/DEVELOPMENT_RECORD.md)——需求缘起、设计演进、关键决策、踩坑与方法论
- [Document Worker 使用指南](docs/doc-worker-usage.md)
- [选型基准报告](docs/doc-worker-benchmark.md)

## 许可证

MIT License
