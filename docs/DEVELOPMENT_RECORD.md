# 开发过程记录：从"研究本地模型"到混合分层 AI 工作环境

> 时间跨度：2026-07-24 ~ 2026-08-16（约 14~15 个工作小时）
> 本文档是项目最具价值的资产：完整记录需求缘起、设计演进、关键决策、基准测试、踩坑与方法论。
> 配套文档：[roadmap.md](roadmap.md)（早期路线图）、[doc-worker-benchmark.md](doc-worker-benchmark.md)（选型基准报告）。

---

## 1. 缘起：为什么会有这个项目

### 1.1 背景

- 2026 年以来，各大 AI 编程助手产品普遍涨价，寻找高性价比替代方案成为刚需；
- DeepSeek V4 代码能力满足日常工作、价格极具竞争力，但**没有图像分析能力**；
- 开发者是嵌入式工程师，日常高频任务包括：从电路图提取引脚信息、查阅 datasheet 寄存器表、把 PDF 表格转成代码；
- 机器为 RTX 4050 Laptop，**6GB 显存**（已由 Vision Worker 从显卡配置截图核实：NVIDIA GeForce RTX 4050 Laptop GPU、专用 GPU 内存 6.0GB、DirectX 12），不适合本地部署大模型。

### 1.2 初始设想

最初的想法比较抽象：

> 本地部署多个轻量模型，让云端主脑根据任务能力进行调度。

最终经过多轮"现实校准"，收敛为一套可运行的系统：**云端强模型负责思考，本地模型负责廉价执行，MCP 负责能力接入**。

### 1.3 时间线总览

项目共 10 次提交，跨 7 月底与 8 月中旬两个工作日段：

| 日期 | 提交 | 里程碑 |
|------|------|--------|
| 07-24 23:47 | `26d3a14` | 初始化 Qwen2.5-VL MCP Server |
| 07-25 00:09 | `d7b62a0` | 项目路线图与方法论文档 |
| 07-25 00:40 | `738cc3a` | 补充路线图真实背景与开发流程 |
| 08-16 00:31 | `6bf6355` | Codex 接入视觉 MCP |
| 08-16 02:00 | `2410818` | 添加硬件环境验证素材 |
| 08-16 02:22 | `bad2189` | 大图自动分区识别与 num_ctx 支持 |
| 08-16 04:14 | `1dfafd7` | 免路径截图直达（剪贴板/最新截图/整屏） |
| 08-16 13:57 | `779e271` | Document Worker 与 qwen3:4b 选型基准 |
| 08-16 14:10 | `d36b437` | Document Worker 正式接入 Codex |
| 08-16 15:36 | `fc3dbd8` | 模型生命周期优化（unload_after_task） |

各阶段的详细过程见第 2~9 节。

---

## 2. 第一阶段：Vision MCP 从 0 到 1（2026-07-24 ~ 07-25）

### 2.1 做了什么

在 Trae 环境完成了首个可运行的 Qwen2.5-VL MCP Server（`run.py`），并同步沉淀了六阶段路线图（`docs/roadmap.md`）。

关键提交：

| 提交 | 内容 |
|------|------|
| `26d3a14` | 初始化 Qwen2.5-VL MCP Server（代码、README、.env.example） |
| `d7b62a0` | 项目路线图和方法论文档 |
| `738cc3a` | 补充路线图真实背景与开发流程 |

### 2.2 技术要点（沿用至今的硬性约束）

- 使用 `POST /api/generate` 端点；
- 图片通过 `images: [base64]` 数组传递，**严禁拼进 prompt**；
- `stream=false` 返回完整 JSON，避免污染 MCP 管道；
- 图片统一 RGB 转换（PNG 透明通道会报错）、最长边 1024、JPEG q85、Base64 去前缀；
- Windows 长路径用 `Path.resolve()` 归一化。

### 2.3 踩坑记录（BUG-001 ~ BUG-005）

| 编号 | 问题 | 原因 | 解决 |
|------|------|------|------|
| BUG-001 | `FastMCP` 没有 `serve` 属性 | fastmcp 版本 API 差异 | `mcp.serve()` 改为 `mcp.run()` |
| BUG-002 | Trae 配置报 `command` 必须为字符串 | 配置格式错误 | command 用字符串，参数放 args 数组 |
| BUG-003 | PNG 识别报错 | 透明通道导致模型解析失败 | 统一转 RGB |
| BUG-004 | 长路径图片无法识别 | 路径转义问题 | `Path.resolve()` 转绝对路径 |
| BUG-005 | 大图显存溢出 | 图片尺寸过大 | 最长边 1024 + 5MB 上限 |

