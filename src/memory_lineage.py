"""mem0 记忆演变留痕：追加式 lineage 日志 + history.db 时间线合并查询。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

LINEAGE_PATH = os.path.expanduser('~/.mem0/lineage.jsonl')
HISTORY_DB = os.path.expanduser('~/.mem0/history.db')

VALID_ACTIONS = frozenset({
    'ADD',
    'UPDATE',
    'DELETE',
    'MERGE',
    'DEDUP_DROP',
    'CATEGORY_CHANGE',
    'GROOMING',
})


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def record_event(
    action: str,
    memory_id: str,
    *,
    source_ids: list[str] | None = None,
    target_id: str = '',
    category: str = '',
    note: str = '',
    content_preview: str = '',
    actor: str = 'system',
) -> dict[str, Any]:
    """追加一条演变记录到 lineage.jsonl。"""
    action_key = action.strip().upper()
    if action_key not in VALID_ACTIONS:
        action_key = 'GROOMING'

    entry: dict[str, Any] = {
        'ts': _now_iso(),
        'action': action_key,
        'memory_id': memory_id,
        'source_ids': [sid for sid in (source_ids or []) if sid],
        'target_id': target_id or '',
        'category': category or '',
        'note': note or '',
        'content_preview': (content_preview or '')[:200],
        'actor': actor or 'system',
    }

    os.makedirs(os.path.dirname(LINEAGE_PATH), exist_ok=True)
    with open(LINEAGE_PATH, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')

    return entry


def _load_lineage_entries() -> list[dict[str, Any]]:
    if not os.path.isfile(LINEAGE_PATH):
        return []

    entries: list[dict[str, Any]] = []
    with open(LINEAGE_PATH, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    entries.append(payload)
            except json.JSONDecodeError:
                continue
    return entries


def _history_events(memory_id: str) -> list[dict[str, Any]]:
    if not os.path.isfile(HISTORY_DB):
        return []

    conn = sqlite3.connect(HISTORY_DB)
    try:
        rows = conn.execute(
            """
            SELECT event, old_memory, new_memory, created_at
            FROM history
            WHERE memory_id = ?
            ORDER BY created_at ASC
            """,
            (memory_id,),
        ).fetchall()
    finally:
        conn.close()

    events: list[dict[str, Any]] = []
    for event, old_memory, new_memory, created_at in rows:
        action = str(event or '').upper()
        if action == 'ADD':
            timeline_action = 'ADD'
        elif action == 'UPDATE':
            timeline_action = 'UPDATE'
        elif action == 'DELETE':
            timeline_action = 'DELETE'
        else:
            timeline_action = action or 'HISTORY'

        events.append({
            'ts': created_at or '',
            'action': timeline_action,
            'memory_id': memory_id,
            'source_ids': [],
            'target_id': '',
            'category': '',
            'note': 'history.db',
            'content_preview': (new_memory or old_memory or '')[:200],
            'actor': 'mem0',
            'origin': 'history',
        })
    return events


def _lineage_related_entries(memory_id: str, all_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    related: list[dict[str, Any]] = []
    for entry in all_entries:
        if entry.get('memory_id') == memory_id:
            entry = dict(entry)
            entry.setdefault('origin', 'lineage')
            related.append(entry)
            continue
        if entry.get('target_id') == memory_id:
            entry = dict(entry)
            entry.setdefault('origin', 'lineage')
            related.append(entry)
            continue
        source_ids = entry.get('source_ids') or []
        if memory_id in source_ids:
            entry = dict(entry)
            entry.setdefault('origin', 'lineage')
            related.append(entry)
    return related


def _chroma_merged_from(memory_id: str) -> list[str]:
    """从 Chroma metadata 读取 merged_from（兼容 grooming 前已写入的合并标记）。"""
    try:
        import chromadb
        from hybrid_search import CHROMA_DB_PATH

        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        result = col.get(ids=[memory_id], include=['metadatas'])
        metas = result.get('metadatas') or []
        if not metas or not metas[0]:
            return []
        return parse_merged_from(metas[0])
    except Exception:
        return []


def collect_ancestor_ids(memory_id: str, *, max_depth: int = 8) -> list[str]:
    """沿 lineage MERGE 记录的 source_ids 向上追溯祖先 ID。"""
    entries = _load_lineage_entries()
    ancestors: list[str] = []
    seen = {memory_id}

    for source_id in _chroma_merged_from(memory_id):
        if source_id and source_id not in seen:
            seen.add(source_id)
            ancestors.append(source_id)

    frontier = list(ancestors) or [memory_id]
    if not frontier:
        frontier = [memory_id]

    for _ in range(max_depth):
        if not frontier:
            break
        next_frontier: list[str] = []
        for current_id in frontier:
            for entry in entries:
                if entry.get('memory_id') != current_id:
                    continue
                if entry.get('action') not in ('MERGE', 'GROOMING'):
                    continue
                for source_id in entry.get('source_ids') or []:
                    if not source_id or source_id in seen:
                        continue
                    seen.add(source_id)
                    ancestors.append(source_id)
                    next_frontier.append(source_id)
        frontier = next_frontier

    return ancestors


def get_direct_merge_source_ids(memory_id: str) -> list[str]:
    """本条记忆的直接合并来源 ID（不展开整条祖先链）。"""
    result: list[str] = []

    def add(source_id: str) -> None:
        if source_id and source_id not in result:
            result.append(source_id)

    for source_id in _chroma_merged_from(memory_id):
        add(source_id)

    for entry in _load_lineage_entries():
        if entry.get('memory_id') != memory_id:
            continue
        if entry.get('action') not in ('MERGE', 'GROOMING'):
            continue
        for source_id in entry.get('source_ids') or []:
            add(str(source_id or ''))

    return result


def resolve_memory_snapshot(memory_id: str) -> dict[str, Any]:
    """读取记忆快照：优先 active，其次 deleted_archive，最后 history 预览。"""
    from memory_delete import get_deleted_record
    from memory_sync import get_active_record

    base: dict[str, Any] = {
        'id': memory_id,
        'status': 'missing',
        'content': '',
        'project': '',
        'category': '',
        'deleted_at': '',
        'reason': '',
        'note': '',
    }

    active = get_active_record(memory_id)
    if active:
        return {
            **base,
            'status': 'active',
            'content': str(active.get('content', '') or ''),
            'project': str(active.get('project', '') or ''),
            'category': str(active.get('category', '') or ''),
        }

    deleted = get_deleted_record(memory_id)
    if deleted:
        return {
            **base,
            'status': 'deleted',
            'content': str(deleted.get('content', '') or ''),
            'project': str(deleted.get('project', '') or ''),
            'category': str(deleted.get('category', '') or ''),
            'deleted_at': str(deleted.get('deleted_at', '') or ''),
            'reason': str(deleted.get('reason', '') or ''),
        }

    for event in reversed(_history_events(memory_id)):
        preview = str(event.get('content_preview', '') or '')
        if preview:
            return {
                **base,
                'status': 'history',
                'content': preview,
                'note': '仅 history.db 预览，完整正文可能已不可恢复',
            }

    return base


def build_merge_source_tree(
    memory_id: str,
    *,
    visited: set[str] | None = None,
) -> list[dict[str, Any]]:
    """递归构建合并来源树；来源若本身由合并产生，继续展开子来源。"""
    visited = visited or set()
    if memory_id in visited:
        return []
    visited.add(memory_id)

    tree: list[dict[str, Any]] = []
    for source_id in get_direct_merge_source_ids(memory_id):
        if source_id in visited:
            continue
        node = resolve_memory_snapshot(source_id)
        child_visited = set(visited)
        node['sources'] = build_merge_source_tree(source_id, visited=child_visited)
        tree.append(node)
    return tree


def build_timeline(memory_id: str, *, include_ancestors: bool = True) -> dict[str, Any]:
    """合并 history.db、lineage.jsonl 与 deleted_archive.db，返回时间线及上游 ID。"""
    lineage_entries = _load_lineage_entries()
    timeline = _history_events(memory_id)
    timeline.extend(_lineage_related_entries(memory_id, lineage_entries))

    try:
        from memory_delete import get_deleted_record

        archived = get_deleted_record(memory_id)
        if archived:
            timeline.append({
                'ts': archived.get('deleted_at', ''),
                'action': 'DELETE',
                'memory_id': memory_id,
                'source_ids': [],
                'target_id': '',
                'category': archived.get('category', ''),
                'note': archived.get('reason', ''),
                'content_preview': (archived.get('content', '') or '')[:200],
                'actor': archived.get('actor', ''),
                'origin': 'deleted_archive',
            })
    except Exception:
        pass

    seen_keys: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in timeline:
        key = (item.get('ts', ''), item.get('action', ''), item.get('memory_id', ''))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(item)

    deduped.sort(key=lambda row: row.get('ts', ''), reverse=True)

    ancestors = collect_ancestor_ids(memory_id) if include_ancestors else []
    ancestor_timelines: dict[str, list[dict[str, Any]]] = {}
    if include_ancestors:
        for ancestor_id in ancestors[:20]:
            ancestor_timelines[ancestor_id] = build_timeline(
                ancestor_id,
                include_ancestors=False,
            )['events']

    return {
        'memory_id': memory_id,
        'events': deduped,
        'ancestor_ids': ancestors,
        'ancestor_timelines': ancestor_timelines,
        'merge_sources': build_merge_source_tree(memory_id),
    }


def parse_merged_from(metadata: dict[str, Any]) -> list[str]:
    """从 metadata 解析 merged_from（支持逗号分隔或 JSON 数组字符串）。"""
    raw = metadata.get('merged_from') or metadata.get('merged_from_json') or ''
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if not raw:
        return []
    text = str(raw).strip()
    if text.startswith('['):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in text.split(',') if part.strip()]


def record_merge_result(
    result_id: str,
    source_ids: list[str],
    *,
    category: str = '',
    note: str = '',
    content_preview: str = '',
    actor: str = 'grooming',
) -> dict[str, Any]:
    """grooming 合并后留痕：结果 ID + 来源 IDs。"""
    return record_event(
        'MERGE',
        result_id,
        source_ids=source_ids,
        category=category,
        note=note or f'合并 {len(source_ids)} 条来源记忆',
        content_preview=content_preview,
        actor=actor,
    )


def record_dedup_drop(
    dropped_id: str,
    kept_id: str,
    *,
    note: str = '',
    content_preview: str = '',
) -> dict[str, Any]:
    """E 策略 DROP_NEW 留痕。"""
    return record_event(
        'DEDUP_DROP',
        dropped_id,
        source_ids=[dropped_id],
        target_id=kept_id,
        note=note or '写入去重：新记忆与已有记忆重复',
        content_preview=content_preview,
        actor='merge_check',
    )
