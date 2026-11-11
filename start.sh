#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "没找到 python3，请先安装 Python 3.9 以上版本"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "首次运行，正在创建虚拟环境..."
  "$PY" -m venv .venv
fi
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

exec python run.py "$@"