### 2.4 确立的项目基调

> "代码只是实现方式，方法论才是真正的价值所在。"

这决定了后续每个阶段都会同步沉淀文档与验证产物。

---

## 3. 第二阶段：迁移到 Codex（2026-08-16 00:31）

**决策：不从头学 MCP 协议**。让 Codex 先检查原工程 → 理解依赖与结构 → 判断可复用部分 → 做 Codex 适配。

配套新增 `scripts/smoke_test.py`：不依赖任何客户端，直接以 MCP stdio 协议验证「MCP → Ollama → Qwen2.5-VL」整条链路。

---

## 4. 第三阶段：大图自动分区识别（02:00 ~ 02:22）

### 4.1 问题

整页图纸 / datasheet 超出单次识别能力：

- 视觉 token 数量受 `num_ctx` 与显存限制，默认 4096 上下文会拒绝大图；
- 即使能读，密集内容在整页尺度下也无法对齐。

### 4.2 方案（`src/vision_tiler.py`）

三级策略：

1. **直接面积阈值**：面积足够小 → 单次直读；
2. **空白投影自然切分**：用行/列墨水密度投影找空白间隙，把图切成自然区域（表格、示意图、多图页面）；
3. **自适应网格 + 重叠**：区域仍超预算时按网格切分，块间重叠保证边界内容不丢失。

每块独立高清识别后，再做一次纯文本综合。`num_ctx` 显式指定（默认 8192），避免默认上下文拒绝大图。

`ROI_GUIDED`（两级模型引导定位）作为实验特性保留。

---

## 5. 第四阶段：免路径截图直达（04:14）

### 5.1 问题

DeepSeek 是纯文本模型，往对话框粘贴截图会提示"当前模型不支持图像输入"；而 `analyze_image` 只接受 `image_path`，每次都要手动保存文件、输入路径。

### 5.2 方案

新增三个工具，让视觉 MCP 自己从系统取图：

- `analyze_clipboard`：直接读剪贴板图片（`Win+Shift+S` 截图的天然搭档）；
- `analyze_latest_screenshot`：扫描系统截图目录，取最新一张；
- `capture_screen`：整屏截取并分析。

三者可按参数互相兜底。日常用法变成一句话："用 vision 分析我的剪贴板截图"。

### 5.3 实测结果

| 工具 | 结果 |
|------|------|
| `analyze_clipboard` | ✅ 正确识别剪贴板截图内容 |
| `analyze_latest_screenshot` | ✅ 自动定位最新截图并识别 |
| `capture_screen` | ⚠️→✅ 沙箱会话内失败（`BitBlt` 返回 0 且 `GetLastError=0`，会话隔离导致屏幕像素不可见）；非沙箱下成功 |

---

## 6. 第五阶段：Provider 调研——一次"验证后放弃"（当天上午）

### 6.1 动机

发现 Codex 不能方便地直接粘贴截图，尝试从 Provider 层解决：研究 `config.toml`、`model_providers`、`model_catalog`、Ollama Provider。

### 6.2 核实结论（官方文档 + 开源源码）

- Codex 同一时刻只启用**一个** `model_provider`；模型目录条目没有 provider 字段，桌面端选择器也没有按模型路由的入口；
- 模型列表只来自 `model_catalog_json`，不会动态查询 Ollama；
- `wire_api` 仅支持 `responses`；Ollama 需 ≥ 0.13.4 才支持 Responses API。

### 6.3 实测验证（用真实报错结束猜测）

把 `model = qwen2.5vl:7b` 配到 DeepSeek provider 下，请求实际被发往 DeepSeek API，返回：

> The supported API model names are deepseek-v4-pro or deepseek-v4-flash, but you passed qwen2.5vl:7b.

（本次发布准备任务中，该报错截图已由 Vision Worker 重新提取出原文，见第 10 节。）

### 6.4 结论

不继续折腾 Codex 内部路由，保留稳定双轨：

- DeepSeek → 主模型（深度推理、代码生成）；
- Qwen-VL → MCP（本地视觉）。

---

## 7. 第六阶段：需求重定义 Coding → Document（13:57 ~ 14:10）

