"""已删除记忆归档：deleted_archive.db 与 history.db 分流，检索只读 deleted_ids；删除必填 reason。"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Callable

from memory_lineage import record_event

DELETED_DB = os.path.expanduser('~/.mem0/deleted_archive.db')
HISTORY_DB = os.path.expanduser('~/.mem0/history.db')
CHROMA_DB_PATH = os.path.expanduser('~/.mem0/chroma_db')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_memories (
    memory_id TEXT PRIMARY KEY,
    content TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    source TEXT NOT NULL DEFAULT '',
    migrated_from_history INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deleted_memories_deleted_at ON deleted_memories(deleted_at);
"""


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def ensure_schema() -> None:
    """初始化 deleted_archive.db 表结构。"""
    os.makedirs(os.path.dirname(DELETED_DB), exist_ok=True)
    conn = sqlite3.connect(DELETED_DB)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    ensure_schema()
    return sqlite3.connect(DELETED_DB)


def load_deleted_ids() -> set[str]:
    """返回已归档删除的 memory_id 集合；首次调用时从 history.db 迁移存量 DELETE。"""
    migrate_history_deletes_if_needed()
    conn = _connect()
    try:
        rows = conn.execute('SELECT memory_id FROM deleted_memories').fetchall()
    finally:
        conn.close()
    return {memory_id for memory_id, in rows if memory_id}


def get_deleted_record(memory_id: str) -> dict[str, Any] | None:
    """读取单条删除归档（含 reason / deleted_at / actor）。"""
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT memory_id, content, project, category, reason, deleted_at, actor, source
            FROM deleted_memories
            WHERE memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        'memory_id': row[0],
        'content': row[1],
        'project': row[2],
        'category': row[3],
        'reason': row[4],
        'deleted_at': row[5],
        'actor': row[6],
        'source': row[7],
    }


def _snapshot_from_chroma(memory_id: str) -> dict[str, str]:
    try:
        import chromadb

        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        raw = col.get(ids=[memory_id], include=['metadatas'])
        ids = raw.get('ids') or []
        if not ids:
            return {}
        meta = (raw.get('metadatas') or [None])[0] or {}
        return {
            'content': str(meta.get('data', '') or ''),
            'project': str(meta.get('project', '') or ''),
            'category': str(meta.get('category', '') or ''),
        }
    except Exception:
        return {}


