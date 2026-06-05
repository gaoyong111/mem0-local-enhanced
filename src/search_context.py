"""CLI：按 query 搜索 mem0 并打印上下文（调试用）"""

import json
import os
import sys

_MEM0_DIR = os.path.expanduser('~/.mem0')
if _MEM0_DIR not in sys.path:
    sys.path.insert(0, _MEM0_DIR)

from hybrid_search import detect_project, format_results_lines, hybrid_search  # noqa: E402


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else ''
    if not query.strip():
        print('（空查询，跳过记忆搜索）')
        return

    project = detect_project()
    results = hybrid_search(query, project=project, max_results=8)
    if not results:
        return

    print(format_results_lines(results, header='[mem0搜索结果]'))


if __name__ == '__main__':
    main()
