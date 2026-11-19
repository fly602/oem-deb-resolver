#!/usr/bin/env python3
"""oem_deb_fetch.py — 从 APT 仓库拉取相对基线版本可升级的 deb 包。

一条主路径：
  1. 读取基线包清单（URL、目录或本地文件，格式 ``包名<TAB>版本``）。
  2. 拉取目标仓库 ``dists/<suite>/Release`` 与 ``<component>/binary-<arch>/Packages``。
  3. 基线中存在且仓库版本更高的包即为升级包（不做依赖解析）。
  4. 下载并校验 size/SHA256，生成 manifest.tsv 与 SHA256SUMS。

仅依赖 Python 标准库。Debian 版本比较由本模块实现，不需要 apt_pkg。
"""

from __future__ import annotations

import argparse
import bz2
import fnmatch
import gzip
import hashlib
import lzma
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

USER_AGENT = "oem-deb-fetcher/2.0"
CHUNK_SIZE = 1 << 20
TIMEOUT = 60

# APT 架构 → 基线清单文件名中的 ISO 架构
ARCH_BASELINE = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "loong64": "loongarch64",
    "sw64": "sw_64",
    "mips64el": "x86_64",  # 无独立基础清单，复用 x86_64
}

ARCH_ALIASES = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "loongarch64": "loong64",
    "sw_64": "sw64",
    "mips64": "mips64el",
}


class FetchError(RuntimeError):
    """网络或仓库数据错误。"""


class MissingResource(FetchError):
    """HTTP 404。"""


# ─── Debian 版本比较 ───────────────────────────────────────────────────────────


def _split_version(version: str) -> tuple[int, str, str]:
    """拆分为 (epoch, upstream, revision)。"""
    epoch = 0
    rest = version
    if ":" in version:
        head, tail = version.split(":", 1)
        if head.isdigit():
            epoch = int(head)
            rest = tail
    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
    else:
        upstream, revision = rest, ""
    return epoch, upstream, revision


def _char_order(char: str) -> int:
    if not char:
        return 0
    if char.isdigit():
        return 0
    if ("a" <= char <= "z") or ("A" <= char <= "Z"):
        return ord(char)
    if char == "~":
        return -1
    return ord(char) + 256


def _compare_part(a: str, b: str) -> int:
    """dpkg 的 verrevcmp：比较 upstream 或 revision 片段。"""
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (
            j < len(b) and not b[j].isdigit()
        ):
            order_a = _char_order(a[i]) if i < len(a) else 0
            order_b = _char_order(b[j]) if j < len(b) else 0
            if order_a != order_b:
                return order_a - order_b
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def compare_versions(a: str, b: str) -> int:
    """返回 -1 / 0 / 1，语义同 dpkg --compare-versions。"""
    epoch_a, upstream_a, revision_a = _split_version(a)
    epoch_b, upstream_b, revision_b = _split_version(b)
    if epoch_a != epoch_b:
        return -1 if epoch_a < epoch_b else 1
    result = _compare_part(upstream_a, upstream_b)
    if result:
        return -1 if result < 0 else 1
    result = _compare_part(revision_a, revision_b)
    if result:
        return -1 if result < 0 else 1
    return 0


def is_newer(candidate: str, baseline: str) -> bool:
    return compare_versions(candidate, baseline) > 0


# ─── 数据模型 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Repo:
    url: str
    suite: str
    components: tuple[str, ...]

    def release_url(self) -> str:
        suite = self.suite.lstrip("/")
        prefix = "" if suite.startswith("dists/") else "dists/"
        return f"{self.url.rstrip('/')}/{prefix}{suite}/Release"

    def index_url(self, relative_path: str) -> str:
        suite = self.suite.lstrip("/")
        prefix = "" if suite.startswith("dists/") else "dists/"
        return f"{self.url.rstrip('/')}/{prefix}{suite}/{relative_path.lstrip('/')}"


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    architecture: str
    filename: str
    size: int
    sha256: str
    repo_url: str
    source: str = ""

    @property
    def url(self) -> str:
        return f"{self.repo_url.rstrip('/')}/{self.filename.lstrip('/')}"

    @property
    def basename(self) -> str:
        return self.filename.rsplit("/", 1)[-1]

    @property
    def directory_name(self) -> str:
        parent = self.filename.rsplit("/", 1)[0]
        return parent.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Upgrade:
    package: Package
    base_version: str

    @property
    def name(self) -> str:
        return self.package.name

    @property
    def version(self) -> str:
        return self.package.version

    @property
    def basename(self) -> str:
        return self.package.basename


