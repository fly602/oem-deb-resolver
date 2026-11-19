#!/usr/bin/env bash
# run-fetch-web.sh — 启动 Web 界面（启动前检查依赖环境）。
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST="0.0.0.0"
PORT="51234"
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
    case "${ARGS[i]}" in
        --host)
            HOST="${ARGS[i + 1]:-0.0.0.0}"
            ;;
        --port)
            PORT="${ARGS[i + 1]:-51234}"
            ;;
    esac
done

check() { printf '[check] %s\n' "$*"; }
fail() {
    printf '[error] %s\n' "$*" >&2
    exit 1
}

echo "== 环境检查 =="

# 1. Python 版本
PYTHON=""
if [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    PYTHON="python3"
else
    fail "需要 Python 3.8+，请安装：sudo apt install python3 python3-venv"
fi
check "Python $($PYTHON -c 'import platform; print(platform.python_version())') ✓"

# 2. 配置与缓存目录
mkdir -p config cache >/dev/null 2>&1 || fail "无法创建 config/ 或 cache/ 目录"
[[ -w config && -w cache ]] || fail "config/ 或 cache/ 目录不可写"
check "config/ cache/ 可写 ✓"

# 3. 端口
if ! "$PYTHON" - "$HOST" "$PORT" <<'PY'
import socket
import sys

host, port_text = sys.argv[1], sys.argv[2]
try:
    port = int(port_text)
except ValueError:
    print(f"端口无效: {port_text}", file=sys.stderr)
    sys.exit(1)
bind_host = "" if host in ("0.0.0.0", "::") else host
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((bind_host, port))
except OSError as exc:
    print(f"端口 {port} 不可用: {exc}", file=sys.stderr)
    sys.exit(1)
finally:
    sock.close()
PY
then
    fail "端口 $PORT 已被占用，请先停止占用进程或使用 --port 指定其他端口"
fi
check "端口 $PORT 可用 ✓"

# 4. Flask
if ! "$PYTHON" -c 'import flask' >/dev/null 2>&1; then
    check "Flask 未安装，正在准备 .venv ..."
    if [[ "$PYTHON" != ".venv/bin/python" ]]; then
        python3 -m venv .venv >/dev/null 2>&1 || {
            fail "无法创建虚拟环境，请安装：sudo apt install python3-venv"
        }
        PYTHON=".venv/bin/python"
    fi
    "$PYTHON" -m pip install -q --upgrade pip >/dev/null 2>&1 \
        || fail "pip 升级失败，请检查网络或手动执行：$PYTHON -m pip install -r requirements.txt"
    "$PYTHON" -m pip install -q -r requirements.txt >/dev/null 2>&1 \
        || fail "依赖安装失败，请检查网络或手动执行：$PYTHON -m pip install -r requirements.txt"
fi
FLASK_VERSION=$("$PYTHON" -c 'import importlib.metadata; print(importlib.metadata.version("flask"))' 2>/dev/null || echo "已安装")
check "Flask $FLASK_VERSION ✓"

# 5. 可选验签工具
if command -v deepin-deb-verify >/dev/null 2>&1; then
    check "deepin-deb-verify ✓"
else
    check "deepin-deb-verify 未安装（下载时将跳过验签）"
fi

echo "== 检查通过，启动服务 =="
exec "$PYTHON" web_fetch.py "${ARGS[@]}"
