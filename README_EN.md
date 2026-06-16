# mem0-local-enhanced

Local mem0 enhancement — hybrid search + smart write policies + three-layer injection + daily review loop + Web UI

[中文文档 → README.md](README.md)

---

## Why this project?

[mem0](https://github.com/mem0ai/mem0) is a solid memory system with an official MCP server. This project fills gaps in real-world Chinese + IDE workflows:

| Scenario | Official gap | Our enhancement |
|----------|--------------|-----------------|
| Chinese + camelCase search | Vector-only, poor keyword recall | **Hybrid search** (bge-m3 vectors primary + keyword for exact terms; weighted RRF fusion) |
| Chinese ingestion | infer English-translates / loses identifiers | **B/C/D/E policies**: category tags, language lock, structured format, LLM dedup; **infer permanently off** |
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

### Search & write

- **Hybrid search**: vector-first, then keyword (serial) → weighted RRF (K=15); Chinese queries exclude `lang=en`; keyword path supports conditional subsequence, non-overlapping hits, relative cutoff; project quota
- **Category tags (B)**: five canonical types (episodic/behavior/workflow/reference/preference); normalized on write; mem_viewer filter/color by category
- **Chinese lock (C)**: patch `use_input_language` (infer path disabled)
- **Structured format (D)**: `[module] field: rule（关键词: …）` improves recall
- **LLM dedup (E)**: keyword + Jaccard pre-filter, then KEEP/DROP_NEW; prefer keeping over deleting

### Runtime

- **MCP tools**: add / search / get_all / delete / **retry_pending**
- **Pending fallback** at `~/.mem0/pending/` (shared by MCP and daily review)
- **LLM fallback**: auto-switch to `config_ollama.json` when primary config fails
- **Hooks**: Claude `UserPromptSubmit` + Cursor `beforeSubmitPrompt`
- **mem_viewer**: Flask + vis.js graph; category filter/color, hybrid search panel (score+source), lineage timeline; **manual add/edit** (content re-embed, project/category), similar-memory warning before add
- **memory_lineage**: `lineage.jsonl` for MERGE/DEDUP/DELETE; grooming merges must include `merged_from`

### Daily review loop (Claude Code cron)

| Component | Path |
|-----------|------|
| Authoritative flow | `~/.claude/skills/daily-review/SKILL.md` |
| Cron trigger | `~/.claude/scheduled_tasks.json` (18:03, prompt references skill only) |
| Helper scripts | `scripts/review_helpers.py` (snapshot/diff/missed-run/resume log) |
| Output dir | `~/daily-reviews/` (review docs, TODO-tracker, mem0-snapshot-*.json) |

Preflight checks Ollama + MCP; **degraded mode** when unavailable (docs still generated, mem0 writes skipped).

## Quick Start

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai) running + `bge-m3` (`ollama pull bge-m3`)
- `pip install mem0ai chromadb mcp`

> **Ollama is required.** MCP fails to start if Ollama is down; add/search are unavailable.

### Install

```bash
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced

# Deploy to ~/.mem0/
cp src/*.py ~/.mem0/
cp src/project_aliases.example.json ~/.mem0/project_aliases.json
cp configs/config_ollama.example.json ~/.mem0/config_local.json
# Or API config: cp configs/config_api.example.json ~/.mem0/config_local.json

# Review helper (optional; skill dir is outside this repo)
mkdir -p ~/.claude/skills/daily-review/scripts
cp scripts/review_helpers.py ~/.claude/skills/daily-review/scripts/
```

### Verify

```bash
# Ollama
curl http://localhost:11434/api/tags

# Hybrid search
python3 ~/.mem0/search_context.py "test query"

# MCP (stdio, Ctrl+C to exit)
python3 ~/.mem0/mcp_server_local.py

# Web UI
python3 ~/.mem0/mem_viewer.py   # http://localhost:8765

# Review helper
python3 scripts/review_helpers.py check-missed-run
python3 scripts/review_helpers.py snapshot
```

## IDE Integration

| IDE | Guide |
|-----|-------|
| Claude Code | [docs/claude-code-setup.md](docs/claude-code-setup.md) |
| Cursor | [docs/cursor-setup.md](docs/cursor-setup.md) |

Notes:
- Register MCP `mem0-local` → `~/.mem0/mcp_server_local.py`
- Hooks need **absolute python path** (pyenv)
- All writes use **`infer=false`** (default; explicit `true` is ignored)

## Configuration

### Primary / fallback

```bash
# Primary: remote LLM (recommended)
cp configs/config_api.example.json ~/.mem0/config_local.json

# Fallback: Ollama-only (auto-switch when API fails)
cp configs/config_ollama.example.json ~/.mem0/config_ollama.json
```

### Pending queue

**Single path** `~/.mem0/pending/` — shared by MCP failures, daily review fallback, and retry_pending.

### infer

**Permanently disabled.** All categories store verbatim content; explicit `infer=true` is ignored.

→ See [docs/architecture.md](docs/architecture.md)

## Web UI

`mem_viewer.py` — graph browse: category filter/color, hybrid search panel (same algorithm as MCP), lineage timeline, thickness metric, delete; **add/edit** memories (`/api/add`, `/api/update`, re-embed on content change; `/api/similar` warning before add).

Design spec → [docs/superpowers/specs/2026-06-01-mem-viewer-design.md](docs/superpowers/specs/2026-06-01-mem-viewer-design.md)

## Project Layout

```text
mem0-local-enhanced/          # This repo (source + docs)
├── src/                      # Deploy to ~/.mem0/
│   ├── mcp_server_local.py
│   ├── mem0_add_policy.py
│   ├── hybrid_search.py
│   ├── mem0_hook.py
│   ├── mem_viewer.py
│   ├── memory_lineage.py
│   └── ...
├── scripts/
│   └── review_helpers.py     # Review: snapshot/diff/missed-run/resume log
├── configs/                  # Config templates
└── docs/
    ├── architecture.md
    ├── daily-review-integration.md
    ├── claude-code-setup.md
    └── cursor-setup.md

~/.mem0/                      # Runtime (install target)
├── pending/                  # Failed-write queue
├── chroma_db/
└── history.db

~/.claude/skills/daily-review/  # Review flow (external, used with this repo)
~/daily-reviews/                # Review output
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEM0_DIR` | `~/.mem0` | Install directory |
| `MEM0_CONFIG` | `~/.mem0/config_local.json` | Primary config |
| `MEM0_FALLBACK_CONFIG` | `~/.mem0/config_ollama.json` | Fallback config |
| `MEM0_USER_ID` | `default-user` | User ID |
| `MEM0_VECTOR_REL_MARGIN` | `0.10` | Vector relative cutoff; set `0` to disable |
| `MEM0_KW_REL_RATIO` | `0.25` | Keyword relative cutoff; set `0` to disable |

## Known Limitations

- Ollama must stay running (common failure after reboot; configure login item or manual start)
- Cross-word synonyms (e.g. 淋雨↔下雨) rely on the vector path; no manual synonym table
- AnthropicLLM provider does not pass `response_format` (affects infer/merge if re-enabled)
- Chroma metadata is scalar-only; nested dicts need `structured_json` serialization

## License

MIT