@dataclass
class DownloadOutcome:
    upgrades: list[Upgrade]
    failures: list[tuple[Upgrade, str]]
    downloaded: int = 0
    cached: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures


# ─── 输入读取 ──────────────────────────────────────────────────────────────────


def normalize_arch(value: str) -> str:
    name = value.strip().lower()
    name = ARCH_ALIASES.get(name, name)
    if name == "all" or name in ARCH_BASELINE:
        return name
    raise ValueError(
        f"不支持的架构: {value}（可选: {', '.join(ARCH_BASELINE)} 或 all）"
    )


def baseline_filename(arch: str) -> str:
    return f"packages-full-{ARCH_BASELINE[arch]}.txt"


_BASELINE_SUBDIRS = ("report/iso-package-list", "iso-package-list")
_FILE_SUFFIXES = (".txt", ".gz", ".xz", ".bz2", ".list")


def _looks_like_file(source: str) -> bool:
    tail = source.rsplit("/", 1)[-1].lower()
    return tail.endswith(_FILE_SUFFIXES)


def _join_source(base: str, relative: str) -> str:
    return f"{base.rstrip('/')}/{relative.lstrip('/')}"


def _source_status(source: str, timeout: int = 20) -> int | None:
    """返回 HTTP 状态码 / 本地 200/404；网络错误返回 None。"""
    if "://" not in source:
        path = Path(source)
        if path.is_file() or path.is_dir():
            return 200
        return 404
    request = urllib.request.Request(
        source, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return 200
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 - 探测失败按不可访问处理
        return None


def resolve_baseline_source(source: str, arch: str) -> str:
    """定位架构对应的基线清单。

    支持三种输入：
      * 发布目录，如 https://.../20260803-stable/
        → <dir>/report/iso-package-list/packages-full-<isoarch>.txt
      * 清单目录，如 .../report/iso-package-list/ 或任意 .../xxxx.txt
        → 取目录后拼 packages-full-<isoarch>.txt
      * 已存在的本地文件 → 直接使用

    找不到对应架构清单时，先判断地址本身是否有效，再给出明确错误。
    """
    source = source.strip()
    if not source:
        raise ValueError("未指定基线包清单")

    name = baseline_filename(arch)
    remote = "://" in source
    if not remote and Path(source).is_file():
        return source

    base = source
    if _looks_like_file(source):
        base = source.rsplit("/", 1)[0] if "/" in source else "."

    candidates = [_join_source(base, name)]
    candidates.extend(
        _join_source(base, f"{subdir}/{name}") for subdir in _BASELINE_SUBDIRS
    )
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)

    for candidate in ordered:
        if not remote:
            if Path(candidate).is_file():
                return candidate
            continue
        if _source_status(candidate) == 200:
            return candidate

    status = _source_status(base)
    tried = "、".join(ordered)
    if status == 200:
        raise FetchError(
            f"基线地址可访问，但未找到架构 {arch} 的清单 {name}。已尝试: {tried}"
        )
    if status is None:
        raise FetchError(f"无法访问基线清单地址: {base}")
    raise FetchError(
        f"基线清单地址无效（HTTP {status}）: {source}。已尝试: {tried}"
    )


def _http_get(url: str, timeout: int = TIMEOUT, retries: int = 1) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise MissingResource(f"资源不存在: {url}") from exc
            last_error = exc
        except Exception as exc:  # noqa: BLE001 - 统一转换为 FetchError
            last_error = exc
        if attempt < retries:
            time.sleep(1 + attempt)
    raise FetchError(f"下载失败: {url}: {last_error}")


def read_source(source: str, timeout: int = TIMEOUT) -> str:
    if "://" in source:
        data = _http_get(source, timeout=timeout)
    else:
        path = Path(source)
        if not path.is_file():
            raise FetchError(f"文件不存在: {source}")
        data = path.read_bytes()
    return data.decode("utf-8", errors="replace")


