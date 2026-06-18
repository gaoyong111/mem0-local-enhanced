"""episodic 梳理：AI 建议 delete/promote/keep；merge 写入当次 hints 文件。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from grooming_metadata import (
    apply_grooming_pending,
    apply_grooming_suggestion,
    merge_hint_for_source,
    write_merge_hints,
)
from mem0_add_policy import DEFAULT_CATEGORY, VALID_CATEGORIES, normalize_category

logger = logging.getLogger(__name__)

GROOMING_ADVISOR_SYSTEM = """你是 mem0 episodic 记忆梳理顾问。只输出 JSON，不自动执行任何操作。

category 含义：
- episodic：踩坑、决策、一次性事件
- reference：稳定技术事实/约定
- workflow：可复用流程/方法论
- behavior：行为规则（优先放 CLAUDE.md，mem0 中谨慎升 behavior）
- preference：用户偏好

判断标准：
- delete：信息已被其他记忆完全覆盖、技术路径已过时、通用八股非个人知识、与 CLAUDE 规则纯重复且无检索价值
- promote：事件已沉淀为稳定事实(reference)或可复用流程(workflow)；不确定则 keep
- keep：个人生活事件、独特决策(含 Why/How)、不确定、仅有 merge 可能（merge 由系统单独处理）

宁可 keep 也不要误删。只输出 JSON。"""


class LlmClient(Protocol):
    def generate_response(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
    ) -> str: ...


@dataclass
class GroomingDecision:
    action: str
    reason: str
    target_category: str = ''


@dataclass
class MergeHint:
    source_id: str
    target_id: str
    reason: str
    score: float


def _token_overlap_ratio(text_a: str, text_b: str) -> float:
    tokens_a = set(re.findall(r'\w+', (text_a or '').lower()))
    tokens_b = set(re.findall(r'\w+', (text_b or '').lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def find_merge_candidates(
    memory_id: str,
    text: str,
    project: str,
    *,
    hybrid_search_fn: Any,
    min_overlap: float = 0.35,
    max_results: int = 6,
) -> list[dict[str, Any]]:
    """检索可能重复/可合并的记忆。"""
    results = hybrid_search_fn(text, project=project, max_results=max_results)
    candidates: list[dict[str, Any]] = []
    for item in results:
        candidate_id = str(item.get('id', '') or '')
        if not candidate_id or candidate_id == memory_id:
            continue
        candidate_text = str(item.get('text', '') or '')
        overlap = _token_overlap_ratio(text, candidate_text)
        if overlap < min_overlap:
            continue
        candidates.append({
            'id': candidate_id,
            'text': candidate_text,
            'score': float(item.get('score', 0) or 0),
            'overlap': round(overlap, 3),
            'project': str(item.get('project', '') or ''),
            'category': str(item.get('category', '') or ''),
        })
    candidates.sort(key=lambda row: (row['overlap'], row['score']), reverse=True)
    return candidates


def _parse_grooming_response(raw: str) -> GroomingDecision | None:
    text = (raw or '').strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group())
        except json.JSONDecodeError:
            return None

    action = str(payload.get('action', 'keep') or 'keep').strip().lower()
    if action not in ('keep', 'delete', 'promote'):
        action = 'keep'
    target_category = ''
    if action == 'promote':
        target_category = normalize_category(payload.get('target_category', ''))
        if target_category not in VALID_CATEGORIES or target_category == DEFAULT_CATEGORY:
            action = 'keep'
            target_category = ''
    return GroomingDecision(
        action=action,
        reason=str(payload.get('reason', '') or '').strip(),
        target_category=target_category,
    )


def advise_episodic_grooming(
    llm: LlmClient | None,
    memory_id: str,
    text: str,
    *,
    category: str = 'episodic',
    project: str = '',
    similar_memories: list[dict[str, Any]] | None = None,
    merge_candidates: list[dict[str, Any]] | None = None,
) -> GroomingDecision:
    """LLM 给出 keep/delete/promote 建议；失败时规则兜底。"""
    similar_lines = []
    for item in (similar_memories or [])[:5]:
        similar_lines.append(
            f"- id={item.get('id', '')} score={item.get('score', 0):.2f} "
            f"cat={item.get('category', '')} text={str(item.get('text', ''))[:120]}"
        )
    merge_lines = []
    for item in (merge_candidates or [])[:3]:
        merge_lines.append(
            f"- id={item.get('id', '')} overlap={item.get('overlap', 0):.2f} "
            f"text={str(item.get('text', ''))[:120]}"
        )

    user_prompt = f"""记忆 ID: {memory_id}
当前 category: {category or 'episodic'}
项目: {project or '全局'}
正文:
{text}

相似记忆（供参考，merge 由系统单独处理）:
{chr(10).join(similar_lines) if similar_lines else '(无)'}

高重叠候选（可能 merge，你只需 keep/delete/promote）:
{chr(10).join(merge_lines) if merge_lines else '(无)'}

