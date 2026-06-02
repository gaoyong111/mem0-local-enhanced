#!/bin/bash
# mem0 记忆可视化 Web UI 启动脚本
# 用法: bash ~/.mem0/mem_viewer.sh 或 chmod +x 后直接 ~/.mem0/mem_viewer.sh

MEM0_DIR="${MEM0_DIR:-$HOME/.mem0}"
export MEM0_DIR

python3 "$MEM0_DIR/mem_viewer.py"