### 7.1 转折

原本设想第二个本地 Worker 是 Coding Agent，调研了 Qwen2.5-Coder、Kimi、豆包 / Seed、Qwen3。最终追问出一个关键问题：

> 真实工作中最重复、最吃 token、最适合卸载的任务到底是什么？

答案不是写代码，而是：需求提取、需求分解、设计初稿、测试用例、文档结构化。

### 7.2 Document Worker 设计（`doc_worker.py`）

- 三个业务工具 + 一个状态查询：`extract_requirements` / `decompose_requirements` / `generate_test_cases` / `get_status`；
- **主代理只传文件路径，Worker 自己读文件**，主代理只收浓缩结果（否则 token 已烧在主上下文，本地化失去意义）；
- 输出强制 JSON schema（Ollama `format=json` + 服务端校验 + 失败重试）；
- 长文档按 markdown 标题分块（3000 字符 + 500 重叠），块结果合并去重、跨块 id 加前缀；
- 温度按任务区分：抽取低（0.2）、生成略高（0.4）。

### 7.3 工具面收敛

正式接入 Codex 时做了一次"减法"：收敛工具参数、产出三层使用指南（`docs/doc-worker-usage.md`），并新增 `scripts/mcp_smoke.py` 验证 Codex 接入链路。

---

## 8. 第七阶段：qwen3:4b 选型基准（13:57 随 Worker 一并落地）

### 8.1 方法（不是"感觉还不错"）

1. **三任务**：A 纯提取（基线）/ B 需求分解 / C 测试用例生成；
2. **三份模拟 PRD**：智能温控器、48V BMS、屏幕截图工具；
3. **真实文档复测**：用作者正在进行的 FOC 学习仓库三份真实文档（只读）验证多分块合并链路；
4. **Git 历史 Ground Truth**：拿过去真实修正过的 SVPWM T0 定义、六扇区查表、SVG 图形、HTML 技术笔记反向构造困难用例；
5. **HTML+SVG 长文重写**：专门验证"技术内容创作"边界。

### 8.2 结果摘要

| 任务 | Schema 合法 | 耗时 / 输出 | 质量评价 |
|------|:---:|:---:|------|
| A 纯提取 | 3/3 | ~14s / ~830 tok | 优秀：无编造，正确区分功能/非功能/验收/待确认 |
| B 需求分解 | 3/3 | ~47s / ~2700 tok | 良好：粒度合理，识别文档歧义；1 处编造约束 |
| C 测试用例 | 3/3 | ~49s / ~2700 tok | 良好：边界/异常覆盖好；个别编造精度数值 |
| 真实长文档（3~4 块） | ✅ | 52~114s / 2559~5939 tok | 跨块合并无丢失、无 id 冲突 |
| HTML+SVG 重写 | ❌ | 多轮失败 | 规划泄漏、长文截断、输出预算耗尽 |

详细报告见 [doc-worker-benchmark.md](doc-worker-benchmark.md)。

### 8.3 关键坑（已解决）

- **qwen3 思考模式与 JSON 冲突**：默认思考时答案进 `thinking`、`response` 为空 → 必须带 `"think": false`；
- **偶发空数组**：基准 9 次出现 3 次，重跑恢复 → 服务器内置"主条目为空则整体重试一次"；
- **16K 上下文挤爆显存**：5.4GB > 6GB，模型被挤到 CPU/GPU 混合明显变慢 → 固定 8K 上下文（3.9GB 纯 GPU）+ 长文档分块。

### 8.4 边界结论

> 4B 是一个不错的**文档加工 Worker**，但不是**技术内容创作者**。

由此确定任务路由边界：

| 任务 | 执行节点 |
|------|----------|
| 简单提取 / 测试用例 | qwen3:4b（L1 全本地） |
| 需求分解 | qwen3:4b 初稿 + DeepSeek 审核（L2） |
| 复杂设计 / 深度推理 | DeepSeek（L3） |
| 原理图 / 截图 | qwen2.5vl:7b |
| PDF / 文件处理 | Codex 原生能力 |

---

## 9. 第八阶段：模型生命周期管理（15:36，收尾）

### 9.1 问题

两个 Worker 都是 Ollama 模型，共用 6GB 显存，串行切换时旧模型要等 keep_alive（默认 5 分钟）过期才会释放，切换时延明显。

