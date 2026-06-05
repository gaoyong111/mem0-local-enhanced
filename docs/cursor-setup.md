# Cursor 集成指南

## 1. MCP server 注册

编辑 `~/.cursor/mcp.json`（建议用 pyenv 绝对路径）：

```json
{
  "mcpServers": {
    "mem0-local": {
      "command": "/Users/YOUR_USER/.pyenv/shims/python3",
      "args": ["/Users/YOUR_USER/.mem0/mcp_server_local.py"]
    }
  }
}
```

项目级：在项目根目录创建 `.cursor/mcp.json`，仅该项目生效。

**前置条件**：Ollama 必须运行（`ollama serve` 或 Ollama.app），否则 MCP 启动失败。

## 2. Hook 自动注入

编辑 `~/.cursor/hooks.json`：

```json
{
  "version": 1,
  "hooks": {
    "beforeSubmitPrompt": [
      {
        "command": "/Users/YOUR_USER/.pyenv/versions/3.10.17/bin/python3 /Users/YOUR_USER/.mem0/mem0_hook.py --format cursor",
        "timeout": 20
      }
    ]
  }
}
```

每次 Agent 发消息前，Hook 从 stdin 读取 prompt，调用 `hybrid_search()`，返回：

```json
{
  "additional_context": "[mem0自动注入的相关记忆]\n- [project] (keyword+vector) ..."
}
```

## 3. 项目级 sessionStart（可选）

在项目 `.cursor/hooks.json` 配置 `sessionStart`，对话开头注入最近 8 条项目+全局记忆（暖启动，不按 query 搜索）。

## 4. Agent 规则（可选）

项目 `.cursor/rules/mem0.mdc` 约束 Agent 何时主动 `search_memory` / `add_memory`：
- 任务涉及架构、API 约定、历史决策 → 先 search
- reference 类写入 → `infer=false`
- 结构化约定 → metadata.structured

## 5. 验证

1. Ollama 运行：`curl http://localhost:11434/api/tags`
2. Cursor Settings → MCP → `mem0-local` 状态正常（失败时 Restart Servers）
3. 发消息时 Hook 输出含 `[mem0自动注入的相关记忆]`

### 常见问题

**Q: MCP server errored？**
Ollama 未启动最常见。启动后 Restart MCP。

**Q: Hook 不生效？**
检查 python 绝对路径、timeout（Ollama 慢时可调到 30s）。

**Q: 和 Claude Code 共用 ~/.mem0 吗？**
是。同一套 chroma_db、history.db、pending 队列。
