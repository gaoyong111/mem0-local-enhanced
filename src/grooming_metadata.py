"""episodic 梳理 metadata 协议：待确认标记、AI 建议字段、合并 hints 文件。"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from mem0_add_policy import DEFAULT_CATEGORY, VALID_CATEGORIES, normalize_category

from mem0_paths import MERGE_HINTS_PATH

GROOMING_ACTIONS = frozenset({'keep', 'delete', 'promote'})
GROOMING_ACTION_LABELS: dict[str, str] = {
    'keep': '保留',
    'delete': '删除',
    'promote': '升类',
}

GROOMING_META_KEYS = (
    'grooming_pending',
    'grooming_action',
    'grooming_reason',
    'grooming_target_category',
    'grooming_at',
)


def _utc_now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def is_grooming_pending(meta: dict[str, Any] | None) -> bool:
    """是否待确认 episodic。"""
    if not meta:
        return False
    value = meta.get('grooming_pending')
    if value is True:
        return True
    return str(value or '').strip().lower() in ('1', 'true', 'yes')


def is_episodic_category(meta: dict[str, Any] | None) -> bool:
    """空 category 视为 episodic。"""
    if not meta:
        return True
    category = normalize_category(meta.get('category', '') or '')
    return category == DEFAULT_CATEGORY


def apply_grooming_pending(meta: dict[str, Any], *, pending: bool = True) -> dict[str, Any]:
    """新 episodic 写入时打待确认标记。Chroma update 合并 metadata，清除须写 0 不能 pop。"""
    meta['grooming_pending'] = 1 if pending else 0
    return meta


def apply_grooming_suggestion(
    meta: dict[str, Any],
    *,
    action: str,
    reason: str,
    target_category: str = '',
) -> dict[str, Any]:
    """写入 AI 梳理建议（不含 merge，merge 走当次 hints 文件）。"""
    action_key = str(action or 'keep').strip().lower()
    if action_key not in GROOMING_ACTIONS:
        action_key = 'keep'
    meta['grooming_action'] = action_key
    meta['grooming_reason'] = str(reason or '').strip()[:500]
    meta['grooming_at'] = _utc_now_iso()
    if action_key == 'promote':
        target = normalize_category(target_category)
        if target in VALID_CATEGORIES and target != DEFAULT_CATEGORY:
            meta['grooming_target_category'] = target
        else:
            meta['grooming_target_category'] = ''
    else:
        meta['grooming_target_category'] = ''
    return meta


def clear_grooming_pending(meta: dict[str, Any]) -> dict[str, Any]:
    """确认保留：仅清除待确认标记，保留 action/reason 供追溯。"""
    meta['grooming_pending'] = 0
    return meta


def clear_grooming_suggestion(meta: dict[str, Any]) -> dict[str, Any]:
    """正文/分类修改后清除全部梳理字段（Chroma 须写空值，不能 pop）。"""
    meta['grooming_pending'] = 0
    meta['grooming_action'] = ''
    meta['grooming_reason'] = ''
    meta['grooming_target_category'] = ''
    meta['grooming_at'] = ''
    return meta


def parse_grooming_fields(meta: dict[str, Any] | None) -> dict[str, Any]:
    """从 Chroma metadata 解析梳理展示字段。"""
    meta = meta or {}
    action = str(meta.get('grooming_action', '') or '').strip().lower()
    if action not in GROOMING_ACTIONS:
        action = ''
    target_category = str(meta.get('grooming_target_category', '') or '').strip()
    if target_category:
        target_category = normalize_category(target_category)
    return {
        'pending': is_grooming_pending(meta),
        'action': action,
        'action_label': GROOMING_ACTION_LABELS.get(action, ''),
        'reason': str(meta.get('grooming_reason', '') or '').strip(),
        'target_category': target_category,
        'at': str(meta.get('grooming_at', '') or '').strip(),
    }


def read_merge_hints() -> dict[str, Any]:
    """读取当次 merge 建议（整文件覆盖，仅保留最近一次 grooming）。"""
    if not os.path.isfile(MERGE_HINTS_PATH):
        return {'generated_at': '', 'hints': []}
    try:
        with open(MERGE_HINTS_PATH, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {'generated_at': '', 'hints': []}
    if not isinstance(payload, dict):
        return {'generated_at': '', 'hints': []}
    hints = payload.get('hints') or []
    if not isinstance(hints, list):
        hints = []
    return {
        'generated_at': str(payload.get('generated_at', '') or ''),
        'hints': [item for item in hints if isinstance(item, dict)],
    }


def write_merge_hints(hints: list[dict[str, Any]]) -> dict[str, Any]:
    """覆盖写入 merge hints（方案 A：只保留最近一次）。"""
    payload = {
        'generated_at': _utc_now_iso(),
        'hints': hints,
    }
    os.makedirs(os.path.dirname(MERGE_HINTS_PATH), exist_ok=True)
    with open(MERGE_HINTS_PATH, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def merge_hint_for_source(source_id: str, hints_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """按 source_id 查找当次 merge 建议。"""
    payload = hints_payload if hints_payload is not None else read_merge_hints()
    for item in payload.get('hints') or []:
        if str(item.get('source_id', '') or '') == source_id:
            return item
    return None


def remove_merge_hint(source_id: str) -> None:
    """合并完成后从当次 hints 移除 source。"""
    payload = read_merge_hints()
    hints = [
        item for item in (payload.get('hints') or [])
        if str(item.get('source_id', '') or '') != source_id
    ]
    if len(hints) == len(payload.get('hints') or []):
        return
    payload['hints'] = hints
    os.makedirs(os.path.dirname(MERGE_HINTS_PATH), exist_ok=True)
    with open(MERGE_HINTS_PATH, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
