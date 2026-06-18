"""每日复盘辅助：mem0 快照/diff、漏跑检测、cron 续期日志。不依赖 Ollama。"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Any

MEM0_DIR = os.path.expanduser('~/.mem0')
REVIEW_DIR = os.path.expanduser('~/daily-reviews')
DATA_DIR = os.path.join(REVIEW_DIR, '.data')
HISTORY_DB = os.path.join(MEM0_DIR, 'history.db')
CHROMA_DB_PATH = os.path.join(MEM0_DIR, 'chroma_db')
CRON_RENEWAL_LOG = os.path.join(REVIEW_DIR, 'cron-renewal.log')
MISSED_RUN_HOURS = 36


def _load_deleted_memory_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT memory_id
        FROM history
        WHERE event = 'DELETE' AND memory_id IS NOT NULL
        """
    ).fetchall()
    return {memory_id for memory_id, in rows if memory_id}


def _load_final_memories() -> dict[str, str]:
    if not os.path.exists(HISTORY_DB):
        return {}

    conn = sqlite3.connect(HISTORY_DB)
    try:
        deleted_ids = _load_deleted_memory_ids(conn)
        rows = conn.execute(
            """
            SELECT memory_id, new_memory, old_memory
            FROM history
            WHERE is_deleted = 0
            ORDER BY created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    final: dict[str, str] = {}
    for memory_id, new_memory, old_memory in rows:
        if not memory_id or memory_id in deleted_ids:
            continue
        text = (new_memory or old_memory or '').strip()
        if text and memory_id not in final:
            final[memory_id] = text
    return final


def _load_memory_metadata() -> dict[str, dict[str, str]]:
    try:
        import chromadb

        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        result = col.get(include=['metadatas'])
    except Exception:
        return {}

    metadata_map: dict[str, dict[str, str]] = {}
    for memory_id, meta in zip(result.get('ids') or [], result.get('metadatas') or []):
        if not meta:
            continue
        metadata_map[memory_id] = {
            'project': str(meta.get('project', '') or ''),
            'category': str(meta.get('category', '') or ''),
        }
    return metadata_map


def build_snapshot() -> dict[str, Any]:
    final_memories = _load_final_memories()
    metadata_map = _load_memory_metadata()
    items = []
    for memory_id, text in sorted(final_memories.items()):
        meta = metadata_map.get(memory_id, {})
        items.append({
            'id': memory_id,
            'memory': text,
            'project': meta.get('project', ''),
            'category': meta.get('category', ''),
        })
    return {
        'captured_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'count': len(items),
        'items': items,
    }


def snapshot_path(date_str: str | None = None) -> str:
    date_str = date_str or datetime.now().strftime('%Y%m%d')
    return os.path.join(DATA_DIR, f'mem0-snapshot-{date_str}.json')


def find_latest_snapshot() -> str | None:
    paths = sorted(glob.glob(os.path.join(DATA_DIR, 'mem0-snapshot-*.json')))
    return paths[-1] if paths else None


def cmd_snapshot(args: argparse.Namespace) -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = snapshot_path(args.date)
    data = build_snapshot()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'OK snapshot: {path} ({data["count"]} items)')
    return 0


def _items_by_id(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item['id']: item for item in snapshot.get('items', [])}


def cmd_diff(args: argparse.Namespace) -> int:
    baseline_path = args.baseline
    if baseline_path == 'latest':
        baseline_path = find_latest_snapshot()
    if not baseline_path or not os.path.exists(baseline_path):
        print('WARN: 无 baseline 快照，跳过 diff')
        return 0

    with open(baseline_path, encoding='utf-8') as f:
        baseline = json.load(f)
    current = build_snapshot()

    old_map = _items_by_id(baseline)
    new_map = _items_by_id(current)
    added = [new_map[mid] for mid in sorted(new_map.keys() - old_map.keys())]
    removed = [old_map[mid] for mid in sorted(old_map.keys() - new_map.keys())]
    changed = []
    for mid in sorted(old_map.keys() & new_map.keys()):
        if old_map[mid]['memory'] != new_map[mid]['memory']:
            changed.append({'id': mid, 'before': old_map[mid]['memory'], 'after': new_map[mid]['memory']})

    report = {
        'baseline': baseline_path,
        'baseline_captured_at': baseline.get('captured_at'),
        'current_captured_at': current.get('captured_at'),
        'added': added,
        'removed': removed,
        'changed': changed,
        'summary': {
            'added': len(added),
            'removed': len(removed),
            'changed': len(changed),
        },
    }

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f'OK diff report: {args.output}')
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def find_latest_review() -> tuple[str, float] | None:
    paths = sorted(glob.glob(os.path.join(REVIEW_DIR, 'daily-review-*.md')))
    if not paths:
        return None
    latest = paths[-1]
    return latest, os.path.getmtime(latest)


def cmd_check_missed_run(args: argparse.Namespace) -> int:
    latest = find_latest_review()
    if not latest:
        print(json.dumps({'missed': False, 'reason': 'no prior review'}, ensure_ascii=False))
        return 0

    path, mtime = latest
    hours = (datetime.now().timestamp() - mtime) / 3600
    missed = hours > MISSED_RUN_HOURS
    result = {
        'missed': missed,
        'latest_review': path,
        'latest_mtime': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M'),
        'hours_since': round(hours, 1),
        'threshold_hours': MISSED_RUN_HOURS,
        'banner': '⚠️ 疑似漏跑，覆盖范围可能跨天' if missed else '',
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_log_cron_renewal(args: argparse.Namespace) -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"old={args.old} new={args.new} | note={args.note or ''}\n"
    )
    with open(CRON_RENEWAL_LOG, 'a', encoding='utf-8') as f:
        f.write(line)
    print(f'OK logged: {CRON_RENEWAL_LOG.strip()}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='daily-review helpers')
    sub = parser.add_subparsers(dest='command', required=True)

    p_snapshot = sub.add_parser('snapshot', help='写入 mem0 快照')
    p_snapshot.add_argument('--date', help='YYYYMMDD，默认今天')
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_diff = sub.add_parser('diff', help='对比 baseline 与当前 mem0')
    p_diff.add_argument('--baseline', default='latest', help='快照路径或 latest')
    p_diff.add_argument('--output', help='diff 报告输出路径')
    p_diff.set_defaults(func=cmd_diff)

    p_missed = sub.add_parser('check-missed-run', help='检测是否漏跑复盘')
    p_missed.set_defaults(func=cmd_check_missed_run)

    p_renewal = sub.add_parser('log-cron-renewal', help='追加 cron 续期日志')
    p_renewal.add_argument('--old', required=True)
    p_renewal.add_argument('--new', required=True)
    p_renewal.add_argument('--note', default='')
    p_renewal.set_defaults(func=cmd_log_cron_renewal)

    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
