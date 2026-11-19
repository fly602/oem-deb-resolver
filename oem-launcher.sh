#!/usr/bin/env bash
# oem-launcher.sh — 桌面启动器：已在运行则打开浏览器；代码更新后自动重启服务。
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${OEM_PORT:-51234}"
HOST="${OEM_HOST:-0.0.0.0}"
BASE_URL="http://127.0.0.1:${PORT}"
STATE_DIR="$APP_DIR/cache"
PID_FILE="$STATE_DIR/server.pid"
STAMP_FILE="$STATE_DIR/run.stamp"
LOG_FILE="$STATE_DIR/server.log"
LOCK_FILE="$STATE_DIR/launcher.lock"

mkdir -p "$STATE_DIR"

open_browser() {
    [[ "${OEM_NO_BROWSER:-0}" == "1" ]] && return 0
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$BASE_URL/" >/dev/null 2>&1 &
    elif command -v gio >/dev/null 2>&1; then
        gio open "$BASE_URL/" >/dev/null 2>&1 &
    elif command -v sensible-browser >/dev/null 2>&1; then
        sensible-browser "$BASE_URL/" >/dev/null 2>&1 &
    elif [[ -n "${BROWSER:-}" ]]; then
        "$BROWSER" "$BASE_URL/" >/dev/null 2>&1 &
    fi
}

health_ok() {
    python3 - "$BASE_URL/health" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

compute_stamp() {
    cd "$APP_DIR"
    {
        cat VERSION 2>/dev/null || true
        find oem_deb_fetch.py web_fetch.py run-fetch-web.sh requirements.txt \
            templates static -type f 2>/dev/null \
            | LC_ALL=C sort \
            | while IFS= read -r file; do
                sha256sum "$file"
            done
    } | sha256sum | awk '{print $1}'
}

notify_error() {
    if command -v notify-send >/dev/null 2>&1; then
        notify-send "OEM 升级包中心" "$1" || true
    fi
    echo "[error] $1" >&2
}

running_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

stop_server() {
    local pid
    pid="$(running_pid || true)"
    [[ -n "$pid" ]] || return 1
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
}

start_server() {
    cd "$APP_DIR"
    if [[ -f "$LOG_FILE" ]] && [[ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5242880 ]]; then
        mv -f "$LOG_FILE" "$LOG_FILE.1"
    fi
    setsid nohup ./run-fetch-web.sh --no-browser --host "$HOST" --port "$PORT" \
        >>"$LOG_FILE" 2>&1 </dev/null 9>&- &
    echo $! >"$PID_FILE"
    for _ in $(seq 1 40); do
        health_ok && return 0
        sleep 0.5
    done
    return 1
}

main() {
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        for _ in $(seq 1 20); do
            health_ok && break
            sleep 0.5
        done
        open_browser
        exit 0
    fi

    if health_ok; then
        local new_stamp old_stamp
        new_stamp="$(compute_stamp)"
        old_stamp="$(cat "$STAMP_FILE" 2>/dev/null || true)"
        if [[ "$new_stamp" == "$old_stamp" ]] || ! running_pid >/dev/null; then
            open_browser
            exit 0
        fi
        echo "检测到代码更新，重启服务..." >&2
        stop_server || true
        for _ in $(seq 1 20); do
            health_ok || break
            sleep 0.3
        done
    fi

    if ! start_server; then
        notify_error "启动失败，请查看 $LOG_FILE"
        exit 1
    fi
    compute_stamp >"$STAMP_FILE"
    open_browser
}

main "$@"
