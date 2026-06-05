#!/bin/bash
# mem0-local-enhanced 一键初始化脚本
# 用法：bash scripts/setup.sh

set -e

MEM0_DIR="${MEM0_DIR:-$HOME/.mem0}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== mem0-local-enhanced 初始化 ==="
echo "安装目录: $MEM0_DIR"

# 1. 创建 ~/.mem0 目录
mkdir -p "$MEM0_DIR"
echo "[1/6] 目录已创建"

# 2. 复制源码
cp "$REPO_ROOT/src/*.py" "$MEM0_DIR/"
echo "[2/6] 源码已复制"

# 3. 项目别名
if [ ! -f "$MEM0_DIR/project_aliases.json" ]; then
    cp "$REPO_ROOT/src/project_aliases.example.json" "$MEM0_DIR/project_aliases.json"
    echo "[3/6] 项目别名模板已创建（请编辑 ~/.mem0/project_aliases.json）"
else
    echo "[3/6] 项目别名已存在，跳过"
fi

# 4. 配置文件
if [ ! -f "$MEM0_DIR/config_local.json" ]; then
    echo "请选择配置模板："
    echo "  1) Ollama 全本地（无需 API key）"
    echo "  2) API LLM（需要填写 API key）"
    read -rp "选择 [1/2]: " choice
    if [ "$choice" = "2" ]; then
        cp "$REPO_ROOT/configs/config_api.example.json" "$MEM0_DIR/config_local.json"
    else
        cp "$REPO_ROOT/configs/config_ollama.example.json" "$MEM0_DIR/config_local.json"
    fi
    echo "[4/6] 配置模板已创建（请编辑 ~/.mem0/config_local.json）"
else
    echo "[4/6] 配置已存在，跳过"
fi

# 5. 环境变量
if [ ! -f "$MEM0_DIR/.env" ]; then
    cp "$REPO_ROOT/.env.example" "$MEM0_DIR/.env"
    echo "[5/6] 环境变量模板已创建（可选编辑 ~/.mem0/.env）"
else
    echo "[5/6] .env 已存在，跳过"
fi

# 6. pending 目录 + 复盘辅助脚本（可选）
mkdir -p "$MEM0_DIR/pending"
SKILL_SCRIPTS="$HOME/.claude/skills/daily-review/scripts"
if [ -f "$REPO_ROOT/scripts/review_helpers.py" ]; then
    mkdir -p "$SKILL_SCRIPTS"
    cp "$REPO_ROOT/scripts/review_helpers.py" "$SKILL_SCRIPTS/"
    echo "[6/6] pending 目录已创建，review_helpers.py 已复制到 $SKILL_SCRIPTS"
else
    echo "[6/6] pending 目录已创建"
fi

echo ""
echo "=== 初始化完成 ==="
echo ""
echo "下一步："
echo "  1. 编辑 $MEM0_DIR/config_local.json（填入 API key 等实际配置）"
echo "  2. 编辑 $MEM0_DIR/project_aliases.json（可选，填入项目映射）"
echo "  3. 确保 Ollama 常驻：ollama serve 或 Ollama.app"
echo "  4. 测试：python3 $MEM0_DIR/search_context.py '测试'"
echo "  5. IDE 集成见 docs/claude-code-setup.md / docs/cursor-setup.md"
echo "  6. 复盘集成见 docs/daily-review-integration.md"
echo ""