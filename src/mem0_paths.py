"""mem0 运行时路径 — 由 MEM0_DIR 与环境变量统一解析。"""

from __future__ import annotations

import os


def _mem0_dir() -> str:
    return os.path.expanduser(os.getenv('MEM0_DIR', '~/.mem0'))


def _path(env_key: str, default_rel: str) -> str:
    return os.path.expanduser(
        os.getenv(env_key, os.path.join(_mem0_dir(), default_rel)),
    )


MEM0_DIR = _mem0_dir()
CHROMA_DB_PATH = _path('MEM0_CHROMA_PATH', 'chroma_db')
HISTORY_DB = _path('MEM0_HISTORY_DB', 'history.db')
ACTIVE_DB = _path('MEM0_ACTIVE_DB', 'active_memories.db')
DELETED_DB = _path('MEM0_DELETED_DB', 'deleted_archive.db')
LINEAGE_PATH = os.path.join(MEM0_DIR, 'lineage.jsonl')
PENDING_DIR = os.path.join(MEM0_DIR, 'pending')
SYNC_PENDING_DIR = os.path.join(MEM0_DIR, 'sync_pending')
MERGE_HINTS_PATH = os.path.join(MEM0_DIR, 'grooming-merge-hints.json')
CONFIG_PATH = os.path.expanduser(
    os.getenv('MEM0_CONFIG', os.path.join(MEM0_DIR, 'config_local.json')),
)
PROJECT_ALIASES_PATH = os.path.expanduser(
    os.getenv('MEM0_PROJECT_ALIASES', os.path.join(MEM0_DIR, 'project_aliases.json')),
)
