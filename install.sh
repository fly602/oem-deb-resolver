#!/bin/bash
# oem-deb-resolver 一键安装脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/fly602/oem-deb-resolver/main/install.sh | bash
set -e

REPO_URL="${INSTALL_URL:-https://github.com/fly602/oem-deb-resolver.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/oem-deb-resolver}"

echo "==> 克隆仓库到 $INSTALL_DIR ..."
if [ -d "$INSTALL_DIR" ]; then
    echo "目录已存在，进入并更新..."
    cd "$INSTALL_DIR" && git pull
else
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

echo "==> 检查 Python 环境..."
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3，请先安装 Python 3"
    exit 1
fi

echo "==> 安装 Python 依赖..."
pip install --break-system-packages -q -r requirements.txt

echo "==> 赋予脚本可执行权限..."
chmod +x web_oem_download.py install.sh run-oem-web.sh

echo ""
echo "安装完成！"
echo "启动方式："
echo "  cd $INSTALL_DIR"
echo "  python3 web_oem_download.py"
echo ""
echo "服务地址: http://127.0.0.1:51234"
