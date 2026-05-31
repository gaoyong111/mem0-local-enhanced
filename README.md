# mem0-local-enhanced

本地 mem0 增强方案 —— 混合检索 + 智能写入策略 + Claude Code / Cursor 集成

[English documentation → README_EN.md](README_EN.md)

---

## 为什么需要这个？

[mem0](https://github.com/mem0ai/mem0) 是一个优秀的记忆管理系统，官方也提供了 MCP server 集成。但在以下场景中，官方方案的能力不足：

| 场景 | 官方方案的局限 | 本方案增强 |
|------|---------------|-----------|
| 中文+技术标识符检索 | mem0 仅向量检索，对中文关键词和 camelCase 标识符（如 `userService`、`loginType`）召回率不足 | **混合检索**（关键词 + 向量），项目记忆优先 |
| 中文记忆入库 | mem0 的 `use_input_language` 参数不可靠，infer 时中文常被翻译成英文；技术细节会被泛化丢失 | **B/C/D/E 四层写入策略**：分类分流、中文锁定、结构化格式、LLM 去重 |
| IDE 上下文自动注入 | mem0 有官方 MCP server，但缺少每次输入自动搜索并注入上下文的机制 | **增强 MCP server + Hook**，零干预自动注入 |

## 核心特性

- **混合检索**：关键词（history.db）+ 向量（Chroma/Ollama），双路召回后合并排序，项目记忆优先
- **智能写入策略**：
  - **B 分类分流**：reference 原样入库，preference/workflow/behavior 自动推断抽取
  - **C 中文锁定**：patch mem0 强制 `use_input_language`，infer prompt 约束中文输出
  - **D 结构化格式**：模块名/字段名/规则 → 固定模板格式，关键词检索命中率大幅提升
  - **E LLM 去重**：两层过滤（关键词分数 + token 重叠率）后提交 LLM 判断，宁多勿删
- **MCP server**：add / search / get_all / delete / retry_pending 五个工具，Claude Code 和 Cursor 直接调用
- **写入兜底**：add 失败时自动存入 pending 目录，retry_pending 工具可批量重试，超3次标记需人工介入
- **Hook 自动注入**：每次用户输入自动搜索相关记忆，注入上下文（Claude Code + Cursor）
- **LLM 兜底**：主配置失败自动切换到备用配置（比如 API 挂了切换到本地 Ollama）
- **项目级作用域**：自动从工作目录推断项目标识，项目记忆和全局记忆分层检索

## 快速开始

### 前置条件

- Python 3.10+
- [Ollama](https://ollama.ai) + `bge-m3` 嵌入模型（`ollama pull bge-m3`）
- mem0 Python 包（`pip install mem0ai`）
- ChromaDB（`pip install chromadb`）

### 安装步骤

```bash
# 1. Clone 仓库
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced

# 2. 复制源码到 ~/.mem0/
cp src/*.py ~/.mem0/
cp src/project_aliases.example.json ~/.mem0/project_aliases.json

# 3. 编辑配置（选择一个模板）
cp configs/config_ollama.example.json ~/.mem0/config_local.json
# 或者使用 API 版本：
# cp configs/config_api.example.json ~/.mem0/config_local.json

# 编辑 config_local.json，填入你的实际 API key、路径等

# 4. 编辑项目别名（可选）
# 编辑 ~/.mem0/project_aliases.json，填入你的项目目录名映射

# 5. 复制环境变量模板
cp .env.example ~/.mem0/.env
```

### 验证安装

```bash
# 测试混合检索
python3 ~/.mem0/search_context.py "测试查询"

# 测试 MCP server 启动
python3 ~/.mem0/mcp_server_local.py
```

## 配置说明

### 全本地配置（Ollama）

`configs/config_ollama.example.json` — LLM 和嵌入模型都使用本地 Ollama，无需 API key。

- 嵌入：`bge-m3`（需要 Ollama 运行）
- LLM：`qwen2.5:7b`（或其他本地模型）
- 向量存储：ChromaDB 本地

### API 配置（远程 LLM）

`configs/config_api.example.json` — 嵌入用本地 Ollama，LLM 用远程 API（Anthropic/OpenAI 兼容）。

- 嵌入：`bge-m3`（本地 Ollama）
- LLM：远程 API（需填入 `YOUR_API_KEY_HERE` 和 `your-api-host:port`）
- 向量存储：ChromaDB 本地

### 兜底机制

MCP server 启动时先尝试主配置（`config_local.json`），失败则自动切换到备用配置（`config_ollama.json`）。你可以同时配置两份：

```bash
# 主配置用 API（速度快，效果好）
cp configs/config_api.example.json ~/.mem0/config_local.json

# 备用配置用 Ollama（API 挂了时自动兜底）
cp configs/config_ollama.example.json ~/.mem0/config_ollama.json
```

### 写入失败兜底

当 mem0 写入失败（MCP 进程异常、LLM 不可用等）时，记忆自动存入 `~/.mem0/pending/` 目录。`retry_pending` 工具扫描该目录逐条重试：成功删除文件，失败累加 retry_count，超过3次标记 `manual_review`。

建议在每日复盘等定时任务中调用 `retry_pending` 清理积压。

### 为什么 reference 类型默认 infer=false？

`infer=true` 会让 LLM "总结"输入内容再入库，但会丢失技术细节（模块名、字段名、权限 ID 被泛化）并可能将中文翻译成英文。reference 类记忆的核心价值是**精确可检索**，原样入库才能保证关键词命中率。

| 场景 | 推荐 infer | 原因 |
|------|-----------|------|
| 技术约定/决策/bug | `false` | 保留精确标识符 |
| 偏好/习惯/行为 | 留空(自动true) | 提取核心意图即可 |

详见 → [docs/architecture.md](docs/architecture.md)

### 本地 vs 远程 LLM

| 维度 | 本地 Ollama | 远程 API |
|------|------------|---------|
| infer 中文保持率 | ~70% | ~95% |
| merge 去重准确率 | ~60% | ~85% |
| 延迟 | 3-8秒 | 1-2秒 |
| 成本 | 免费 | 按 token 计费 |

**推荐**：主配置用远程 API，备用配置用本地 Ollama（自动兜底）。嵌入始终用本地 bge-m3（免费、稳定）。

## Claude Code 集成

### MCP server 注册

在 `~/.claude.json` 的 `mcpServers` 中添加：

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

### Hook 自动注入

在 `~/.claude/settings.json` 的 `hooks.UserPromptSubmit` 中添加：

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

详细步骤见 → [docs/claude-code-setup.md](docs/claude-code-setup.md)

## Cursor 集成

在 `~/.cursor/mcp.json` 中添加 MCP server：

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

Cursor Hook 配置见 → [docs/cursor-setup.md](docs/cursor-setup.md)

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEM0_DIR` | `~/.mem0` | mem0 安装目录 |
| `MEM0_CONFIG` | `~/.mem0/config_local.json` | 主配置文件路径 |
| `MEM0_FALLBACK_CONFIG` | `~/.mem0/config_ollama.json` | 备用配置文件路径 |
| `MEM0_CHROMA_PATH` | `~/.mem0/chroma_db` | Chroma 向量数据库路径 |
| `MEM0_HISTORY_DB` | `~/.mem0/history.db` | history.db 路径 |
| `MEM0_USER_ID` | `default-user` | mem0 用户标识 |

## 写入策略详解

→ [docs/architecture.md](docs/architecture.md)

## 项目文件结构

```
~/.mem0/
├── mcp_server_local.py      # MCP server 主入口
├── mem0_add_policy.py       # 写入策略 B+C+D+E
├── hybrid_search.py         # 混合检索
├── mem0_hook.py             # Hook（Claude Code + Cursor）
├── hook_search.py           # Hook 入口包装
├── search_context.py        # CLI 调试工具
├── project_aliases.json     # 项目别名映射（自定义）
├── config_local.json        # 主配置（自定义）
├── config_ollama.json       # 备用配置（可选）
├── pending/                 # 写入失败待办队列（自动生成）
├── chroma_db/               # Chroma 向量数据库（自动生成）
├── history.db               # 记忆历史数据库（自动生成）
```

## License

MIT