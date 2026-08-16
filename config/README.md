# 各平台 MCP 配置模板

本目录提供可直接拷贝的 MCP 配置，用于把 Vision / Document 两个本地 Worker 接入不同 AI 客户端。

## 兼容性矩阵

| 平台 | 配置文件 | 实测状态 | 放置位置 |
|------|----------|:--------:|----------|
| Codex（桌面端 / CLI） | [codex.toml](codex.toml) | ✅ 已实测（本机） | 追加到 `~/.codex/config.toml` 后重启 Codex |
| Trae IDE | [trae.json](trae.json) | ✅ 已实测（本机） | 项目根 `mcp.json`，或 Trae MCP 设置导入 |
| Claude Code | [claude-code.json](claude-code.json) | ⚠️ 未实测 | 项目根 `.mcp.json`，或 `claude mcp add` |
| VSCode Copilot | [vscode-copilot.json](vscode-copilot.json) | ⚠️ 未实测 | `.vscode/mcp.json` |
| Cursor | [cursor.json](cursor.json) | ⚠️ 未实测 | `.cursor/mcp.json` |

> 实测状态说明：✅ 表示在作者本机（Windows + Ollama 0.32.3）验证过完整调用链路；⚠️ 表示配置格式按官方文档编写，但作者未在对应客户端上实际验证，使用前请自行确认。

## 使用前必读

1. **替换占位符**：所有模板使用 `<python>` 与 `<项目根目录>` 占位符，替换为你的实际 Python 绝对路径（示例：`C:\Python312\python.exe`）与仓库绝对路径。
2. **模型准备**：先安装依赖并拉取模型：

   ```bash
   pip install -r requirements.txt
   ollama pull qwen2.5vl:7b
   ollama pull qwen3:4b
   ```

3. **显存调度**：两个 Worker 共用一块 6GB 显存。调用工具时按需传 `unload_after_task`：
   - 连续调用同一个 Worker → `unload_after_task=false`（保持驻留）；
   - 切换 Worker 或长时间不用 → `unload_after_task=true`（默认，任务结束即卸载）。
4. **验证链路**：不依赖任何客户端，直接运行：

   ```bash
   python scripts/mcp_client.py --server run.py --list
   python scripts/mcp_client.py --server doc_worker.py --list
   ```

   更完整的冒烟测试见 `scripts/smoke_test.py`（视觉）与 `scripts/mcp_smoke.py`（文档）。

## 平台差异提示

- **JSON 不支持注释**：trae/claude-code/vscode-copilot/cursor 的模板均为合法 JSON；`env` 中所有值必须为字符串。
- **Claude Code**：除 `.mcp.json` 外，也可以用命令注册（需在项目目录执行）：

  ```bash
  claude mcp add --scope project vision -- python run.py
  claude mcp add --scope project doc-worker -- python doc_worker.py
  ```

  环境变量通过 `--env KEY=VALUE` 传入，或直接使用根目录 `.env`（Worker 启动时会自动加载）。