def load_baseline(source: str, arch: str) -> dict[str, str]:
    """读取基线清单，返回 {包名: 版本}，仅保留目标架构相关条目。"""
    accepted_suffixes = {"", "all", arch}
    if arch == "mips64el":
        accepted_suffixes.add("amd64")

    baseline: dict[str, str] = {}
    text = read_source(source)
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            raise FetchError(f"基线清单第 {lineno} 行格式错误: {line!r}")
        raw_name, version = fields[0], fields[1]
        name, _, suffix = raw_name.partition(":")
        if suffix and suffix not in accepted_suffixes:
            continue
        previous = baseline.get(name)
        if previous is not None and previous != version:
            raise FetchError(f"基线清单中 {name} 版本冲突: {previous} / {version}")
        baseline[name] = version

    if not baseline:
        raise FetchError(f"基线清单中没有架构 {arch} 的软件包: {source}")
    return baseline


# ─── 仓库索引 ──────────────────────────────────────────────────────────────────


def parse_repo_spec(spec: str) -> Repo:
    """解析 ``deb [opts] URL suite comp...`` 或 ``URL suite comp...``。"""
    try:
        tokens = shlex.split(spec)
    except ValueError as exc:
        raise ValueError(f"仓库配置解析失败: {spec!r}: {exc}") from exc
    if not tokens:
        raise ValueError("仓库配置为空")

    if tokens[0] in ("deb", "deb-src"):
        if tokens[0] == "deb-src":
            raise ValueError("只支持二进制仓库（deb），不支持 deb-src")
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("["):
            while tokens:
                if tokens.pop(0).endswith("]"):
                    break
    if len(tokens) < 3:
        raise ValueError(
            f"仓库配置需要 'URL suite component...': {spec!r}"
        )

    url, suite = tokens[0], tokens[1]
    components: list[str] = []
    for token in tokens[2:]:
        components.extend(part for part in token.split(",") if part)
    if not components:
        raise ValueError(f"仓库缺少 component: {spec!r}")
    return Repo(url=url, suite=suite, components=tuple(components))


def parse_release_sha256(text: str) -> dict[str, tuple[str, int]]:
    """解析 Release 的 SHA256 段，返回 {相对路径: (sha256, size)}。"""
    checksums: dict[str, tuple[str, int]] = {}
    in_sha256 = False
    for line in text.splitlines():
        if not line.strip():
            in_sha256 = False
            continue
        if not line[0].isspace():
            in_sha256 = line.strip().lower().startswith("sha256:")
            continue
        if not in_sha256:
            continue
        fields = line.split()
        if len(fields) == 3 and len(fields[0]) == 64 and fields[1].isdigit():
            checksums[fields[2]] = (fields[0].lower(), int(fields[1]))
    return checksums


