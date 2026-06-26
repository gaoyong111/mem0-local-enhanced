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
│ 每日复盘（Claude Code cron 9:30 + skill）                    │
│ Preflight → 采集 → pending重试 → 写文档 → 进化提取 → 升格建议 → grooming → 快照 │
└─────────────────────────────────────────────────────────────┘
```

## 对话深度分析（采集环节）

**先拉会话清单**（对话分析开始前）：

```bash
HELPER=~/.claude/skills/daily-review/scripts/review_helpers.py
DATE=$(date +%Y%m%d)
python3 $HELPER list-sessions \
  --since "<扫描起点>" \
  --output ~/daily-reviews/.data/session-inventory-${DATE}.json
```

用多代理并行扫描 Claude 和 Cursor 会话 JSONL。

**每个会话文件都必须派代理扫描**，不论长短。短会话代理几秒就完成，但跳过可能遗漏关键信息。

每个代理除常规提取外，还须返回一行「提取内容摘要」（15~40 字），供文档末尾 `## 会话来源` 引用。

特别关注：
- 用户重复要求的操作（AI没做到位）
- 用户频繁提出的问题（可固化为经验）
- 用户随口偏好或吐槽（preference 类记忆来源）

**筛选方式**（Claude 与 Cursor 策略不同，不可混用）：
- **Claude Code 会话**：按 JSONL 内部 `timestamp` 字段过滤（精确）
- **Cursor 会话**：Cursor JSONL 只有 `role` + `message` 无 `timestamp`，改用文件 `mtime` 筛选（mtime ≥ 时间起点即视为相关）。排除 `/subagents/` 子目录

## 复盘文档：会话来源（文档最末尾）

参考文献式溯源，位于基础设施告警、配置变更记录等**所有章节之后**：

| # | 容器 | 项目 | 会话 ID | 提取内容摘要 |
|---|------|------|---------|-------------|
| 1 | Cursor | ehealth-weixin-h5 | fd45e6b1 | 用药指导接口与桌面 docx 比对，发现缺参 |
| 2 | Claude | (全局) | 89bf86c0 | cron 复盘主会话：多代理扫描、写文档 |

- **容器**：`Claude` / `Cursor`
- **项目**：从会话路径解码（如 `ngaripc`）；无具体项目写 `(全局)`
- **会话 ID**：UUID 前 8 位，与正文引用一致
- **提取内容摘要**：该会话对本次复盘贡献了什么；无实质内容写「无实质内容」

权威格式见 `~/.claude/skills/daily-review/SKILL.md`「输出格式 → 会话来源」。

## 权威流程

复盘流程的**唯一真相源**：

`~/.claude/skills/daily-review/SKILL.md`

cron prompt（`~/.claude/scheduled_tasks.json`）仅负责定时触发 + 降级约束，**不得**在 prompt 里重复完整流程。

## Preflight 与降级模式

| 检查项 | 命令/方式 | 失败影响 |
|--------|-----------|----------|
| 权限自检 | 检查 `settings.local.json` 的 `permissions.allow` 是否覆盖复盘核心 Bash/Write | don't ask 模式下执行到一半被阻塞，需用户按 Ctrl+C 才恢复 |
| Ollama | `curl localhost:11434/api/tags` | embedding 不可用 |
| mem0 MCP | `get_all_memories` / `search_memory` | 无法在线写入 |
| pending | `ls ~/.mem0/pending/*.json` | 仅记录条数 |
| 漏跑 | `review_helpers.py check-missed-run` | 文档开头加 banner |

**降级模式**（Ollama 或 MCP 不可用）：
- 跳过：pending 重试、进化提取、升格建议、memory-grooming
- 照常：git、对话分析、复盘文档、TODO-tracker、工具统计
- 文档末尾写 `## 基础设施告警`

> **关键依赖**：Ollama 必须常驻（`bge-m3`）。重启 Mac 后若 Ollama 未启动，MCP 启动失败、add/search 全部不可用。

## pending 队列（统一路径）

**add 失败**：`~/.mem0/pending/` · **多表同步失败**：`~/.mem0/sync_pending/`（`retry_pending` 一并重试）

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
# 或仓库内：HELPER=scripts/review_helpers.py（在 mem0-local-enhanced 根目录执行）

python3 $HELPER check-missed-run
python3 $HELPER list-sessions --since "YYYY-MM-DD HH:MM" \
  --output ~/daily-reviews/.data/session-inventory-YYYYMMDD.json
