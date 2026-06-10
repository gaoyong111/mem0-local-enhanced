"""本地mem0 MCP server — 支持项目级作用域，混合检索，B+C+D+E 写入策略，LLM 自动兜底"""

from mcp.server.fastmcp import FastMCP
import json
import logging
import os
import sys
import time

# mem0 安装目录
_MEM0_DIR = os.getenv('MEM0_DIR', os.path.expanduser('~/.mem0'))
if _MEM0_DIR not in sys.path:
    sys.path.insert(0, _MEM0_DIR)

# C：在 Memory 初始化前打补丁
from mem0_add_policy import (  # noqa: E402
    CHINESE_INFER_INSTRUCTIONS,
    apply_mem0_patches,
    prepare_add_plan,
    run_merge_check,
)
from memory_lineage import parse_merged_from, record_event, record_merge_result  # noqa: E402

apply_mem0_patches()

from mem0 import Memory  # noqa: E402
from hybrid_search import (  # noqa: E402
    detect_project,
    format_mcp_search_output,
    hybrid_search,
    normalize_project,
)

logger = logging.getLogger('mem0-local')

_PRIMARY_CONFIG = os.getenv('MEM0_CONFIG', os.path.expanduser('~/.mem0/config_local.json'))
_FALLBACK_CONFIG = os.getenv('MEM0_FALLBACK_CONFIG', os.path.expanduser('~/.mem0/config_ollama.json'))
DEFAULT_USER = os.getenv('MEM0_USER_ID', 'default-user')
DEFAULT_MAX_RESULTS = 8
PENDING_DIR = os.path.expanduser('~/.mem0/pending')
MAX_RETRY_COUNT = 3

mcp = FastMCP('mem0-local')


def _init_memory(primary_path: str, fallback_path: str) -> Memory:
    """先尝试主配置，失败则自动切换到兜底配置。"""
    for label, path in [('主配置', primary_path), ('兜底配置', fallback_path)]:
        try:
            with open(path, encoding='utf-8') as f:
                config = json.load(f)
            mem = Memory.from_config(config)
            # 用一次轻量 search 验证 LLM 通路可用
            mem.get_all(filters={'user_id': DEFAULT_USER})
            logger.info('mem0 初始化成功: %s (%s)', label, path)
            return mem
        except Exception as exc:
            logger.warning('mem0 初始化失败: %s (%s): %s', label, path, exc)
    raise RuntimeError('mem0 主配置和兜底配置均初始化失败')


_memory = _init_memory(_PRIMARY_CONFIG, _FALLBACK_CONFIG)


def _safe_delete(memory_id: str) -> None:
    try:
        _memory.delete(memory_id)
    except ValueError as error:
        if 'not found' not in str(error).lower():
            raise
    except IndexError:
        pass


@mcp.tool()
def add_memory(content: str, metadata: str = '', project: str = '', infer: str = 'false') -> str:
    """添加一条记忆到本地mem0。

    content: 要记忆的内容（中文完整句，含模块名/字段名更易检索）

    metadata: 可选 JSON，例如：
      - {"category":"reference","project":"your-project"}
      - {"category":"reference","structured":{"module":"moduleName","field":"fieldName","rule":"字符串","keywords":["关键词","API"]}}
      - {"category":"episodic"}  （category 标签，五类之一；留空默认 episodic）

    category 取值：episodic（踩坑/事件）、behavior（行为规则）、workflow（流程方法）、
    reference（事实知识）、preference（用户偏好）。未知/历史标签会自动规范化。

    project: 项目标识
    infer: 默认 false；显式 true 亦被忽略，全部 verbatim 原样入库
    """
    plan = prepare_add_plan(content, metadata, project, infer)

    add_kwargs: dict = {
        'user_id': DEFAULT_USER,
        'metadata': plan.metadata,
        'infer': plan.use_infer,
    }
    if plan.infer_prompt:
        add_kwargs['prompt'] = plan.infer_prompt

    try:
        result = _memory.add(plan.content, **add_kwargs)
    except Exception as exc:
        logger.error('mem0 add 失败，写入 pending 队列: %s', exc)
        _write_to_pending(plan.content, plan.metadata, project, plan.use_infer)
        return f'写入mem0失败: {exc}。已自动存入待办队列({PENDING_DIR})，每日复盘时会重试。'
    items = result.get('results', []) if isinstance(result, dict) else []
    if not items:
        return '添加完成（无新记忆产生，可能已存在类似记忆）'

    ids = [item.get('id', '?') for item in items]
    events = [item.get('event', '?') for item in items]
    scope = project or '全局'

    mode_notes = {
        'structured': '结构化原样入库',
        'verbatim': '原样入库',
    }
    mode_note = mode_notes.get(plan.storage_mode, '原样入库')

    merge_notes: list[str] = []
    lineage_notes: list[str] = []
    if plan.run_merge_check and ids and ids[0] != '?':
        merge_note = run_merge_check(
            _memory.llm,
            ids[0],
            plan.content,
            normalize_project(project),
            delete_memory=_safe_delete,
            hybrid_search_fn=hybrid_search,
        )
        if merge_note:
            merge_notes.append(merge_note)
            if '已删除' in merge_note:
                return f'记忆[{scope}]（{mode_note}）与已有记忆重复，未新增。{merge_note}'

    if ids and ids[0] != '?':
        merged_sources = parse_merged_from(plan.metadata)
        if merged_sources:
            record_merge_result(
                ids[0],
                merged_sources,
                category=str(plan.metadata.get('category', '') or ''),
                content_preview=plan.content,
                actor='add_memory',
            )
            lineage_notes.append(f'合并留痕：来源 {",".join(merged_sources)}')

    extra_parts = merge_notes + lineage_notes
    extra = f'；{"；".join(extra_parts)}' if extra_parts else ''
    return f'已处理记忆[{scope}]（{mode_note}），ID: {ids}, 事件: {events}{extra}'


