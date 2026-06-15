"""方案一：active_memories 查询库 + history 追溯；多表同步事务与 pending 重试。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from memory_lineage import record_event

ACTIVE_DB = os.path.expanduser('~/.mem0/active_memories.db')
HISTORY_DB = os.path.expanduser('~/.mem0/history.db')
CHROMA_DB_PATH = os.path.expanduser('~/.mem0/chroma_db')
SYNC_PENDING_DIR = os.path.expanduser('~/.mem0/sync_pending')
MAX_SYNC_RETRY = 3

from memory_delete import (  # noqa: E402
    DELETED_DB,
    ensure_schema as ensure_deleted_schema,
    get_deleted_record,
    load_deleted_ids,
    migrate_history_deletes_if_needed,
    snapshot_memory,
)

_ACTIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_memories (
    memory_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    lang TEXT NOT NULL DEFAULT 'zh',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_active_memories_project ON active_memories(project);
"""

# 各操作期望的 SQLite/Chroma 行数变动（固定值，用于事务校验）
EXPECTED_COUNTS: dict[str, dict[str, int]] = {
    'delete': {
        'active_delete': 1,
        'archive_insert': 1,
        'history_insert': 1,
        'chroma_delete': 1,
    },
    'active_insert': {
        'active_insert': 1,
    },
    'active_update': {
        'active_update': 1,
    },
    'active_meta_update': {
        'active_update': 1,
    },
}


class SyncError(Exception):
    """多表同步行数不符合预期。"""

    def __init__(self, op: str, expected: dict[str, int], actual: dict[str, int], step: str = ''):
        self.op = op
        self.expected = expected
        self.actual = actual
        self.step = step
        super().__init__(f'sync {op} failed at {step}: expected={expected} actual={actual}')


