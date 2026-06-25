# mem0-local-enhanced

本地 mem0 增强方案 —— 混合检索 + 智能写入策略 + 三层注入 + 每日复盘闭环 + Web UI 可视化

[English documentation → README_EN.md](README_EN.md)

---

## 为什么需要这个？

[mem0](https://github.com/mem0ai/mem0) 是优秀的记忆管理系统，官方也提供 MCP server。但在以下场景中，官方方案能力不足：

| 场景 | 官方局限 | 本方案增强 |
|------|----------|-----------|
| 中文 + 技术标识符检索 | 纯向量检索，camelCase / 中文关键词召回率低 | **混合检索**（向量 bge-m3 为主 + keyword 补精确词；加权 RRF 融合） |
| 中文记忆入库 | `use_input_language` 不可靠，infer 易英文化 | **B/C/D/E 写入策略**：分类分流、中文锁、结构化、LLM 去重 |
| IDE 上下文注入 | 有 MCP 但无每次输入自动搜索注入 | **Hook + MCP**，Claude Code / Cursor 零干预注入 |
| 记忆运维 | 写入失败无兜底、无复盘反哺 | **pending 队列 + 每日复盘 cron + 快照 diff** |

## 系统架构

```text
运行时注入（L1 暖启动 / L2 Hook / L3 MCP search）
        ↓
~/.mem0/  hybrid_search · mem0_add_policy · MCP · pending · mem_viewer
        ↓
每日复盘（cron 18:03 + skill）→ 进化提取写回 mem0 → 升格建议 → 快照 baseline
```

→ 详细设计：[docs/architecture.md](docs/architecture.md) · [docs/daily-review-integration.md](docs/daily-review-integration.md)

## 核心特性

### 检索与写入

- **混合检索**：先向量后 keyword 串行召回 → 加权 RRF（K=15）；中文 query 排除 `lang=en`；keyword 支持子序列补进池、去重叠、相对截断；项目配额优先
- **B 分类标签**：五类规范（episodic/behavior/workflow/reference/preference），写入自动规范化，mem_viewer 按类筛选/上色
- **C 中文锁定**：patch `use_input_language`（infer 路径已关闭）
- **D 结构化格式**：`[module] field: rule（关键词: …）` 提升命中率
- **E LLM 去重**：关键词 + Jaccard 预筛后 LLM 判 KEEP/DROP_NEW，宁多勿删

### 运行时

- **MCP 六工具**：add / search / get_all / delete / **retry_pending** / **confirm_grooming**
- **pending 兜底**：写入失败 → `~/.mem0/pending/`；多表同步失败 → `~/.mem0/sync_pending/`（`retry_pending` 一并重试）
- **LLM 兜底**：主配置失败自动切 `config_ollama.json`
- **Hook 注入**：Claude `UserPromptSubmit` + Cursor `beforeSubmitPrompt`（每次 max=5）
- **mem_viewer**：Flask + vis.js 图谱；category 筛选/上色、混合检索结果面板（score+source，max=8）、演变时间线；**手动新增/编辑**记忆；**episodic 待确认**筛选（橙框）、AI 梳理建议面板（确认保留/采纳删升/合并重校验）
- **episodic 人机梳理**：新 episodic 自动 `grooming_pending=1`；AI 只写 keep/delete/promote 建议（merge 进当次 `grooming-merge-hints.json`）；用户在 viewer 或对话确认，不自动执行
- **memory_lineage**：`lineage.jsonl` 记录 MERGE/DEDUP/DELETE，grooming 合并须带 `merged_from`

### 每日复盘闭环（Claude Code cron）

| 组件 | 路径 |
|------|------|
| 流程权威 | `~/.claude/skills/daily-review/SKILL.md` |
| cron 触发 | `~/.claude/scheduled_tasks.json`（18:03，prompt 仅引用 skill） |
| 辅助脚本 | `scripts/review_helpers.py`（快照/diff/漏跑/续期/会话清单） |
| 产出目录 | `~/daily-reviews/`（复盘文档含「升格建议」「会话来源」、TODO-tracker、mem0-snapshot-*.json） |

Preflight 检查 Ollama + MCP；不可用时**降级模式**（文档照常，跳过 mem0 写入）。

## 快速开始

### 前置条件

- Python 3.10+
- [Ollama](https://ollama.ai) 常驻 + `bge-m3`（`ollama pull bge-m3`）
- `pip install -r requirements.txt`（或 `pip install mem0ai chromadb mcp flask`）

> Ollama 是硬依赖：未启动则 MCP 初始化失败、add/search 全部不可用。

### 安装

**方式 A：一键脚本（推荐）**

```bash
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced
bash scripts/setup.sh
```

**方式 B：手动部署**

```bash
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced

mkdir -p ~/.mem0
cp src/*.py ~/.mem0/
cp src/mem_viewer.sh ~/.mem0/
cp src/project_aliases.example.json ~/.mem0/project_aliases.json
cp configs/config_ollama.example.json ~/.mem0/config_local.json
# 或 API 版：cp configs/config_api.example.json ~/.mem0/config_local.json
cp .env.example ~/.mem0/.env   # 可选

# 复盘辅助（可选）
mkdir -p ~/.claude/skills/daily-review/scripts
cp scripts/review_helpers.py ~/.claude/skills/daily-review/scripts/
```

### 验证

```bash
# Ollama
curl http://localhost:11434/api/tags

# 混合检索
python3 ~/.mem0/search_context.py "测试查询"

# MCP（stdio，Ctrl+C 退出）
python3 ~/.mem0/mcp_server_local.py

# Web UI
python3 ~/.mem0/mem_viewer.py   # 或 bash ~/.mem0/mem_viewer.sh → http://localhost:8765

# 复盘辅助
python3 scripts/review_helpers.py check-missed-run
python3 scripts/review_helpers.py list-sessions --since "2026-06-24 11:50"
python3 scripts/review_helpers.py snapshot

# episodic 梳理（写 AI 建议 + merge hints；mem_viewer 关闭时 LLM 更稳）
MEM0_DIR=~/.mem0 python3 scripts/episodic_grooming_run.py --dry-run
```

## IDE 集成

| IDE | 文档 |
|-----|------|
| Claude Code | [docs/claude-code-setup.md](docs/claude-code-setup.md) |
| Cursor | [docs/cursor-setup.md](docs/cursor-setup.md) |

要点：
- MCP 注册 `mem0-local` → `~/.mem0/mcp_server_local.py`
- Hook 用 **python 绝对路径**（pyenv）；`hook_search.py` 为 Claude 兼容入口
- 所有写入 **`infer=false`**（默认即 false，显式 true 亦被忽略）

## 配置说明

### 主 / 备配置

mem0 运行时读取 **JSON** 配置（`config_local.json` / `config_ollama.json`）。`configs/config.example.yaml` 为同等内容的 YAML 参考，便于阅读，**不可直接加载**。

```bash
# 主：远程 LLM（推荐）
cp configs/config_api.example.json ~/.mem0/config_local.json

# 备：纯 Ollama（API 挂时自动切换）
cp configs/config_ollama.example.json ~/.mem0/config_ollama.json
```

### pending / sync_pending 队列

| 路径 | 用途 |
|------|------|
| `~/.mem0/pending/` | add 写入失败（MCP、复盘兜底、`retry_pending`） |
| `~/.mem0/sync_pending/` | 多表删除/同步失败（`retry_pending` 顺带重试） |

### infer

**已永久关闭**。所有 category 统一 verbatim 原样入库；显式 `infer=true` 亦被忽略。

→ 详见 [docs/architecture.md](docs/architecture.md)

## 可视化 Web UI

`mem_viewer.py` — 图谱浏览记忆：category 筛选/上色、混合检索结果面板（score+source，与 MCP 同算法 max=8）、演变时间线、厚度指标、删除；**新增/编辑**记忆（`/api/add`、`/api/update`，扩写时重嵌向量；写入前 `/api/similar` 相似预警 max=5）；**episodic 待确认**（筛选/橙框/AI 建议面板，确认保留仅清 `grooming_pending`）。

episodic 梳理批处理 → `scripts/episodic_grooming_run.py`；协议详见 [docs/architecture.md](docs/architecture.md)「episodic 人机梳理」一节。

设计规格 → [docs/mem-viewer-design.md](docs/mem-viewer-design.md)

## 项目结构

```text
mem0-local-enhanced/          # 本仓库（源码 + 文档）
├── src/                      # 部署到 ~/.mem0/
│   ├── mcp_server_local.py   # MCP 六工具
│   ├── mem0_add_policy.py    # B/C/D/E 写入策略
│   ├── hybrid_search.py      # 混合检索核心
│   ├── mem0_paths.py         # 路径与环境变量统一解析
│   ├── memory_sync.py        # active/history/Chroma 多表同步
│   ├── memory_delete.py      # 归档删除 + deleted_archive
│   ├── memory_lineage.py     # lineage.jsonl 演变留痕
│   ├── mem0_hook.py          # IDE Hook 统一入口
│   ├── hook_search.py        # Claude Hook 兼容入口
│   ├── search_context.py     # CLI 混合检索测试
│   ├── mem_viewer.py         # Web UI
│   ├── mem_viewer.sh         # Web UI 启动脚本
│   ├── grooming_metadata.py  # episodic 梳理 metadata 协议
│   └── grooming_episodic.py  # AI 建议 + merge hints 逻辑
├── scripts/
│   ├── setup.sh                # 一键初始化 ~/.mem0
│   ├── review_helpers.py       # 复盘：快照/diff/漏跑/续期/会话清单 list-sessions
│   ├── episodic_grooming_run.py  # episodic 梳理批处理（可重复）
│   └── english_grooming_run.py   # 历史 infer 英文遗留一次性迁移（含硬编码 ID）
├── configs/
│   ├── config_api.example.json
│   ├── config_ollama.example.json
│   └── config.example.yaml     # YAML 参考（运行时用 JSON）
└── docs/
    ├── architecture.md
    ├── mem-viewer-design.md
    ├── daily-review-integration.md
    ├── claude-code-setup.md
    └── cursor-setup.md

~/.mem0/                      # 运行时（安装目标）
├── pending/                  # add 写入失败队列
├── sync_pending/             # 多表同步失败队列
├── grooming-merge-hints.json # 当次 merge 建议（grooming 覆盖写）
├── active_memories.db        # keyword 检索数据源
├── deleted_archive.db        # 删除台账
├── history.db                # 追溯日志（检索不读）
├── lineage.jsonl             # 演变事件
├── chroma_db/                # 向量库
└── .env                      # 可选环境变量

~/.claude/skills/daily-review/  # 复盘流程（非本仓库，配套使用）
~/daily-reviews/                # 复盘产出
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MEM0_DIR` | `~/.mem0` | 安装目录 |
| `MEM0_CONFIG` | `~/.mem0/config_local.json` | 主配置 |
| `MEM0_FALLBACK_CONFIG` | `~/.mem0/config_ollama.json` | 备用配置 |
| `MEM0_CHROMA_PATH` | `~/.mem0/chroma_db` | Chroma 路径 |
| `MEM0_HISTORY_DB` | `~/.mem0/history.db` | history 追溯库 |
| `MEM0_ACTIVE_DB` | `~/.mem0/active_memories.db` | active 查询库 |
| `MEM0_DELETED_DB` | `~/.mem0/deleted_archive.db` | 删除归档库 |
| `MEM0_PROJECT_ALIASES` | `~/.mem0/project_aliases.json` | 目录名→project 映射 |
| `MEM0_USER_ID` | `default-user` | 用户标识 |
| `MEM0_DEFAULT_USER_ID` | 同 `MEM0_USER_ID` | hybrid_search 专用 fallback |
| `MEM0_VECTOR_REL_MARGIN` | `0.10` | 向量相对阈值；设 `0` 关闭 |
| `MEM0_KW_REL_RATIO` | `0.25` | keyword 相对截断；设 `0` 关闭 |

## 已知限制

- Ollama 必须常驻，重启 Mac 后需手动启动或配置登录项
- 跨词同义（如淋雨↔下雨）依赖向量路，keyword 不维护同义词表
- AnthropicLLM provider 不传递 `response_format`（infer/merge 受影响）
- Chroma metadata 仅支持标量，嵌套 dict 需 `structured_json` 序列化
- Chroma `update` 合并 metadata，删除字段须写 `0`/空串（如 `grooming_pending=0`），不能 pop 键

## License

MIT
