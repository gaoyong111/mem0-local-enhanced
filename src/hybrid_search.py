"""mem0 混合检索：关键词(history.db) + 向量(Chroma/Ollama)，供 MCP / Claude / Cursor 共用"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from typing import Any

CONFIG_PATH = os.path.expanduser('~/.mem0/config_local.json')
DEFAULT_USER = os.getenv('MEM0_DEFAULT_USER_ID', os.getenv('MEM0_USER_ID', 'default-user'))
HISTORY_DB = os.path.expanduser('~/.mem0/history.db')
CHROMA_DB_PATH = os.path.expanduser('~/.mem0/chroma_db')

DEFAULT_TOP_K = 15
DEFAULT_MAX_RESULTS = 5
RRF_RECALL_TOP_K = 50

# 加权 RRF：score = 1/(K+vec_rank) + α·1/(K+kw_rank) [+ β 双路都命中]
RRF_K = 15
RRF_KW_WEIGHT = 0.5
RRF_BOTH_BONUS = 0.008

# Phase 3 lang 分轨：中文 query 排除纯英文记忆
_CJK_RE = re.compile(r'[一-鿿]')
LANG_VECTOR_OVERSAMPLE = 4

GENERIC_DIR_NAMES = frozenset({
    'Desktop', 'Documents', 'Home', 'home', 'Downloads', 'src', 'code', 'projects', 'tmp',
})

PROJECT_ALIASES_PATH = os.path.join(
    os.getenv('MEM0_DIR', os.path.expanduser('~/.mem0')),
    'project_aliases.json',
)


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
    """从查询中提取中英文关键词。中文用滑动窗口提取2-4字词组，避免单字噪音和整句匹配。"""
    keywords: list[str] = []
    for segment in re.findall(r"[一-鿿]+", query):
        for size in range(2, min(5, len(segment) + 1)):
            for i in range(len(segment) - size + 1):
                keywords.append(segment[i:i + size])
    keywords.extend(re.findall(r"[a-zA-Z0-9_]+", query.lower()))
    seen: set[str] = set()
    unique: list[str] = []
    for keyword in keywords:
        key = keyword.lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(keyword)
    return unique


def _load_deleted_memory_ids(conn: sqlite3.Connection) -> set[str]:
    """mem0 删除时 ADD 行可能仍 is_deleted=0，需以 DELETE 事件为准。"""
    rows = conn.execute(
        """
        SELECT DISTINCT memory_id
        FROM history
        WHERE event = 'DELETE' AND memory_id IS NOT NULL
        """
    ).fetchall()
    return {memory_id for memory_id, in rows if memory_id}


def _load_final_memories() -> dict[str, str]:
    """从 history.db 构建 memory_id -> 最终文本（排除已 DELETE 的记忆）。"""
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
    """从 Chroma 加载 memory_id -> {project, category}。"""
    try:
        import chromadb

        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        col = client.get_collection('mem0')
        result = col.get(include=['metadatas'])
    except Exception:
        return {}

    metadata_map: dict[str, dict[str, str]] = {}
    ids = result.get('ids') or []
    metas = result.get('metadatas') or []
    for memory_id, meta in zip(ids, metas):
        if not meta:
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
) -> list[dict[str, Any]]:
    """关键词匹配 history 最终记忆文本。"""
    keywords = extract_keywords(query)
    if not keywords:
        return []

    final_memories = _load_final_memories()
    metadata_map = _load_memory_metadata()
    scored: list[dict[str, Any]] = []

    for memory_id, text in final_memories.items():
        meta = metadata_map.get(memory_id, {})
        if filter_lang_en and should_exclude_lang_en(query, text, meta.get('lang', '')):
            continue

        score = 0.0
        text_lower = text.lower()
        for keyword in keywords:
            keyword_lower = keyword.lower()
            count = text_lower.count(keyword_lower)
            if count > 0:
                weight = min(len(keyword_lower), 5)
                score += count * weight

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
    return scored[:top_k]


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

        import chromadb

        client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        collection = client.get_collection('mem0')

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
            if len(results) >= top_k:
                break

        results.sort(key=lambda item: item['score'], reverse=True)
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

    merged.sort(
        key=lambda item: (
            -float(item.get('score', 0) or 0),
            item.get('vector_rank') or 9999,
            item.get('keyword_rank') or 9999,
            item.get('id', ''),
        ),
    )

    if not project:
        return _attach_rank(merged[:max_results])

    project_items = [item for item in merged if _project_matches(item.get('project', ''), project)]
    global_items = [item for item in merged if not _project_matches(item.get('project', ''), project)]

    # 指定 project 但检索结果里没有该项目记忆时，退回全量 Top-N（避免只剩 2 条全局）
    if not project_items:
        return _attach_rank(merged[:max_results])

    final = project_items[:max(3, max_results - 2)] + global_items[:2]
    return _attach_rank(final[:max_results])


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
    keyword_results = keyword_search(query, top_k=recall_k, filter_lang_en=filter_lang_en)
    vector_results = vector_search(query, top_k=recall_k, filter_lang_en=filter_lang_en)
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

    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
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
        rrf_score = float(item.get('score', 0) or 0)
        kw_rank = int(item.get('keyword_rank', 0) or 0)
        vec_rank = int(item.get('vector_rank', 0) or 0)
        rank_prefix = f'#{rank} ' if rank else ''
        lines.append(
            f'{rank_prefix}[{item.get("id", "")}] {scope_tag} ({source}) '
            f'kw={keyword_score:.2f} vec={vector_score:.2f} '
            f'kw_rank={kw_rank} vec_rank={vec_rank} rrf={rrf_score:.4f} '
            f'{item.get("text", "")}'
        )
    return '\n'.join(lines)
