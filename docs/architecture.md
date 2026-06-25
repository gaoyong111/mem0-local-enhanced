# 架构详解：写入策略 B/C/D/E + 混合检索

[English overview → README_EN.md](../README_EN.md) · [Web UI spec → mem-viewer-design.md](mem-viewer-design.md)

## 写入策略

### B：分类标签（Category Tags）

`category` 仅作 metadata 标签，用于 mem_viewer 筛选、复盘进化提取分类。**不决定存储模式**。

| category | 典型用途 |
|----------|---------|
| `reference` | 事实、技术约定、领域知识 |
| `preference` | 用户偏好 |
| `workflow` | 可复用流程/方法论 |
| `behavior` | 行为规则类发现 |
| `episodic` | 踩坑、决策、事件记录 |

所有 category **统一 verbatim 原样入库**（含 `structured` 的走 D 策略确定性格式化，不经 LLM 推断抽取）。

**规范五类**（写入时 `metadata.category`，MCP 会自动规范化）：

| category | 用途 | mem_viewer 显示 |
|----------|------|-----------------|
| episodic | 踩坑、决策、事件；**默认**（留空时） | 踩坑/事件 |
| behavior | 行为规则 | 行为规则 |
| workflow | 流程/方法论 | 流程方法 |
| reference | 事实、技术约定 | 事实知识 |
| preference | 用户偏好 | 用户偏好 |

历史非标标签（如 `api`、`module`）在写入/展示时映射为 `reference`；空标签展示为 `episodic`。

`infer` 参数（**已永久关闭**）：
- 默认 `false`
- 显式 `true`：**忽略**，强制 verbatim
- 原因：infer 导致信息丢失、英文化、过度拆分；三 provider 均不可靠

### C：中文锁定（Chinese Language Lock）

mem0 默认会将中文记忆翻译成英文存储，导致中文检索命中率极低。C 策略通过两个层面锁定中文：

1. **Monkey-patch**：`apply_mem0_patches()` 在 Memory 初始化前修改 `mem0.configs.prompts.generate_additive_extraction_prompt`，强制传入 `use_input_language=True`（保留以备 mem0 上游行为变化；当前 infer 路径已关闭）
2. ~~**Infer prompt**~~：infer 已永久关闭，不再传入 `CHINESE_INFER_INSTRUCTIONS`

### D：结构化格式化（Structured Formatting）

对于包含明确模块/字段/规则的技术信息，D 策略将其格式化为固定模板：

```
metadata: {"category":"reference","structured":{"module":"userService","field":"loginType","rule":"字符串","keywords":["登录","API"]}}
```

格式化结果：
```
[userService] loginType: 字符串（关键词: 登录, API）
```

格式化后的文本关键词密度更高，检索命中率大幅提升。

Chroma metadata 仅支持标量值，嵌套的 `structured` dict 会被序列化为 `structured_json` 字符串存储。

### E：LLM 去重（LLM Dedup）

每次写入 verbatim 或 structured 类型记忆后，E 策略会自动执行去重检查：

**第一层过滤**：关键词分数 ≥ 15.0 的候选记忆
**第二层过滤**：token Jaccard 重叠率 ≥ 0.5（排除关键词高分但语义无关的情况）
**LLM 判断**：提交给 LLM 决策，输出 `KEEP` 或 `DROP_NEW`

设计原则：宁多勿删。只有当新旧记忆描述的是**同一组事实**且旧记忆已完整覆盖时才选择 `DROP_NEW`。

## 存储分层（方案一，2026-06）

| 库 / 文件 | 职责 | 检索是否读取 |
|-----------|------|-------------|
| **`active_memories.db`** | 活跃记忆查询表（正文 + project/category/lang） | **是**（keyword 唯一数据源） |
| **`history.db`** | 追加式追溯日志（ADD / UPDATE / DELETE 事件） | **否** |
| **`deleted_archive.db`** | 删除台账（reason / 时间 / 快照 / actor） | **否**（仅提供 deleted_ids） |
| **Chroma** | 向量 + metadata.data | **是**（vector 路） |
| **`lineage.jsonl`** | 演变事件（MERGE / DEDUP / DELETE 等） | **否** |
| **`sync_pending/`** | 多表同步失败待重试（类比 add pending） | **否** |

