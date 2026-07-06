"""mem0 混合检索：关键词(history.db) + 向量(Chroma/Ollama)，供 MCP / Claude / Cursor 共用"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from typing import Any

from mem0_add_policy import normalize_category  # noqa: E402

from mem0_paths import CHROMA_DB_PATH, CONFIG_PATH, HISTORY_DB, PROJECT_ALIASES_PATH  # noqa: E402

DEFAULT_USER = os.getenv('MEM0_DEFAULT_USER_ID', os.getenv('MEM0_USER_ID', 'default-user'))

DEFAULT_TOP_K = 15
DEFAULT_MAX_RESULTS = 5
RRF_RECALL_TOP_K = 50

# 加权 RRF：score = 1/(K+vec_rank) + α·1/(K+kw_rank) [+ β 双路都命中]
RRF_K = 15
RRF_KW_WEIGHT = 0.5
RRF_BOTH_BONUS = 0.0  # 双路 RRF 相加已表达共识；实测 +0.008 不改变 top5

# Phase 4 keyword 增强（#18 Batch 1）
TF_CAP = 3
PRIMARY_CJK_MAX_LEN = 6
SUBSEQ_RATIO = 0.5
SUBSEQ_VEC_GATE_MIN = 15
SUBSEQ_VEC_GATE_MAX = 30

# Phase 4D：keyword 相对截断 kw_score < top1 × ratio 不进池；0 或 MEM0_KW_REL_RATIO=0 关闭
KW_RELATIVE_RATIO = float(os.getenv('MEM0_KW_REL_RATIO', '0.25'))

# project 配额：RRF 上加匹配奖励分，再 project 前 3 + 全局保底 2
RRF_PROJECT_BONUS = 0.005
PREFERENCE_CROSS_BONUS = 0.008
PROJECT_QUOTA_TOP = 3
PROJECT_QUOTA_MIN_RRF = 0.03
GLOBAL_QUOTA_MIN = 2

# Phase 3 lang 分轨：中文 query 排除纯英文记忆
_CJK_RE = re.compile(r'[一-鿿]')
LANG_VECTOR_OVERSAMPLE = 4

# 向量相对阈值：vec_score < top1 - δ 视为未命中（不进 vector_results → vec_rank=0）
# 设 0 或环境变量 MEM0_VECTOR_REL_MARGIN=0 可关闭
VECTOR_SCORE_REL_MARGIN = float(os.getenv('MEM0_VECTOR_REL_MARGIN', '0.10'))

GENERIC_DIR_NAMES = frozenset({
    'Desktop', 'Documents', 'Home', 'home', 'Downloads', 'src', 'code', 'projects', 'tmp',
})


def _load_project_aliases() -> dict[str, str]:
    """从 ~/.mem0/project_aliases.json 加载目录名 -> project 映射（不硬编码在源码）。"""
    path = os.getenv('MEM0_PROJECT_ALIASES', PROJECT_ALIASES_PATH)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding='utf-8') as config_file:
        return json.load(config_file)


_CHROMA_CLIENT = None


def _mem0_chroma_settings() -> Any:
    """与 mem0.vector_stores.chroma.ChromaDB 本地 path 模式 settings 保持一致。"""
    from chromadb.config import Settings

    settings = Settings(anonymized_telemetry=False)
    settings.persist_directory = CHROMA_DB_PATH
    settings.is_persistent = True
    return settings


def get_chroma_client() -> Any:
    """返回共享的 Chroma 客户端，settings 与 mem0 一致，避免同进程冲突。"""
    global _CHROMA_CLIENT
    if _CHROMA_CLIENT is not None:
        return _CHROMA_CLIENT
    import chromadb

    _CHROMA_CLIENT = chromadb.Client(_mem0_chroma_settings())
    return _CHROMA_CLIENT


def normalize_project(project: str) -> str:
    """统一项目标识，空字符串表示全局。"""
    value = (project or '').strip()
    if value == '全局':
        return ''
    return value


def _project_matches(item_project: str, target_project: str) -> bool:
    """判断记忆的 project 标签是否属于目标项目作用域。"""
    return normalize_project(item_project) == normalize_project(target_project)


def detect_project(cwd: str | None = None) -> str:
    """从工作目录推断 mem0 project 标识。"""
    work_dir = (cwd or os.getcwd()).rstrip('/')
    name = os.path.basename(work_dir) if work_dir else ''
    aliases = _load_project_aliases()
    if name in aliases:
        return aliases[name]
    if name in GENERIC_DIR_NAMES:
        return ''
    return name


def query_has_cjk(text: str) -> bool:
    """判定 query 是否含中文汉字（CJK 表意文字）。"""
    return bool(_CJK_RE.search(text or ''))


def infer_memory_lang(text: str) -> str:
    """推断记忆语言：含中文→zh，纯英文→en（infer 时代碎片，中文 query 时过滤）。"""
    value = (text or '').strip()
    if not value:
        return 'zh'
    if _CJK_RE.search(value):
        return 'zh'
    return 'en'


def resolve_memory_lang(text: str, metadata_lang: str = '') -> str:
    """优先用 metadata.lang，缺失时按正文临时推断。"""
    stored = (metadata_lang or '').strip().lower()
    if stored in ('zh', 'en', 'mixed'):
        return stored
    return infer_memory_lang(text)


def should_exclude_lang_en(query: str, text: str, metadata_lang: str = '') -> bool:
    """中文 query 时排除 lang=en 的记忆；mixed/zh 保留。"""
    if not query_has_cjk(query):
        return False
    return resolve_memory_lang(text, metadata_lang) == 'en'


def extract_keywords(query: str) -> list[str]:
    """从查询中提取中英文关键词。中文滑窗 2–4 字 + 最长中文段（≤6 字）作主 keyword。"""
    keywords: list[str] = []
    cjk_segments = re.findall(r'[一-鿿]+', query)
    for segment in cjk_segments:
        if len(segment) >= 2:
            primary = segment[:PRIMARY_CJK_MAX_LEN]
            keywords.append(primary)
        for size in range(2, min(5, len(segment) + 1)):
            for i in range(len(segment) - size + 1):
                keywords.append(segment[i:i + size])
    keywords.extend(re.findall(r'[a-zA-Z0-9_]+', query.lower()))
    seen: set[str] = set()
    unique: list[str] = []
    for keyword in keywords:
        key = keyword.lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(keyword)
    return unique


def primary_cjk_keyword(query: str) -> str:
    """query 中最长连续中文段，上限 PRIMARY_CJK_MAX_LEN（4G 子序列仅对此触发）。"""
    segments = re.findall(r'[一-鿿]+', query)
    if not segments:
        return ''
    segment = max(segments, key=len)
    if len(segment) < 2:
        return ''
    return segment[:PRIMARY_CJK_MAX_LEN]


def _is_cjk_keyword(keyword: str) -> bool:
    """纯中文 2–4 字 keyword（子序列触发条件）。"""
    if not keyword or len(keyword) < 2 or len(keyword) > 4:
        return False
    return bool(_CJK_RE.search(keyword)) and not re.search(r'[a-zA-Z]', keyword)


def _subseq_vec_gate(memory_count: int) -> int:
    """4G vec_rank 门控：clamp(round(0.2×N), 15, 30)。"""
    if memory_count <= 0:
        return SUBSEQ_VEC_GATE_MAX
    return max(SUBSEQ_VEC_GATE_MIN, min(SUBSEQ_VEC_GATE_MAX, round(0.2 * memory_count)))


def _subsequence_hit(text: str, keyword: str) -> bool:
    """字符按序出现，最大跨度 ≤ len(keyword)+1。"""
    if not keyword:
        return False
    max_span = len(keyword) + 1
    ti = 0
    positions: list[int] = []
    for ch in keyword:
        found = text.find(ch, ti)
        if found < 0:
            return False
        positions.append(found)
        ti = found + 1
    return positions[-1] - positions[0] <= max_span


def _ranges_overlap(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    """区间是否与已占区间重叠。"""
    return any(not (end <= occ_start or start >= occ_end) for occ_start, occ_end in occupied)


def _non_overlapping_hits(text_lower: str, keyword_lower: str, occupied: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """在 text 中找不与 occupied 重叠的 keyword 出现位置。"""
    hits: list[tuple[int, int]] = []
    start = 0
    while start <= len(text_lower) - len(keyword_lower):
        pos = text_lower.find(keyword_lower, start)
        if pos < 0:
            break
        end = pos + len(keyword_lower)
        if not _ranges_overlap(pos, end, occupied):
            hits.append((pos, end))
        start = pos + 1
    return hits


def _best_match_keyword_score(text_lower: str, keywords: list[str]) -> float:
    """4A 最长命中优先 + 4C TF cap：去重叠子串计分。"""
    occupied: list[tuple[int, int]] = []
    score = 0.0
    for keyword in sorted(keywords, key=len, reverse=True):
        keyword_lower = keyword.lower()
        if not keyword_lower:
            continue
        hits = _non_overlapping_hits(text_lower, keyword_lower, occupied)
        if not hits:
            continue
        count = min(len(hits), TF_CAP)
        weight = min(len(keyword_lower), 5)
        score += count * weight
        occupied.extend(hits)
    return score


def _keyword_score_for_memory(
    text: str,
    keywords: list[str],
    primary_kw: str,
    *,
    vec_rank: int = 0,
    vec_gate: int = SUBSEQ_VEC_GATE_MAX,
) -> float:
    """单条记忆 keyword 分：子串去重叠 + 条件子序列弱分。"""
    text_lower = text.lower()
    score = _best_match_keyword_score(text_lower, keywords)

    if not primary_kw or not _is_cjk_keyword(primary_kw):
        return score
    if primary_kw.lower() in text_lower:
        return score
    if vec_rank <= 0 or vec_rank > vec_gate:
        return score
    if _subsequence_hit(text, primary_kw):
        score += min(len(primary_kw), 5) * SUBSEQ_RATIO
    return score


def _load_deleted_memory_ids(conn: sqlite3.Connection | None = None) -> set[str]:
    """已删除 memory_id 集合；优先读 deleted_archive.db（与 history DELETE 分流）。"""
    del conn  # 保留签名兼容；deleted_ids 不再扫 history.db
    from memory_delete import load_deleted_ids

    return load_deleted_ids()


def _load_final_memories() -> dict[str, str]:
    """从 active_memories.db 加载活跃记忆（方案一：history 仅追溯，不参与检索）。"""
    from memory_sync import load_active_memories

    return load_active_memories()


def _load_memory_metadata() -> dict[str, dict[str, str]]:
    """从 active_memories 加载 metadata；Chroma 作兜底。"""
    from memory_sync import load_active_metadata

    metadata_map = load_active_metadata()
    if metadata_map:
        return metadata_map

    try:
        col = get_chroma_client().get_collection('mem0')
        result = col.get(include=['metadatas'])
    except Exception:
        return {}

    ids = result.get('ids') or []
    metas = result.get('metadatas') or []
    for memory_id, meta in zip(ids, metas):
        if not meta or memory_id in metadata_map:
            continue
        metadata_map[memory_id] = {
            'project': str(meta.get('project', '') or ''),
            'category': str(meta.get('category', '') or ''),
            'lang': str(meta.get('lang', '') or ''),
        }
    return metadata_map


def keyword_search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    filter_lang_en: bool = False,
    vec_rank_map: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """关键词匹配活跃记忆；4G 子序列依赖 vec_rank_map（须先 vector_search）。"""
    keywords = extract_keywords(query)
    if not keywords:
        return []

    primary_kw = primary_cjk_keyword(query)
    vec_rank_map = vec_rank_map or {}
    final_memories = _load_final_memories()
    vec_gate = _subseq_vec_gate(len(final_memories))
    metadata_map = _load_memory_metadata()
    scored: list[dict[str, Any]] = []

    for memory_id, text in final_memories.items():
        meta = metadata_map.get(memory_id, {})
        if filter_lang_en and should_exclude_lang_en(query, text, meta.get('lang', '')):
            continue

        score = _keyword_score_for_memory(
            text,
            keywords,
            primary_kw,
            vec_rank=vec_rank_map.get(memory_id, 0),
            vec_gate=vec_gate,
        )
        if score <= 0:
            continue

        scored.append({
            'id': memory_id,
            'text': text,
            'keyword_score': score,
            'vector_score': 0.0,
            'score': score,
            'source': 'keyword',
            'project': meta.get('project', ''),
            'category': meta.get('category', ''),
            'lang': meta.get('lang', '') or infer_memory_lang(text),
        })

    scored.sort(key=lambda item: item['score'], reverse=True)
    scored = _apply_keyword_relative_cutoff(scored)
    return scored[:top_k]


def _apply_keyword_relative_cutoff(
    results: list[dict[str, Any]],
    ratio: float | None = None,
) -> list[dict[str, Any]]:
    """4D：kw_score < top1 × ratio 截断，弱命中不进 RRF（kw_rank=0）。至少保留 top1。"""
    rel_ratio = KW_RELATIVE_RATIO if ratio is None else ratio
    if rel_ratio <= 0 or not results:
        return results

    top_score = float(results[0].get('keyword_score', results[0].get('score', 0)) or 0)
    if top_score <= 0:
        return results

    cutoff = top_score * rel_ratio
    filtered = [
        item for item in results
        if float(item.get('keyword_score', item.get('score', 0)) or 0) >= cutoff
    ]
    return filtered if filtered else results[:1]


def _apply_vector_relative_threshold(
    results: list[dict[str, Any]],
    margin: float | None = None,
) -> list[dict[str, Any]]:
    """按 top1 - δ 截断向量路：低于阈值的条目不进 RRF（vec_rank=0）。至少保留 top1。"""
    delta = VECTOR_SCORE_REL_MARGIN if margin is None else margin
    if delta <= 0 or not results:
        return results

    results.sort(key=lambda item: item['score'], reverse=True)
    top_score = float(results[0].get('vector_score', results[0].get('score', 0)) or 0)
    cutoff = top_score - delta
    filtered = [item for item in results if float(item.get('vector_score', item.get('score', 0)) or 0) >= cutoff]
    return filtered if filtered else results[:1]


def vector_search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    filter_lang_en: bool = False,
) -> list[dict[str, Any]]:
    """Ollama embedding + Chroma 向量检索。"""
    try:
        config = _load_config()
        embed_model = config.get('embedder', {}).get('config', {}).get('model', 'bge-m3')
        ollama_url = config.get('embedder', {}).get('config', {}).get(
            'ollama_base_url', 'http://localhost:11434'
        )

        payload = json.dumps({'model': embed_model, 'prompt': query}).encode()
        request = urllib.request.Request(
            f'{ollama_url}/api/embeddings',
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        response = urllib.request.urlopen(request, timeout=20)
        query_vector = json.loads(response.read()).get('embedding', [])
        if not query_vector:
            return []

        collection = get_chroma_client().get_collection('mem0')

        n_results = top_k
        if filter_lang_en:
            try:
                total = collection.count()
                n_results = min(max(top_k * LANG_VECTOR_OVERSAMPLE, top_k), total)
            except Exception:
                n_results = top_k * LANG_VECTOR_OVERSAMPLE

        raw = collection.query(
            query_embeddings=[query_vector],
            n_results=n_results,
            include=['metadatas', 'distances'],
        )

        results: list[dict[str, Any]] = []
        ids_list = raw.get('ids', [[]]) or [[]]
        metas_list = raw.get('metadatas', [[]]) or [[]]
        dists_list = raw.get('distances', [[]]) or [[]]

        for index in range(len(ids_list[0]) if ids_list and ids_list[0] else 0):
            memory_id = ids_list[0][index]
            meta = metas_list[0][index] if index < len(metas_list[0]) else {}
            distance = dists_list[0][index] if index < len(dists_list[0]) else 1.0
            score = 1.0 - distance / 2.0
            data_text = (meta or {}).get('data', '')
            if not data_text:
                continue

            lang = resolve_memory_lang(data_text, str((meta or {}).get('lang', '') or ''))
            if filter_lang_en and lang == 'en':
                continue

            results.append({
                'id': memory_id,
                'text': data_text,
                'keyword_score': 0.0,
                'vector_score': score,
                'score': score,
                'source': 'vector',
                'project': (meta or {}).get('project', '') or '',
                'category': (meta or {}).get('category', '') or '',
                'lang': lang,
            })
            if len(results) >= n_results:
                break

        results = _apply_vector_relative_threshold(results)
        return results[:top_k]
    except Exception:
        return []


def _rrf_term(rank: int, weight: float = 1.0) -> float:
    """单路 RRF 贡献；rank=0 表示该路未命中。"""
    if rank <= 0:
        return 0.0
    return weight / (RRF_K + rank)


def _normalize_source(source: str) -> str:
    """统一 source 标签（兼容旧 keyword+vector）。"""
    if source == 'keyword+vector':
        return 'both'
    return source


def _attach_rank(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为排序后的结果附加 1-based rank。"""
    for index, item in enumerate(results, start=1):
        item['rank'] = index
    return results


