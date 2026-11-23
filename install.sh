#!/bin/bash
# oem-deb-resolver 一键部署脚本
# 用法: curl -fsSL https://github.com/fly602/oem-deb-resolver/raw/main/install.sh | bash
set -e

REPO_URL="${INSTALL_URL:-https://github.com/fly602/oem-deb-resolver.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.cache/oem-deb-resolver}"
START_SERVICE="${START_SERVICE:-1}"
INSTALL_DESKTOP="${INSTALL_DESKTOP:-1}"

install_pkg() {
    local pkg="$1"
    if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        return 0
    fi
    echo "安装系统包: $pkg ..."
    sudo apt-get update
    sudo apt-get install -y "$pkg"
}

echo "==> 检查系统依赖..."
install_pkg git
install_pkg python3
install_pkg python3-venv
install_pkg python3-pip

echo "==> 获取代码到 $INSTALL_DIR（分支 $BRANCH）..."
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    git fetch --depth=1 origin "$BRANCH"
    git checkout -B "$BRANCH" "origin/$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
        echo "错误：$INSTALL_DIR 已存在且不是 Git 仓库，请更换 INSTALL_DIR。" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo "==> 创建虚拟环境并安装 Python 依赖..."
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

echo "==> 赋予脚本可执行权限..."
chmod +x oem_deb_fetch.py web_fetch.py run-fetch-web.sh \
    oem-launcher.sh install-desktop.sh self-update.sh install.sh

if [ "$INSTALL_DESKTOP" != "0" ] \
    && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    echo "==> 安装桌面启动器..."
    ./install-desktop.sh || true
fi

if [ "$START_SERVICE" != "0" ]; then
    echo "==> 启动服务..."
    ./oem-launcher.sh || true
fi

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "部署完成！"
echo "  目录: $INSTALL_DIR"
echo "  本机: http://127.0.0.1:51234/"
if [ -n "$LAN_IP" ]; then
    echo "  局域网: http://$LAN_IP:51234/"
fi
echo ""
echo "CLI 用法："
echo "  cd $INSTALL_DIR"
echo "  ./oem_deb_fetch.py --base-list <基线清单> --repo '<仓库>' --arch amd64 --zip"
echo ""
echo "如需停止服务："
echo "  kill \$(cat $INSTALL_DIR/cache/server.pid)"
