# mem0-local-enhanced

A local mem0 enhancement — hybrid search + smart write policies + Web UI view

[中文文档 → README.md](README.md)

***

## Why this project?

[mem0](https://github.com/mem0ai/mem0) is a great memory management system with an official MCP server. But in certain scenarios, its capabilities fall short:

| Scenario                              | Official limitation                                                                                                      | Our enhancement                                                                              |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Chinese + technical identifier search | mem0 only does vector search — poor recall for Chinese keywords and camelCase identifiers (`userService`, `loginType`)   | **Hybrid search** (keyword + vector), project memory priority                                |
| Chinese memory ingestion              | mem0's `use_input_language` is unreliable — infer often translates Chinese to English; technical details get generalized | **B/C/D/E write policies**: category routing, Chinese lock, structured formatting, LLM dedup |
| IDE context auto-injection            | mem0 has an official MCP server, but lacks per-input auto-search and context injection                                   | **Enhanced MCP server + Hook**, zero-intervention auto-injection                             |

## Core Features

- **Hybrid search**: Keyword (history.db) + Vector (Chroma/Ollama), dual-path recall with merged ranking, project memory priority
- **Smart write policies**:
  - **B Category routing**: reference → verbatim, preference/workflow/behavior → infer extraction
  - **C Chinese lock**: patch mem0 to force `use_input_language`, infer prompt constrains Chinese output
  - **D Structured formatting**: module/field/rule → fixed template format, significantly improves keyword search hit rate
  - **E LLM dedup**: Two-layer filtering (keyword score + token overlap ratio) before LLM judgment, prefers keeping over deleting
- **MCP server**: add / search / get\_all / delete / retry\_pending — five tools, callable from Claude Code and Cursor
- **Write fallback**: Auto-saves failed writes to pending directory, retry\_pending tool for batch retry, marks for manual review after 3 failures
- **Visualization Web UI**: Graph-driven memory browsing interface, Flask + vis.js Network, zero npm
- **Hook auto-injection**: Auto-searches relevant memories on every user input, injects into context (Claude Code + Cursor)
- **LLM fallback**: Auto-switches to backup config when primary fails (e.g., API down → local Ollama)
- **Project scoping**: Auto-detects project identifier from working directory, layered recall (project + global)

## Quick Start

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai) + `bge-m3` embedding model (`ollama pull bge-m3`)
- mem0 Python package (`pip install mem0ai`)
- ChromaDB (`pip install chromadb`)

### Installation

```bash
# 1. Clone
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced

# 2. Copy source files
cp src/*.py ~/.mem0/
cp src/project_aliases.example.json ~/.mem0/project_aliases.json

# 3. Choose and edit config
cp configs/config_ollama.example.json ~/.mem0/config_local.json
# Or for API-based LLM:
# cp configs/config_api.example.json ~/.mem0/config_local.json

# Edit config_local.json with your actual API key, paths, etc.

# 4. Edit project aliases (optional)
# Edit ~/.mem0/project_aliases.json with your project directory mappings

# 5. Copy env template
cp .env.example ~/.mem0/.env
```

### Verify Installation

```bash
# Test hybrid search
python3 ~/.mem0/search_context.py "test query"

# Test MCP server startup
python3 ~/.mem0/mcp_server_local.py
```

## Configuration

### Full local (Ollama)

`configs/config_ollama.example.json` — Both LLM and embedding use local Ollama, no API key needed.

- Embedding: `bge-m3` (requires Ollama running)
- LLM: `qwen2.5:7b` (or other local model)
- Vector store: ChromaDB local

### API-based LLM

`configs/config_api.example.json` — Embedding uses local Ollama, LLM uses remote API (Anthropic/OpenAI compatible).

- Embedding: `bge-m3` (local Ollama)
- LLM: Remote API (fill in `YOUR_API_KEY_HERE` and `your-api-host:port`)
- Vector store: ChromaDB local

### Fallback mechanism

MCP server tries primary config (`config_local.json`) first, auto-switches to backup (`config_ollama.json`) on failure:

```bash
# Primary: API (fast, high quality)
cp configs/config_api.example.json ~/.mem0/config_local.json

# Backup: Ollama (auto-fallback when API is down)
cp configs/config_ollama.example.json ~/.mem0/config_ollama.json
```

### Why is `infer=false` default for reference type?

`infer=true` lets LLM "summarize" input before storage, but loses technical details (module names, field names, permission IDs get generalized) and may translate Chinese to English. The core value of reference memories is **precise retrievability** — storing verbatim ensures keyword hit rate.

| Scenario                             | Recommended infer       | Reason                     |
| ------------------------------------ | ----------------------- | -------------------------- |
| Technical conventions/decisions/bugs | `false`                 | Preserve exact identifiers |
| Preferences/habits/behaviors         | Leave blank (auto true) | Extract core intent only   |

See → [docs/architecture.md](docs/architecture.md) for details.

### Local vs Remote LLM

| Dimension              | Local Ollama | Remote API    |
| ---------------------- | ------------ | ------------- |
| Chinese retention rate | \~70%        | \~95%         |
| Merge dedup accuracy   | \~60%        | \~85%         |
| Latency                | 3-8s         | 1-2s          |
| Cost                   | Free         | Pay per token |

**Recommendation**: Use remote API as primary (fast, high quality), local Ollama as backup (auto-fallback). Always use local bge-m3 for embedding (free, stable).

## Claude Code Integration

### MCP server registration

Add to `~/.claude.json` `mcpServers`:

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

### Hook auto-injection

Add to `~/.claude/settings.json` `hooks.UserPromptSubmit`:

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
                        "statusMessage": "Searching mem0 memories"
                    }
                ]
            }
        ]
    }
}
```

See → [docs/claude-code-setup.md](docs/claude-code-setup.md) for detailed steps.

## Cursor Integration

Add MCP server to `~/.cursor/mcp.json`:

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

See → [docs/cursor-setup.md](docs/cursor-setup.md) for Hook configuration.

## Environment Variables

| Variable               | Default                      | Description                 |
| ---------------------- | ---------------------------- | --------------------------- |
| `MEM0_DIR`             | `~/.mem0`                    | mem0 installation directory |
| `MEM0_CONFIG`          | `~/.mem0/config_local.json`  | Primary config path         |
| `MEM0_FALLBACK_CONFIG` | `~/.mem0/config_ollama.json` | Fallback config path        |
| `MEM0_CHROMA_PATH`     | `~/.mem0/chroma_db`          | Chroma vector DB path       |
| `MEM0_HISTORY_DB`      | `~/.mem0/history.db`         | history.db path             |
| `MEM0_USER_ID`         | `default-user`               | mem0 user identifier        |

## Write Policy Details

→ [docs/architecture.md](docs/architecture.md)

## Project Structure

```
~/.mem0/
├── mcp_server_local.py      # MCP server main entry
├── mem0_add_policy.py       # Write policies B+C+D+E
├── hybrid_search.py         # Hybrid search
├── mem_viewer.py            # Visualization Web UI (graph-driven)
├── mem_viewer.sh            # Visualization startup script
├── mem0_hook.py             # Hook (Claude Code + Cursor)
├── hook_search.py           # Hook entry wrapper
├── search_context.py        # CLI debug tool
├── project_aliases.json     # Project alias mappings (customize)
├── config_local.json        # Primary config (customize)
├── config_ollama.json       # Backup config (optional)
├── pending/                 # Write failure pending queue (auto-generated)
├── chroma_db/               # Chroma vector database (auto-generated)
├── history.db               # Memory history database (auto-generated)
```

## License

MIT