def _decompress(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(data)
    if data[:2] == b"BZ":
        return bz2.decompress(data)
    return data


def _fetch_index_bytes(
    repo: Repo,
    relative_path: str,
    checksums: dict[str, tuple[str, int]],
) -> bytes | None:
    """下载并校验单个索引文件；不存在返回 None。"""
    expected = checksums.get(relative_path)
    url = repo.index_url(relative_path)
    try:
        data = _http_get(url, timeout=TIMEOUT * 2)
    except MissingResource:
        if expected is not None:
            raise FetchError(f"Release 声明了索引但下载失败: {url}") from None
        return None
    if expected is not None:
        expected_hash, expected_size = expected
        if len(data) != expected_size:
            raise FetchError(
                f"索引大小不匹配: {relative_path} "
                f"(期望 {expected_size}, 实际 {len(data)})"
            )
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_hash:
            raise FetchError(f"索引 SHA256 不匹配: {relative_path}")
    return data


def fetch_index(repo: Repo, arch: str) -> dict[str, Package]:
    """拉取一个仓库/架构的 Packages 索引，返回 {包名: 最高版本}。"""
    checksums: dict[str, tuple[str, int]] = {}
    try:
        release_text = read_source(repo.release_url(), timeout=TIMEOUT * 2)
        checksums = parse_release_sha256(release_text)
    except MissingResource:
        print(f"警告: 仓库缺少 Release，跳过索引校验: {repo.release_url()}", file=sys.stderr)

    merged: dict[str, Package] = {}
    found_component = False
    for component in repo.components:
        text: str | None = None
        for suffix in (".xz", ".gz", ".bz2", ""):
            relative = f"{component}/binary-{arch}/Packages{suffix}"
            data = _fetch_index_bytes(repo, relative, checksums)
            if data is not None:
                text = _decompress(data).decode("utf-8", errors="replace")
                found_component = True
                break
        if text is None:
            print(
                f"警告: 仓库 {repo.url} 缺少 {component}/binary-{arch}/Packages",
                file=sys.stderr,
            )
            continue
        _parse_packages_text(text, repo, arch, merged)

    if not found_component:
        raise FetchError(f"无法获取 Packages 索引: {repo.url} ({arch})")
    return merged


def _parse_packages_text(
    text: str, repo: Repo, arch: str, merged: dict[str, Package]
) -> None:
    stanza: list[str] = []

    def flush() -> None:
        if not stanza:
            return
        fields: dict[str, str] = {}
        for raw in stanza:
            if not raw or raw[0] in " \t":
                continue
            key, separator, value = raw.partition(":")
            if separator:
                fields[key.strip().lower()] = value.strip()
        stanza.clear()

        package_arch = fields.get("architecture", "")
        if package_arch not in (arch, "all"):
            return
        name = fields.get("package", "")
        version = fields.get("version", "")
        filename = fields.get("filename", "")
        if not name or not version or not filename:
            return
        if filename.startswith("./"):
            filename = filename[2:]
        filename = filename.lstrip("/")
        if not filename.endswith(".deb") or ".." in filename.split("/"):
            raise FetchError(f"索引中的下载路径无效: {filename}")
        size_text = fields.get("size", "")
        sha256 = fields.get("sha256", "").lower()
        if not size_text.isdigit() or len(sha256) != 64:
            return
        source = fields.get("source", "").strip()
        source = source.split()[0] if source else name
        package = Package(
            name=name,
            version=version,
            architecture=package_arch,
            filename=filename,
            size=int(size_text),
            sha256=sha256,
            repo_url=repo.url,
            source=source,
        )
        previous = merged.get(name)
        if previous is None:
            merged[name] = package
            return
        comparison = compare_versions(package.version, previous.version)
        if comparison > 0 or (
            comparison == 0
            and previous.architecture != arch
            and package.architecture == arch
        ):
            merged[name] = package

    for line in text.splitlines():
        if line.strip():
            stanza.append(line)
        else:
            flush()
    flush()


def fetch_indexes(repos: Sequence[Repo], arch: str) -> dict[str, Package]:
    """合并多个仓库，同名包取最高版本（同版本保留先出现的仓库）。"""
    merged: dict[str, Package] = {}
    for repo in repos:
        for name, package in fetch_index(repo, arch).items():
            previous = merged.get(name)
            if previous is None or compare_versions(package.version, previous.version) > 0:
                merged[name] = package
    if not merged:
        raise FetchError(f"仓库中没有架构 {arch} 的软件包")
    return merged


# ─── 升级选择 ──────────────────────────────────────────────────────────────────


def _matches(package: Package, pattern: str) -> bool:
    return fnmatch.fnmatchcase(package.name, pattern) or fnmatch.fnmatchcase(
        package.directory_name, pattern
    )


def find_upgrades(
    packages: dict[str, Package],
    baseline: dict[str, str],
    filters: Sequence[str] = (),
) -> list[Upgrade]:
    upgrades: list[Upgrade] = []
    for name, package in packages.items():
        base_version = baseline.get(name)
        if base_version is None:
            continue
        if not is_newer(package.version, base_version):
            continue
        if filters and not any(_matches(package, pattern) for pattern in filters):
            continue
        upgrades.append(Upgrade(package=package, base_version=base_version))
    upgrades.sort(key=lambda item: item.name)
    return upgrades


# ─── 下载与输出 ────────────────────────────────────────────────────────────────


def _verify_file(path: Path, size: int, sha256: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest() == sha256


def _download_one(upgrade: Upgrade, dest: Path, retries: int) -> str:
    package = upgrade.package
    if _verify_file(dest, package.size, package.sha256):
        return "cached"

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        digest = hashlib.sha256()
        size = 0
        try:
            request = urllib.request.Request(
                package.url,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            )
            with urllib.request.urlopen(request, timeout=600) as response, partial.open(
                "wb"
            ) as handle:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if size != package.size:
                raise FetchError(
                    f"包大小不匹配: {package.basename} "
                    f"(期望 {package.size}, 实际 {size})"
                )
            if digest.hexdigest() != package.sha256:
                raise FetchError(f"包 SHA256 不匹配: {package.basename}")
            os.replace(partial, dest)
            return "downloaded"
        except Exception as exc:  # noqa: BLE001 - 重试后统一上报
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(1 + attempt)
    raise FetchError(f"下载失败: {package.url}: {last_error}")


def download_upgrades(
    upgrades: Sequence[Upgrade],
    output_dir: Path,
    jobs: int = 8,
    retries: int = 1,
    progress: Callable[[str], None] | None = None,
) -> DownloadOutcome:
    output_dir.mkdir(parents=True, exist_ok=True)
    outcome = DownloadOutcome(upgrades=list(upgrades), failures=[])

    basenames: set[str] = set()
    for upgrade in upgrades:
        if upgrade.basename in basenames:
            raise FetchError(f"不同包的下载文件名冲突: {upgrade.basename}")
        basenames.add(upgrade.basename)

    def work(upgrade: Upgrade) -> tuple[Upgrade, str, Exception | None]:
        try:
            status = _download_one(upgrade, output_dir / upgrade.basename, retries)
            return upgrade, status, None
        except Exception as exc:  # noqa: BLE001 - 汇总失败项
            return upgrade, "failed", exc

    workers = max(1, min(jobs, len(upgrades))) if upgrades else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(work, upgrade) for upgrade in upgrades]
        for future in as_completed(futures):
            upgrade, status, error = future.result()
            if error is not None:
                outcome.failures.append((upgrade, str(error)))
                if progress:
                    progress(f"失败: {upgrade.basename}: {error}")
            else:
                if status == "cached":
                    outcome.cached += 1
                    if progress:
                        progress(f"已存在且校验通过: {upgrade.basename}")
                else:
                    outcome.downloaded += 1
                    if progress:
                        progress(f"已下载并校验: {upgrade.basename}")

    write_manifest(outcome, output_dir)
    return outcome


def write_manifest(outcome: DownloadOutcome, output_dir: Path) -> None:
    failed_basenames = {upgrade.basename for upgrade, _ in outcome.failures}
    rows = [u for u in outcome.upgrades if u.basename not in failed_basenames]
    rows.sort(key=lambda item: item.name)

    manifest = output_dir / "manifest.tsv"
    sums = output_dir / "SHA256SUMS"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        handle.write(
            "Package\tArchitecture\tBaseVersion\tUpgradeVersion\tSize\tSHA256\tURL\tFile\n"
        )
        for upgrade in rows:
            package = upgrade.package
            handle.write(
                f"{package.name}\t{package.architecture}\t{upgrade.base_version}\t"
                f"{package.version}\t{package.size}\t{package.sha256}\t"
                f"{package.url}\t{package.basename}\n"
            )
    with sums.open("w", encoding="utf-8", newline="") as handle:
        for upgrade in rows:
            handle.write(f"{upgrade.package.sha256}  {upgrade.basename}\n")


def make_zip(output_root: Path, arches: Sequence[str], zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arch in arches:
            arch_dir = output_root / arch
            if not arch_dir.is_dir():
                continue
            for path in sorted(arch_dir.rglob("*")):
                if path.is_file() and not path.name.endswith(".part"):
                    archive.write(path, f"{arch}/{path.relative_to(arch_dir)}")
    return zip_path


# ─── CLI ───────────────────────────────────────────────────────────────────────


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 APT 仓库拉取相对基线版本可升级的 deb 包（不解析依赖）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  %(prog)s --base-list https://example.com/iso-package-list \\\n"
            "           --repo 'https://apt.example.com/pkg/x unstable main' --arch amd64\n"
            "  %(prog)s --base-list ./packages-full-x86_64.txt \\\n"
            "           --repo 'deb [trusted=yes] https://apt.example.com/pkg/x unstable main' \\\n"
            "           --filter 'dde-*' --dry-run\n"
        ),
    )
    parser.add_argument(
        "--base-list",
        required=True,
        help="基线清单 URL、目录（自动拼 packages-full-<isoarch>.txt）或本地文件",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="URL SUITE COMP...",
        help="目标仓库，可重复；也接受 sources.list 的 'deb ...' 行",
    )
    parser.add_argument(
        "--arch",
        default="amd64",
        help="目标架构: amd64|arm64|loong64|sw64|mips64el|all（默认 amd64）",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="PATTERN",
        help="包名或仓库目录名的通配过滤，可重复或逗号分隔；省略则全部升级包",
    )
    parser.add_argument(
        "--output", default="upgrade-debs", help="输出根目录（默认 upgrade-debs）"
    )
    parser.add_argument("--jobs", type=int, default=8, help="并发下载数（默认 8）")
    parser.add_argument("--retries", type=int, default=1, help="下载重试次数（默认 1）")
    parser.add_argument("--dry-run", action="store_true", help="只列出升级包，不下载")
    parser.add_argument("--zip", action="store_true", help="完成后打包为 <output>.zip")
    return parser