@mcp.tool()
def search_memory(query: str, project: str = '') -> str:
    """搜索本地mem0中的记忆（关键词+向量混合检索）。
    query为搜索关键词，project可选(限定项目范围，留空则自动从上下文推断)
    """
    effective_project = normalize_project(project) or detect_project()
    results = hybrid_search(
        query,
        project=effective_project,
        max_results=DEFAULT_MAX_RESULTS,
    )
    return format_mcp_search_output(results)


@mcp.tool()
def get_all_memories(project: str = '') -> str:
    """获取本地mem0中的所有记忆。project可选(限定项目范围，留空获取全局)"""
    if project:
        filters = {'user_id': DEFAULT_USER, 'project': project}
    else:
        filters = {'user_id': DEFAULT_USER}
    resp = _memory.get_all(filters=filters)
    results = resp.get('results', []) if isinstance(resp, dict) else resp
    if not results:
        return '暂无记忆'
    lines = []
    for item in results:
        mem_project = item.get('metadata', {}) or {}
        proj = mem_project.get('project', '')
        scope_tag = f'[{proj}]' if proj else '[全局]'
        lines.append(f"[{item.get('id', '')}] {scope_tag} {item.get('memory', str(item))}")
    return '\n'.join(lines)


@mcp.tool()
def delete_memory(memory_id: str) -> str:
    """删除一条记忆。memory_id为要删除的记忆ID"""
    try:
        _memory.delete(memory_id)
        record_event(
            'DELETE',
            memory_id,
            note='MCP delete_memory',
            actor='mcp',
        )
        return f'已删除记忆 {memory_id}'
    except ValueError as error:
        if 'not found' in str(error).lower():
            return f'记忆 {memory_id} 已不存在（可能此前已删除）'
        raise
    except IndexError:
        return f'记忆 {memory_id} 已不存在（可能此前已删除）'


def _write_to_pending(content: str, metadata: dict, project: str, use_infer: bool) -> None:
    """写入失败时将记忆存到 pending 目录，等后续重试。"""
    os.makedirs(PENDING_DIR, exist_ok=True)
    slug = content[:20].replace(' ', '_').replace('/', '_')
    filename = f'{slug}_{int(time.time())}.json'
    filepath = os.path.join(PENDING_DIR, filename)
    payload = {
        'content': content,
        'metadata': metadata,
        'project': project,
        'use_infer': False,  # 永久关闭infer，pending也强制false
        'retry_count': 0,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info('已写入 pending: %s', filepath)


@mcp.tool()
def retry_pending() -> str:
    """扫描 pending 目录，重试写入 mem0。成功则删除文件，失败则 retry_count+1。
    超过 MAX_RETRY_COUNT 次失败标记为 manual_review。每日复盘 cron 会调用此工具。"""
    if not os.path.isdir(PENDING_DIR):
        return 'pending 目录不存在，无需重试'

    files = sorted(f for f in os.listdir(PENDING_DIR) if f.endswith('.json'))
    if not files:
        return 'pending 队列空，无需重试'

    success_count = 0
    fail_count = 0
    manual_review = []

    for filename in files:
        filepath = os.path.join(PENDING_DIR, filename)
        with open(filepath, encoding='utf-8') as f:
            payload = json.load(f)

        payload['retry_count'] += 1
        plan = prepare_add_plan(
            payload['content'],
            json.dumps(payload.get('metadata', {})),
            payload.get('project', ''),
            'false',  # 永久关闭infer，pending重试也强制false
        )

        try:
            add_kwargs: dict = {
                'user_id': DEFAULT_USER,
                'metadata': plan.metadata,
                'infer': plan.use_infer,
            }
            if plan.infer_prompt:
                add_kwargs['prompt'] = plan.infer_prompt
            result = _memory.add(plan.content, **add_kwargs)
            os.remove(filepath)
            success_count += 1
        except Exception as exc:
            if payload['retry_count'] >= MAX_RETRY_COUNT:
                payload['status'] = 'manual_review'
                payload['last_error'] = str(exc)
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                manual_review.append(filename)
            else:
                payload['last_error'] = str(exc)
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                fail_count += 1

    lines = [f'重试完成: 成功{success_count}条, 失败{fail_count}条']
    if manual_review:
        lines.append(f'需人工介入: {manual_review}')
    return '\n'.join(lines)


if __name__ == '__main__':
    mcp.run()