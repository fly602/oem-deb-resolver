# OEM Deb Resolver

基于 APT 仓库的 OEM 补丁包离线依赖解析与下载工具。

## 功能特性

- **补丁包解析**：指定补丁包，自动从补丁仓库取版本最高的包
- **相关包扫描**：同一目录下相关包版本高于 base 则一并下载
- **依赖递归解析**：下载后用 `dpkg-deb` 解析真实依赖，递归处理直到无新包
- **虚拟包智能处理**：自动识别虚拟包（Provides），找不到提供者时给出提示
- **全量升级模式**：扫描仓库所有包，下载 base 中版本更高的包
- **无需本地 apt**：纯 HTTP 获取远程仓库元数据，不依赖本地 apt-get

## 快速开始

### 安装依赖

```bash
# 自动安装（推荐）
chmod +x install.sh && ./install.sh

# 或手动安装
pip install -r requirements.txt
```

### 启动服务

```bash
python3 web_oem_download.py
# 或
./web_oem_download.py
```

服务启动后访问 **http://127.0.0.1:51234**

### 使用流程

1. 配置 Base Package List（基础镜像包列表 URL 或本地路径）
2. 配置目标架构（amd64 / arm64 / sw_64 / loongarch64 / mips64 等）
3. 配置补丁仓库（APT sources.list 格式）
4. 填写补丁包列表（或使用全量升级模式）
5. 点击"预览解析结果"查看下载计划和依赖问题
6. 确认后下载，打包为 zip 保存到本地

## 目录结构

```
oem-deb-resolver/
├── web_oem_download.py    # Web 服务入口
├── patch_resolver.py      # 核心解析引擎
├── requirements.txt       # Python 依赖
├── install.sh            # 安装脚本
├── templates/
│   ├── index.html       # 配置首页
│   ├── preview.html      # 预览结果页
│   └── result.html       # 下载结果页
├── config/
│   ├── patch-packages.txt     # 补丁包列表
│   ├── base_list.txt         # 当前 base 列表路径
│   ├── base_lists.json       # 历史 base 列表
│   └── oem-patch-sources.list  # 补丁仓库配置
└── cache/
    ├── deb-temp/              # 下载临时目录
    └── preview_cache/          # 预览结果缓存
```

## 架构说明

- **patch_resolver.py**：核心解析逻辑，无需 apt-get，纯 HTTP + dpkg-deb
- **web_oem_download.py**：Flask Web 界面，调用解析引擎
- **config/**：用户配置持久化目录（首次运行自动创建）
- **cache/**：预览缓存和临时下载文件

## 环境要求

- Python 3.8+
- dpkg-deb（系统自带）
- 网络可访问补丁仓库

## License

Internal use only.
