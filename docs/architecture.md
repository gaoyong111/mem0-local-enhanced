# 架构详解：写入策略 B/C/D/E + 混合检索

[English version not yet available — refer to README_EN.md for overview]

## 写入策略

### B：分类分流（Category Routing）

不同类型的记忆应采用不同的存储策略：

| category | 存储模式 | 说明 |
|----------|---------|------|
| `reference` | verbatim（原样入库） | 事实/决策/约定类，保留原文，不做推断改写 |
| `preference` | inferred（推断抽取） | 偏好类，允许 LLM 抽取核心意图 |
| `workflow` | inferred | 流程类，允许 LLM 形式化 |
| `behavior` | inferred | 行为类，允许 LLM 归纳 |

`infer` 参数控制：
- 留空：按 category 自动判断
- `true`：强制开启推断
- `false`：强制关闭推断

### C：中文锁定（Chinese Language Lock）

mem0 默认会将中文记忆翻译成英文存储，导致中文检索命中率极低。C 策略通过两个层面锁定中文：

1. **Monkey-patch**：`apply_mem0_patches()` 在 Memory 初始化前修改 `mem0.configs.prompts.generate_additive_extraction_prompt`，强制传入 `use_input_language=True`
2. **Infer prompt**：`CHINESE_INFER_INSTRUCTIONS` 作为 `prompt` 参数传入 `memory.add()`，约束 LLM 输出语言与输入一致

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

## 混合检索

### 检索流程

```
用户查询
    │
    ├── 关键词检索（history.db）
    │   ├─ 提取中英文关键词
    │   ├─ 在最终记忆文本中匹配计分
    │   └─ 返回 top_k 结果
    │
    ├── 向量检索（Chroma + Ollama）
    │   ├─ Ollama bge-m3 生成查询向量
    │   ├─ Chroma 向量近邻搜索
    │   ├─ 过滤 score < 0.35 的低质量结果
    │   └─ 返回 top_k 结果
    │
    └── 合并排序（merge_and_rank）
        ├─ 去重合并（同一 ID 的关键词+向量结果加分）
        ├─ 项目记忆优先排序
        │   ├─ 项目记忆最多 max(3, max_results-2) 条
        │   ├─ 全局记忆最多 2 条
        │   └─ 合计不超过 max_results 条
        └─ 返回最终结果
```

### 关键词检索与 DELETE 幽灵记忆

`_load_final_memories()` 从 history.db 构建最终记忆文本时：
1. 先收集所有 `event=DELETE` 的 memory_id
2. 再遍历 ADD/UPDATE 行，**排除**已 DELETE 的 id

避免 mem0 删除后 ADD 行仍 `is_deleted=0` 导致关键词检索出现「幽灵记忆」。

### 关键词计分规则

- 每个关键词在记忆文本中的出现次数 × min(关键词长度, 5)
- 长关键词（≥5字符）权重更高，防止短关键词误召回
- 最终按总分排序，取 top_k

### 向量计分规则

- Chroma 返回的是 cosine distance
- score = 1.0 - distance / 2.0（近似 cosine similarity）
- 低于 `MIN_VECTOR_SCORE`（0.35）的结果被过滤

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
| `delete_memory` | 删除指定 ID 的记忆 |
| `retry_pending` | 扫描 `~/.mem0/pending/` 批量重试失败写入；≥3 次标记 `manual_review` |

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

MCP server 启动时：

1. 尝试加载主配置（`MEM0_CONFIG` 指定的路径）
2. 执行一次轻量 `get_all()` 验证 LLM 通路可用
3. 失败则尝试备用配置（`MEM0_FALLBACK_CONFIG`）
4. 两者都失败则抛出 RuntimeError

这样即使 API 挂了，也能自动切换到本地 Ollama 继续工作。

## 为什么 infer 默认按 category 自动（reference 默认 false）

`infer=true` 让 mem0 调用 LLM 对输入内容做"推断抽取"——提取核心意图、去重合并后存储。看起来更智能，但在实际使用中问题很大：

### infer=true 的三大风险

1. **信息丢失**：LLM 会"总结"而非"保留原文"。技术细节（模块名 `userService`、字段名 `loginType`、权限 ID）会被泛化成"某模块的某字段是字符串"，关键词检索命中率暴跌
2. **语言漂移**：即使有 C 策略（中文锁定），infer 仍可能将中文翻译为英文。实测中，`infer=true` + Ollama qwen2.5:7b 约有 30% 的记忆被翻译成英文
3. **格式不可控**：infer 的输出格式依赖 LLM 遵守 prompt 约束，不同模型遵守程度差异极大

### 为什么 reference 类型必须原样入库

reference 类记忆的核心价值是**精确可检索**。例如：

```
输入：userService 模块的 loginType 字段必须是字符串类型，传数字会导致登录接口报错

infer=true 输出：登录模块的登录类型应为字符串格式（信息丢失：模块名、字段名、错误场景）
verbatim 输出：原样保留全文（可按 userService/loginType/登录/字符串 精确命中）
```

### 偏好/行为类为什么可以 infer

这类记忆的核心是**意图**而非**细节**：
- "我喜欢简洁的回答，不要总结" → infer 提取为 "偏好：简洁回复风格"
- 即便丢失了一些措辞细节，核心意图不变，检索仍有效

### 推荐策略

| 场景 | infer | 原因 |
|------|-------|------|
| 技术约定/决策/bug记录 | `false` | 保留精确标识符 |
| 偏好/习惯/行为 | 留空(自动true) | 提取核心意图即可 |
| 项目级约定 | `false` | 项目名/字段名不可泛化 |

## 本地 LLM vs 远程 LLM 实测对比

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
- **纯本地环境**：建议对 reference 类严格 `infer=false`，避免本地模型的 infer 质量问题
- **混合方案**：嵌入始终用本地 Ollama bge-m3（无需 API，效果稳定），LLM 按场景选择

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
- history.db 中的 DELETE 事件可能不完整（依赖 mem0 内部行为），keyword_search 通过二次过滤已缓解
- Hook 超时默认 20 秒，Ollama 响应慢时可能超时
- Ollama 未启动时 MCP 无法初始化（embedding 硬依赖 localhost:11434）
- 中文关键词子串匹配弱（如「下雨」≠「下大雨」），待改进分词/向量权重

## 相关文档

- [每日复盘集成](daily-review-integration.md) — cron、pending、快照 diff、进化提取
- [Claude Code 集成](claude-code-setup.md)
- [Cursor 集成](cursor-setup.md)