写入 mem0（Chroma + history ADD）后，MCP 会调用 `sync_active_insert` 同步 **active** 表。删除统一走 `memory_sync.execute_delete`，**必须填写 reason**。

### 多表同步事务（`memory_sync.py`）

删除时期望行数（固定校验）：

| 步骤 | 表 | 期望 |
|------|-----|------|
| SQLite 事务 | active DELETE | 1 |
| SQLite 事务 | deleted_archive INSERT | 1 |
| SQLite 事务 | history INSERT (DELETE 事件) | 1 |
| 事务外 | Chroma delete | 1 |

任一步不符 → SQLite **ROLLBACK** 或 Chroma 失败后**还原 active** → 写入 `sync_pending/`。`retry_pending` 末尾会顺带执行 `retry_sync_pending()`。

**注意**：向量写入须带 Ollama 预计算 embedding（`upsert(embeddings=...)`）。勿对 Chroma 使用裸 `col.add(documents=...)`，否则会触发 Chroma 内置 ONNX MiniLM 下载。

## 混合检索

### 检索流程

```
用户查询
    │
    ├── ① 向量检索（Chroma + Ollama bge-m3）— 语义为主
    │   ├─ 中文 query 排除 lang=en（oversample×4）
    │   ├─ 相对阈值：vec_score < top1−0.10 不进池
    │   └─ top-50 → vec_rank_map
    │
    ├── ② 关键词检索（active_memories.db）— 依赖 vec_rank_map
    │   ├─ 滑窗 2–4 字 + primary≤6 + 英文 token
    │   ├─ 最长命中去重叠、TF cap=3
    │   ├─ 条件子序列（主 keyword，vec_gate 门控）
    │   ├─ kw 相对截断：score < top1×0.25 不进池
    │   └─ top-50 → kw_rank
    │
    └── ③ 加权 RRF 融合（merge_and_rank）
        ├─ rrf = 1/(K+vec_rank) + 0.5·1/(K+kw_rank)；K=15，β=0
        ├─ project 匹配 +0.005；preference 跨类 +0.008
        ├─ 配额：project 前 3 直保 + 全局保底 2
        └─ 返回条数：Hook L2 max=5 · MCP search max=8 · viewer 搜索面板 max=8 · viewer /api/similar max=5
```

跨词同义（如淋雨↔下雨）交给向量路，不维护手动同义词表。设计文档见 `daily-reviews/hybrid-search-design.md`。

### 关键词数据源

`_load_final_memories()` 只读 **`active_memories.db`**，不再扫描 history.db ADD 行。history 仅作追溯；删除过滤由 `deleted_archive.db` 保证 active 中无已删 id。

### 关键词计分规则

- 最长命中优先去重叠（同一段文本不重复计分）
- 单 keyword 命中次数 cap：`TF_CAP=3`
- 子串未命中时：主 keyword（2–4 字）可走条件子序列（弱分 ×0.5，且 `vec_rank ≤ gate`）
- 排序后相对截断：`score < top1 × 0.25` 不进 keyword 池
- RRF 只看 kw_rank，不看 kw 绝对分

### 向量计分规则

- Chroma 返回 cosine distance；展示分 `1.0 - distance/2.0`
- 相对阈值 `VECTOR_SCORE_REL_MARGIN=0.10`：低于 top1−δ 不进向量池
- 排序融合走 RRF rank，不看 vec 绝对分阈值

**注意**：MCP 输出 `kw=` / `vec=` / `kw_rank` / `vec_rank` / `rrf=` / 可选 `proj=`，不可把 keyword 分当作 0～1 语义相关度。

mem_viewer 搜索面板与 MCP `search_memory` 使用同一 `hybrid_search`（max=8）。Hook 自动注入为 max=5，写入前相似预警 `/api/similar` 亦为 max=5。展示 rank / score / source 便于对比。

### 项目检测

`detect_project()` 从当前工作目录的 basename 推断项目标识：

1. 读 `~/.mem0/project_aliases.json`（安装时从 `project_aliases.example.json` 复制，**仅本地配置，勿提交真实项目名**）
2. Generic 目录名（Desktop、Documents 等）返回空字符串（全局）
3. 其他目录名直接用作项目标识

### Hook 注入格式

**Claude Code**：
```json
{
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "格式化的记忆文本"
    }
}
```