def _rank_key(item: dict[str, Any]) -> tuple:
    """merge 排序键：score 降序，再 vector/keyword rank 升序。"""
    return (
        -float(item.get('score', 0) or 0),
        item.get('vector_rank') or 9999,
        item.get('keyword_rank') or 9999,
        item.get('id', ''),
    )


def _is_preference_memory(item: dict[str, Any]) -> bool:
    """category=preference 的记忆（规范化后判断）。"""
    return normalize_category(str(item.get('category', '') or '')) == 'preference'


def _apply_result_bonuses(merged: list[dict[str, Any]], project: str = '') -> None:
    """就地写入 rrf_score / project_bonus / preference_bonus / score。"""
    for item in merged:
        rrf_score = float(item.get('score', 0) or 0)
        project_bonus = RRF_PROJECT_BONUS if project and _project_matches(item.get('project', ''), project) else 0.0
        preference_bonus = PREFERENCE_CROSS_BONUS if _is_preference_memory(item) else 0.0
        item['rrf_score'] = rrf_score
        item['project_bonus'] = project_bonus
        item['preference_bonus'] = preference_bonus
        item['score'] = rrf_score + project_bonus + preference_bonus


def _pick_with_project_quota(
    merged: list[dict[str, Any]],
    project: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """project 达标直保 + 全局保底，余量按 score 填充。直保要求 rrf_score >= PROJECT_QUOTA_MIN_RRF。"""
    project_pool = sorted(
        [item for item in merged if _project_matches(item.get('project', ''), project)],
        key=_rank_key,
    )
    global_pool = sorted(
        [item for item in merged if not _project_matches(item.get('project', ''), project)],
        key=_rank_key,
    )

    if not project_pool:
        return sorted(merged, key=_rank_key)[:max_results]

    picked: list[dict[str, Any]] = []
    picked_ids: set[str] = set()

    eligible_project = [
        item for item in project_pool
        if float(item.get('rrf_score', 0) or 0) >= PROJECT_QUOTA_MIN_RRF
    ]
    for item in eligible_project[:PROJECT_QUOTA_TOP]:
        picked.append(item)
        picked_ids.add(item['id'])

    for item in global_pool[:GLOBAL_QUOTA_MIN]:
        if item['id'] in picked_ids:
            continue
        picked.append(item)
        picked_ids.add(item['id'])

    if len(picked) < max_results:
        for item in sorted(merged, key=_rank_key):
            if item['id'] in picked_ids:
                continue
            picked.append(item)
            picked_ids.add(item['id'])
            if len(picked) >= max_results:
                break

    return picked[:max_results]


def merge_and_rank(
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    project: str = '',
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[dict[str, Any]]:
    """加权 RRF 合并两路排名，保留 keyword_score / vector_score 分离展示。"""
    project = normalize_project(project)

    kw_rank_map = {item['id']: rank for rank, item in enumerate(keyword_results, start=1)}
    vec_rank_map = {item['id']: rank for rank, item in enumerate(vector_results, start=1)}
    kw_item_map = {item['id']: item for item in keyword_results}
    vec_item_map = {item['id']: item for item in vector_results}

    merged: list[dict[str, Any]] = []
    for memory_id in set(kw_rank_map) | set(vec_rank_map):
        kw_rank = kw_rank_map.get(memory_id, 0)
        vec_rank = vec_rank_map.get(memory_id, 0)
        kw_item = kw_item_map.get(memory_id)
        vec_item = vec_item_map.get(memory_id)
        base = kw_item or vec_item or {}

        keyword_score = float((kw_item or {}).get('keyword_score', (kw_item or {}).get('score', 0)) or 0)
        vector_score = float((vec_item or {}).get('vector_score', (vec_item or {}).get('score', 0)) or 0)

        rrf_score = _rrf_term(vec_rank) + _rrf_term(kw_rank, RRF_KW_WEIGHT)
        if kw_rank > 0 and vec_rank > 0:
            rrf_score += RRF_BOTH_BONUS
            source = 'both'
        elif kw_rank > 0:
            source = 'keyword'
        else:
            source = 'vector'

        merged.append({
            **base,
            'id': memory_id,
            'keyword_score': keyword_score,
            'vector_score': vector_score,
            'keyword_rank': kw_rank,
            'vector_rank': vec_rank,
            'score': rrf_score,
            'source': source,
        })

    merged.sort(key=_rank_key)

    _apply_result_bonuses(merged, project)
    if not project:
        return _attach_rank(merged[:max_results])

    return _attach_rank(_pick_with_project_quota(merged, project, max_results))


def hybrid_search(
    query: str,
    project: str = '',
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """混合检索入口。"""
    query = (query or '').strip()
    if not query:
        return []

    recall_k = max(top_k, RRF_RECALL_TOP_K)
    filter_lang_en = query_has_cjk(query)
    vector_results = vector_search(query, top_k=recall_k, filter_lang_en=filter_lang_en)
    vec_rank_map = {item['id']: rank for rank, item in enumerate(vector_results, start=1)}
    keyword_results = keyword_search(
        query,
        top_k=recall_k,
        filter_lang_en=filter_lang_en,
        vec_rank_map=vec_rank_map,
    )
    return merge_and_rank(
        keyword_results,
        vector_results,
        project=project,
        max_results=max_results,
    )


def format_results_lines(
    results: list[dict[str, Any]],
    header: str = '[mem0相关记忆]',
) -> str:
    """格式化为可注入上下文的文本。"""
    if not results:
        return ''

    lines = [header]
    for item in results:
        project = item.get('project', '')
        scope_tag = f'[{project}]' if project else '[全局]'
        source = _normalize_source(item.get('source', ''))
        text = item.get('text', '')
        rank = item.get('rank', '')
        rank_prefix = f'#{rank} ' if rank else ''
        lines.append(f'- {rank_prefix}{scope_tag} ({source}) {text}')
    return '\n'.join(lines)


def backfill_lang_metadata(*, dry_run: bool = False) -> dict[str, int]:
    """一次性给 Chroma 存量记忆写入 metadata.lang（zh/en）。"""
    try:
        import chromadb
    except ImportError:
        return {'error': 1}

    client = get_chroma_client()
    collection = client.get_collection('mem0')
    raw = collection.get(include=['metadatas'])
    ids = raw.get('ids') or []
    metas = raw.get('metadatas') or []

    stats: dict[str, int] = {'total': 0, 'updated': 0, 'skipped': 0, 'zh': 0, 'en': 0}
    for memory_id, meta in zip(ids, metas):
        meta = dict(meta or {})
        text = str(meta.get('data', '') or '')
        lang = infer_memory_lang(text)
        stats['total'] += 1
        stats[lang] = stats.get(lang, 0) + 1

        if meta.get('lang') == lang:
            stats['skipped'] += 1
            continue

        meta['lang'] = lang
        if not dry_run:
            collection.update(ids=[memory_id], metadatas=[meta])
        stats['updated'] += 1

    return stats


def format_mcp_search_output(results: list[dict[str, Any]]) -> str:
    """MCP search_memory 返回格式。"""
    if not results:
        return '未找到相关记忆'

    lines: list[str] = []
    for item in results:
        project = item.get('project', '')
        scope_tag = f'[{project}]' if project else '[全局]'
        source = _normalize_source(item.get('source', ''))
        rank = item.get('rank', '')
        keyword_score = float(item.get('keyword_score', 0) or 0)
        vector_score = float(item.get('vector_score', 0) or 0)
        rrf_score = float(item.get('rrf_score', item.get('score', 0)) or 0)
        project_bonus = float(item.get('project_bonus', 0) or 0)
        preference_bonus = float(item.get('preference_bonus', 0) or 0)
        kw_rank = int(item.get('keyword_rank', 0) or 0)
        vec_rank = int(item.get('vector_rank', 0) or 0)
        rank_prefix = f'#{rank} ' if rank else ''
        bonus_parts = []
        if project_bonus > 0:
            bonus_parts.append(f'proj=+{project_bonus:.4f}')
        if preference_bonus > 0:
            bonus_parts.append(f'pref=+{preference_bonus:.4f}')
        bonus_part = (' ' + ' '.join(bonus_parts)) if bonus_parts else ''
        lines.append(
            f'{rank_prefix}[{item.get("id", "")}] {scope_tag} ({source}) '
            f'kw={keyword_score:.2f} vec={vector_score:.2f} '
            f'kw_rank={kw_rank} vec_rank={vec_rank} rrf={rrf_score:.4f}{bonus_part} '
            f'{item.get("text", "")}'
        )
    return '\n'.join(lines)
