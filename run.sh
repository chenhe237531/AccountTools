#!/usr/bin/env bash
# AccountTools Web 控制台 —— 一键启动（首次自动创建 .venv 并安装依赖）
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "首次运行，正在创建虚拟环境…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python -m app "$@"