@dataclass
class SyncResult:
    """事务执行结果，含各表行数。"""

    op: str
    memory_id: str
    ok: bool
    counts: dict[str, int] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=dict)
    pending_path: str = ''
    detail: str = ''


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def ensure_active_schema() -> None:
    os.makedirs(os.path.dirname(ACTIVE_DB), exist_ok=True)
    conn = sqlite3.connect(ACTIVE_DB)
    try:
        conn.executescript(_ACTIVE_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    ensure_deleted_schema()


def _counts_match(op: str, actual: dict[str, int], *, phase: str = 'all') -> bool:
    expected = EXPECTED_COUNTS.get(op, {})
    if phase != 'all':
        expected = {key: value for key, value in expected.items() if key.startswith(phase) or phase in key}
    for key, want in expected.items():
        if key == 'chroma_delete':
            continue
        if actual.get(key, -1) != want:
            return False
    return True


def _attach_connection() -> sqlite3.Connection:
    ensure_active_schema()
    conn = sqlite3.connect(ACTIVE_DB)
    conn.execute(f"ATTACH DATABASE '{HISTORY_DB}' AS hist")
    conn.execute(f"ATTACH DATABASE '{DELETED_DB}' AS arch")
    return conn


def get_active_record(memory_id: str) -> dict[str, Any] | None:
    ensure_active_schema()
    migrate_active_if_needed()
    conn = sqlite3.connect(ACTIVE_DB)
    try:
        row = conn.execute(
            """
            SELECT memory_id, content, project, category, lang, created_at, updated_at
            FROM active_memories WHERE memory_id = ?
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
        'lang': row[4],
        'created_at': row[5],
        'updated_at': row[6],
    }


def load_active_memories() -> dict[str, str]:
    """keyword 检索数据源：仅 active_memories（方案一）。"""
    ensure_active_schema()
    migrate_active_if_needed()
    conn = sqlite3.connect(ACTIVE_DB)
    try:
        rows = conn.execute(
            'SELECT memory_id, content FROM active_memories ORDER BY updated_at DESC'
        ).fetchall()
    finally:
        conn.close()
    return {memory_id: content for memory_id, content in rows if memory_id and (content or '').strip()}


def load_active_metadata() -> dict[str, dict[str, str]]:
    ensure_active_schema()
    migrate_active_if_needed()
    conn = sqlite3.connect(ACTIVE_DB)
    try:
        rows = conn.execute(
            'SELECT memory_id, project, category, lang FROM active_memories'
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, dict[str, str]] = {}
    for memory_id, project, category, lang in rows:
        if not memory_id:
            continue
        result[memory_id] = {
            'project': project or '',
            'category': category or '',
            'lang': lang or 'zh',
        }
    return result


def migrate_active_if_needed() -> dict[str, Any]:
    """从 Chroma 构建 active_memories（排除 deleted_archive）。"""
    migrate_history_deletes_if_needed()
    ensure_active_schema()

    conn = sqlite3.connect(ACTIVE_DB)
    try:
        existing = conn.execute('SELECT COUNT(*) FROM active_memories').fetchone()[0]
        if existing > 0:
            return {'skipped': True, 'existing': existing}
    finally:
        conn.close()

    deleted = load_deleted_ids()
    try:
        import chromadb

        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        raw = col.get(include=['metadatas'])
    except Exception as error:
        return {'error': str(error)}

    ids = raw.get('ids') or []
    metas = raw.get('metadatas') or []
    now = _now_iso()
    inserted = 0

    conn = sqlite3.connect(ACTIVE_DB)
    try:
        for memory_id, meta in zip(ids, metas):
            if not memory_id or memory_id in deleted:
                continue
            meta = meta or {}
            content = str(meta.get('data', '') or '').strip()
            if not content:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO active_memories (
                    memory_id, content, project, category, lang, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    content,
                    str(meta.get('project', '') or ''),
                    str(meta.get('category', '') or ''),
                    str(meta.get('lang', '') or 'zh'),
                    now,
                    now,
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return {'migrated': inserted, 'total_chroma': len(ids)}


def _chroma_delete(memory_id: str) -> int:
    try:
        import chromadb

        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        existing = col.get(ids=[memory_id], include=[])
        if not existing.get('ids'):
            return 0
        col.delete(ids=[memory_id])
        after = col.get(ids=[memory_id], include=[])
        return 1 if not after.get('ids') else 0
    except Exception:
        return 0


def _write_sync_pending(payload: dict[str, Any]) -> str:
    os.makedirs(SYNC_PENDING_DIR, exist_ok=True)
    filename = f"{payload.get('op', 'sync')}_{payload.get('memory_id', uuid.uuid4().hex[:8])}_{int(time.time())}.json"
    path = os.path.join(SYNC_PENDING_DIR, filename)
    payload.setdefault('created_at', _now_iso())
    payload.setdefault('retry_count', 0)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def _sqlite_delete_phase(
    memory_id: str,
    reason: str,
    snap: dict[str, str],
    *,
    actor: str,
    source: str,
) -> dict[str, int]:
    """SQLite 三表同一事务：active 删 + archive 增 + history 追溯 DELETE。"""
    deleted_at = _now_iso()
    history_id = f'{memory_id}_del_{int(time.time())}'
    counts: dict[str, int] = {}

    conn = _attach_connection()
    try:
        conn.execute('BEGIN')
        cur = conn.execute('DELETE FROM active_memories WHERE memory_id = ?', (memory_id,))
        counts['active_delete'] = cur.rowcount

        cur = conn.execute(
            """
            INSERT INTO arch.deleted_memories (
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
        counts['archive_insert'] = cur.rowcount

        cur = conn.execute(
            """
            INSERT INTO hist.history (
                id, memory_id, old_memory, new_memory, event, created_at, is_deleted
            ) VALUES (?, ?, ?, NULL, 'DELETE', ?, 1)
            """,
            (
                history_id,
                memory_id,
                snap.get('content', ''),
                deleted_at,
            ),
        )
        counts['history_insert'] = cur.rowcount

        expected = EXPECTED_COUNTS['delete']
        for key in ('active_delete', 'archive_insert', 'history_insert'):
            if counts.get(key, -1) != expected[key]:
                conn.execute('ROLLBACK')
                raise SyncError('delete', expected, counts, step='sqlite')

        conn.commit()
    except SyncError:
        raise
    except Exception as error:
        conn.execute('ROLLBACK')
        raise SyncError('delete', EXPECTED_COUNTS['delete'], counts, step=f'sqlite:{error}') from error
    finally:
        conn.close()

    return counts


def _restore_active_after_chroma_failure(memory_id: str, snap: dict[str, str]) -> None:
    """Chroma 删除失败时还原 active 行并移除刚写入的 archive（history DELETE 保留作追溯）。"""
    now = _now_iso()
    conn = _attach_connection()
    try:
        conn.execute('BEGIN')
        conn.execute(
            """
            INSERT OR REPLACE INTO active_memories (
                memory_id, content, project, category, lang, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                snap.get('content', ''),
                snap.get('project', ''),
                snap.get('category', ''),
                snap.get('lang', 'zh'),
                snap.get('created_at', now),
                now,
            ),
        )
        conn.execute('DELETE FROM arch.deleted_memories WHERE memory_id = ?', (memory_id,))
        conn.commit()
    finally:
        conn.close()


def execute_delete(
    memory_id: str,
    reason: str,
    *,
    actor: str = 'system',
    source: str = '',
) -> SyncResult:
    """统一删除：事务同步 active + archive + history，再删 Chroma；失败写 sync_pending。"""
    memory_id = (memory_id or '').strip()
    reason = (reason or '').strip()
    if not memory_id:
        raise ValueError('memory_id 不能为空')
    if not reason:
        raise ValueError('删除必须填写 reason（删除原因）')

    if get_deleted_record(memory_id):
        return SyncResult(
            op='delete',
            memory_id=memory_id,
            ok=True,
            detail='already_archived',
        )

    migrate_active_if_needed()

    snap = get_active_record(memory_id) or snapshot_memory(memory_id)
    if not snap.get('content'):
        raise ValueError(f'无法获取记忆快照: {memory_id}')

    counts: dict[str, int] = {}
    try:
        counts.update(_sqlite_delete_phase(memory_id, reason, snap, actor=actor, source=source))
        counts['chroma_delete'] = _chroma_delete(memory_id)
        if counts['chroma_delete'] != EXPECTED_COUNTS['delete']['chroma_delete']:
            _restore_active_after_chroma_failure(memory_id, snap)
            pending = _write_sync_pending({
                'op': 'delete',
                'memory_id': memory_id,
                'reason': reason,
                'snap': snap,
                'actor': actor,
                'source': source,
                'expected': EXPECTED_COUNTS['delete'],
                'actual': counts,
                'failed_step': 'chroma_delete',
            })
            raise SyncError('delete', EXPECTED_COUNTS['delete'], counts, step='chroma')

        record_event(
            'DELETE',
            memory_id,
            note=reason,
            content_preview=snap.get('content', ''),
            actor=actor or source or 'system',
        )
        return SyncResult(
            op='delete',
            memory_id=memory_id,
            ok=True,
            counts=counts,
            expected=EXPECTED_COUNTS['delete'],
            detail='deleted',
        )
    except SyncError as error:
        pending = _write_sync_pending({
            'op': 'delete',
            'memory_id': memory_id,
            'reason': reason,
            'snap': snap,
            'actor': actor,
            'source': source,
            'expected': error.expected,
            'actual': error.actual,
            'failed_step': error.step,
        })
        return SyncResult(
            op='delete',
            memory_id=memory_id,
            ok=False,
            counts=error.actual,
            expected=error.expected,
            pending_path=pending,
            detail=str(error),
        )


def sync_active_insert(
    memory_id: str,
    content: str,
    *,
    project: str = '',
    category: str = '',
    lang: str = 'zh',
) -> SyncResult:
    """mem0 add 成功后写入 active 查询库（history/Chroma 已由 mem0 写入）。"""
    memory_id = (memory_id or '').strip()
    content = (content or '').strip()
    if not memory_id or not content:
        raise ValueError('memory_id 与 content 不能为空')

    now = _now_iso()
    counts: dict[str, int] = {}
    conn = sqlite3.connect(ACTIVE_DB)
    try:
        conn.execute('BEGIN')
        cur = conn.execute(
            """
            INSERT OR REPLACE INTO active_memories (
                memory_id, content, project, category, lang, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, project or '', category or '', lang or 'zh', now, now),
        )
        counts['active_insert'] = cur.rowcount
        if counts['active_insert'] != EXPECTED_COUNTS['active_insert']['active_insert']:
            conn.execute('ROLLBACK')
            pending = _write_sync_pending({
                'op': 'active_insert',
                'memory_id': memory_id,
                'content': content,
                'project': project,
                'category': category,
                'lang': lang,
                'expected': EXPECTED_COUNTS['active_insert'],
                'actual': counts,
            })
            return SyncResult(
                op='active_insert',
                memory_id=memory_id,
                ok=False,
                counts=counts,
                expected=EXPECTED_COUNTS['active_insert'],
                pending_path=pending,
            )
        conn.commit()
    finally:
        conn.close()

    return SyncResult(
        op='active_insert',
        memory_id=memory_id,
        ok=True,
        counts=counts,
        expected=EXPECTED_COUNTS['active_insert'],
    )


def sync_active_update_meta(
    memory_id: str,
    *,
    project: str | None = None,
    category: str | None = None,
    lang: str | None = None,
) -> SyncResult:
    """更新 active 表 metadata 字段。"""
    record = get_active_record(memory_id)
    if not record:
        return SyncResult(op='active_meta_update', memory_id=memory_id, ok=False, detail='not_in_active')

    now = _now_iso()
    new_project = record['project'] if project is None else (project or '')
    new_category = record['category'] if category is None else (category or '')
    new_lang = record['lang'] if lang is None else (lang or 'zh')

    conn = sqlite3.connect(ACTIVE_DB)
    try:
        conn.execute('BEGIN')
        cur = conn.execute(
            """
            UPDATE active_memories
            SET project = ?, category = ?, lang = ?, updated_at = ?
            WHERE memory_id = ?
            """,
            (new_project, new_category, new_lang, now, memory_id),
        )
        counts = {'active_update': cur.rowcount}
        if counts['active_update'] != 1:
            conn.execute('ROLLBACK')
            return SyncResult(
                op='active_meta_update',
                memory_id=memory_id,
                ok=False,
                counts=counts,
                expected=EXPECTED_COUNTS['active_meta_update'],
            )
        conn.commit()
    finally:
        conn.close()

    return SyncResult(
        op='active_meta_update',
        memory_id=memory_id,
        ok=True,
        counts={'active_update': 1},
        expected=EXPECTED_COUNTS['active_meta_update'],
    )


def sync_active_update_content(
    memory_id: str,
    content: str,
    *,
    project: str = '',
    category: str = '',
    lang: str = 'zh',
) -> SyncResult:
    """正文替换（grooming / viewer 改文）同步 active + history UPDATE 追溯。"""
    content = (content or '').strip()
    if not memory_id or not content:
        raise ValueError('memory_id 与 content 不能为空')

    old = get_active_record(memory_id) or {}
    now = _now_iso()
    counts: dict[str, int] = {}

    conn = _attach_connection()
    try:
        conn.execute('BEGIN')
        cur = conn.execute(
            """
            UPDATE active_memories
            SET content = ?, project = ?, category = ?, lang = ?, updated_at = ?
            WHERE memory_id = ?
            """,
            (
                content,
                project or old.get('project', ''),
                category or old.get('category', ''),
                lang or old.get('lang', 'zh'),
                now,
                memory_id,
            ),
        )
        counts['active_update'] = cur.rowcount
        if counts['active_update'] != 1:
            conn.execute('ROLLBACK')
            raise SyncError('active_update', EXPECTED_COUNTS['active_update'], counts)

        cur = conn.execute(
            """
            INSERT INTO hist.history (
                id, memory_id, old_memory, new_memory, event, created_at, is_deleted
            ) VALUES (?, ?, ?, ?, 'UPDATE', ?, 0)
            """,
            (
                f'{memory_id}_upd_{int(time.time())}',
                memory_id,
                old.get('content', ''),
                content,
                now,
            ),
        )
        counts['history_insert'] = cur.rowcount
        conn.commit()
    except SyncError:
        raise
    except Exception as error:
        conn.execute('ROLLBACK')
        raise SyncError('active_update', EXPECTED_COUNTS['active_update'], counts, str(error)) from error
    finally:
        conn.close()

    return SyncResult(
        op='active_update',
        memory_id=memory_id,
        ok=True,
        counts=counts,
        expected=EXPECTED_COUNTS['active_update'],
    )


def retry_sync_pending() -> list[str]:
    """重试 sync_pending 队列（cron / 手动）。"""
    if not os.path.isdir(SYNC_PENDING_DIR):
        return ['sync_pending 目录不存在']

    messages: list[str] = []
    for name in sorted(os.listdir(SYNC_PENDING_DIR)):
        if not name.endswith('.json'):
            continue
        path = os.path.join(SYNC_PENDING_DIR, name)
        try:
            with open(path, encoding='utf-8') as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, OSError):
            continue

        retry_count = int(payload.get('retry_count', 0) or 0)
        if retry_count >= MAX_SYNC_RETRY:
            messages.append(f'{name}: 超过重试上限，需人工处理')
            continue

        op = payload.get('op', '')
        memory_id = payload.get('memory_id', '')
        ok = False

        if op == 'delete':
            result = execute_delete(
                memory_id,
                payload.get('reason', 'pending重试'),
                actor=payload.get('actor', 'retry'),
                source=payload.get('source', 'sync_pending'),
            )
            ok = result.ok
            messages.append(f'{name}: delete {"成功" if ok else result.detail}')
        elif op == 'active_insert':
            result = sync_active_insert(
                memory_id,
                payload.get('content', ''),
                project=payload.get('project', ''),
                category=payload.get('category', ''),
                lang=payload.get('lang', 'zh'),
            )
            ok = result.ok
            messages.append(f'{name}: active_insert {"成功" if ok else result.detail}')

        if ok:
            os.remove(path)
        else:
            payload['retry_count'] = retry_count + 1
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)

    return messages or ['无待重试项']
