# OEM 升级包中心（OEM Deb Fetcher）

从目标 APT 仓库拉取相对基线镜像可升级的 deb 包。只做版本对比，不做依赖解析。

给定基线包清单与目标仓库后：

- 目标仓库中**基线已存在**且**版本更高**的包 → 升级包
- 基线中不存在的包 → 视为新增包，跳过
- 版本相同或更低 → 跳过
- 下载后校验 `Size` 与 `SHA256`，生成 `manifest.tsv`、`SHA256SUMS` 和 zip

核心实现只依赖 Python 标准库，不需要本地 `apt-get`/`apt_pkg`。

## 一键部署

```bash
curl -fsSL https://github.com/fly602/oem-deb-resolver/raw/main/install.sh | bash
```

脚本会自动：

- 安装 `git`、`python3`、`python3-venv`、`python3-pip`
- 克隆/更新代码到 `~/.cache/oem-deb-resolver`
- 创建 `.venv` 并安装 Python 依赖
- 在桌面环境安装启动器（可选）
- 后台启动服务并尝试打开浏览器

部署完成后访问 `http://127.0.0.1:51234/`，局域网可用 `http://<本机IP>:51234/`。

可选环境变量：

```bash
# 指定目录
curl -fsSL https://github.com/fly602/oem-deb-resolver/raw/main/install.sh \
  | INSTALL_DIR=/opt/oem-deb-resolver bash

# 只安装，不启动
curl -fsSL https://github.com/fly602/oem-deb-resolver/raw/main/install.sh \
  | START_SERVICE=0 bash

# 指定分支
curl -fsSL https://github.com/fly602/oem-deb-resolver/raw/main/install.sh \
  | BRANCH=refactor/simplified-fetch bash
```

## CLI

```bash
./oem_deb_fetch.py \
  --base-list https://cdimage.uniontech.com/daily-iso/2500-update1/professional/daily-stable/20260803-stable/report/iso-package-list \
  --repo 'deb [trusted=yes] https://aptly.uniontech.com/pkg/snipe-2500u1/release-candidate/... unstable main' \
  --arch amd64 \
  --zip
```

`--base-list` 支持：

- 发布根目录：`https://.../20260803-stable/` → 自动定位 `report/iso-package-list/packages-full-<isoarch>.txt`
- 清单目录或任意 `.../report/iso-package-list/xxxx.txt` → 取目录后按架构拼文件名
- 已存在的本地清单文件 → 直接使用

每个架构使用各自的 `packages-full-<isoarch>.txt`（`x86_64`、`aarch64`、`loongarch64`、`sw_64`）。
找不到对应架构清单时会先校验地址本身，地址有效则报“缺少该架构清单”，地址无效则报 HTTP 状态。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--base-list SRC` | 基线清单 URL、目录或本地文件（必填） |
| `--repo SPEC` | 目标仓库，可重复；支持 `sources.list` 的 `deb ...` 行 |
| `--arch ARCH` | `amd64` / `arm64` / `loong64` / `sw64` / `mips64el` / `all`（默认 `amd64`） |
| `--filter PATTERN` | 包名或仓库目录名的通配过滤，可重复或逗号分隔 |
| `--output DIR` | 输出根目录（默认 `upgrade-debs`） |
| `--jobs N` | 并发下载数（默认 8） |
| `--retries N` | 下载重试次数（默认 1） |
| `--dry-run` | 只列升级包，不下载 |
| `--zip` | 完成后打包为 `<output>.zip` |

示例：

```bash
# 只看会升级哪些包
./oem_deb_fetch.py --base-list ./packages-full-x86_64.txt \
  --repo 'https://apt.example.com/pkg/x unstable main' --dry-run

# 只拉 dde 相关包
./oem_deb_fetch.py --base-list ./packages-full-x86_64.txt \
  --repo 'https://apt.example.com/pkg/x unstable main' \
  --filter 'dde-*' --zip

# 全部架构
./oem_deb_fetch.py --base-list <清单目录> --repo '<仓库>' --arch all --zip
```

## Web

```bash
./run-fetch-web.sh
# 默认监听 0.0.0.0:51234，本机访问 http://127.0.0.1:51234
```

默认监听 `0.0.0.0`，局域网内可通过 `http://<服务器IP>:51234` 访问；如需仅本机监听，使用 `--host 127.0.0.1`。

启动前会自动检查：

- Python 3.8+
- `config/`、`cache/` 可写
- 端口是否可用（可用 `--port` 修改）
- Flask 是否可导入；缺失时自动创建 `.venv` 并安装 `requirements.txt`
- `deepin-deb-verify` 是否安装（可选，缺失时跳过验签）

任何必需项失败会直接退出并给出处理提示。

### 桌面启动器

```bash
./install-desktop.sh
```

