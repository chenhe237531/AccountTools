#!/usr/bin/env bash
# 多平台自动签到中心 —— 一键启动
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "首次运行，正在创建虚拟环境…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python main.py "$@"
