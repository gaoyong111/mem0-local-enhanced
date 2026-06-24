# mem_viewer 设计规格

Flask + vis.js 本地 Web UI，用于浏览、检索、编辑 mem0 记忆，并支持 episodic 人机梳理确认。

## 目标

- 可视化记忆图谱（节点 = 记忆，边 = `merged_from` / 同 project 弱关联）
- 与 MCP 共用 `hybrid_search` 算法，便于对比检索效果
- 支持手动新增/编辑、删除、演变时间线
- episodic 待确认流程：AI 建议只读展示，用户确认后才清 `grooming_pending`

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Flask（`mem_viewer.py`） |
| 前端 | 内嵌 HTML + vis.js Network |
| 向量 | Ollama bge-m3（编辑/新增时 re-embed） |
| 数据 | Chroma + `active_memories.db` + `lineage.jsonl` |

## 启动

```bash
python3 ~/.mem0/mem_viewer.py          # http://localhost:8765
bash ~/.mem0/mem_viewer.sh             # 同上（包装脚本）
```

## 检索条数

| 入口 | max_results | 说明 |
|------|-------------|------|
| 搜索面板 `/search` | 8 | 与 MCP `search_memory` 一致 |
| 相似预警 `/api/similar` | 5 | 写入前查重 |
| Hook L2（`mem0_hook.py`） | 5 | IDE 自动注入，非 viewer |

## HTTP API

| 路由 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 主页面（图谱 + 侧栏） |
| `/search` | GET | 混合检索，`?q=&project=` |
| `/api/timeline/<id>` | GET | 演变时间线（history + lineage） |
| `/api/similar` | GET | 相似记忆预警，`?q=&project=` |
| `/api/add` | POST | 新增记忆（JSON body） |
| `/api/update/<id>` | POST | 编辑记忆，正文变更时 re-embed |
| `/api/grooming/confirm/<id>` | POST | 确认保留，清 `grooming_pending` |
| `/api/grooming/promote/<id>` | POST | 采纳 promote 建议 |
| `/api/grooming/merge/<source_id>` | POST | 合并（当场 hybrid_search 重校验） |
| `/delete/<id>` | POST | 删除（必填 reason，走 `memory_sync`） |

## UI 功能

### 图谱

- 节点按 category 上色（五类规范色）
- 侧栏 category 筛选
- 节点厚度反映检索命中频次（可选指标）

### 混合检索面板

- 展示 rank、score、source（vector / keyword / both）
- 标注 `effective_project` 与 MCP 输出格式对齐

### 演变时间线

- 合并 `history.db` 事件与 `lineage.jsonl`（MERGE / DEDUP / DELETE）
- 可点击上游 ID 跳转节点

### episodic 待确认

- 筛选 `grooming_pending=1` 的节点（橙框）
- AI 建议面板：`grooming_action` / `grooming_reason` / promote 目标
- merge 建议来自 `~/.mem0/grooming-merge-hints.json`（当次覆盖，有时效性）
- 用户操作：确认保留 / 采纳删升 / 合并重校验 — **不自动执行**

### 新增 / 编辑

- 写入前 `/api/similar` 相似预警
- 扩写正文时 Ollama re-embed + `sync_active_update_content`
- category 规范化与 MCP `add_memory` 一致

## 与 grooming 批处理的关系

- `scripts/episodic_grooming_run.py` 写 AI 建议到 Chroma metadata
- viewer **运行时** Chroma 被占用，批处理 LLM 可能失败；需要 LLM 分析时先关闭 viewer
- 协议详见 [architecture.md](architecture.md)「episodic 人机梳理」

## 相关文档

- [architecture.md](architecture.md) — 混合检索、存储分层、写入策略
- [README.md](../README.md) — 安装与 IDE 集成
