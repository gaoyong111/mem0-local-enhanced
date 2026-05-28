# Claude Code 集成指南

## 1. MCP server 注册

有两种方式注册 MCP server：

### 方式 A：全局注册（推荐）

编辑 `~/.claude.json`，在 `mcpServers` 中添加：

```json
{
    "mcpServers": {
        "mem0-local": {
            "type": "stdio",
            "command": "python3",
            "args": ["~/.mem0/mcp_server_local.py"]
        }
    }
}
```

### 方式 B：项目级注册

在项目目录下的 `.claude/settings.local.json` 中添加同样的配置，仅在该项目生效。

### 方式 C：使用 /mcp 命令

在 Claude Code 中运行：
```
/mcp add mem0-local -- python3 ~/.mem0/mcp_server_local.py
```

## 2. Hook 自动注入

编辑 `~/.claude/settings.json`，添加 `UserPromptSubmit` hook：

```json
{
    "hooks": {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 ~/.mem0/mem0_hook.py --format claude",
                        "timeout": 20,
                        "statusMessage": "搜索mem0相关记忆"
                    }
                ]
            }
        ]
    }
}
```

### Hook 工作原理

每次用户发送消息时，Claude Code 会执行 Hook：
1. Hook 从 stdin 读取用户消息
2. 提取关键词，调用 `hybrid_search()` 搜索相关记忆
3. 格式化结果，作为 `additionalContext` 返回给 Claude Code
4. Claude Code 将搜索结果自动注入到对话上下文中

你会在对话开头看到类似这样的注入：
```
[mem0自动注入的相关记忆]
- [全局] (keyword+vector) 某条记忆内容 (相关度:15.38)
```

## 3. 权限配置

在 `~/.claude/settings.json` 或 `settings.local.json` 中，将 mem0 工具加入 allow 列表：

```json
{
    "permissions": {
        "allow": [
            "mcp__mem0-local__add_memory",
            "mcp__mem0-local__search_memory",
            "mcp__mem0-local__get_all_memories",
            "mcp__mem0-local__delete_memory"
        ]
    }
}
```

## 4. 验证

重启 Claude Code 后，你应该能看到：
- MCP server 连接状态正常
- 可以调用 `add_memory`、`search_memory` 等工具
- 每次输入时，Hook 自动搜索并注入相关记忆

### 常见问题

**Q: MCP server 启动失败？**
检查 `~/.mem0/config_local.json` 路径和内容是否正确，Ollama 是否运行。

**Q: Hook 超时？**
默认 timeout 为 20 秒。如果 Ollama 响应慢，可以增大 timeout。

**Q: 中文记忆被翻译成英文？**
检查 `apply_mem0_patches()` 是否被调用（C 策略）。