安装后可在系统启动器搜索 **OEM 升级包中心**，点击即可：

- 服务未运行 → 后台启动并自动打开默认浏览器
- 服务已运行 → 直接打开网页，不会重复启动
- 检测到代码或 `VERSION` 更新 → 自动停止旧进程并重启
- 关闭浏览器或终端后服务继续常驻，不依赖 systemd

运行状态文件：

- `cache/server.pid`：服务进程
- `cache/run.stamp`：启动时的代码指纹，用于判断是否更新
- `cache/server.log`：服务日志

卸载启动器：`./install-desktop.sh --remove`

页面功能：

- **版本管理**（首页）：新增版本时填写基线清单地址与架构；若基线地址是具体的 `packages-full-<isoarch>.txt`（或 `*-<arch>.txt`），会根据文件名自动识别并显示架构；再按自定义名称（如 `dde`、`系统`、`窗管`）添加仓库组，组内可放多个 sources.list 仓库；每行可直接检查更新、编辑或删除
- **关于**：首页“关于”显示当前版本、Python/Flask/验签工具环境，并提供“检查更新”；默认读取项目 GitHub 的 `VERSION` 文件，也可用环境变量 `OEM_UPDATE_URL` 指定版本文件地址；GitHub 地址为 `https://github.com/fly602/oem-deb-resolver`
- **本地部署**：远程访问时“关于”页显示“本地部署”，可从当前环境下载部署包（含代码、启动脚本和当前 `config/versions.json`，不含 `.venv/cache`）；本机访问或检测到本机已部署时按钮自动隐藏；部署页显示部署源健康状态
- **配置导入**：首页“导入配置”可上传或粘贴 `versions.json`；始终按整合处理——同 id 保留现有基线与架构，只追加缺失的仓库组和仓库；导入文件中没有的版本不受影响；打包 zip 根目录也会附带可直接导入的 `versions.json`
- **有效性检查**：版本页可单独检查基线清单或全部补丁仓库；结果直接显示在页面下方（架构 + 包数量），不跳转；仓库 0 个包视为正常，基线 0 个包视为异常
- **逐个仓库检查**：每个仓库单独拉取索引并与基线比较；一个仓库组可放多个仓库（每行一个），组内结果合并，同一个包取最高版本，最终每个仓库组只显示一张卡片；按源码项目（Source）归总，一行一个项目，包列表、版本变更（`旧 → 新`）、架构、合计大小分列展示
- **结果与下载**：仓库列表默认展开、项目全部勾选；底部选择架构（单个或 `all`）后下载，进入实时下载页：按项目显示下载/验签进度（下载中、验签中、打包中、正在下载、下载完成），未签名项目置顶并要求勾选确认后才能下载；全部验签通过时自动打包下载，zip 命名为 `版本-时间戳.zip`；右上角可停止或返回
- **打包内容**：zip 根目录附带 `versions.json`（仅当前版本，可直接导入）、`info.txt`（版本、架构、打包时间、仓库配置与统计）、`comparison.tsv`（项目、包名、架构、基础版本 → 升级版本、下载/验签状态）
- **下载结果**：实时页与下载完成页均显示 deb 文件列表；检测到 `deepin-deb-verify` 时逐个执行 `deepin-deb-verify -f xxx.deb` 并展示验签结果，未安装则跳过并提示

版本配置保存在 `config/versions.json`（首次运行时自动创建）。

## 输出

```
upgrade-debs/
└── amd64/
    ├── *.deb
    ├── manifest.tsv   # 包名、架构、基础版本、升级版本、大小、SHA256、URL、文件名
    └── SHA256SUMS
upgrade-debs.zip       # --zip 时生成，包含各架构目录
```

## 架构映射

| APT 架构 | 基线清单后缀 |
| --- | --- |
| `amd64` | `x86_64` |
| `arm64` | `aarch64` |
| `loong64` | `loongarch64` |
| `sw64` | `sw_64` |
| `mips64el` | `x86_64`（无独立清单） |

## 仓库索引

- 读取 `dists/<suite>/Release` 的 `SHA256` 段
- 按 `<component>/binary-<arch>/Packages.{xz,gz,bz2}` 顺序取第一个可用索引，并用 Release 校验大小与 SHA256
- 多个仓库/组件合并，同名包取最高版本
- deb 包下载后再次校验 `Size`/`SHA256`

## 旧实现

原有补丁包解析、依赖递归、Flask 多页界面已归档到分支 `archive/legacy-web-resolver`。
重构期间旧文件暂留仓库根目录，验证完成后删除。

## 环境要求

- Python 3.8+
- Web 界面需要 Flask（CLI 不需要任何第三方包）
- 可选：`deepin-deb-verify`（存在时自动验签）
- 网络可访问目标仓库与基线清单

## License

Internal use only.
