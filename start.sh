#!/usr/bin/env bash
# 从任意目录启动真实产品 API 与同端口 Gradio；不覆盖用户现有服务。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
umask 077

# 显式解释器优先，其次使用本机已配置的 Conda 环境，再考虑项目虚拟环境。
if [[ -z "${RAG_PYTHON:-}" ]]; then
    if [[ -x "$HOME/opt/anaconda3/envs/enterprise-rag/bin/python" ]]; then
        RAG_PYTHON="$HOME/opt/anaconda3/envs/enterprise-rag/bin/python"
    elif [[ -x "$ROOT/.venv/bin/python" ]]; then
        RAG_PYTHON="$ROOT/.venv/bin/python"
    else
        RAG_PYTHON="$(command -v python3)"
    fi
fi
"$RAG_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11); import fastapi, gradio, torch, transformers, asyncpg' \
    || { printf '解释器不满足依赖，请设置 RAG_PYTHON 为已安装项目依赖的 Python 3.11+。\n'; exit 1; }

# .env 仅进入进程环境，不回显配置值；stop/status 不依赖模型或数据库配置。
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$RAG_PYTHON" "$ROOT/scripts/local_runtime.py" "${1:-start}"