def _parse_filters(values: Sequence[str]) -> list[str]:
    patterns: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                patterns.append(part)
    return patterns


def _print_upgrades(upgrades: Sequence[Upgrade]) -> None:
    if not upgrades:
        print("没有可升级的包。")
        return
    print(f"{'包名':<32} {'基础版本':<28} {'升级版本':<28} {'大小':>12}")
    for upgrade in upgrades:
        print(
            f"{upgrade.name:<32} {upgrade.base_version:<28} "
            f"{upgrade.version:<28} {_human_size(upgrade.package.size):>12}"
        )


def run(
    base_list: str,
    repos: Sequence[Repo],
    arch: str,
    filters: Sequence[str] = (),
    output_root: Path = Path("upgrade-debs"),
    jobs: int = 8,
    retries: int = 1,
    dry_run: bool = False,
    make_archive: bool = False,
) -> int:
    """执行完整流程；返回进程退出码。"""
    arches = list(ARCH_BASELINE) if arch == "all" else [arch]
    failures: list[str] = []
    processed: list[str] = []
    total_upgrades = 0

    for current_arch in arches:
        print(f"=== 架构: {current_arch} ===")
        try:
            source = resolve_baseline_source(base_list, current_arch)
            print(f"基线清单: {source}")
            baseline = load_baseline(source, current_arch)
            print(f"基线包: {len(baseline)}")
            packages = fetch_indexes(repos, current_arch)
            upgrades = find_upgrades(packages, baseline, filters)
            print(f"仓库包: {len(packages)}；可升级: {len(upgrades)}")
            total_upgrades += len(upgrades)
            _print_upgrades(upgrades)

            if dry_run or not upgrades:
                processed.append(current_arch)
                continue

            output_dir = output_root / current_arch
            outcome = download_upgrades(
                upgrades,
                output_dir,
                jobs=jobs,
                retries=retries,
                progress=print,
            )
            print(
                f"下载完成: 新下载 {outcome.downloaded}，已存在 {outcome.cached}，"
                f"失败 {len(outcome.failures)}"
            )
            for upgrade, error in outcome.failures:
                failures.append(f"{current_arch}/{upgrade.basename}: {error}")
            print(f"输出目录: {output_dir}")
            processed.append(current_arch)
        except FetchError as exc:
            failures.append(f"{current_arch}: {exc}")
            print(f"错误: {exc}", file=sys.stderr)

    if make_archive and not dry_run and processed:
        zip_path = output_root.with_name(output_root.name + ".zip")
        make_zip(output_root, processed, zip_path)
        try:
            archived = ", ".join(processed)
            print(f"已打包: {zip_path}（{_human_size(zip_path.stat().st_size)}；{archived}）")
        except OSError as exc:
            failures.append(f"打包失败: {exc}")

    print(f"合计可升级包: {total_upgrades}")
    if failures:
        print("存在错误:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        arch = normalize_arch(args.arch)
        if not args.repo:
            parser.error("至少需要一个 --repo")
        repos = [parse_repo_spec(spec) for spec in args.repo]
        filters = _parse_filters(args.filter)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return run(
        base_list=args.base_list,
        repos=repos,
        arch=arch,
        filters=filters,
        output_root=Path(args.output),
        jobs=args.jobs,
        retries=args.retries,
        dry_run=args.dry_run,
        make_archive=args.zip,
    )


if __name__ == "__main__":
    raise SystemExit(main())
