# 每日复盘与 mem0 集成

mem0-local-enhanced 负责**记忆的存、搜、注入**；每日复盘负责**从对话/git 中提炼认知并反哺 mem0**。两者通过 pending 队列、快照 diff、cron 定时任务衔接。

## 整体架构

```text
┌─────────────────────────────────────────────────────────────┐
│ 运行时注入（读记忆）                                          │
├─────────────────────────────────────────────────────────────┤
│ L1 sessionStart     项目 .cursor/hooks → 最近 8 条暖启动     │
│ L2 beforeSubmitPrompt ~/.cursor/hooks.json → hybrid_search    │
│ L3 Agent 主动       MCP search_memory / add_memory          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ mem0-local-enhanced（~/.mem0/）                              │
│ hybrid_search · mem0_add_policy · MCP · pending · mem_viewer│
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 每日复盘（Claude Code cron 18:03 + skill）                    │
│ Preflight → 采集 → pending重试 → 写文档 → 进化提取 → 快照    │
└─────────────────────────────────────────────────────────────┘
```

## 权威流程

复盘流程的**唯一真相源**：

`~/.claude/skills/daily-review/SKILL.md`

cron prompt（`~/.claude/scheduled_tasks.json`）仅负责定时触发 + 降级约束，**不得**在 prompt 里重复完整流程。

## Preflight 与降级模式

| 检查项 | 命令/方式 | 失败影响 |
|--------|-----------|----------|
| Ollama | `curl localhost:11434/api/tags` | embedding 不可用 |
| mem0 MCP | `get_all_memories` / `search_memory` | 无法在线写入 |
| pending | `ls ~/.mem0/pending/*.json` | 仅记录条数 |
| 漏跑 | `review_helpers.py check-missed-run` | 文档开头加 banner |

**降级模式**（Ollama 或 MCP 不可用）：
- 跳过：pending 重试、进化提取、memory-grooming
- 照常：git、对话分析、复盘文档、TODO-tracker、工具统计
- 文档末尾写 `## 基础设施告警`

> **关键依赖**：Ollama 必须常驻（`bge-m3`）。重启 Mac 后若 Ollama 未启动，MCP 启动失败、add/search 全部不可用。

## pending 队列（统一路径）

**唯一路径**：`~/.mem0/pending/`

| 来源 | 触发条件 |
|------|----------|
| MCP `add_memory` 失败 | `_write_to_pending()` 自动写入 |
| 复盘进化提取失败 | 手动写入 JSON |
| MCP `retry_pending` | 批量重试 |
| 复盘 cron | 逐条 `add_memory(infer=false)` |

JSON 格式：

```json
{
  "content": "...",
  "metadata": {"category": "reference", "project": "your-project"},
  "project": "your-project",
  "use_infer": false,
  "retry_count": 0,
  "created_at": "2026-06-05T12:00:00",
  "source": "daily-review-20260605"
}
```

重试规则：成功删文件；失败 `retry_count+1`；≥3 次设 `status=manual_review`。

## mem0 快照与 diff

复盘辅助脚本（本仓库 `scripts/review_helpers.py`，安装后可复制到 skill 目录）：

```bash
HELPER=~/.claude/skills/daily-review/scripts/review_helpers.py
# 或：HELPER=~/Desktop/mem0-local-enhanced/scripts/review_helpers.py

python3 $HELPER check-missed-run
python3 $HELPER diff --baseline latest --output ~/daily-reviews/mem0-diff-YYYYMMDD.json
python3 $HELPER snapshot
python3 $HELPER log-cron-renewal --old <旧id> --new <新id>
```

- 读 `history.db` + Chroma metadata，**不依赖 Ollama**
- 快照：`~/daily-reviews/mem0-snapshot-YYYYMMDD.json`
- diff 报告：`~/daily-reviews/mem0-diff-YYYYMMDD.json`
- 续期日志：`~/daily-reviews/cron-renewal.log`

## cron 自续期

Claude Code cron 有生命周期限制。每日复盘 cron（`3 18 * * *`）在 `createdAt` 距今 **≥ 3 天** 时：

1. `CronCreate` 重建（同样 schedule + 精简 prompt）
2. `review_helpers.py log-cron-renewal` 写日志
3. 复盘文档记录 `cron renewed: old=… new=…`
4. `CronDelete` 删旧 cron

精简 prompt 模板见 `scheduled_tasks.json`，核心：先 Read skill → Preflight → 降级模式 → 自续期。

## 进化提取写入 mem0

复盘末尾从当日事件中提炼规律，分四类写入（每次最多 **3 条**）：

| 类型 | category | 示例 |
|------|----------|------|
| 行为规则 | behavior | 「涉及用户个人经历必须先 search_memory」 |
| 可复用流程 | workflow | 「legacy 页面改 JS+HTML+CSS 三件套」 |
| 领域知识 | reference | 「某模块某字段为字符串类型」 |
| 用户偏好 | preference | 「一步操作不走 skill」 |

每条须含 **Why** + **How to apply**。写入前 `search_memory` 查重；reference 类用 `infer=false`。

## 相关文件一览

| 路径 | 用途 |
|------|------|
| `~/.mem0/` | mem0 运行时（本仓库 src 部署目标） |
| `~/.claude/skills/daily-review/SKILL.md` | 复盘流程权威文档 |
| `~/daily-reviews/` | 复盘文档、TODO-tracker、快照、diff |
| `~/.claude/scheduled_tasks.json` | cron 持久化配置 |
| `~/.cursor/hooks.json` | Cursor beforeSubmitPrompt |
| `~/.cursor/mcp.json` | Cursor MCP mem0-local |

## 已知待办（mem0 质量）

- category 分流 vs infer 永久关闭的策略统一（TODO-tracker #9）
- 文件记忆（`~/.claude/projects/.../memory/`）与 mem0 存储重叠（#10）
- hybrid_search 中文子串匹配（「下雨」≠「下大雨」）（#18）