### 9.2 方案（`src/model_lifecycle.py`）

- 使用 Ollama 官方文档记载的 graceful unload：`POST /api/generate {"keep_alive": 0}`；
- 先查 `/api/ps` 确认模型已驻留再发卸载请求（避免空 prompt 把模型先加载起来再卸载）；
- best-effort 设计：所有异常只记日志，绝不抛出，不影响任务结果；
- 实测：卸载请求 77~309ms，模型约 0.53s 完全释放。

### 9.3 参数化（`unload_after_task`）

不固定卸载，由主代理按次决定：

- 预计连续调用同一个 Worker → `unload_after_task=false`（保持驻留，避免反复加载）；
- 调用后切换 Worker 或长时间不用 → `unload_after_task=true`（默认值）。

约定写入 `AGENTS.md`，成为项目级使用规范；Ollama 自身 keep_alive 负责"长时间空闲"兜底。

---

## 10. 真实使用记录（2026-08-16）

以仓库公开化前的整理工作为真实任务，两个 Worker 全程参与。

### 10.1 Vision Worker

| 任务输入 | 产出 |
|----------|------|
| 历史报错截图 | 提取 DeepSeek 报错原文："The supported API model names are deepseek-v4-pro or deepseek-v4-flash, but you passed qwen2.5vl:7b."——Provider 路由失败的实证 |
| 电路拓扑图 | 识别出三相测量系统：电流表、U/V/W 电压传感器、1mH 三相电感、1kΩ 电阻等模块 |
| 系统显卡配置截图 | 核实开发环境硬件：RTX 4050 Laptop GPU、专用显存 6.0GB、DirectX 12 |

### 10.2 Document Worker

对仓库公开化整理需求文档（9 条功能需求 + 5 条非功能需求）依次执行：

| 任务 | 结果 |
|------|------|
| `extract_requirements` | 9 条功能需求 + 5 条非功能需求，忠实无编造 |
| `decompose_requirements` | 9 条结构化条目，依赖关系合理；主代理审核通过，无臆造、无过度分解 |
| `generate_test_cases` | 8 条"发布就绪验收用例"（TC-001~008），直接作为整理工作的验收清单使用 |

### 10.3 结论

L1/L2 产出达到"机械初稿 + 主代理终审"的设计预期；两个 Worker 的调用、显存切换（vision 完成后卸载 → document 加载）、工具面与文档承诺一致。

---

## 11. 踩坑总表

| 坑 | 现象 | 原因 | 解决 |
|----|------|------|------|
| fastmcp API 差异 | `serve` 属性不存在 | 版本差异 | 用 `mcp.run()` |
| PNG 透明通道 | 识别报错 | 非 RGB 模式 | 统一转 RGB |
| 大图显存溢出 | OOM / 拒绝 | 尺寸过大 | 压缩 + 分区识别 |
| 大图上下文拒绝 | 默认 4096 不够 | 视觉 token 超限 | 显式 `num_ctx=8192` |
| 沙箱截屏失败 | `BitBlt` 返回 0 | 会话隔离不可见 | 非沙箱运行 / 用剪贴板或截图文件 |
| Provider 路由 | 请求发错 API | Codex 单 provider 架构 | 保留双轨，不做内部路由 |
| qwen3 JSON 空响应 | 答案进 thinking | 思考模式与 format=json 冲突 | `think=false` |
| 偶发空数组 | 主条目为空 | 模型稳定性 | 整体重试一次 |
| 16K 上下文变慢 | CPU/GPU 混合 | 超 6GB 显存 | 8K + 分块 |
| 密钥明文入配置 | 安全风险 | 配置习惯 | 立即轮换 + 改用 env_key / 环境变量 |
| MCP 工具注册但模型不可见 | 应用内 MCP 工具为空，stdio 直连正常 | Deferred 暴露 + 自定义 provider 无 tool_search | `supports_search_tool=false` + 显式 cwd + 完全重启（详见 §15） |

---

## 12. 方法论沉淀

### 12.1 四条原则

1. **原生能力够用，就不要自己造**（PDF 走 Codex 原生，而不是视觉 MCP）；
2. **任务定模型**（云端不是因为强就什么都做，本地不是因为便宜就什么都做）；
3. **验证定边界**（每个模型/能力都要有真实任务验证，记录成功与失败边界）；
4. **文档定规范**（路线图、基准、使用指南、AGENTS.md 同步沉淀）。

