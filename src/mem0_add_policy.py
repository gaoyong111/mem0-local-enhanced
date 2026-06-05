"""mem0 写入策略：B 分类分流 + C 语言锁 + D 结构化 reference + E 合并决策（不改写正文）"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_PATCHED = False

# B + C：infer=true 时附加到 mem0 prompt（与 config custom_instructions 一致）
CHINESE_INFER_INSTRUCTIONS = """
## 语言与格式（最高优先级）
1. 输出语言必须与用户输入一致；输入为中文时，记忆正文必须用中文，禁止翻译成英文。
2. 保留原文中的模块名、字段名、权限 ID、API 名（如 userService、loginType、orderId）。
3. 只做抽取与去重，不要改写成英文散文；每条记忆仍应是可独立检索的中文完整句。
4. 技术约定类信息优先保留 camelCase 标识符，不要用泛化描述替代字段名。
""".strip()

# E：合并决策 prompt（只输出 JSON，不改写记忆正文）
MERGE_ADVISOR_SYSTEM = """你是 mem0 记忆去重顾问。比较「新记忆」与「候选旧记忆」，判断是否语义重复。

判断标准（严格遵守）：
- DROP_NEW：新记忆和某条旧记忆描述的是**同一组事实/同一事件/同一决策**，旧记忆已完整覆盖新记忆的信息，新记忆没有任何增量信息。仅当两者核心内容高度重叠时才选 DROP_NEW。
- KEEP：以下情况一律选 KEEP：
  1. 新记忆和旧记忆虽然涉及同一主题/项目，但描述的是**不同的事实或不同的事件**（如一个是bug修复记录，一个是迭代计划讨论）
  2. 新记忆比旧记忆**更详细**或包含增量信息（旧记忆只提了结论，新记忆补充了根因/方案）
  3. 两者有任何实质性的信息差异
  4. 你不确定是否真正重复

宁可多保留一条记忆，也不要误删有增量信息的记忆。

你只能输出 JSON，不得改写或生成新的记忆正文。"""

STRUCTURED_META_KEYS = ('module', 'field', 'rule')


class LlmClient(Protocol):
    def generate_response(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, str] | None = None,
    ) -> str: ...


@dataclass
class AddPlan:
    content: str
    metadata: dict[str, Any]
    use_infer: bool
    infer_prompt: str | None
    run_merge_check: bool
    storage_mode: str


@dataclass
class MergeDecision:
    action: str
    target_id: str
    reason: str


def apply_mem0_patches() -> None:
    """C：强制 mem0 抽取阶段开启 use_input_language。"""
    global _PATCHED
    if _PATCHED:
        return
    try:
        import mem0.configs.prompts as prompts_module

        original = prompts_module.generate_additive_extraction_prompt

        def patched_generate_additive_extraction_prompt(*args: Any, **kwargs: Any) -> str:
            kwargs['use_input_language'] = True
            return original(*args, **kwargs)

        prompts_module.generate_additive_extraction_prompt = patched_generate_additive_extraction_prompt
        _PATCHED = True
        logger.debug('mem0 use_input_language patch applied')
    except Exception as error:
        logger.warning('mem0 patch skipped: %s', error)


def _parse_metadata(raw: str) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _normalize_keywords(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[,，\s]+', value) if part.strip()]
    return []


def _extract_structured(meta: dict[str, Any]) -> dict[str, Any] | None:
    structured = meta.get('structured')
    if isinstance(structured, dict) and structured.get('module'):
        return structured
    if all(meta.get(key) for key in STRUCTURED_META_KEYS):
        return {key: str(meta[key]).strip() for key in STRUCTURED_META_KEYS}
    return None


def format_structured_memory(content: str, structured: dict[str, Any]) -> str:
    """D：将结构化字段格式化为固定中文模板，便于关键词检索。"""
    module = str(structured.get('module', '')).strip()
    field = str(structured.get('field', '')).strip()
    rule = str(structured.get('rule', '')).strip()
    keywords = _normalize_keywords(structured.get('keywords'))
    if not keywords:
        keywords = [module, field]
    keywords = [keyword for keyword in keywords if keyword]

    parts: list[str] = []
    if module and field:
        parts.append(f'[{module}] {field}')
    elif module:
        parts.append(f'[{module}]')

    body = rule or content.strip()
    if body:
        parts.append(f': {body}' if parts else body)

    if keywords:
        parts.append(f'（关键词: {", ".join(keywords)}）')

    formatted = ''.join(parts).strip()
    return formatted or content.strip()


def should_use_infer(metadata: dict[str, Any], infer_flag: str) -> bool:
    """B：仅偏好/行为类默认 infer；infer 留空时按 category 自动，显式 false 才强制关闭。"""
    flag = (infer_flag or '').strip().lower()
    if flag in ('true', '1', 'yes'):
        return True
    if flag in ('false', '0', 'no'):
        return False
    if metadata.get('infer') is True:
        return True
    if metadata.get('infer') is False:
        return False
    category = str(metadata.get('category', '')).lower()
    return category in ('preference', 'workflow', 'behavior')


def prepare_add_plan(
    content: str,
    metadata_raw: str = '',
    project: str = '',
    infer_flag: str = 'false',
) -> AddPlan:
    """根据 B/C/D/E 生成写入计划。"""
    meta = _parse_metadata(metadata_raw)
    if project:
        meta['project'] = project

    structured = _extract_structured(meta)
    if structured:
        canonical = format_structured_memory(content, structured)
        # Chroma metadata 仅支持标量，嵌套 dict 需序列化
        meta.pop('structured', None)
        for key in STRUCTURED_META_KEYS:
            meta.pop(key, None)
        meta['structured_json'] = json.dumps(structured, ensure_ascii=False)
        meta['module'] = str(structured.get('module', ''))
        meta['field'] = str(structured.get('field', ''))
        meta['storage_mode'] = 'structured'
        meta.setdefault('category', 'reference')
        keywords = _normalize_keywords(structured.get('keywords'))
        if keywords:
            meta['keywords'] = ','.join(keywords)
        return AddPlan(
            content=canonical,
            metadata=meta,
            use_infer=False,
            infer_prompt=None,
            run_merge_check=True,
            storage_mode='structured',
        )

    use_infer = should_use_infer(meta, infer_flag)
    if use_infer:
        meta['storage_mode'] = 'inferred'
        return AddPlan(
            content=content.strip(),
            metadata=meta,
            use_infer=True,
            infer_prompt=CHINESE_INFER_INSTRUCTIONS,
            run_merge_check=False,
            storage_mode='inferred',
        )

    meta['storage_mode'] = meta.get('storage_mode') or 'verbatim'
    return AddPlan(
        content=content.strip(),
        metadata=meta,
        use_infer=False,
        infer_prompt=None,
        run_merge_check=True,
        storage_mode='verbatim',
    )


def _parse_merge_response(raw: str) -> MergeDecision | None:
    text = raw.strip()
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

    action = str(payload.get('action', 'KEEP')).upper()
    if action not in ('KEEP', 'DROP_NEW'):
        action = 'KEEP'
    return MergeDecision(
        action=action,
        target_id=str(payload.get('target_id', '') or ''),
        reason=str(payload.get('reason', '') or ''),
    )


def advise_merge(
    llm: LlmClient,
    new_memory_id: str,
    new_text: str,
    candidates: list[dict[str, Any]],
) -> MergeDecision:
    """E：LLM 仅决定去重，返回是否删除新记忆。"""
    if not candidates:
        return MergeDecision(action='KEEP', target_id='', reason='无相似候选')

    candidate_lines = []
    for item in candidates:
        candidate_lines.append(
            f"- id={item.get('id', '')} score={item.get('score', 0):.2f} text={item.get('text', '')}"
        )

    user_prompt = f"""新记忆 ID: {new_memory_id}
