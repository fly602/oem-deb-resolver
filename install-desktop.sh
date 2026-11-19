#!/usr/bin/env bash
# install-desktop.sh — 安装启动器图标到当前用户的应用菜单。
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON_SRC="$APP_DIR/static/icon.svg"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
APPS_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APPS_DIR/oem-deb-resolver.desktop"

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$DESKTOP_FILE" "$ICON_DIR/oem-deb-resolver.svg"
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
    echo "已移除 OEM 升级包中心启动器。"
    exit 0
fi

[[ -f "$ICON_SRC" ]] || { echo "缺少图标: $ICON_SRC" >&2; exit 1; }
mkdir -p "$ICON_DIR" "$APPS_DIR"
cp -f "$ICON_SRC" "$ICON_DIR/oem-deb-resolver.svg"
chmod +x "$APP_DIR/oem-launcher.sh" "$APP_DIR/run-fetch-web.sh"

cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=OEM 升级包中心
Comment=APT 升级包检查、下载与验签
Exec=$APP_DIR/oem-launcher.sh
TryExec=$APP_DIR/oem-launcher.sh
Path=$APP_DIR
Icon=$ICON_DIR/oem-deb-resolver.svg
Terminal=false
Categories=Utility;
Keywords=OEM;deb;APT;升级;补丁;
StartupNotify=true
EOF
chmod +x "$DESKTOP_FILE"

update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "已安装启动器：$DESKTOP_FILE"
echo "图标：$ICON_DIR/oem-deb-resolver.svg"
echo "可在启动器中搜索“OEM 升级包中心”。"