python3 $HELPER diff --baseline latest --output ~/daily-reviews/.data/mem0-diff-YYYYMMDD.json
python3 $HELPER snapshot
python3 $HELPER record-scan-end
python3 $HELPER log-cron-renewal --old <旧id> --new <新id>
```

- 读 `history.db` + Chroma metadata，**不依赖 Ollama**
- 会话清单：`~/daily-reviews/.data/session-inventory-YYYYMMDD.json`（容器/项目/ID，代理填摘要后写入文档末尾）
- 快照：`~/daily-reviews/.data/mem0-snapshot-YYYYMMDD.json`
- diff 报告：`~/daily-reviews/.data/mem0-diff-YYYYMMDD.json`
- 续期日志：`~/daily-reviews/cron-renewal.log`

## cron 自续期

Claude Code cron 有生命周期限制。每日复盘 cron（`30 9 * * *`）在 `createdAt` 距今 **≥ 3 天** 时：

1. `CronCreate` 重建（同样 schedule + 精简 prompt）
2. `review_helpers.py log-cron-renewal` 写日志
3. 复盘文档记录 `cron renewed: old=… new=…`
4. `CronDelete` 删旧 cron

精简 prompt 模板见 `scheduled_tasks.json`，核心：先 Read skill → Preflight → 降级模式 → 自续期。

## 进化提取写入 mem0

复盘末尾从当日事件中提炼规律，分四类写入：

| 类型 | category | 示例 |
|------|----------|------|
| 行为规则 | behavior | 「涉及用户个人经历必须先 search_memory」 |
| 可复用流程 | workflow | 「legacy 页面改 JS+HTML+CSS 三件套」 |
| 领域知识 | reference | 「某模块某字段为字符串类型」 |
| 用户偏好 | preference | 「一步操作不走 skill」 |
| 踩坑/事件 | episodic | 「某次 bug 根因与修复」；不确定分类时用此项 |

每条须含 **Why** + **How to apply**。写入前 `search_memory` 查重；统一 `infer=false`；category 漏填默认 episodic（自动 `grooming_pending=1`）。

## 升格建议（mem0 → CLAUDE.md P0/P1）

进化提取之后、memory-grooming 之前：评估 mem0 条目是否应升格到 `~/.claude/CLAUDE.md` P0/P1。

- **只写建议**，不自动改 CLAUDE.md / `global-user-rules.md`
- **无候选写「无」**，不硬凑
- grooming 的 `promote→behavior` 仍在 mem0 内；升格建议是再筛一层——是否该进全局硬规则

纳入须同时满足：跨话题、已验证（重复 correction / grooming promote）、可二元化、不与现有 P0/P1 重复。

写入复盘文档 `## 升格建议`（位于 `## mem0 变化` 之后）。权威判定见 `~/.claude/skills/daily-review/SKILL.md`「升格建议」章节。

## memory-grooming（episodic 人机梳理）

复盘 **memory-grooming** 阶段（mem0 可用时）：**不自动删/合/升 episodic**，只写 AI 建议，用户在 mem_viewer 确认。

```bash
# 默认：待确认或无 grooming_at 的 episodic
MEM0_DIR=~/.mem0 python3 scripts/episodic_grooming_run.py

# 预览
MEM0_DIR=~/.mem0 python3 scripts/episodic_grooming_run.py --dry-run
```

| 输出 | 存储 |
|------|------|
| keep / delete / promote 建议 + 理由 | Chroma metadata（`grooming_action`、`grooming_reason` 等） |
| merge 建议 | `~/.mem0/grooming-merge-hints.json`（**当次覆盖**，有时效性） |
| 待确认标记 | `grooming_pending=1`；用户「确认保留」→ `0` |

用户在 **mem_viewer**（待确认筛选 / AI 建议面板）或对话中让 AI 执行建议后，调用 MCP `confirm_grooming` 清标记。

> mem_viewer 运行时 Chroma 被占用，批处理 LLM 可能失败并走规则兜底；需要 LLM 分析时先关闭 viewer 再跑脚本。

→ 协议详见 [architecture.md](architecture.md)「episodic 人机梳理」

## 相关文件一览

| 路径 | 用途 |
|------|------|
| `~/.mem0/` | mem0 运行时（本仓库 src 部署目标） |
| `~/.claude/skills/daily-review/SKILL.md` | 复盘流程权威文档 |
| `~/daily-reviews/` | 复盘文档（含「升格建议」「会话来源」）、TODO-tracker |
| `~/daily-reviews/.data/` | 快照、diff 报告、session-inventory（机器数据，与文档分离） |
| `~/.claude/scheduled_tasks.json` | cron 持久化配置 |
| `~/.cursor/hooks.json` | Cursor beforeSubmitPrompt |
| `~/.cursor/mcp.json` | Cursor MCP mem0-local |

## 已知待办（mem0 质量）

- hybrid_search rerank 层（#43，库变大后再评估）