输出 JSON:
{{"action":"keep"|"delete"|"promote","target_category":"reference"|"workflow"|"behavior"|"preference"|"","reason":"<中文理由，≤200字>"}}
"""
    if llm is not None:
        try:
            response = llm.generate_response(
                messages=[
                    {'role': 'system', 'content': GROOMING_ADVISOR_SYSTEM},
                    {'role': 'user', 'content': user_prompt},
                ],
                response_format={'type': 'json_object'},
            )
            decision = _parse_grooming_response(response)
            if decision:
                if merge_candidates and decision.action == 'keep' and not decision.reason:
                    decision.reason = '见当次合并建议或暂无明确 delete/promote 动作'
                return decision
        except Exception as error:
            logger.warning('grooming advisor failed %s: %s', memory_id, error)

    return _rule_based_decision(text, category, merge_candidates)


def _rule_based_decision(
    text: str,
    category: str,
    merge_candidates: list[dict[str, Any]] | None,
) -> GroomingDecision:
    """LLM 不可用时的保守规则。"""
    normalized = normalize_category(category)
    if normalized != DEFAULT_CATEGORY:
        return GroomingDecision(
            action='promote',
            target_category=normalized,
            reason=f'当前已标为 {normalized}，若确属 episodic 请手动改回',
        )

    lower = text.lower()
    if any(keyword in lower for keyword in ('redis 缓存击穿', 'docker 时区', 'tz=utc')):
        return GroomingDecision(action='delete', reason='通用八股，非个人 episodic 知识')

    if 'how to apply' in lower or 'why：' in text or 'why:' in lower:
        if any(word in text for word in ('流程', '方法论', '步骤', '复盘')):
            return GroomingDecision(
                action='promote',
                target_category='workflow',
                reason='含 Why/How 且偏流程方法论，建议升 workflow',
            )
        if any(word in text for word in ('api', 'bug', 'provider', 'chroma', 'mem0')):
            return GroomingDecision(
                action='promote',
                target_category='reference',
                reason='含 Why/How 且偏技术事实，建议升 reference',
            )

    if merge_candidates:
        top = merge_candidates[0]
        return GroomingDecision(
            action='keep',
            reason=f'与高重叠记忆 {top.get("id", "")[:8]}… 可能重复，见当次合并建议',
        )

    return GroomingDecision(action='keep', reason='暂无明确 delete/promote 动作，建议保留观察')


def build_merge_hints(
    memories: list[dict[str, Any]],
    *,
    hybrid_search_fn: Any,
    min_overlap: float = 0.35,
) -> list[dict[str, Any]]:
    """为一批 episodic 生成当次 merge hints。"""
    hints: list[dict[str, Any]] = []
    seen_pairs: set[str] = set()

    for memory in memories:
        memory_id = str(memory.get('id', '') or '')
        text = str(memory.get('text', '') or '')
        project = str(memory.get('project', '') or '')
        if not memory_id or not text:
            continue

        candidates = find_merge_candidates(
            memory_id,
            text,
            project,
            hybrid_search_fn=hybrid_search_fn,
            min_overlap=min_overlap,
        )
        if not candidates:
            continue

        target = candidates[0]
        pair_key = '|'.join(sorted([memory_id, target['id']]))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        hints.append({
            'source_id': memory_id,
            'target_id': target['id'],
            'reason': f'与高重叠记忆主题相近（overlap={target["overlap"]:.2f}），建议合并保留 {target["id"][:8]}…',
            'score': target['overlap'],
            'source_preview': text[:120],
            'target_preview': str(target.get('text', ''))[:120],
        })

    return hints


def analyze_memory_grooming(
    memory: dict[str, Any],
    *,
    llm: LlmClient | None,
    hybrid_search_fn: Any,
) -> tuple[GroomingDecision, list[dict[str, Any]]]:
    """单条 episodic 分析：返回 (decision, merge_candidates)。"""
    memory_id = str(memory.get('id', '') or '')
    text = str(memory.get('text', '') or '')
    project = str(memory.get('project', '') or '')
    category = normalize_category(memory.get('category', '') or DEFAULT_CATEGORY)

    similar = hybrid_search_fn(text, project=project, max_results=5)
    similar = [item for item in similar if str(item.get('id', '')) != memory_id]
    merge_candidates = find_merge_candidates(
        memory_id,
        text,
        project,
        hybrid_search_fn=hybrid_search_fn,
    )
    decision = advise_episodic_grooming(
        llm,
        memory_id,
        text,
        category=category,
        project=project,
        similar_memories=similar,
        merge_candidates=merge_candidates,
    )
    return decision, merge_candidates


def apply_grooming_to_chroma_metadata(
    meta: dict[str, Any],
    decision: GroomingDecision,
    *,
    set_pending: bool = True,
) -> dict[str, Any]:
    """将梳理结果写入 metadata dict。"""
    if set_pending:
        apply_grooming_pending(meta, pending=True)
    apply_grooming_suggestion(
        meta,
        action=decision.action,
        reason=decision.reason,
        target_category=decision.target_category,
    )
    return meta


def revalidate_merge_target(
    source_id: str,
    source_text: str,
    project: str,
    hinted_target_id: str,
    *,
    hybrid_search_fn: Any,
) -> dict[str, Any] | None:
    """采纳合并前重校验：返回最终 target（可能与 hint 不同）。"""
    candidates = find_merge_candidates(
        source_id,
        source_text,
        project,
        hybrid_search_fn=hybrid_search_fn,
        min_overlap=0.25,
    )
    if not candidates:
        return None

    for item in candidates:
        if item['id'] == hinted_target_id:
            return item

    best = candidates[0]
    if best['overlap'] >= 0.3:
        return best
    return None