**Cursor**：
```json
{
    "additional_context": "格式化的记忆文本"
}
```

## MCP server 工具

| 工具 | 说明 |
|------|------|
| `add_memory` | 添加记忆，支持 metadata/project/infer 参数，自动执行写入策略 |
| `search_memory` | 混合检索，支持 project 限定范围 |
| `get_all_memories` | 获取所有记忆，支持 project 限定范围 |
| `delete_memory` | 删除指定 ID（**reason 必填**），多表事务同步 |
| `retry_pending` | 重试 `pending/` 写入失败 + `sync_pending/` 同步失败 |
| `confirm_grooming` | 确认 episodic 梳理建议（写 `grooming_pending=0`，不改正文） |

## 三层记忆注入

| 层 | 入口 | 行为 |
|----|------|------|
| L1 暖启动 | 项目 `.cursor/hooks.json` sessionStart | 按时间取最近 8 条，不看 query |
| L2 自动注入 | `~/.cursor/hooks.json` beforeSubmitPrompt / Claude `UserPromptSubmit` | 每次发消息按 query 做 hybrid_search |
| L3 主动检索 | Agent 调用 MCP `search_memory` | 任务开始前按关键词搜索 |

L2/L3 使用同一套 `hybrid_search.py`；L1 仅做项目上下文预热。

## pending 写入兜底

add 失败时 MCP 写入 `~/.mem0/pending/*.json`（与复盘兜底**同一路径**）。字段：`content`、`metadata`、`project`、`use_infer`、`retry_count`、`created_at`。

复盘 cron 或 `retry_pending` 工具负责重试。详见 → [daily-review-integration.md](daily-review-integration.md)

## sync_pending 同步兜底

多表写入/删除失败时写入 `~/.mem0/sync_pending/*.json`（`op`、`memory_id`、`reason`、`expected`、`actual`、`failed_step` 等）。`retry_sync_pending()` 由 `retry_pending` 顺带调用；≥3 次需人工处理。

## 记忆演变留痕（lineage）

| 数据源 | 记录什么 |
|--------|----------|
| `history.db` | mem0 原生 ADD / UPDATE / DELETE（**仅追溯**，检索不读） |
| `deleted_archive.db` | 删除 reason / 时间 / 快照 |
| `lineage.jsonl` | MERGE（grooming 合并）、DEDUP_DROP（E 去重）、DELETE |

grooming 合并写入时 metadata 须带 `merged_from`（来源 ID）。mem_viewer 详情面板「演变时间线」可查看并点击上游 ID。

## episodic 人机梳理（2026-06）

episodic 不自动删/合/升，AI 只写建议，用户在 mem_viewer 或对话中确认。

| 字段 / 文件 | 含义 |
|-------------|------|
| `grooming_pending=1` | 待确认 episodic（新写入 episodic 自动打上） |
| `grooming_action` | `keep` / `delete` / `promote`（**不含 merge**） |
| `grooming_reason` | AI 理由 |
| `grooming_target_category` | promote 目标 category |
| `grooming_at` | 建议生成时间 |
| `~/.mem0/grooming-merge-hints.json` | **当次** merge 建议，下次 grooming **整文件覆盖** |

**确认保留**：写 `grooming_pending=0`（Chroma update 合并 metadata，pop 键无效），保留 action/reason 供追溯。正文或 category 修改时 grooming 字段写空值。

**merge 采纳**：viewer「合并（重校验）」当场 hybrid_search 重验目标，再删 source + target 写 `merged_from`。

批处理脚本：`scripts/episodic_grooming_run.py`（复盘 grooming 阶段调用）。MCP 工具 `confirm_grooming` 供 AI 清标记。

### 历史 infer 英文遗留（一次性迁移）

早期 `infer=true` 写入的纯英文/碎片记忆可用 `scripts/english_grooming_run.py` 做**一次性**改写或删除（脚本内含硬编码 memory ID，仅供个人库迁移参考；新库无需运行）。需 Ollama 可用以 re-embed。日常 episodic 质量维护请用 `episodic_grooming_run.py` + mem_viewer 待确认流程。

MCP server 启动时：

