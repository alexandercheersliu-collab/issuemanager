#!/usr/bin/env bash
# MathMaster Edu 一键环境初始化（macOS / Linux）。
#
# 用法：
#   bash scripts/setup_env.sh                # 完整初始化
#   bash scripts/setup_env.sh --skip-model   # 跳过嵌入模型预下载
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

SKIP_MODEL=0
for arg in "$@"; do
    case "$arg" in
        --skip-model) SKIP_MODEL=1 ;;
        -h|--help)
            echo "用法: bash scripts/setup_env.sh [--skip-model]"
            exit 0
            ;;
        *)
            echo "未知参数: $arg（支持 --skip-model）" >&2
            exit 2
            ;;
    esac
done

step() { printf '\n==> %s\n' "$1"; }

# ---------- 1. 定位 Python ----------
step '检查 Python ...'
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PYTHON="$cmd"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo '错误：未找到 Python，请先安装 Python 3.11+。' >&2
    exit 1
fi
"$PYTHON" --version

# ---------- 2. 创建 venv ----------
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    step '.venv 已存在，跳过创建。'
else
    step '创建虚拟环境 .venv ...'
    "$PYTHON" -m venv "$PROJECT_ROOT/.venv"
fi

# ---------- 3. 安装依赖 ----------
step '安装依赖 requirements.txt ...'
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"

# ---------- 4. 准备 .env ----------
if [ -f "$PROJECT_ROOT/.env" ]; then
    step '.env 已存在，跳过复制。'
elif [ -f "$PROJECT_ROOT/.env.example" ]; then
    step '复制 .env.example -> .env ...'
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    echo '    请按需编辑 .env（AI_PROVIDER / AI_API_KEY 等）；未配置 Key 时以 mock 演示模式运行。'
else
    echo '    警告：未找到 .env.example，跳过。' >&2
fi

# ---------- 5. 预下载嵌入模型 ----------
if [ "$SKIP_MODEL" -eq 1 ]; then
    step '按参数要求跳过嵌入模型预下载（首次启动时 ChromaDB 会自行下载）。'
else
    step '预下载 ChromaDB 内置嵌入模型（all-MiniLM-L6-v2 ONNX，约 79MB）...'
    if ! "$VENV_PYTHON" "$PROJECT_ROOT/scripts/install_onnx_model.py"; then
        echo '    模型预下载失败（网络问题可稍后重试）：' >&2
        echo "    $VENV_PYTHON scripts/install_onnx_model.py" >&2
    fi
fi

step '环境初始化完成！'
echo ''
echo '启动应用（二选一）：'
echo "  Streamlit 界面:  $VENV_PYTHON -m streamlit run app.py"
echo "  FastAPI 网关:    $VENV_PYTHON -m uvicorn api.main:app --port 8000"
