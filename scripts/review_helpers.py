"""每日复盘辅助：mem0 快照/diff、漏跑检测、cron 续期日志、会话清单。不依赖 Ollama。"""

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
LAST_SCAN_END_FILE = os.path.join(DATA_DIR, 'last-scan-end.txt')
MISSED_RUN_HOURS = 36
CLAUDE_PROJECT_PREFIXES = (
    '-Users-gaoyong-Desktop-h5-release-',
    'Users-gaoyong-Desktop-h5-release-',
    '-Users-gaoyong-Desktop-',
    'Users-gaoyong-Desktop-',
    '-Users-gaoyong-',
    'Users-gaoyong-',
)


def _parse_since(since: str) -> float:
    """解析 --since 参数为 Unix 时间戳。"""
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(since, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f'无法解析时间: {since}')


def _decode_project_dir(project_dir: str) -> str:
    """从 Claude/Cursor 项目目录名解码可读项目名。"""
    name = project_dir.lstrip('-')
    for prefix in CLAUDE_PROJECT_PREFIXES:
        clean = prefix.lstrip('-')
        if name.startswith(clean):
            decoded = name[len(clean):]
            return decoded or '(全局)'
    if name in ('Users-gaoyong',):
        return '(全局)'
    return name or project_dir


def _project_from_session_path(path: str, container: str) -> str:
    """从会话 JSONL 路径提取项目名。"""
    marker = '/projects/'
    if marker not in path:
        return 'unknown'
    rest = path.split(marker, 1)[1]
    project_dir = rest.split('/', 1)[0]
    return _decode_project_dir(project_dir)


def _session_id_from_path(path: str) -> str:
    """从路径提取会话 ID 短码（UUID 前 8 位）。"""
    base = os.path.basename(path)
    if base.endswith('.jsonl'):
        base = base[:-6]
    if base.startswith('agent-'):
        return base[:8]
    return base.split('-')[0][:8]


def _parse_jsonl_timestamp(raw: Any) -> float | None:
    """解析 Claude JSONL 行内 timestamp。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return raw / 1000.0 if raw > 1e12 else float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def _claude_session_in_range(path: str, since_ts: float) -> bool:
    """Claude 会话：任一行 timestamp ≥ since 即纳入。"""
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                ts = _parse_jsonl_timestamp(obj.get('timestamp'))
                if ts is not None and ts >= since_ts:
                    return True
    except (OSError, json.JSONDecodeError):
        return False
    return False


def _collect_session_inventory(since_ts: float) -> list[dict[str, Any]]:
    """扫描 Claude + Cursor 会话，返回结构化清单（不含提取摘要）。"""
    sessions: list[dict[str, Any]] = []

    claude_glob = os.path.expanduser('~/.claude/projects/**/*.jsonl')
    for path in glob.glob(claude_glob, recursive=True):
        if '/subagents/' in path:
            continue
        if not _claude_session_in_range(path, since_ts):
            continue
        sessions.append({
            'id': _session_id_from_path(path),
            'container': 'Claude',
            'project': _project_from_session_path(path, 'claude'),
            'path': path,
            'filter': 'timestamp',
        })

    cursor_glob = os.path.expanduser('~/.cursor/projects/**/agent-transcripts/**/*.jsonl')
    for path in glob.glob(cursor_glob, recursive=True):
        if '/subagents/' in path:
            continue
        if os.path.getmtime(path) < since_ts:
            continue
        sessions.append({
            'id': _session_id_from_path(path),
            'container': 'Cursor',
            'project': _project_from_session_path(path, 'cursor'),
            'path': path,
            'filter': 'mtime',
        })

    sessions.sort(key=lambda s: (s['container'], s['project'], s['id']))
    return sessions


def _load_cron_last_fired() -> float | None:
    """读 ~/.claude/scheduled_tasks.json，返回复盘 cron 的 lastFiredAt（Unix 秒）。

    仅返回 lastFiredAt，不含 createdAt（createdAt 可能是几天前，不适合做扫描边界）。
    """
    path = os.path.expanduser('~/.claude/scheduled_tasks.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception:
        return None
    tasks = cfg.get('tasks') or []
    review_tasks = [
        t for t in tasks
        if 'daily-review/SKILL.md' in (t.get('prompt') or '')
    ]
    if not review_tasks:
        return None
    fired = [t.get('lastFiredAt') for t in review_tasks if t.get('lastFiredAt')]
    return max(fired) / 1000.0 if fired else None


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
    """检测漏跑 + 返回下次扫描起点。

    scan_start 优先级：
    1. .data/last-scan-end.txt（上次复盘结束时记录的 lastFiredAt）
    2. 最新复盘文件 mtime（fallback）
    """
    scan_start_str = None
    scan_start_ts = None
    scan_start_source = None
    if os.path.exists(LAST_SCAN_END_FILE):
        with open(LAST_SCAN_END_FILE, encoding='utf-8') as f:
            scan_start_str = f.read().strip()
        try:
            scan_start_ts = datetime.strptime(scan_start_str, '%Y-%m-%d %H:%M').timestamp()
            scan_start_source = 'last-scan-end.txt'
        except ValueError:
            scan_start_str = None

    latest = find_latest_review()
    if scan_start_ts is None:
        if not latest:
            print(json.dumps({'missed': False, 'reason': 'no prior review'}, ensure_ascii=False))
            return 0
        _, scan_start_ts = latest
        scan_start_str = datetime.fromtimestamp(scan_start_ts).strftime('%Y-%m-%d %H:%M')
        scan_start_source = 'file_mtime_fallback'

    hours = (datetime.now().timestamp() - scan_start_ts) / 3600
    missed = hours > MISSED_RUN_HOURS
    result = {
        'missed': missed,
        'scan_start': scan_start_str,
        'scan_start_source': scan_start_source,
        'latest_review': latest[0] if latest else None,
        'hours_since_scan_start': round(hours, 1),
        'threshold_hours': MISSED_RUN_HOURS,
        'banner': '⚠️ 疑似漏跑，覆盖范围可能跨天' if missed else '',
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_record_scan_end(args: argparse.Namespace) -> int:
    """本次复盘结束时调用：把扫描边界写入 .data/last-scan-end.txt。

    默认写 cron lastFiredAt（复盘可能拖到下午才写完，仍以触发时刻为边界）。
    --manual：手动复盘，写 now。
    无 lastFiredAt 时 fallback 到 now。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    if args.manual:
        end_ts = datetime.now().timestamp()
        src = 'now_manual'
    else:
        cron_fired = _load_cron_last_fired()
        if cron_fired:
            end_ts = cron_fired
            src = 'cron_lastFiredAt'
        else:
            end_ts = datetime.now().timestamp()
            src = 'now_fallback'
    end_str = datetime.fromtimestamp(end_ts).strftime('%Y-%m-%d %H:%M')
    with open(LAST_SCAN_END_FILE, 'w', encoding='utf-8') as f:
        f.write(end_str)
    print(f'OK scan end recorded: {end_str} (source={src})')
    return 0


