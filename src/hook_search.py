"""Claude Code UserPromptSubmit hook — 委托给 mem0_hook（保持原路径兼容）"""

import sys

sys.argv = [sys.argv[0], '--format', 'claude']

from mem0_hook import main

if __name__ == '__main__':
    main()