### 12.2 协作分工

| 人 | AI（Codex / DeepSeek） |
|----|----|
| 提出问题 | 查代码 |
| 判断价值 | 写代码 |
| 提供真实场景 | 调试 |
| 验收 | 实验与验证 |
| 决定取舍 | 整理 |

### 12.3 Git 作为实验记录系统

本次基准已经无意中证明了这套方法的有效性：Git 提交中的真实修正记录（commit A 初稿 → commit B 人工修正）是模型评测的天然 Ground Truth。后续可演进为 `tests/regression` 数据集，让模型选型与 Prompt 优化拥有自己的真实数据。

### 12.4 做减法清单

| 候选方案 | 验证 | 结论 |
|----------|------|------|
| PDF 走视觉 MCP | Codex 原生可处理 | 放弃 |
| Provider 层多模型路由 | 实测失败 | 放弃 |
| Coding Worker | 真实痛点是文档 | 重定义为 Document Worker |
| qwen3:8b 升级 | 4B 已够用 | 暂不升级 |
| Kimi / 豆包 / Seed | 硬件不适合 | 不部署 |
| 固定卸载 | 连续任务不理想 | 参数化 |
| HTML+SVG 本地生成 | 实测失败 | 留给云端 |

---

## 13. 安全教训

- API 密钥应通过 `env_key` / 环境变量注入，**不写进配置文件**；
- 若发生密钥泄露，应立即吊销并轮换密钥，并从此禁止在文档中记录密钥；
- 公开仓库前必须清理用户名、绝对路径、个人项目样本；从未推送过的仓库可以直接重建 Git 历史，比改写更干净。

---

## 14. 现状与展望

### 14.1 当前系统

```
Codex（编排层）── DeepSeek 主脑
├── 原生能力：PDF / 文件 / Git
├── Vision Worker：qwen2.5vl:7b（6 个工具）
└── Document Worker：qwen3:4b（4 个工具）
    └── 资源调度：unload_after_task 参数化
```

### 14.2 已明确的"暂不做"

- 任务化分块（按标题/需求 ID 切，而不是固定字符数）——等文档规模上来再做；
- 跨分块近似去重（同义不同字）——已知限制，可考虑编辑距离方案；
- qwen3:8b 定向对照——仅当出现"某类文档创作强烈希望本地完成"的明确反馈再拉；
- 新增 OCR / 2B 省显存模型——不追新，先收集真实工作流收益数据。

### 14.3 下一步

把系统放进真实工作流里跑，用"成本账"验证收益。收益的定义（以作者实际诉求为准）：

> 在**同样完成任务**的前提下，本地 Worker 慢一点可以接受（单任务多花几十秒到几分钟），但云端 token 开销大幅下降——**省钱是第一收益，时间是多花的成本**。

度量方式：对同一类任务分别记两笔账——

- 全云端方案：完成该任务的云端 token 费用（换算成金额）；
- 本地方案：本地推理耗时 + 少量云端审核 token + 人工修正时间。

判定标准：当本地方案的云端 token 费用显著低于全云端方案、且总耗时仍在可接受范围内，即判定收益成立。例如文档提取 / 测试用例 100% 走本地（L1），需求分解 80% 本地 + 少量云端审核（L2），复杂创作保留云端（L3）——整体云端 token 账单明显下降。

那才是这 14~15 个小时真正产生的回报：同样的工作，更少的云端费用。

---

## 15. 疑难排查：Codex Desktop 应用内 MCP 工具不可用（2026-08-16 晚）

### 15.1 现象

- 在其他开发任务（不同工作目录）中，Codex Desktop 的 MCP 面板"资源列表"一直为空；
- 云端主代理无法以「应用内 MCP 工具」身份调用 Vision / Document Worker（工具不在模型工具列表里）；
- 但用独立 MCP stdio 客户端（`scripts/mcp_smoke.py`）执行 `initialize` → `tools/list` → `tools/call` 全部正常——**Worker 的 MCP 实现本身没有问题**。

### 15.2 分层定位

按"从 Worker 到主模型"逐层核对（L1~L6）：

