# mem0-local-enhanced

Local mem0 enhancement — hybrid search + smart write policies + three-layer injection + daily review loop + Web UI

[中文文档 → README.md](README.md)

---

## Why this project?

[mem0](https://github.com/mem0ai/mem0) is a solid memory system with an official MCP server. This project fills gaps in real-world Chinese + IDE workflows:

| Scenario | Official gap | Our enhancement |
|----------|--------------|-----------------|
| Chinese + camelCase search | Vector-only, poor keyword recall | **Hybrid search** (history.db keywords + bge-m3 vectors) |
| Chinese ingestion | infer often English-translates content | **B/C/D/E policies**: routing, language lock, structured format, LLM dedup |
| IDE context | MCP exists, no per-input auto-inject | **Hooks + MCP** for Claude Code & Cursor |
| Ops | No write fallback, no review loop | **pending queue + daily cron + snapshot diff** |

## Architecture

```text
Runtime injection (L1 warm-start / L2 Hook / L3 MCP search)
        ↓
~/.mem0/  hybrid_search · mem0_add_policy · MCP · pending · mem_viewer
        ↓
Daily review (cron 18:03 + skill) → evolve memories → snapshot baseline
```

→ [docs/architecture.md](docs/architecture.md) · [docs/daily-review-integration.md](docs/daily-review-integration.md)

## Core Features

- **Hybrid search** with project scoping and DELETE ghost filtering
- **Write policies B/C/D/E** (category routing, Chinese lock, structured refs, LLM dedup)
- **MCP tools**: add / search / get_all / delete / **retry_pending**
- **Pending fallback** at `~/.mem0/pending/` (shared by MCP and daily review)
- **Hooks** for Claude Code & Cursor auto-injection
- **mem_viewer** Web UI (Flask + vis.js)
- **Daily review loop** with Preflight, degraded mode, mem0 snapshot/diff via `scripts/review_helpers.py`

## Quick Start

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai) running + `bge-m3`
- `pip install mem0ai chromadb mcp`

> **Ollama is required.** MCP fails to start if Ollama is down.

### Install

```bash
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced

cp src/*.py ~/.mem0/
cp configs/config_ollama.example.json ~/.mem0/config_local.json
cp scripts/review_helpers.py ~/.claude/skills/daily-review/scripts/  # optional
```

### Verify

```bash
curl http://localhost:11434/api/tags
python3 ~/.mem0/search_context.py "test query"
python3 scripts/review_helpers.py snapshot
```

## IDE Integration

| IDE | Guide |
|-----|-------|
| Claude Code | [docs/claude-code-setup.md](docs/claude-code-setup.md) |
| Cursor | [docs/cursor-setup.md](docs/cursor-setup.md) |

Use **absolute python paths** (pyenv). Write technical facts with **`infer=false`**.

## Configuration

- Primary: `config_local.json` (API LLM recommended)
- Fallback: `config_ollama.json` (auto-switch on primary failure)
- Pending queue: **`~/.mem0/pending/`** only

## Project Layout

```text
mem0-local-enhanced/
├── src/           → deploy to ~/.mem0/
├── scripts/       → review_helpers.py (snapshot/diff/missed-run)
├── configs/       → example configs
└── docs/          → architecture, daily-review, IDE setup
```

## Known Limitations

- Ollama must stay running (common failure after reboot)
- Weak Chinese substring matching in keyword search
- AnthropicLLM ignores `response_format` in mem0

## License

MIT
