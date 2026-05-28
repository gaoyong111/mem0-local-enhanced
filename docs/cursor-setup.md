# Cursor 集成指南

## 1. MCP server 注册

编辑 `~/.cursor/mcp.json`：

```json
{
    "mcpServers": {
        "mem0-local": {
            "command": "python3",
            "args": ["~/.mem0/mcp_server_local.py"]
        }
    }
}
```

或者项目级配置，在项目根目录创建 `.cursor/mcp.json`。

## 2. Hook 自动注入（Cursor Agent 模式）

Cursor 的 `beforeSubmitPrompt` hook 可以注入上下文。

### 配置方式

目前 Cursor 的 Hook 集成方式与 Claude Code 不同，需要根据 Cursor 版本调整。核心是让 `mem0_hook.py` 以 `--format cursor` 模式运行：

```bash
python3 ~/.mem0/mem0_hook.py --format cursor
```

输出格式：
```json
{
    "additional_context": "格式化的记忆文本"
}
```

### 具体配置方法

请参考 Cursor 最新文档中关于 Agent hooks 的说明。核心流程：
1. 在 Cursor 设置中启用 beforeSubmitPrompt hook
2. 配置 hook 命令为 `python3 ~/.mem0/mem0_hook.py --format cursor`
3. Hook 从 stdin 读取用户消息，自动搜索并返回

## 3. 验证

重启 Cursor 后：
- MCP server 连接状态正常
- 可以在 Agent 模式中调用 mem0 工具

### 常见问题

**Q: MCP server 在 Cursor 中不显示？**
检查 `python3` 路径是否正确，可以用绝对路径（如 `/usr/bin/python3` 或 pyenv 路径）。

**Q: Hook 不生效？**
Cursor 的 Hook 集成仍在演进中，请关注 Cursor 最新版本的文档更新。