1. 尝试加载主配置（`MEM0_CONFIG` 指定的路径）
2. 执行一次轻量 `get_all()` 验证 LLM 通路可用
3. 失败则尝试备用配置（`MEM0_FALLBACK_CONFIG`）
4. 两者都失败则抛出 RuntimeError

这样即使 API 挂了，也能自动切换到本地 Ollama 继续工作。

## infer 永久关闭（2026-06）

`infer=true` 让 mem0 调用 LLM 做「推断抽取」，在实际使用中问题很大：

1. **信息丢失**：模块名/字段名被泛化，关键词检索命中率暴跌
2. **语言漂移**：约 30% 记忆被翻译成英文（本地 Ollama 更明显）
3. **过度拆分**：三 provider 均不可靠，产生大量碎片记忆

**决策**：所有写入永久 `infer=false`，显式 `true` 亦被忽略。category 仅作分类标签。

**检索不受影响**：hybrid_search 对 verbatim 入库文本做 embedding。keyword 增强（子序列/截断等）见架构「混合检索」与设计文档。

### 本地 LLM vs 远程 LLM（E 策略 merge 去重）

以下基于实际使用中的对比观察（本地 Ollama qwen2.5:7b vs 远程 API glm-5.1/claude-sonnet）：

### Infer 质量

| 维度 | 本地 qwen2.5:7b | 远程 API (claude-sonnet/glm-5.1) |
|------|-----------------|----------------------------------|
| 中文保持率 | ~70%，约 30% 被翻译为英文 | ~95%，偶尔漂移 |
| 细节保留 | 经常丢失模块名/字段名 | 较好保留，但仍会适度泛化 |
| 输出格式 | 不稳定，有时不遵守 JSON 约束 | 稳定遵守格式约束 |
| 延迟 | 3-8秒（取决于硬件） | 1-2秒 |

### Merge 去重判断

| 维度 | 本地 qwen2.5:7b | 远程 API |
|------|-----------------|----------|
| 准确率 | 约 60%，偶尔误判为 DROP_NEW | 约 85%，判断更精准 |
| JSON 输出 | 经常输出非 JSON，需 fallback 解析 | 稳定输出 JSON |
| 风险 | 误删概率较高（宁多勿删兜底） | 误删概率低 |

### 实际建议

- **生产环境**：主配置用远程 API，备用配置用本地 Ollama（LLM 兜底机制自动切换）
- **写入**：全部 `infer=false`，AI 写什么存什么
- **混合方案**：嵌入始终用本地 Ollama bge-m3（无需 API，效果稳定），LLM 仅用于 E 策略 merge 去重

## 已知限制

### AnthropicLLM provider 丢弃 response_format

mem0 的 `AnthropicLLM` provider 不会将 `response_format` 参数传递给底层 API 调用。这意味着：

- `infer=true` 场景：mem0 内部抽取流程期望 JSON 输出，但 AnthropicLLM 不强制 JSON 格式
- `merge` 场景（E 策略）：`advise_merge()` 使用 `response_format={'type': 'json_object'}`，但 AnthropicLLM 会忽略此参数，导致输出可能是自由文本而非 JSON

**影响范围**：所有使用 `provider: "anthropic"` 的 infer/merge 流程

**解决方案**：
1. 使用 `provider: "openai"` 替代（OpenAI provider 正确传递 response_format）
2. `mem0_add_policy.py` 中的 `_parse_merge_response()` 已内置 fallback 解析（正则提取 JSON），可处理大部分非 JSON 输出
3. 等 mem0 官方修复此 bug

### 其他限制

- Chroma metadata 仅支持 str/int/float 标量值，嵌套 dict 需序列化为 JSON 字符串
- 多表同步依赖 `memory_sync`；Chroma 不在 SQLite 事务内，极端失败时查 `sync_pending/`
- Hook 超时默认 20 秒，Ollama 响应慢时可能超时
- Ollama 未启动时 MCP 无法初始化（embedding 硬依赖 localhost:11434）
- 库变大（数千条）时可评估 SQLite FTS5 / rerank（TODO #43）

## 相关文档

- [每日复盘集成](daily-review-integration.md) — cron、pending、快照 diff、会话来源、进化提取
- [mem_viewer 设计规格](mem-viewer-design.md) — Web UI API、检索条数、grooming 面板
- [Claude Code 集成](claude-code-setup.md)
- [Cursor 集成](cursor-setup.md)