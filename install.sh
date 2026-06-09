#!/bin/bash
# oem-deb-resolver 安装脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> oem-deb-resolver 安装"
echo ""

echo "==> 检查 Python 环境..."
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3，请先安装 Python 3"
    exit 1
fi

echo "==> 安装 Python 依赖..."
python3 -m pip install --break-system-packages -q -r "$SCRIPT_DIR/requirements.txt"

echo "==> 赋予脚本可执行权限..."
chmod +x "$SCRIPT_DIR/web_oem_download.py"

echo ""
echo "安装完成！启动方式："
echo "  cd $SCRIPT_DIR"
echo "  python3 web_oem_download.py"
echo ""
echo "服务地址: http://127.0.0.1:51234"