def cmd_list_sessions(args: argparse.Namespace) -> int:
    """列出时间范围内的 Claude/Cursor 会话清单。"""
    since_ts = _parse_since(args.since)
    sessions = _collect_session_inventory(since_ts)
    claude_count = sum(1 for s in sessions if s['container'] == 'Claude')
    cursor_count = sum(1 for s in sessions if s['container'] == 'Cursor')
    report = {
        'since': args.since,
        'since_ts': since_ts,
        'total': len(sessions),
        'claude': claude_count,
        'cursor': cursor_count,
        'sessions': sessions,
    }

    if args.output:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f'OK session inventory: {args.output} ({len(sessions)} sessions)')

    if args.markdown:
        lines = [
            f"> 共扫描 {len(sessions)} 个会话（Claude {claude_count} + Cursor {cursor_count}）。"
            f"筛选起点：{args.since}",
            '',
            '| # | 容器 | 项目 | 会话 ID | 提取内容摘要 |',
            '|---|------|------|---------|-------------|',
        ]
        for idx, s in enumerate(sessions, 1):
            lines.append(
                f"| {idx} | {s['container']} | {s['project']} | {s['id']} | （待代理扫描后填写） |"
            )
        md = '\n'.join(lines)
        with open(args.markdown, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f'OK session markdown stub: {args.markdown}')

    if not args.output and not args.markdown:
        print(json.dumps(report, ensure_ascii=False, indent=2))
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

    p_scan_end = sub.add_parser('record-scan-end', help='记录本次扫描终点到 .data/last-scan-end.txt')
    p_scan_end.add_argument('--manual', action='store_true', help='手动复盘：写 now 而非 cron lastFiredAt')
    p_scan_end.set_defaults(func=cmd_record_scan_end)

    p_sessions = sub.add_parser('list-sessions', help='列出时间范围内的 Claude/Cursor 会话')
    p_sessions.add_argument('--since', required=True, help='扫描起点，如 "2026-06-24 11:50"')
    p_sessions.add_argument('--output', help='JSON 清单输出路径')
    p_sessions.add_argument('--markdown', help='Markdown 表格 stub 输出路径')
    p_sessions.set_defaults(func=cmd_list_sessions)

    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
