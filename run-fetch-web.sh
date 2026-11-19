#!/usr/bin/env bash
# run-fetch-web.sh — 启动 Web 界面（依赖 Flask）。
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import flask' >/dev/null 2>&1; then
    exec .venv/bin/python web_fetch.py "$@"
fi

if ! python3 -c 'import flask' >/dev/null 2>&1; then
    echo "[setup] 未检测到 Flask，创建 .venv 并安装 requirements.txt ..."
    if [[ ! -x .venv/bin/python ]]; then
        python3 -m venv .venv || {
            echo "ERROR: 无法创建虚拟环境，请安装 python3-venv：sudo apt install python3-venv" >&2
            exit 1
        }
    fi
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip install -q -r requirements.txt
    exec .venv/bin/python web_fetch.py "$@"
fi

exec python3 web_fetch.py "$@"
