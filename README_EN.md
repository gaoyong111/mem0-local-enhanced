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

- **MCP tools**: add / search / get_all / delete / **retry_pending** / **confirm_grooming**
- **Pending fallback**: `~/.mem0/pending/` for failed adds; `~/.mem0/sync_pending/` for multi-table sync failures (both retried by `retry_pending`)
- **LLM fallback**: auto-switch to `config_ollama.json` when primary config fails
- **Hooks**: Claude `UserPromptSubmit` + Cursor `beforeSubmitPrompt` (max=5 per injection)
- **mem_viewer**: Flask + vis.js graph; category filter/color, hybrid search panel (score+source, max=8), lineage timeline; **manual add/edit**; **episodic pending** filter (orange border), AI grooming panel (confirm keep / adopt delete-promote / merge with re-validation)
- **Episodic human-in-the-loop grooming**: new episodic gets `grooming_pending=1`; AI writes keep/delete/promote only (merge in session `grooming-merge-hints.json`); user confirms in viewer or chat — no auto delete/merge/promote
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
- `pip install -r requirements.txt` (or `pip install mem0ai chromadb mcp flask`)

> **Ollama is required.** MCP fails to start if Ollama is down; add/search are unavailable.

### Install

**Option A: setup script (recommended)**

```bash
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced
bash scripts/setup.sh
```

**Option B: manual**

```bash
git clone https://github.com/gaoyong111/mem0-local-enhanced.git
cd mem0-local-enhanced

mkdir -p ~/.mem0
cp src/*.py ~/.mem0/
cp src/mem_viewer.sh ~/.mem0/
cp src/project_aliases.example.json ~/.mem0/project_aliases.json
cp configs/config_ollama.example.json ~/.mem0/config_local.json
# Or API: cp configs/config_api.example.json ~/.mem0/config_local.json
cp .env.example ~/.mem0/.env   # optional

mkdir -p ~/.claude/skills/daily-review/scripts
cp scripts/review_helpers.py ~/.claude/skills/daily-review/scripts/
```

### Verify

```bash
curl http://localhost:11434/api/tags
python3 ~/.mem0/search_context.py "test query"
python3 ~/.mem0/mcp_server_local.py
python3 ~/.mem0/mem_viewer.py   # or bash ~/.mem0/mem_viewer.sh → http://localhost:8765
python3 scripts/review_helpers.py check-missed-run
python3 scripts/review_helpers.py snapshot
MEM0_DIR=~/.mem0 python3 scripts/episodic_grooming_run.py --dry-run
```

## IDE Integration

| IDE | Guide |
|-----|-------|
| Claude Code | [docs/claude-code-setup.md](docs/claude-code-setup.md) |
| Cursor | [docs/cursor-setup.md](docs/cursor-setup.md) |

Notes:
- Register MCP `mem0-local` → `~/.mem0/mcp_server_local.py`
- Hooks need **absolute python path** (pyenv); `hook_search.py` is the Claude compatibility entry
- All writes use **`infer=false`** (default; explicit `true` is ignored)

## Configuration

### Primary / fallback

Runtime loads **JSON** configs. `configs/config.example.yaml` is a YAML reference with the same content — **not loadable directly**.

```bash
cp configs/config_api.example.json ~/.mem0/config_local.json
cp configs/config_ollama.example.json ~/.mem0/config_ollama.json
```

### Pending queues

| Path | Purpose |
|------|---------|
| `~/.mem0/pending/` | Failed add writes |
| `~/.mem0/sync_pending/` | Multi-table sync failures |

### infer

**Permanently disabled.** All categories store verbatim content; explicit `infer=true` is ignored.

→ See [docs/architecture.md](docs/architecture.md)

## Web UI

`mem_viewer.py` — graph browse: category filter/color, hybrid search panel (same algorithm as MCP, max=8), lineage timeline, thickness metric, delete; **add/edit** memories; **episodic pending** grooming panel.

Batch script → `scripts/episodic_grooming_run.py`; protocol in [docs/architecture.md](docs/architecture.md).

Design spec → [docs/mem-viewer-design.md](docs/mem-viewer-design.md)

## Project Layout

See [README.md](README.md) for the full tree (Chinese). Key paths:

```text
src/           → deploy to ~/.mem0/
scripts/       → setup.sh, review_helpers.py, episodic_grooming_run.py, english_grooming_run.py
configs/       → JSON templates + config.example.yaml (reference only)
docs/          → architecture, IDE setup, daily review, mem-viewer-design
~/.mem0/       → runtime: pending/, sync_pending/, active_memories.db, deleted_archive.db, chroma_db/, ...
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEM0_DIR` | `~/.mem0` | Install directory |
| `MEM0_CONFIG` | `~/.mem0/config_local.json` | Primary config |
| `MEM0_FALLBACK_CONFIG` | `~/.mem0/config_ollama.json` | Fallback config |
| `MEM0_CHROMA_PATH` | `~/.mem0/chroma_db` | Chroma path |
| `MEM0_HISTORY_DB` | `~/.mem0/history.db` | History audit DB |
| `MEM0_ACTIVE_DB` | `~/.mem0/active_memories.db` | Active query DB (keyword source) |
| `MEM0_DELETED_DB` | `~/.mem0/deleted_archive.db` | Deletion archive |
| `MEM0_PROJECT_ALIASES` | `~/.mem0/project_aliases.json` | Dir name → project map |
| `MEM0_USER_ID` | `default-user` | User ID |
| `MEM0_DEFAULT_USER_ID` | same as `MEM0_USER_ID` | hybrid_search fallback |
| `MEM0_VECTOR_REL_MARGIN` | `0.10` | Vector relative cutoff; `0` disables |
| `MEM0_KW_REL_RATIO` | `0.25` | Keyword relative cutoff; `0` disables |

## Known Limitations

- Ollama must stay running (common failure after reboot)
- Cross-word synonyms rely on the vector path; no manual synonym table
- AnthropicLLM provider does not pass `response_format`
- Chroma metadata is scalar-only; nested dicts need `structured_json`
- Chroma `update` merges metadata; clearing fields requires `0`/empty string, not key deletion

## License

MIT
