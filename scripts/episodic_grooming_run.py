#!/usr/bin/env python3
"""episodic 梳理批处理：AI 建议写入 metadata；merge 覆盖写入 grooming-merge-hints.json。"""

from __future__ import annotations

import argparse
import json
import os
import sys

_MEM0_DIR = os.getenv('MEM0_DIR', os.path.expanduser('~/.mem0'))
_SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
for path in (_MEM0_DIR, _SRC_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from grooming_episodic import (  # noqa: E402
    analyze_memory_grooming,
    apply_grooming_to_chroma_metadata,
    build_merge_hints,
)
from grooming_metadata import apply_grooming_pending, write_merge_hints  # noqa: E402
from hybrid_search import CHROMA_DB_PATH, hybrid_search, normalize_project  # noqa: E402
from mem0_add_policy import DEFAULT_CATEGORY, apply_mem0_patches, normalize_category  # noqa: E402

apply_mem0_patches()


def _load_grooming_from_chroma() -> dict[str, dict]:
    import chromadb

    from grooming_metadata import parse_grooming_fields

    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    col = client.get_collection('mem0')
    raw = col.get(include=['metadatas'])
    result: dict[str, dict] = {}
    for memory_id, meta in zip(raw.get('ids') or [], raw.get('metadatas') or []):
        if memory_id:
            result[memory_id] = parse_grooming_fields(meta or {})
    return result


def _load_memories() -> list[dict]:
    from memory_sync import load_active_memories, load_active_metadata

    text_map = load_active_memories()
    meta_map = load_active_metadata()
    grooming_map = _load_grooming_from_chroma()
    rows: list[dict] = []
    for memory_id, text in text_map.items():
        meta = meta_map.get(memory_id, {})
        grooming = grooming_map.get(memory_id, {})
        category = normalize_category(meta.get('category', '') or DEFAULT_CATEGORY)
        rows.append({
            'id': memory_id,
            'text': text,
            'project': normalize_project(meta.get('project', '') or ''),
            'category': category,
            'metadata': meta,
            'grooming': grooming,
        })
    return rows


def _load_llm():
    try:
        from mem0 import Memory

        config_path = os.getenv('MEM0_CONFIG', os.path.expanduser('~/.mem0/config_ollama.json'))
        with open(config_path, encoding='utf-8') as handle:
            config = json.load(handle)
        return Memory.from_config(config).llm
    except Exception as error:
        print(f'LLM 不可用，使用规则兜底: {error}')
        return None


def _update_chroma_metadata(memory_id: str, patches: dict) -> None:
    import chromadb

    from grooming_metadata import GROOMING_META_KEYS
    from mem0_add_policy import apply_category_metadata

    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    col = client.get_collection('mem0')
    raw = col.get(ids=[memory_id], include=['metadatas'])
    if not raw.get('ids'):
        raise ValueError(f'Chroma 不存在: {memory_id}')

    meta = dict((raw.get('metadatas') or [{}])[0] or {})
    for key, value in patches.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value

    apply_category_metadata(meta)
    clean = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
        else:
            clean[key] = str(value)
    col.update(ids=[memory_id], metadatas=[clean])


def _select_targets(memories: list[dict], *, all_episodic: bool) -> list[dict]:
    selected: list[dict] = []
    for memory in memories:
        if memory.get('category') != DEFAULT_CATEGORY:
            continue
        grooming = memory.get('grooming') or {}
        if all_episodic:
            selected.append(memory)
            continue
        if grooming.get('pending') or not grooming.get('at'):
            selected.append(memory)
    return selected


def run(*, all_episodic: bool = False, dry_run: bool = False) -> dict:
    memories = _load_memories()
    targets = _select_targets(memories, all_episodic=all_episodic)
    llm = None if dry_run else _load_llm()

    merge_source_memories = targets if all_episodic else [
        memory for memory in memories if memory.get('category') == DEFAULT_CATEGORY
    ]
    merge_hints = build_merge_hints(merge_source_memories, hybrid_search_fn=hybrid_search)

    results: list[dict] = []
    for memory in targets:
        decision, merge_candidates = analyze_memory_grooming(
            memory,
            llm=llm,
            hybrid_search_fn=hybrid_search,
        )
        meta_patch = apply_grooming_to_chroma_metadata({}, decision, set_pending=True)
        apply_grooming_pending(meta_patch, pending=True)

        row = {
            'id': memory['id'],
            'action': decision.action,
            'reason': decision.reason,
            'target_category': decision.target_category,
            'merge_candidates': len(merge_candidates),
        }
        results.append(row)

        if dry_run:
            print(json.dumps(row, ensure_ascii=False))
            continue

        _update_chroma_metadata(memory['id'], meta_patch)

    if not dry_run:
        from grooming_metadata import write_merge_hints

        write_merge_hints(merge_hints)

    summary = {
        'analyzed': len(results),
        'merge_hints': len(merge_hints),
        'dry_run': dry_run,
        'results': results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description='episodic grooming 批处理')
    parser.add_argument('--all-episodic', action='store_true', help='分析全部 episodic')
    parser.add_argument('--dry-run', action='store_true', help='只打印不写库')
    args = parser.parse_args()
    run(all_episodic=args.all_episodic, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
