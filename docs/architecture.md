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
metadata: {"category":"reference","structured":{"module":"pharmacyInquiry","field":"timeType","rule":"字符串","keywords":["问诊单","API"]}}
```

格式化结果：
```
[pharmacyInquiry] timeType: 字符串（关键词: 问诊单, API）
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

1. 先检查 `PROJECT_ALIASES` 映射（从 `~/.mem0/project_aliases.json` 加载）
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

## LLM 兜底机制

MCP server 启动时：

1. 尝试加载主配置（`MEM0_CONFIG` 指定的路径）
2. 执行一次轻量 `get_all()` 验证 LLM 通路可用
3. 失败则尝试备用配置（`MEM0_FALLBACK_CONFIG`）
4. 两者都失败则抛出 RuntimeError

这样即使 API 挂了，也能自动切换到本地 Ollama 继续工作。