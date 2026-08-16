# MCP Worker 模型资源调度约定

本地两个 Ollama MCP Worker（Vision: qwen2.5vl:7b；Document: qwen3:4b）共用一块 6GB 显存，切换模型需要先释放再加载。

调用 MCP 工具时按以下策略传 `unload_after_task` 参数：

- 预计还会连续调用同一个 Worker：传 `unload_after_task=false`，保持模型驻留，避免反复加载；
- 本次调用完成后将切换到另一个 Worker，或长时间不再使用：传 `unload_after_task=true`（默认值），让 Ollama 尽快释放模型。

Ollama 自身的 keep_alive（默认 5 分钟）负责"长时间空闲"的兜底释放，无需额外处理。