新记忆正文:
{new_text}

候选旧记忆:
{chr(10).join(candidate_lines)}

请输出 JSON:
{{"action":"KEEP"|"DROP_NEW","target_id":"<旧记忆ID或空>","reason":"<简短中文>"}}
"""
    try:
        response = llm.generate_response(
            messages=[
                {'role': 'system', 'content': MERGE_ADVISOR_SYSTEM},
                {'role': 'user', 'content': user_prompt},
            ],
            response_format={'type': 'json_object'},
        )
        decision = _parse_merge_response(response)
        if decision:
            return decision
    except Exception as error:
        logger.warning('merge advisor failed: %s', error)

    return MergeDecision(action='KEEP', target_id='', reason='合并顾问不可用，保留新记忆')


def _token_overlap_ratio(newText: str, oldText: str) -> float:
    """计算两段文本的 token 重叠比率（jaccard），防止关键词匹配高分但语义无关。"""
    newTokens = set(re.findall(r'\w+', newText.lower()))
    oldTokens = set(re.findall(r'\w+', oldText.lower()))
    if not newTokens or not oldTokens:
        return 0.0
    intersection = newTokens & oldTokens
    union = newTokens | oldTokens
    return len(intersection) / len(union)


def run_merge_check(
    llm: LlmClient,
    memory_id: str,
    text: str,
    project: str,
    *,
    delete_memory: Any,
    hybrid_search_fn: Any,
    min_keyword_score: float = 15.0,
    min_overlap: float = 0.5,
) -> str | None:
    """E：原样入库后做去重；两层过滤（关键词分数 + 语义重叠）后才提交 LLM。"""
    results = hybrid_search_fn(text, project=project, max_results=6)
    candidates = [
        item for item in results
        if item.get('id') != memory_id and (item.get('score') or 0) >= min_keyword_score
    ]
    # 第二层：语义 token 重叠率过低则排除（关键词高分但内容不同）
    candidates = [
        item for item in candidates
        if _token_overlap_ratio(text, item.get('text', '')) >= min_overlap
    ]
    if not candidates:
        return None

    decision = advise_merge(llm, memory_id, text, candidates)
    if decision.action != 'DROP_NEW':
        return None

    try:
        delete_memory(memory_id)
    except Exception as error:
        logger.warning('drop duplicate failed %s: %s', memory_id, error)
        return None

    target = decision.target_id or candidates[0].get('id', '')
    return f'去重：新记忆已删除（与 {target} 重复）。{decision.reason}'