def _snapshot_from_history(memory_id: str) -> dict[str, str]:
    if not os.path.isfile(HISTORY_DB):
        return {}
    conn = sqlite3.connect(HISTORY_DB)
    try:
        rows = conn.execute(
            """
            SELECT new_memory, old_memory, event
            FROM history
            WHERE memory_id = ? AND is_deleted = 0
            ORDER BY created_at DESC
            """,
            (memory_id,),
        ).fetchall()
    finally:
        conn.close()

    for new_memory, old_memory, event in rows:
        text = (new_memory or old_memory or '').strip()
        if text:
            return {'content': text, 'project': '', 'category': ''}

    conn = sqlite3.connect(HISTORY_DB)
    try:
        row = conn.execute(
            """
            SELECT old_memory, new_memory
            FROM history
            WHERE memory_id = ? AND event = 'DELETE'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (memory_id,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        text = (row[0] or row[1] or '').strip()
        if text:
            return {'content': text, 'project': '', 'category': ''}
    return {}


def _snapshot_memory(memory_id: str) -> dict[str, str]:
    snap = _snapshot_from_chroma(memory_id)
    if snap.get('content'):
        return snap
    return _snapshot_from_history(memory_id)


def migrate_history_deletes_if_needed() -> dict[str, int]:
    """一次性把 history.db 中已有 DELETE 事件迁入 deleted_archive.db。"""
    ensure_schema()
    if not os.path.isfile(HISTORY_DB):
        return {'skipped': 1}

    conn_archive = _connect()
    try:
        existing = conn_archive.execute('SELECT COUNT(*) FROM deleted_memories').fetchone()[0]
        if existing > 0:
            return {'skipped': 1, 'existing': existing}

        conn_history = sqlite3.connect(HISTORY_DB)
        try:
            rows = conn_history.execute(
                """
                SELECT memory_id, old_memory, new_memory, created_at
                FROM history
                WHERE event = 'DELETE' AND memory_id IS NOT NULL
                ORDER BY created_at ASC
                """
            ).fetchall()
        finally:
            conn_history.close()

        # 同一 memory_id 可能有多条 DELETE，只保留最后一次
        latest_by_id: dict[str, tuple[str, str, str]] = {}
        for memory_id, old_memory, new_memory, created_at in rows:
            if not memory_id:
                continue
            latest_by_id[memory_id] = (old_memory or '', new_memory or '', created_at or '')

        stats = {'migrated': 0, 'total': len(latest_by_id), 'history_rows': len(rows)}
        for memory_id, (old_memory, new_memory, created_at) in latest_by_id.items():
            content = (old_memory or new_memory or '').strip()
            cursor = conn_archive.execute(
                """
                INSERT OR IGNORE INTO deleted_memories (
                    memory_id, content, project, category, reason,
                    deleted_at, actor, source, migrated_from_history
                ) VALUES (?, ?, '', '', ?, ?, 'migration', 'history.db', 1)
                """,
                (
                    memory_id,
                    content,
                    'history.db 存量 DELETE 事件迁移（无原始 reason）',
                    created_at or _now_iso(),
                ),
            )
            stats['migrated'] += cursor.rowcount
        conn_archive.commit()
        return stats
    finally:
        conn_archive.close()


def snapshot_memory(memory_id: str) -> dict[str, str]:
    """删除前抓取记忆快照（Chroma 优先，history 兜底）。"""
    return _snapshot_memory(memory_id)


def write_deleted_record(
    memory_id: str,
    reason: str,
    snap: dict[str, str],
    *,
    actor: str = 'system',
    source: str = '',
) -> dict[str, Any]:
    """写入 deleted_archive.db 并记录 lineage（Chroma/history 已由调用方处理）。"""
    memory_id = (memory_id or '').strip()
    reason = (reason or '').strip()
    if not memory_id:
        raise ValueError('memory_id 不能为空')
    if not reason:
        raise ValueError('删除必须填写 reason（删除原因）')

    if get_deleted_record(memory_id):
        return {'memory_id': memory_id, 'status': 'already_archived'}

    deleted_at = _now_iso()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO deleted_memories (
                memory_id, content, project, category, reason,
                deleted_at, actor, source, migrated_from_history
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                memory_id,
                snap.get('content', ''),
                snap.get('project', ''),
                snap.get('category', ''),
                reason,
                deleted_at,
                actor or 'system',
                source or '',
            ),
        )
        conn.commit()
    finally:
        conn.close()

    record_event(
        'DELETE',
        memory_id,
        note=reason,
        content_preview=snap.get('content', ''),
        actor=actor or source or 'system',
    )

    return {
        'memory_id': memory_id,
        'status': 'deleted',
        'deleted_at': deleted_at,
        'reason': reason,
    }


def archive_delete(
    memory_id: str,
    reason: str,
    *,
    actor: str = 'system',
    source: str = '',
    memory_delete_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """
    删除记忆（方案一：走 memory_sync 多表事务）。

    memory_delete_fn 已废弃，保留参数仅为兼容旧调用。
    """
    del memory_delete_fn
    from memory_sync import SyncError, execute_delete

    result = execute_delete(memory_id, reason, actor=actor, source=source)
    if result.detail == 'already_archived':
        return {'memory_id': memory_id, 'status': 'already_archived'}
    if not result.ok:
        raise SyncError(result.op, result.expected, result.counts, result.detail)
    return {
        'memory_id': memory_id,
        'status': 'deleted',
        'counts': result.counts,
        'reason': reason,
    }
