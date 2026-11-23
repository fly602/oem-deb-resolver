#!/usr/bin/env bash
# self-update.sh — 手动触发的自更新：拉取上游分支、装依赖、重启服务。
# 由 /about 页面的「立即更新」按钮通过 web_fetch.py 派生运行，也可手工执行：
#   ./self-update.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="${UPDATE_BRANCH:-main}"
LOG="$APP_DIR/cache/self-update.log"
PID_FILE="$APP_DIR/cache/server.pid"

mkdir -p "$APP_DIR/cache"
: >"$LOG"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }
fail() { log "错误：$*"; log "== 更新失败，服务保持原状"; exit 1; }

cd "$APP_DIR" || fail "无法进入 $APP_DIR"
[[ -d .git ]] || fail "$APP_DIR 不是 git 仓库，请用 install.sh 部署后再用此功能"

BACKUP="$APP_DIR/cache/versions.json.bak"
if [[ -f config/versions.json ]]; then
    cp -f config/versions.json "$BACKUP"
    log "已备份当前配置到 cache/versions.json.bak"
fi

log "== 拉取 origin/$BRANCH"
if ! git fetch --depth=1 origin "$BRANCH" >>"$LOG" 2>&1; then
    fail "git fetch 失败（检查网络或仓库权限）"
fi

OLD_VERSION="$(cat VERSION 2>/dev/null || echo '?')"
OLD_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
if ! git reset --hard "origin/$BRANCH" >>"$LOG" 2>&1; then
    fail "git reset --hard origin/$BRANCH 失败"
fi
NEW_VERSION="$(cat VERSION 2>/dev/null || echo '?')"
NEW_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
log "代码：$OLD_SHA (v$OLD_VERSION) → $NEW_SHA (v$NEW_VERSION)"

# 恢复本实例自己的配置：上游版本库里的 config/versions.json 只是默认值
if [[ -f "$BACKUP" ]]; then
    cp -f "$BACKUP" config/versions.json
    log "已恢复本实例配置 config/versions.json"
fi

log "== 更新 Python 依赖"
if [[ -x .venv/bin/python ]]; then
    PIP=.venv/bin/python
else
    PIP=python3
fi
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
    "$PIP" -m pip install -q --timeout 30 --retries 2 -r requirements.txt >>"$LOG" 2>&1 \
    || log "警告：依赖安装失败，继续重启（可稍后手工执行 pip install -r requirements.txt）"

log "== 重启服务"
if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 0.5
        done
        kill -9 "$OLD_PID" 2>/dev/null || true
        log "已停止旧进程 $OLD_PID"
    fi
fi

OEM_NO_BROWSER=1 "$APP_DIR/oem-launcher.sh" >>"$LOG" 2>&1
if [[ $? -eq 0 ]]; then
    log "== 更新完成：v$OLD_VERSION → v$NEW_VERSION"
else
    log "错误：服务启动失败，请查看 cache/server.log"
    exit 1
fi
