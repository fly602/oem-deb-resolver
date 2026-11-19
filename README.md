# OEM 升级包中心（OEM Deb Fetcher）

从目标 APT 仓库拉取相对基线镜像可升级的 deb 包。只做版本对比，不做依赖解析。

给定基线包清单与目标仓库后：

- 目标仓库中**基线已存在**且**版本更高**的包 → 升级包
- 基线中不存在的包 → 视为新增包，跳过
- 版本相同或更低 → 跳过
- 下载后校验 `Size` 与 `SHA256`，生成 `manifest.tsv`、`SHA256SUMS` 和 zip

核心实现只依赖 Python 标准库，不需要本地 `apt-get`/`apt_pkg`。

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
# 浏览器访问 http://127.0.0.1:51234
```

首次运行若缺少 Flask，会自动创建 `.venv` 并安装 `requirements.txt`。
页面功能：

- **版本管理**（首页）：新增版本时填写基线清单地址与架构；若基线地址是具体的 `packages-full-<isoarch>.txt`（或 `*-<arch>.txt`），会根据文件名自动识别并显示架构；再按自定义名称（如 `dde`、`系统`、`窗管`）添加仓库组，组内可放多个 sources.list 仓库
- **有效性检查**：版本页可单独检查基线清单或全部补丁仓库；结果直接显示在页面下方（架构 + 包数量），不跳转；仓库 0 个包视为正常，基线 0 个包视为异常
- **逐个仓库检查**：每个仓库单独拉取索引并与基线比较；一个仓库组可放多个仓库（每行一个），组内结果合并，同一个包取最高版本，最终每个仓库组只显示一张卡片；按源码项目（Source）归总，一行一个项目，包列表、版本变更（`旧 → 新`）、架构、合计大小分列展示
- **结果与下载**：仓库列表默认展开、项目全部勾选；底部选择架构（单个或 `all`）后下载，进入实时下载页：按项目显示下载/验签进度（下载中、验签中、打包中、正在下载、下载完成），未签名项目置顶并要求勾选确认后才能下载；全部验签通过时自动打包下载，zip 命名为 `版本-时间戳.zip`；右上角可停止或返回
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