| 层 | 环节 | 结论 | 依据 |
|----|------|------|------|
| L1 | Worker 启动 | ✅ | 日志：`vision` / `doc-worker` 均 `Service initialized` |
| L2 | initialize 握手 | ✅ | 协议版本 2025-06-18 |
| L3 | tools/list | ✅ | 日志：`tool_count=10`（视觉 6 + 文档 4 全部返回） |
| L4 | Codex 连接管理器接收 | ✅ | 同一连接管理器的 `tool_count=10` |
| L5 | 注入当前线程工具面 | ❌ | 工具被注册为 Deferred（延迟）暴露，不进前置工具列表 |
| L6 | 主模型可见 | ❌ | 自定义 provider 下 `tool_search` 未下发，延迟工具永远加载不出来 |

日志中反复出现：

```
Failed to list resource templates for MCP server 'vision': Mcp error: -32601: Method not found
```

这是**正常现象**：两个 Worker 只实现了 tools（工具），没有实现 resources（资源）。MCP 中 `resources/list` 与 `tools/list` 是两套独立能力，"资源列表为空"不代表 MCP 不可用。

### 15.3 根因

1. Codex 的 MCP 工具暴露策略（源码 `core/src/mcp_tool_exposure.rs` + `spec_plan.rs`）：
   - `search_tool_enabled = model.supports_search_tool && provider.namespace_tools`；
   - 为真 → 工具 `ToolExposure::Deferred`（需通过 `tool_search` 按名加载）；为假 → `ToolExposure::Direct`（直接进入模型工具列表）。
2. 本机 `models.json` 中 deepseek 两个模型的 `supports_search_tool` 为 `true`，provider 默认支持 namespace tools → MCP 工具走 Deferred 暴露。
3. 但自定义 deepseek provider 下 `tool_search` 并未下发（对应 GitHub issue #31750：custom model_provider → 无 tool_search / 动态工具发现），于是出现"工具已注册、模型永远看不到"。
4. 主模型退而用 `list_mcp_resource_templates` 检查可用性 → 返回空（Worker 本就没有资源）→ 误判"MCP 不可用"，放弃分配。

外部佐证（均为 openai/codex 公开 issue）：

- #34018 / #19425 / #20771 / #38162 / #30343：Windows Desktop / stdio MCP 工具已被 `tools/list` 发现，却不进入 Desktop 线程工具面；
- #31750：自定义 model_provider 无 tool_search / 动态工具发现；
- #14449：未显式设置 cwd 时，本地 stdio MCP 在 Desktop 中不暴露。本机默认工作目录 `~/code` 不存在，正命中该场景。

### 15.4 解决方案

1. **cwd 修复**：新建会话时把工作目录从 `~/code`（目录不存在）改为项目根目录；
2. **强制 Direct 暴露（方案 A）**：将 `~/.codex/models.json` 中 `deepseek-v4-flash` / `deepseek-v4-pro` 的 `supports_search_tool` 改为 `false`。修改前已备份为 `~/.codex/models.json.bak-20260816`；网页搜索走独立机制，不受该开关影响；
3. **完全退出 Codex**（非仅关窗口）→ 重新打开 → 新建会话（旧会话恢复后同样生效）。

未采用的方案 B：在全局 AGENTS.md 增加"不要用 `list_mcp_resources` 判断 MCP 可用性"的指引——当前已可用，暂不做多余工作。

### 15.5 验证结果

- 新会话：应用内 MCP 工具可用；
- 旧会话（本会话恢复后）：工具列表出现全部 10 个 `mcp__vision__*` / `mcp__doc_worker__*`；
- `mcp__vision__get_status`、`mcp__doc_worker__get_status` 均返回 `ok: true`（Ollama 0.32.3 在线）；
- `mcp__doc_worker__extract_requirements` 端到端成功：`tests/fixtures/prd_vision_tool.md` → 12 条功能需求 + 3 条非功能需求，耗时约 18.5s；
- 任务结束后 `ollama ps` 为空：`unload_after_task` 生命周期优化在应用内 MCP 调用路径上同样生效。

### 15.6 经验与后续注意

- MCP 中 resources 与 tools 是两套能力：tool-only 服务器的资源列表为空是**正常现象**，判断可用性应看 `tools/list` 或直接调用 `get_status`；
- 自定义 model_provider + Deferred 暴露是当前版本的已知坑：工具"已注册但模型看不见"。升级 Codex、切换官方后端或修改 `models.json` 后需复查该行为；
- 修改 `~/.codex/models.json` 前先备份；本仓库为公开仓库，文档中涉及本机路径一律用 `~` 或相对表达，不写真实用户名。
