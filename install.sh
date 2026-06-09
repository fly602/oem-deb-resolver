#!/bin/bash
# oem-deb-resolver 一键安装脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/fly602/oem-deb-resolver/main/install.sh | bash
set -e

REPO_URL="${INSTALL_URL:-https://github.com/fly602/oem-deb-resolver.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.cache/oem-deb-resolver}"

# ── 检查并安装依赖 ──
check_and_install() {
    local cmd="$1"
    local pkg="$2"
    if ! command -v "$cmd" &> /dev/null; then
        echo "未找到 $cmd，正在安装..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq "$pkg"
    fi
}

echo "==> 检查系统依赖..."
check_and_install git git
check_and_install python3 python3
check_and_install pip3 python3-pip

echo "==> 克隆仓库到 $INSTALL_DIR ..."
if [ -d "$INSTALL_DIR" ]; then
    echo "目录已存在，进入并更新..."
    cd "$INSTALL_DIR" && git pull
else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

echo "==> 安装 Python 依赖..."
pip3 install --break-system-packages -q -r requirements.txt

echo "==> 赋予脚本可执行权限..."
chmod +x web_oem_download.py install.sh run-oem-web.sh

echo ""
echo "安装完成！"
echo "启动方式："
echo "  cd $INSTALL_DIR"
echo "  python3 web_oem_download.py"
echo ""
echo "服务地址: http://127.0.0.1:51234"
