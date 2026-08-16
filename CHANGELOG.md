# Changelog

本项目尚未发布正式版本，以下按开发里程碑记录。

## 0.3.0 — 2026-08-16

混合分层架构收尾：

- 新增 `src/model_lifecycle.py`：Ollama 模型优雅卸载（`keep_alive=0` + `/api/ps` 预检 + best-effort）；
- Vision / Document Worker 全部工具参数化 `unload_after_task`，由主代理按次决定模型驻留 / 卸载；
- 新增 `scripts/verify_model_unload.py`：卸载与串行切换时延对比验证（实测卸载请求 77~309ms、约 0.53s 完全释放）；
- 新增 `AGENTS.md`：双 Worker 显存调度约定。

## 0.2.1 — 2026-08-16

Document Worker 正式接入 Codex：

- 工具面收敛（参数简化、净减代码）；
- 新增三层使用指南 `docs/doc-worker-usage.md`（L1 全本地 / L2 本地生成+云端审核 / L3 云端）；
- 新增 `scripts/mcp_smoke.py`：以 MCP stdio 客户端身份验证 Codex 接入链路。

## 0.2.0 — 2026-08-16

Document Worker 与 qwen3:4b 选型基准：

- 新增 `doc_worker.py`：需求提取 / 需求分解 / 测试用例生成，强制 JSON schema + 分块合并 + 失败重试；
- 新增 A/B/C 三任务基准：3 份模拟 PRD + 真实文档复测 + HTML/SVG 重写边界测试；
- 新增 `scripts/doc_worker_bench.py`、`review_bench_results.py`、`rewrite_eval.py`、`extract_git_section.py`；
- 结论：结构化文档任务适合 4B，长篇 HTML+SVG 创作不适合（能力边界已记录）。

## 0.1.3 — 2026-08-16

免路径截图直达：

- 新增 `analyze_clipboard` / `analyze_latest_screenshot` / `capture_screen` 三个工具；
- 剪贴板 / 最新截图 / 整屏三级兜底，日常使用无需保存文件、无需输入路径。

## 0.1.2 — 2026-08-16

大图自动分区识别：

- 新增 `src/vision_tiler.py`：空白投影 + 自适应网格 + 重叠分块，逐块高清识别后综合；
- `num_ctx` 显式指定（默认 8192），避免大图视觉 token 超过默认上下文被拒绝。

## 0.1.1 — 2026-08-16

Codex 接入：

- Vision MCP 从 Trae 迁移适配到 Codex；
- 新增 `scripts/smoke_test.py`：不依赖客户端验证「MCP → Ollama → Qwen2.5-VL」全链路；
- 新增硬件环境验证素材（开发板手册与配置截图，已归档至本地私有目录，不随仓库分发）。

## 0.1.0 — 2026-07-24 ~ 07-25

初始版本：

- 初始化 Qwen2.5-VL MCP Server（`run.py`）：单图分析 + 状态查询；
- 完整路线图与方法论文档（`docs/roadmap.md`）；
- 技术要点：`/api/generate` + `images` 数组传图、`stream=false`、RGB 转换、JPEG 压缩。
