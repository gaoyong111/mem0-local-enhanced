# Claude Code 集成指南

## 1. MCP server 注册

### 方式 A：全局注册（推荐）

编辑 `~/.claude.json`：

```json
{
  "mcpServers": {
    "mem0-local": {
      "type": "stdio",
      "command": "/Users/YOUR_USER/.pyenv/shims/python3",
      "args": ["/Users/YOUR_USER/.mem0/mcp_server_local.py"]
    }
  }
}
```

### 方式 B：Cursor 共用配置

若已在 `~/.cursor/mcp.json` 配置，Claude Code 需单独在 `~/.claude.json` 注册（格式略有不同）。

### 方式 C：/mcp 命令

```
/mcp add mem0-local -- /Users/YOUR_USER/.pyenv/shims/python3 /Users/YOUR_USER/.mem0/mcp_server_local.py
```

## 2. Hook 自动注入

编辑 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/YOUR_USER/.pyenv/versions/3.10.17/bin/python3 /Users/YOUR_USER/.mem0/mem0_hook.py --format claude",
            "timeout": 20,
            "statusMessage": "搜索mem0相关记忆"
          }
        ]
      }
    ]
  }
}
```

`hook_search.py` 为兼容入口，内部委托 `mem0_hook.py --format claude`。

## 3. 权限配置

```json
{
  "permissions": {
    "allow": [
      "mcp__mem0-local__add_memory",
      "mcp__mem0-local__search_memory",
      "mcp__mem0-local__get_all_memories",
      "mcp__mem0-local__delete_memory",
      "mcp__mem0-local__retry_pending"
    ]
  }
}
```

## 4. 每日复盘 cron（可选）

`~/.claude/scheduled_tasks.json` 持久化定时任务，默认每天 18:03 触发复盘。

cron prompt 精简为引用 skill，完整流程见：
- `~/.claude/skills/daily-review/SKILL.md`
- [docs/daily-review-integration.md](daily-review-integration.md)

## 5. 验证

1. Ollama 运行 + MCP 工具可调用
2. 发消息时看到 mem0 注入上下文
3. `add_memory` + `search_memory` 中文 reference 可检索

### 常见问题

**Q: MCP 启动失败？**
Ollama 未运行或 `config_local.json` 路径错误。

**Q: infer 把中文变英文？**
infer 已永久关闭，所有写入 verbatim 原样入库。若仍见英文记忆，是历史 infer 遗留，靠 grooming 清理。

**Q: add 失败数据丢了吗？**
自动进 `~/.mem0/pending/`，用 `retry_pending` 或等复盘 cron 重试。
