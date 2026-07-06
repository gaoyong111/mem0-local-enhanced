#!/usr/bin/env python3
"""一次性英文/infer 遗留记忆治理。需 Ollama 可用以 re-embed。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time

_MEM0_DIR = os.getenv('MEM0_DIR', os.path.expanduser('~/.mem0'))
_SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
for path in (_MEM0_DIR, _SRC_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from mem0_add_policy import apply_mem0_patches  # noqa: E402

apply_mem0_patches()

from mem0 import Memory  # noqa: E402
from hybrid_search import get_chroma_client, HISTORY_DB  # noqa: E402
from memory_lineage import record_event  # noqa: E402

_CJK_RE = re.compile(r'[一-鿿]')
_ASCII_WORD_RE = re.compile(r'[A-Za-z]{8,}')


def infer_memory_lang(text: str) -> str:
    """推断记忆语言。库内统一 zh；含中文即 zh，纯英文碎片应改写或删除。"""
    value = (text or '').strip()
    if not value:
        return 'zh'
    if _CJK_RE.search(value):
        return 'zh'
    return 'en'


CONFIG = os.getenv('MEM0_CONFIG', os.path.expanduser('~/.mem0/config_local.json'))
DEFAULT_USER = os.getenv('MEM0_USER_ID', 'default-user')


def _embed(text: str) -> list[float]:
    with open(CONFIG, encoding='utf-8') as handle:
        cfg = json.load(handle)
    model = cfg.get('embedder', {}).get('config', {}).get('model', 'bge-m3')
    url = cfg.get('embedder', {}).get('config', {}).get('ollama_base_url', 'http://localhost:11434')
    import urllib.request

    payload = json.dumps({'model': model, 'prompt': text}).encode()
    request = urllib.request.Request(
        f'{url}/api/embeddings',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    response = urllib.request.urlopen(request, timeout=30)
    return json.loads(response.read()).get('embedding', [])


def delete_memory(memory_id: str, note: str = '') -> None:
    from memory_delete import archive_delete

    reason = (note or '').strip() or 'english_grooming'
    archive_delete(
        memory_id,
        reason,
        actor='english_grooming',
        source='english_grooming',
    )


def update_metadata(memory_id: str, patches: dict[str, str]) -> None:
    col = get_chroma_client().get_collection('mem0')
    raw = col.get(ids=[memory_id], include=['metadatas'])
    if not raw['ids']:
        raise ValueError(f'missing chroma id {memory_id}')
    meta = dict(raw['metadatas'][0] or {})
    meta.update(patches)
    col.update(ids=[memory_id], metadatas=[meta])
    record_event('CATEGORY_CHANGE', memory_id, note=f'metadata {patches}', actor='english_grooming')


def replace_text_keep_id(memory_id: str, new_text: str, meta_patches: dict[str, str]) -> None:
    col = get_chroma_client().get_collection('mem0')
    raw = col.get(ids=[memory_id], include=['metadatas'])
    if not raw['ids']:
        raise ValueError(f'missing chroma id {memory_id}')
    old_meta = dict(raw['metadatas'][0] or {})
    old_text = str(old_meta.get('data', '') or '')

    new_meta = {**old_meta, **meta_patches, 'data': new_text}
    new_meta['lang'] = meta_patches.get('lang') or infer_memory_lang(new_text)
    vector = _embed(new_text)
    if not vector:
        raise RuntimeError(f'embed failed for {memory_id}')

    col.update(
        ids=[memory_id],
        documents=[new_text],
        embeddings=[vector],
        metadatas=[new_meta],
    )

    sys.path.insert(0, os.path.expanduser('~/.mem0'))
    from memory_sync import sync_active_update_content

    sync_active_update_content(
        memory_id,
        new_text,
        project=str(new_meta.get('project', '') or ''),
        category=str(new_meta.get('category', '') or ''),
        lang=str(new_meta.get('lang', '') or 'zh'),
    )

    record_event(
        'UPDATE',
        memory_id,
        note='english_grooming text replace',
        content_preview=new_text,
        actor='english_grooming',
    )


def add_memory(content: str, metadata: dict[str, str], source_ids: list[str] | None = None) -> str:
    meta = dict(metadata)
    meta['lang'] = meta.get('lang') or infer_memory_lang(content)
    meta['storage_mode'] = 'verbatim'
    if meta.get('project') == '全局':
        meta.pop('project', None)

    result = memory.add(content, user_id=DEFAULT_USER, metadata=meta, infer=False)
    items = result.get('results', []) if isinstance(result, dict) else []
    if not items:
        raise RuntimeError(f'add returned empty: {content[:40]}')
    new_id = items[0].get('id', '')
    record_event(
        'MERGE' if source_ids else 'ADD',
        new_id,
        source_ids=source_ids or [],
        note='english_grooming rewrite',
        content_preview=content,
        category=meta.get('category', ''),
        actor='english_grooming',
    )
    return new_id


print('init Memory...')
with open(CONFIG, encoding='utf-8') as handle:
    memory = Memory.from_config(json.load(handle))

# --- 删除：重复 / 过时 / 已合并 ---
DELETE_IDS = [
    ('3cdc6310-bebc-4e5a-b146-182fb3ed3900', 'dup 线上流程'),
    ('92868da2-e60c-43d8-b5b9-5cb4edb77033', 'dup 线上流程'),
    ('acd375a2-f22f-42af-b09b-b1bcbed3911c', 'dup mem0添加失败'),
    ('7a51824d-b4d1-46ab-82a2-032b84edd871', 'dup favorites乐观更新'),
    ('94684d1e-790a-4559-be17-0088cf2d1e39', 'merged into f88eccbe'),
    ('a013d297-5450-4438-8af6-00015647c595', 'merged into 31fb4cb4'),
    ('a2ba2147-f9f1-4a7d-9295-1cccdfbfc2d7', 'merged workflow写入规范'),
    ('889c7fcd-f2f6-46f1-8617-1e1ee33a5eae', 'dup 线上流程短版'),
    ('3148cb75-b716-4bf6-a2fd-e84a27b6b3a7', 'dup 线上流程'),
    ('88878ecb-52e2-4ff6-b8e1-83f95cbd316e', 'obsolete BM25'),
    ('17561330-c881-4681-a3c2-9eb9e23d750c', 'generic Redis'),
    ('6e2a6dd1-a7e2-4b98-8814-8925174a50c6', 'generic Docker TZ'),
    ('284b96f9-a078-44bf-9821-737dc90a78ee', 'dup infer根因 e29e9c76'),
    ('1ef278db-f849-46a1-a3b9-49bcd679deda', 'rules in CLAUDE P0'),
    ('0462efcc-7518-4867-af07-807b5d61237e', 'merged into 330226b8'),
    ('1cc243c4-f003-4f7a-85b3-3bdae93e15b7', 'merged into pharmacy全局'),
    ('206e42ef-39c2-4955-a283-423b9f20a712', 'merged new workflow写入'),
]

for mid, note in DELETE_IDS:
    try:
        delete_memory(mid, note)
        print('DELETE', mid[:8], note)
    except Exception as exc:
        print('DELETE FAIL', mid[:8], exc)

# --- 改写成中文：删旧存新 ---
REWRITES = [
    {
        'old_id': 'daa8896f-a249-4bab-8b6a-2364563e8e8e',
        'content': '用户偏好使用 Claude Code 开发，启动命令为 claude --mode auto-accept。',
        'meta': {'category': 'preference', 'lang': 'zh'},
        'sources': ['daa8896f-a249-4bab-8b6a-2364563e8e8e'],
    },
    {
        'old_id': '98e46c83-1bda-46d0-a8bd-95da34b1ded2',
        'content': (
            'ehealth-yypt-pharmacy（药房新项目）：Vue3+Rsbuild+Pinia+TypeScript+Element Plus，'
            'Tailwind v4(tw-前缀)；本地路径 /Users/gaoyong/Desktop/h5_release/yypt/ehealth-yypt-pharmacy，'
            '部署子路径 /ehealth-yypt-pharmacy/。'
        ),
        'meta': {'category': 'reference', 'lang': 'zh'},
        'sources': [
            '98e46c83-1bda-46d0-a8bd-95da34b1ded2',
            '1cc243c4-f003-4f7a-85b3-3bdae93e15b7',
        ],
    },
    {
        'old_id': '13bdbe51-9013-4362-885c-ad6a638db2ee',
        'content': (
            'ehealth-yypt-opscenter（管理新项目）：Vue3+Rsbuild+Pinia+TypeScript；'
            '本地路径 /Users/gaoyong/Desktop/h5_release/yypt/ehealth-yypt-opscenter，'
            '部署子路径 /ehealth-yypt-opscenter/。'
        ),
        'meta': {'category': 'reference', 'lang': 'zh'},
        'sources': ['13bdbe51-9013-4362-885c-ad6a638db2ee'],
    },
    {
        'old_id': 'd690e72b-70cf-4a76-a290-eb51d21057bf',
        'content': '用户姓名高勇，杭州全栈工程师，偏好 Mac 开发，对话语言使用中文。',
        'meta': {'category': 'preference', 'lang': 'zh'},
        'sources': ['d690e72b-70cf-4a76-a290-eb51d21057bf'],
    },
    {
        'old_id': 'b8e412d5-2900-463a-9da4-1dc827eb3243',
        'content': (
            '老项目 ehealth-yypt：Vue3+Vuex 单体，80+ 模块微前端宿主，'
            '路径 /Users/gaoyong/Desktop/h5_release/ehealth-yypt，'
            'URL 格式 https://域名/ehealth-yypt/#/路由路径。'
        ),
        'meta': {'category': 'reference', 'lang': 'zh'},
        'sources': ['b8e412d5-2900-463a-9da4-1dc827eb3243'],
    },
    {
        'old_id': 'f88eccbe-c5c6-4227-a3fe-955f5d67ed96',
        'content': (
            '新项目 ehealth-yypt-pharmacy 与 ehealth-yypt-opscenter 通过 /iframe?url= 嵌入老项目 ehealth-yypt 未迁移页面，'
            '沿用 URL 格式 https://域名/ehealth-yypt/#/路由路径；两项目共享 ehealth-somp 架构、独立部署，从老单体渐进迁移。'
        ),
        'meta': {'category': 'reference', 'lang': 'zh'},
        'sources': ['f88eccbe-c5c6-4227-a3fe-955f5d67ed96', '94684d1e-790a-4559-be17-0088cf2d1e39'],
    },
    {
        'old_id': 'b36a081d-facf-43e0-85d0-780a343cc0cf',
        'content': 'healthWeb-base 项目：Vue2+Webpack 技术栈，医疗健康互联网平台。',
        'meta': {'category': 'reference', 'lang': 'zh'},
        'sources': ['b36a081d-facf-43e0-85d0-780a343cc0cf'],
    },
]

for item in REWRITES:
    try:
        delete_memory(item['old_id'], 'rewrite zh')
        new_id = add_memory(item['content'], item['meta'], item.get('sources'))
        print('REWRITE', item['old_id'][:8], '->', new_id[:8])
    except Exception as exc:
        print('REWRITE FAIL', item['old_id'][:8], exc)

# --- 新增：workflow 写入规范（承接 206e42ef / a2ba2147）---
try:
    wf_id = add_memory(
        '何时写入 mem0：完成有价值修复或决策后，根因不显而易见或涉及非通用知识时记录；'
        '写入前先 search_memory 查重，避免碎片重复。'
        'Why：记忆价值在可检索的 why 层面经验，不是操作流水。'
        'How to apply：add_memory 前 search_memory；禁止重复碎片；infer 永久 false，原样中文入库。',
        {'category': 'workflow', 'lang': 'zh'},
        ['206e42ef-39c2-4955-a283-423b9f20a712', 'a2ba2147-f9f1-4a7d-9295-1cccdfbfc2d7'],
    )
    print('ADD workflow', wf_id[:8])
except Exception as exc:
    print('ADD workflow FAIL', exc)

# --- 原地更新正文 ---
col = get_chroma_client().get_collection('mem0')
raw = col.get(ids=['330226b8-fa49-4d69-8131-7d575992cb29'], include=['metadatas'])
base_text = str((raw['metadatas'][0] or {}).get('data', ''))
merged_330 = (
    base_text
    + '\n\n入库策略：reference/structured 原样入库，infer 永久关闭全部 verbatim；'
    'category 五类（episodic/behavior/workflow/reference/preference）仅作标签。'
    '写入后触发 E-strategy 合并去重。检索走 hybrid_search（keyword+向量 RRF）。'
)
try:
    replace_text_keep_id(
        '330226b8-fa49-4d69-8131-7d575992cb29',
        merged_330,
        {'category': 'reference', 'lang': 'zh'},
    )
    print('UPDATE 330226b8 merged 0462efcc')
except Exception as exc:
    print('UPDATE 330226b8 FAIL', exc)

pyenv_text = (
    'macOS 下 Homebrew Python 与 pyenv 可能冲突，PATH 优先级导致 pip 指向错误解释器。'
    'Why：系统 python3 默认为 Xcode 自带 3.9，无第三方包。'
    'How to apply：.zshrc 中 pyenv 初始化放在 brew PATH 之后；'
    '脚本依赖用 /Users/gaoyong/.pyenv/versions/3.10.17/bin/python3；用 pyenv which python 确认活跃版本。'
)
try:
    replace_text_keep_id(
        '31fb4cb4-bece-4aea-a83f-f2b52b685018',
        pyenv_text,
        {'category': 'workflow', 'lang': 'zh', 'project': '全局'},
    )
    print('UPDATE 31fb4cb4 pyenv')
except Exception as exc:
    print('UPDATE 31fb4cb4 FAIL', exc)

review_text = (
    '复盘时间范围必须增量而非按自然月硬切：从上次截止点扫描到当前。'
    'Why：用户设计为增量复盘，按月切会重复覆盖已复盘历史。'
    'How to apply：daily-review 取 last_review_date 至今的 sessions/commits，不用自然月边界。'
)
try:
    replace_text_keep_id(
        '99757b8f-15ee-4571-baa0-507972568878',
        review_text,
        {'category': 'episodic', 'lang': 'zh'},
    )
    print('UPDATE 99757b8f')
except Exception as exc:
    print('UPDATE 99757b8f FAIL', exc)

# --- 仅元数据 ---
META_PATCHES = [
    ('3a05e8d2-6158-4ac1-8c65-d9a20f3ee33f', {'category': 'workflow', 'lang': 'zh'}),
    ('94440507-115b-4f0d-8132-933eeae46bc3', {'category': 'reference', 'lang': 'zh'}),
    ('9902fa07-ed86-4fad-8923-683e6e458990', {'category': 'episodic', 'lang': 'zh'}),
    ('97682f4b-a983-48ce-a393-0ce7a043f291', {'category': 'reference', 'lang': 'zh', 'project': 'mem0'}),
    ('de100565-6f0b-4126-a607-75e49c0fe86d', {'category': 'episodic', 'lang': 'zh', 'project': 'mem0'}),
    ('3a53c692-48f2-4880-9d73-3b7bb0750c21', {'category': 'episodic', 'lang': 'mixed', 'project': 'favorites'}),
    ('1fdc42ed-1f4f-40c6-806b-1e00bf38a82a', {'category': 'episodic', 'lang': 'mixed', 'project': 'favorites'}),
    ('e8732e5b-30f4-4064-92b1-c652d11b07ef', {'category': 'behavior', 'lang': 'zh'}),
]

all_raw = col.get(include=['metadatas'])
for mid, meta in zip(all_raw['ids'], all_raw['metadatas']):
    meta = meta or {}
    text = str(meta.get('data', '') or '')
    if not text:
        continue
    lang = infer_memory_lang(text)
    patches: dict[str, str] = {}
    if not meta.get('lang'):
        patches['lang'] = lang
    if not str(meta.get('category', '') or '').strip():
        patches['category'] = 'reference' if lang == 'zh' else 'reference'
    if patches and mid not in {p[0] for p in META_PATCHES}:
        try:
            update_metadata(mid, patches)
        except Exception:
            pass

for mid, patches in META_PATCHES:
    try:
        update_metadata(mid, patches)
        print('META', mid[:8], patches)
    except Exception as exc:
        print('META FAIL', mid[:8], exc)

print('DONE')
