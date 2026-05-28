"""mem0 混合检索：关键词(history.db) + 向量(Chroma/Ollama)，供 MCP / Claude / Cursor 共用"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from typing import Any

CONFIG_PATH = os.getenv('MEM0_CONFIG', os.path.expanduser('~/.mem0/config_local.json'))
DEFAULT_USER = os.getenv('MEM0_USER_ID', 'default-user')
HISTORY_DB = os.getenv('MEM0_HISTORY_DB', os.path.expanduser('~/.mem0/history.db'))
CHROMA_DB_PATH = os.getenv('MEM0_CHROMA_PATH', os.path.expanduser('~/.mem0/chroma_db'))

DEFAULT_TOP_K = 15
DEFAULT_MAX_RESULTS = 5
MIN_VECTOR_SCORE = 0.35

GENERIC_DIR_NAMES = frozenset({
    'Desktop', 'Documents', 'Home', 'home', 'Downloads', 'src', 'code', 'projects', 'tmp',
})

# 项目别名映射：从 ~/.mem0/project_aliases.json 加载
_ALIASES_PATH = os.path.expanduser('~/.mem0/project_aliases.json')


def _load_project_aliases() -> dict[str, str]:
    """从配置文件加载项目别名映射，不存在时返回空字典。"""
    try:
        with open(_ALIASES_PATH, encoding='utf-8') as f:
            aliases = json.load(f)
        return aliases if isinstance(aliases, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


PROJECT_ALIASES: dict[str, str] = _load_project_aliases()


def _load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding='utf-8') as config_file:
        return json.load(config_file)


def normalize_project(project: str) -> str:
    """统一项目标识，空字符串表示全局。"""
    return (project or '').strip()


def detect_project(cwd: str | None = None) -> str:
    """从工作目录推断 mem0 project 标识。"""
    work_dir = (cwd or os.getcwd()).rstrip('/')
    name = os.path.basename(work_dir) if work_dir else ''
    if name in PROJECT_ALIASES:
        return PROJECT_ALIASES[name]
    if name in GENERIC_DIR_NAMES:
        return ''
    return name


def extract_keywords(query: str) -> list[str]:
    """从查询中提取中英文关键词。"""
    keywords: list[str] = []
    chinese_words = re.findall(r'[一-鿿]{2,}', query)
    keywords.extend(chinese_words)
    keywords.extend(re.findall(r'[一-鿿]', query))
    keywords.extend(re.findall(r'[a-zA-Z0-9_]+', query.lower()))
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
        }
    return metadata_map


def keyword_search(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict[str, Any]]:
    """关键词匹配 history 最终记忆文本。"""
    keywords = extract_keywords(query)
    if not keywords:
        return []

    final_memories = _load_final_memories()
    metadata_map = _load_memory_metadata()
    scored: list[dict[str, Any]] = []

    for memory_id, text in final_memories.items():
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

        meta = metadata_map.get(memory_id, {})
        scored.append({
            'id': memory_id,
            'text': text,
            'score': score,
            'source': 'keyword',
            'project': meta.get('project', ''),
            'category': meta.get('category', ''),
        })

    scored.sort(key=lambda item: item['score'], reverse=True)
    return scored[:top_k]


def vector_search(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict[str, Any]]:
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
        raw = collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
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
            if not data_text or score < MIN_VECTOR_SCORE:
                continue
            results.append({
                'id': memory_id,
                'text': data_text,
                'score': score,
                'source': 'vector',
                'project': (meta or {}).get('project', '') or '',
                'category': (meta or {}).get('category', '') or '',
            })

        results.sort(key=lambda item: item['score'], reverse=True)
        return results
    except Exception:
        return []


def merge_and_rank(
    keyword_results: list[dict[str, Any]],
    vector_results: list[dict[str, Any]],
    project: str = '',
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[dict[str, Any]]:
    """合并关键词与向量结果，项目记忆优先。"""
    project = normalize_project(project)
    seen: dict[str, dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []

    for item in keyword_results:
        memory_id = item['id']
        if memory_id not in seen:
            seen[memory_id] = item
            merged.append(item)

    for item in vector_results:
        memory_id = item['id']
        if memory_id not in seen:
            seen[memory_id] = item
            merged.append(item)
        else:
            existing = seen[memory_id]
            existing['score'] = existing['score'] + item['score'] * 0.5
            existing['source'] = 'keyword+vector'

    merged.sort(key=lambda item: item['score'], reverse=True)

    if not project:
        return merged[:max_results]

    project_items = [item for item in merged if item.get('project') == project]
    global_items = [item for item in merged if item.get('project') != project]
    final = project_items[:max(3, max_results - 2)] + global_items[:2]
    return final[:max_results]


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

    keyword_results = keyword_search(query, top_k=top_k)
    vector_results = vector_search(query, top_k=top_k)
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
        source = item.get('source', '')
        score = item.get('score', 0)
        text = item.get('text', '')
        if isinstance(score, (int, float)) and score > 0:
            lines.append(f'- {scope_tag} ({source}) {text} (相关度:{score:.2f})')
        else:
            lines.append(f'- {scope_tag} ({source}) {text}')
    return '\n'.join(lines)


def format_mcp_search_output(results: list[dict[str, Any]]) -> str:
    """MCP search_memory 返回格式。"""
    if not results:
        return '未找到相关记忆'

    lines: list[str] = []
    for item in results:
        project = item.get('project', '')
        scope_tag = f'[{project}]' if project else '[全局]'
        score = item.get('score', 0)
        source = item.get('source', '')
        score_text = f' score={score:.2f}' if isinstance(score, (int, float)) else ''
        lines.append(
            f"[{item.get('id', '')}] {scope_tag} ({source}){score_text} {item.get('text', '')}"
        )
    return '\n'.join(lines)