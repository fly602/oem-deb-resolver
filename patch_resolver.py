#!/usr/bin/env python3
"""
apt_cache_resolver — Patch-centric APT dependency resolver.

Algorithm:
  1. Parse multiple repo Packages indexes, merge into a unified index (highest version wins).
  2. For each requested patch, find the HIGHEST version in the merged index.
  3. For each found patch, scan its directory for related packages from the index.
     Mark a related package if its version > base version.
  4. Resolve dependencies purely from Packages index (Depends/Pre-Depends/Provides).
     Skip deps satisfied by base. Otherwise query merged index for the highest version.
  5. Recurse until no new packages appear.
  6. Report unresolved deps with base-version hints.
  7. Download .deb files only in the separate download phase.

No local apt-get/apt-cache required for repo queries.
No .deb download needed during dependency resolution — all metadata from Packages index.
"""

import argparse
import hashlib
import gzip
import bz2
import json
import lzma
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any
from functools import cmp_to_key
from collections import deque

# ─── Version comparison (apt_pkg) ──────────────────────────────────────────────

try:
    import apt_pkg
    apt_pkg.init()
    _HAS_APT_PKG = True
except ImportError:
    _HAS_APT_PKG = False


# ─── apt-cache provider resolver ───────────────────────────────────────────────────

_apt_provider_cache: dict[str, Optional[str]] = {}


def _query_apt_provider(virtual_pkg: str) -> Optional[str]:
    """
    Query apt-cache showpkg to find the real package that provides a virtual package.
    Returns the provider package name, or None if not found.
    Results are cached per-process.
    """
    if virtual_pkg in _apt_provider_cache:
        return _apt_provider_cache[virtual_pkg]

    try:
        result = subprocess.run(
            ["apt-cache", "showpkg", virtual_pkg],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _apt_provider_cache[virtual_pkg] = None
        return None

    # Parse "Reverse Provides:" section
    # Format: "libqt6core6 6.8.0+dfsg-0deepin23 (= 6.8.0)"
    in_provides = False
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if line == "Reverse Provides:":
            in_provides = True
            continue
        if in_provides:
            if line.strip() == "" or line.startswith("Dependencies:"):
                break
            # "pkgname version (= constraint)" or just "pkgname version"
            parts = line.split()
            if parts:
                provider = parts[0]
                _apt_provider_cache[virtual_pkg] = provider
                return provider

    _apt_provider_cache[virtual_pkg] = None
    return None



def _compare_versions(v1: str, v2: str) -> int:
    """
    Compare two Debian version strings using apt_pkg.
    Returns: -1 if v1 < v2, 0 if equal, 1 if v1 > v2.
    Empty version is treated as lower than any non-empty version.
    """
    if not v1 and not v2:
        return 0
    if not v1:
        return -1
    if not v2:
        return 1
    return apt_pkg.version_compare(v1, v2)


# Singleton key function for sorting versions (use as: sorted(pkgs, key=_version_key)
_version_key = cmp_to_key(apt_pkg.version_compare)


# ─── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PatchPackage:
    """
    A package entry from the Packages index.
    """
    name: str
    version: str
    architecture: str
    filename: str       # e.g. "pool/commercial/d/dde-shell/dde-shell_2.0.42.1-1_arm64.deb"
    size: int = 0
    sha256: Optional[str] = None
    source_repo: str = ""   # base URL
    base_version: Optional[str] = None  # version in base list (for display)

    def directory(self) -> str:
        """Return the directory prefix of this package's filename."""
        fn = self.filename.rsplit("/", 1)[0]
        return fn

    def parent_dir(self) -> str:
        """Return the parent directory (e.g. 'pool/main/d' for 'pool/main/d/dde-shell/pkg.deb')."""
        return str(Path(self.filename).parent.parent)

    def parent_name(self) -> str:
        """Return the immediate parent dir name (e.g. 'd' for 'pool/main/d/dde-shell/')."""
        return Path(self.filename).parent.name

    def download_url(self) -> str:
        base = self.source_repo.rstrip("/")
        return f"{base}/{self.filename.lstrip('/')}"


@dataclass
class ResolvedPackage:
    """
    Represents a package that has been (or will be) downloaded.
    """
    name: str
    version: str
    source: str           # "patch" | "related" | "dep"
    reason: str = ""      # human-readable why this was included
    local_file: Optional[Path] = None
    unsatisfied_deps: list["UnsatisfiedDep"] = field(default_factory=list)
    directory: str = ""   # from PatchPackage.directory()


@dataclass
class DepCacheEntry:
    """
    单一依赖项的缓存结果，所有包共享。
    解析结果（base满足 / 需升级 / 缺失 / 虚拟包）作为状态标记。
    """
    status: str              # "ok" | "upgrade" | "missing" | "virtual-provided"
    base_version: Optional[str] = None       # base 中的版本（如果有）
    patch_pkg: Optional["PatchPackage"] = None  # 待下载的补丁包（如果有）
    provider_name: Optional[str] = None       # 虚拟包的提供者（base 中的包名）
    note: str = ""                          # 人类可读说明

    @property
    def is_satisfied(self) -> bool:
        """base 中已有且满足版本约束，或由 base 中包提供。"""
        return self.status in ("ok", "virtual-provided")

    @property
    def needs_download(self) -> bool:
        """需要从补丁仓库下载。"""
        return self.status == "upgrade"

    @property
    def is_missing(self) -> bool:
        """补丁仓库和 base 中都不存在，无法解决。"""
        return self.status == "missing"


@dataclass
class UnsatisfiedDep:
    """
    A dependency that is NOT satisfied by the base image.
    """
    raw: str              # raw dependency string as it appears in dpkg-deb output
    package_name: str
    relation: Optional[str] = None
    target_version: Optional[str] = None
    alternatives: list[str] = field(default_factory=list)
    base_version: Optional[str] = None  # version in base list (if present)
    resolution_status: str = "pending"   # pending | resolved | unresolved
    resolved_patch: Optional["PatchPackage"] = None
    resolution_note: str = ""
    is_recommends: bool = False  # 是否为 Recommends 可选依赖


@dataclass
class RepoIndex:
    """In-memory index of a single repository's Packages file."""
    base_url: str
    by_name: dict[str, PatchPackage] = field(default_factory=dict)
    # Map: directory_prefix → list of PatchPackage in that directory
    by_directory: dict[str, list[PatchPackage]] = field(default_factory=dict)
    # Map: virtual package name → list of real package names that Provide it
    provides: dict[str, list[str]] = field(default_factory=dict)
    # Map: package name → list of pre-parsed dependencies (from Packages index)
    by_depends: dict[str, list[UnsatisfiedDep]] = field(default_factory=dict)
    _loaded: bool = False


@dataclass
class MultiRepoIndex:
    """
    Manages multiple patch repositories with priority ordering.
    """
    repos: list[dict[str, Any]] = field(default_factory=list)
    # Combined index: name → PatchPackage (from highest-priority source)
    by_name: dict[str, PatchPackage] = field(default_factory=dict)
    # Combined: directory_prefix → [PatchPackage]
    by_directory: dict[str, list[PatchPackage]] = field(default_factory=dict)
    # Combined: virtual package name → list of real package names that Provide it
    provides: dict[str, list[str]] = field(default_factory=dict)
    # Combined: package name → pre-parsed dependencies from Packages index
    by_depends: dict[str, list[UnsatisfiedDep]] = field(default_factory=dict)

    def add_repo(self, repo_url: str, distribution: str, components: list[str],
                 architecture: str, priority: int):
        self.repos.append({
            "url": repo_url,
            "distribution": distribution,
            "components": components,
            "architecture": architecture,
            "priority": priority,
        })


# ─── HTTP & decompression ───────────────────────────────────────────────────────

_MAGIC_GZIP = b"\x1f\x8b"
_MAGIC_XZ   = b"\xfd\x37\x7a\x58\x5a"
_MAGIC_BZ2  = b"BZ"


def _decompress(data: bytes) -> bytes:
    if data[:2] == _MAGIC_GZIP:
        return gzip.decompress(data)
    if data[:6] == _MAGIC_XZ:
        return lzma.decompress(data)
    if data[:2] == _MAGIC_BZ2:
        return bz2.decompress(data)
    return data


# ─── Release file parser ────────────────────────────────────────────────────────

@dataclass
class ReleaseInfo:
    """Parsed Release file: SHA256 hashes keyed by relative file path."""
    origin: str = ""
    suite: str = ""
    components: list[str] = field(default_factory=list)
    architectures: list[str] = field(default_factory=list)
    hashes: dict[str, tuple[str, int]] = field(default_factory=dict)  # path → (sha256, size)


def _parse_release(text: str) -> ReleaseInfo:
    """
    Parse a Release file text, extracting SHA256 hashes from the SHA256 field.
    Paths are relative to the dists/ root, e.g. 'main/binary-amd64/Packages.gz'.
    """
    info = ReleaseInfo()
    in_sha256 = False

    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line:
            if in_sha256:
                in_sha256 = False
            continue
        if line.startswith("SHA256:"):
            in_sha256 = True
            continue
        if in_sha256:
            # Format: "sha256 size path" (space-separated)
            parts = line.split()
            if len(parts) >= 3:
                sha256_hex = parts[0]
                try:
                    size = int(parts[1])
                    path = parts[2]
                    info.hashes[path] = (sha256_hex, size)
                except ValueError:
                    pass
        else:
            if line.startswith("Origin:"):
                info.origin = line.split(":", 1)[1].strip()
            elif line.startswith("Suite:"):
                info.suite = line.split(":", 1)[1].strip()
            elif line.startswith("Components:"):
                info.components = line.split(":", 1)[1].strip().split()
            elif line.startswith("Architectures:"):
                info.architectures = line.split(":", 1)[1].strip().split()

    return info


def _hash_data(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ─── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_key(base_url: str, distribution: str, components: list[str], architecture: str) -> str:
    """Generate a safe cache key for an index."""
    comp_part = "_".join(sorted(components))
    # Sanitize URL for use as directory name
    safe_url = re.sub(r'[/:.+*?#%]', '_', base_url)[:80]
    return f"{safe_url}__{distribution}__{comp_part}__{architecture}"


def _load_cached_index(cache_dir: Path, cache_key: str) -> tuple[Optional[str], Optional[str]]:
    """
    Load cached Packages text and Release text from cache directory.
    Returns (packages_text, release_text) or (None, None) if not cached.
    """
    cache_dir = cache_dir / cache_key
    pkg_cache = cache_dir / "Packages.txt"
    rel_cache = cache_dir / "Release"
    if pkg_cache.exists():
        packages_text = pkg_cache.read_text(encoding="utf-8", errors="replace")
    else:
        packages_text = None
    if rel_cache.exists():
        release_text = rel_cache.read_text(encoding="utf-8", errors="replace")
    else:
        release_text = None
    return packages_text, release_text


def _save_cached_index(cache_dir: Path, cache_key: str, packages_text: str, release_text: Optional[str]):
    """Save Packages text and Release text to cache directory."""
    cache_dir = cache_dir / cache_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "Packages.txt").write_text(packages_text, encoding="utf-8")
    if release_text is not None:
        (cache_dir / "Release").write_text(release_text, encoding="utf-8")
    # Write metadata
    meta = {
        "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "packages_size": len(packages_text),
    }
    (cache_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _fetch_url(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"Accept-Encoding": "identity", "User-Agent": "patch-resolver/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ─── Index fetch with Release verification ─────────────────────────────────────

def _fetch_and_parse_index(
    base_url: str,
    distribution: str,
    components: list[str],
    architecture: str,
    timeout: int = 60,
    cache_dir: Optional[Path] = None,
    verify: bool = True,
) -> RepoIndex:
    """
    Fetch Packages index from a single APT repository and parse it.
    1. Download Release file, parse SHA256 hashes.
    2. Download Packages file, verify against Release SHA256.
    3. Save both to cache_dir for debugging.
    4. Falls back to unauthenticated download if Release fetch fails.
    """
    comp_str = "+".join(components) if len(components) > 1 else components[0]
    base = base_url.rstrip("/")

    # 收集所有 (comp, arch) 组合的 Packages 索引 URL
    # 所有标准 APT 仓库结构都是 base/dists/<suite>/<component>/binary-<arch>/Packages
    # 仅当 distribution 已带 dists/ 前缀时跳过
    if distribution.startswith("dists/"):
        dist_prefix = ""
    else:
        dist_prefix = "dists/"

    index = RepoIndex(base_url=base_url)
    release_info: Optional[ReleaseInfo] = None
    last_error: Optional[Exception] = None

    # ── Step 1: Download Release file ──
    release_url = f"{base}/{dist_prefix}{distribution}/Release"
    try:
        raw_release = _fetch_url(release_url, timeout=timeout)
        release_text = raw_release.decode("utf-8", errors="replace")
        release_info = _parse_release(release_text)
    except Exception as exc:
        last_error = exc
        release_info = None
        # Non-fatal: continue without Release verification

    # ── Step 2: Try each component+arch combination ──
    tried_urls: list[tuple[str, str]] = []
    for comp in components:
        index_base = f"{base}/{dist_prefix}{distribution}/{comp}/binary-{architecture}/"
        for suffix in ['', '.gz', '.xz', '.bz2']:
            url = index_base + "Packages" + suffix
            tried_urls.append((url, comp))

    # 尝试下载所有组件的 Packages 文件，合并到同一个 index 中
    all_texts: list[str] = []
    for url, comp in tried_urls:
        try:
            raw = _fetch_url(url, timeout=timeout)
            text = _decompress(raw).decode("utf-8", errors="replace")
            all_texts.append(text)
            _parse_packages_text(text, base_url, index)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                last_error = exc
                continue
            raise RepositoryFetchError(f"HTTP {exc.code} for {url}") from exc
        except Exception as exc:
            last_error = exc
            continue

    # 所有组件都尝试完后，保存合并缓存
    if cache_dir is not None and all_texts:
        key = _cache_key(base_url, distribution, components, architecture)
        merged_text = "\n\n".join(all_texts)
        _save_cached_index(cache_dir, key, merged_text, release_text if release_info else None)

    if index.by_name:
        index._loaded = True
        return index

    tried_sample = "\n  ".join(u for u, _ in tried_urls[:3])
    if len(tried_urls) > 3:
        tried_sample += f"\n  ... (+{len(tried_urls) - 3} more URLs)"
    raise RepositoryFetchError(
        f"Could not fetch Packages index (tried):\n  {tried_sample}\nLast error: {last_error}"
    )


class RepositoryFetchError(Exception):
    """Failed to fetch a repository index."""


# ─── Packages index parser ─────────────────────────────────────────────────────







def _parse_dep(raw: str) -> list[UnsatisfiedDep]:
    """
    Parse a raw Depends/Pre-Depends field value using apt_pkg.
    Returns a list of UnsatisfiedDep (one per comma-separated item).
    apt_pkg handles version constraint normalization (e.g. >> → >) and | alternatives.
    """
    deps = []
    for alt_group in apt_pkg.parse_depends(raw + ','):
        if not alt_group:
            continue
        first = alt_group[0]
        pkg_name = first[0]
        target_ver = first[1] or None
        relation = first[2] or None
        alternatives = [a[0] for a in alt_group]
        raw_str = ' | '.join(alternatives)
        if target_ver and relation:
            raw_str += f' ({relation} {target_ver})'
        deps.append(UnsatisfiedDep(
            raw=raw_str,
            package_name=pkg_name,
            relation=relation,
            target_version=target_ver,
            alternatives=alternatives,
        ))
    return deps



def _normalize_sha256(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    if ":" in raw:
        return raw.split(":", 1)[1].strip()
    return raw


def _parse_packages_text(text: str, source_repo: str, index: RepoIndex):
    """
    Parse a Packages index text block into a RepoIndex.
    Populates index.by_name, index.by_directory, and index.by_depends.

    CRITICAL: by_depends is built from the HIGHEST version of each package only.
    All raw blocks are collected first, then by_name/by_directory are populated,
    and finally by_depends is populated from the highest-version blocks.
    """
    text = text.replace("\r\n", "\n")
    raw_blocks = re.split(r"\n\n+", text)

    # ── Pass 1: parse all blocks into (fields, PatchPackage) ──
    parsed: list[tuple[dict[str, str], PatchPackage]] = []

    for block in raw_blocks:
        if not block.strip():
            continue

        fields: dict[str, str] = {}
        cur_key, cur_val = None, ""

        for line in block.split("\n"):
            if line and line[0] in " \t" and cur_key is not None:
                cur_val += " " + line.strip()
            elif ":" in line:
                if cur_key is not None:
                    fields[cur_key] = cur_val.strip()
                key, _, val = line.partition(":")
                cur_key = key.strip()
                cur_val = val.strip()
            elif not line.strip() and cur_key is not None:
                fields[cur_key] = cur_val.strip()
                cur_key = None

        if cur_key is not None:
            fields[cur_key] = cur_val.strip()

        pkg_name = fields.get("Package", "").strip()
        version = fields.get("Version", "").strip()
        if not pkg_name or not version:
            continue

        pkg = PatchPackage(
            name=pkg_name,
            version=version,
            architecture=fields.get("Architecture", "all").strip(),
            filename=fields.get("Filename", "").strip(),
            size=int(fields.get("Size", "0") or 0),
            sha256=_normalize_sha256(fields.get("SHA256", "").strip()),
            source_repo=source_repo,
        )

        if not pkg.filename:
            continue

        parsed.append((fields, pkg))

        dir_prefix = pkg.directory()

        # by_name: keep highest version per package name
        if pkg.name not in index.by_name:
            index.by_name[pkg.name] = pkg
        else:
            if _compare_versions(pkg.version, index.by_name[pkg.name].version) > 0:
                index.by_name[pkg.name] = pkg

        # by_directory: keep all packages (used for related-package scanning)
        if dir_prefix not in index.by_directory:
            index.by_directory[dir_prefix] = []
        index.by_directory[dir_prefix].append(pkg)

        # Parse Provides field
        provides_raw = fields.get("Provides", "").strip()
        if provides_raw:
            for prov_str in provides_raw.split(","):
                prov_str = prov_str.strip()
                if not prov_str:
                    continue
                m = re.match(r'^([a-zA-Z0-9][a-zA-Z0-9.+\-]*)(?:\s*\(.*\))?\s*$', prov_str)
                if m:
                    prov_pkg_name = m.group(1)
                    if prov_pkg_name not in index.provides:
                        index.provides[prov_pkg_name] = []
                    index.provides[prov_pkg_name].append(pkg.name)

    # ── Pass 2: build by_depends from highest version only ──
    for fields, pkg in parsed:
        best = index.by_name.get(pkg.name)
        if best is None or best.version != pkg.version:
            continue  # Not the highest version, skip
        if pkg.name in index.by_depends:
            continue  # Already parsed deps for this package
        pkg_deps: list[UnsatisfiedDep] = []
        # 解析必需依赖
        for dep_field in ("Depends", "Pre-Depends"):
            raw_dep_str = fields.get(dep_field, "").strip()
            if raw_dep_str:
                pkg_deps.extend(_parse_dep(raw_dep_str))
        # 解析 Recommends（可选依赖，不强制满足）
        for dep_field in ("Recommends",):
            raw_dep_str = fields.get(dep_field, "").strip()
            if raw_dep_str:
                rec_deps = _parse_dep(raw_dep_str)
                # 标记为 Recommends 类型
                for dep in rec_deps:
                    dep.is_recommends = True
                pkg_deps.extend(rec_deps)
        index.by_depends[pkg.name] = pkg_deps


def _merge_indexes(target: MultiRepoIndex, *sources: RepoIndex):
    """
    Merge source RepoIndex into target MultiRepoIndex by name (highest version wins).

    Architecture:
      1. Merge by_name, by_directory, provides (incremental, version-aware).
      2. Track which source repo contributed the highest version for each package.
      3. After ALL sources merged, populate by_depends from the winning sources.
    """
    # Track which RepoIndex contributed the highest version for each package.
    # This enables correct by_depends lookup after all repos are merged.
    _winner: dict[str, RepoIndex] = {}

    # ── Step 1: Merge by_name (highest version wins) ──
    for src in sources:
        for name, pkg in src.by_name.items():
            if name not in target.by_name:
                target.by_name[name] = pkg
                _winner[name] = src
            elif _compare_versions(pkg.version, target.by_name[name].version) > 0:
                target.by_name[name] = pkg
                _winner[name] = src

    # ── Step 2: Merge by_directory ──
    for src in sources:
        for dir_, pkgs in src.by_directory.items():
            if dir_ not in target.by_directory:
                target.by_directory[dir_] = []
            existing_names = {p.name for p in target.by_directory.get(dir_, [])}
            for p in pkgs:
                if p.name not in existing_names:
                    target.by_directory[dir_].append(p)

    # ── Step 3: Merge Provides ──
    for src in sources:
        for virt_name, provider_list in src.provides.items():
            if virt_name not in target.provides:
                target.provides[virt_name] = []
            for prov in provider_list:
                if prov not in target.provides[virt_name]:
                    target.provides[virt_name].append(prov)

    # ── Step 4: Populate by_depends from winning sources ──
    # Each package's deps come from the source RepoIndex that contributed
    # its highest version. This avoids the incremental-merge corruption bug.
    for name, winner_src in _winner.items():
        deps = winner_src.by_depends.get(name)
        if deps is not None:
            target.by_depends[name] = deps


# ─── Base package loader ────────────────────────────────────────────────────────

@dataclass
class BasePackage:
    """A package entry from the base image package list."""
    name: str
    version: str
    architecture: Optional[str] = None  # e.g. "amd64" if name ends with :amd64


def _fetch_text(path: str) -> str:
    """Fetch text content from a local file path or HTTP URL."""
    if path.startswith("http://") or path.startswith("https://"):
        req = urllib.request.Request(
            path,
            headers={"User-Agent": "patch-resolver/1.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    else:
        return Path(path).read_text(encoding="utf-8")


def load_base_packages(path: str) -> dict[str, BasePackage]:
    """
    Load base package list (tab-separated: name\\tversion).
    Supports both local file path and HTTP URL.
    Also handles :amd64 / :arm64 suffixes in package names.
    Returns: {name → BasePackage}
    """
    try:
        text = _fetch_text(path)
    except Exception as exc:
        return {}

    result: dict[str, BasePackage] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Tab or space separated: "name\tversion" or "name version"
        parts = re.split(r"[\t ]+", line, 1)
        name_raw = parts[0].strip()
        version = parts[1].strip() if len(parts) > 1 else ""

        # Handle :amd64, :arm64 suffixes in name
        arch: Optional[str] = None
        name = name_raw
        m = re.match(r"^(.+):(amd64|arm64|all)$", name_raw, re.IGNORECASE)
        if m:
            name = m.group(1)
            arch = m.group(2).lower()

        if name not in result:
            result[name] = BasePackage(name=name, version=version, architecture=arch)

    return result


def detect_architecture_from_base(path: str) -> Optional[str]:
    """Infer architecture from base list filename (URL or path) or content."""
    # Extract filename from URL or local path
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    m = re.search(r"packages-full[_-]([a-z0-9_]+)", filename, re.IGNORECASE)
    if m:
        arch_str = m.group(1).lower()
        if "x86_64" in arch_str or "amd64" in arch_str:
            return "amd64"
        if "arm64" in arch_str or "aarch64" in arch_str:
            return "arm64"

    # From content: look for :amd64 or :arm64 suffixes
    base = load_base_packages(path)
    arches = {bp.architecture for bp in base.values() if bp.architecture}
    if "amd64" in arches:
        return "amd64"
    if "arm64" in arches:
        return "arm64"

    return None


# ─── dpkg-deb dependency parser (uses apt_pkg for RFC822 + dep parsing) ────────

def parse_dpkg_info(deb_path: Path) -> list[UnsatisfiedDep]:
    """
    Run `dpkg-deb --field <deb>` and parse the Depends / Pre-Depends fields
    using apt_pkg (RFC822 TagSection + parse_depends).

    dpkg-deb --field outputs RFC822 format, which apt_pkg.TagSection can parse directly.
    """
    try:
        result = subprocess.run(
            ["dpkg-deb", "--field", str(deb_path)],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    raw_text = result.stdout
    if not raw_text.strip():
        return []

    all_deps: list[UnsatisfiedDep] = []
    _parse_dpkg_fields(raw_text, all_deps)
    return all_deps


def _parse_dpkg_fields(rfc822_text: str, all_deps: list[UnsatisfiedDep]):
    """Parse RFC822 text from dpkg-deb --field, extracting Depends/Pre-Depends via apt_pkg."""
    rfc822_bytes = rfc822_text.replace("\n", "\r\n").encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(prefix="apt_")
    try:
        os.write(fd, rfc822_bytes)
        os.close(fd)
        tf = apt_pkg.TagFile(tmp_path)
        while tf.step():
            sec = tf.section
            for field_name in ("Depends", "Pre-Depends"):
                raw_value = sec.get(field_name, "")
                if not raw_value:
                    continue
                all_deps.extend(_parse_dep(raw_value))
    finally:
        os.unlink(tmp_path)


# ─── Version constraint checking ───────────────────────────────────────────────

def _dep_satisfied_by(dep: UnsatisfiedDep, base_version: str) -> bool:
    """
    Check if a base package version satisfies a dependency's constraint.
    Uses apt_pkg.check_dep for accurate Debian version comparison.
    """
    if dep.relation is None or dep.target_version is None:
        return True  # No version constraint — package is present

    # apt_pkg.check_dep(pkg_ver, op, dep_ver) returns True if satisfied
    return apt_pkg.check_dep(base_version, dep.relation, dep.target_version)


# ─── Core resolver ─────────────────────────────────────────────────────────────

class PatchResolver:
    """
    Patch-centric dependency resolver.

    Workflow:
      1. resolve_patches()    — find requested patches (highest version in repo)
      2. scan_directory_upgrade() — scan same directory, mark if version > base
      3. resolve_deps()       — pure Packages index parsing → check base → query repo
      4. Recurse until no new packages
      5. Download phase (separate): download .deb files for confirmed packages
    """

    def __init__(
        self,
        multi_index: MultiRepoIndex,
        base_packages: dict[str, BasePackage],
        architecture: str = "amd64",
        max_workers: int = 8,
        max_depth: int = 10,
    ):
        self.multi_index = multi_index
        self.base_packages = base_packages
        self.architecture = architecture
        self.max_workers = max_workers
        self.max_depth = max_depth

        # Resolution state
        self.resolved: dict[str, ResolvedPackage] = {}  # name → ResolvedPackage
        self.patch_pkgs: dict[str, PatchPackage] = {}     # name → from patch repo
        self.related_pkgs: dict[str, PatchPackage] = {}   # name → from related
        self.related_scans: list[RelatedScan] = []        # all scanned related packages
        self.dep_pkgs: dict[str, PatchPackage] = {}      # name → from dep resolution
        self.not_found: list[tuple[str, str]] = []  # (name, base_version)
        self.log_lines: list[str] = []
        self.config_summary: str = ""  # set by caller for passing to result
        # 依赖缓存：所有包共享，同一 dep 只解析一次
        # key: 包名, value: DepCacheEntry
        self.dep_cache: dict[str, DepCacheEntry] = {}
        # 目录扫描中：防止递归扫描时重复处理正在扫描的目录
        self._scanning_dirs: set[str] = set()

    # ── Step 1: Resolve requested patches ────────────────────────────────────

    def resolve_patches(self, requested_names: list[str]) -> list[str]:
        """
        For each requested package, find the highest-version entry in the patch repo.
        Returns list of successfully found package names.
        Not-found packages are logged with base version hints.
        """
        found = []
        for raw_name in requested_names:
            name = self._strip_suffix(raw_name)
            if name not in self.multi_index.by_name:
                base_ver = self.base_packages.get(name)
                base_ver_str = base_ver.version if base_ver else None
                self.not_found.append((name, base_ver_str))
                self.log(
                    f"  [PATCH] {name}: 未在补丁仓库中找到"
                    + (f"  (base 版本: {base_ver_str})" if base_ver_str else "")
                )
                continue

            candidates = []
            for pkg in self.multi_index.by_name.values():
                if self._strip_suffix(pkg.name) == name:
                    candidates.append(pkg)

            if not candidates:
                self.log(f"  [PATCH] {name}: 未找到")
                continue

            # Pick highest version
            best = max(candidates, key=lambda p: _version_key(p.version))
            base_pkg = self.base_packages.get(name)
            best.base_version = base_pkg.version if base_pkg else None
            self.patch_pkgs[name] = best
            found.append(name)
            self.log(
                f"  [PATCH] {name}: 选中版本 {best.version}"
                + (f"  (base 版本: {self.base_packages[name].version})"
                   if name in self.base_packages else "  (base 中不存在)")
            )

        return found

    # ── Step 2: Find related packages in same parent directory ──────────────────

    def find_related(self, patch_names: list[str]) -> list[str]:
        """
        For each patch package, scan ALL packages in its parent directory
        (e.g. 'pool/main/d/') from the patch repo. Download a package if its
        version is strictly higher than the base version.
        """
        found = []
        seen_parent_dirs: set[str] = set()
        seen_related: set[str] = set()

        for name in patch_names:
            if name not in self.patch_pkgs:
                continue
            pkg = self.patch_pkgs[name]
            parent_dir = pkg.parent_dir()    # e.g. 'pool/main/d'
            parent_name = pkg.parent_name()  # e.g. 'd'

            if parent_name in seen_parent_dirs:
                continue
            seen_parent_dirs.add(parent_name)

            self.log(f"  [RELATED] 扫描目录 {parent_dir}/ ...")

            # Find all packages in this parent directory
            related_candidates: list[PatchPackage] = []
            for dir_, pkgs in self.multi_index.by_directory.items():
                if dir_.startswith(parent_dir + "/"):
                    for p in pkgs:
                        p_name = self._strip_suffix(p.name)
                        if p_name not in self.patch_pkgs and p_name not in seen_related:
                            related_candidates.append(p)

            for rel_pkg in related_candidates:
                rel_name = self._strip_suffix(rel_pkg.name)
                seen_related.add(rel_name)

                base_pkg = self.base_packages.get(rel_name)

                # 只有在 base 中存在且补丁版本 > base 版本时才下载
                if not base_pkg:
                    self.related_scans.append(RelatedScan(
                        directory=rel_pkg.directory(),
                        pkg_name=rel_name,
                        patch_version=rel_pkg.version,
                        base_version=None,
                        action="skip_not_in_base",
                        reason="base 中不存在，跳过",
                    ))
                    self.log(f"    {rel_name}: 相关包但 base 中不存在，跳过")
                    continue

                cmp = _compare_versions(rel_pkg.version, base_pkg.version)
                rel_pkg.base_version = base_pkg.version
                if cmp > 0:
                    self.related_pkgs[rel_name] = rel_pkg
                    found.append(rel_name)
                    self.related_scans.append(RelatedScan(
                        directory=rel_pkg.directory(),
                        pkg_name=rel_name,
                        patch_version=rel_pkg.version,
                        base_version=base_pkg.version,
                        action="download",
                        reason=f"补丁版本更高 (base {base_pkg.version} → patch {rel_pkg.version})",
                    ))
                    self.log(
                        f"    {rel_name}: 相关包 (base 版本 {base_pkg.version} → 补丁版本 {rel_pkg.version})"
                    )
                else:
                    self.related_scans.append(RelatedScan(
                        directory=rel_pkg.directory(),
                        pkg_name=rel_name,
                        patch_version=rel_pkg.version,
                        base_version=base_pkg.version,
                        action="skip_same_or_lower",
                        reason=f"base 版本已是 {base_pkg.version} 或更高，跳过",
                    ))
                    self.log(
                        f"    {rel_name}: 相关包但版本不更高 (base={base_pkg.version})，跳过"
                    )

        return found

    def scan_directory_upgrade(self, pkg: PatchPackage) -> list[str]:
        """
        给定一个已确定的包，扫描其所在目录（如 pool/main/d/dde-shell/）
        下所有补丁仓库中的包。排除 dbgsym 调试包，排除已标记的包。
        若包在 base 中存在且补丁版本 > base 版本，则加入 related_pkgs。
        同一目录不会重复扫描。

        CRITICAL: 对每个候选包名，取合并索引中的最高版本（by_name），
        而非目录列表中的第一个满足条件的版本。
        """
        found = []
        target_dir = pkg.directory()

        # 跳过已扫描或正在扫描的目录
        if target_dir in self._scanning_dirs:
            return found
        self._scanning_dirs.add(target_dir)

        candidates = self.multi_index.by_directory.get(target_dir, [])
        # 收集去重后的包名
        seen_names: set[str] = set()
        for p in candidates:
            name = self._strip_suffix(p.name)
            if name in seen_names:
                continue
            seen_names.add(name)

            # 排除 dbgsym 调试包
            if "dbgsym" in name:
                continue

            # 排除已标记的包
            if name in self.patch_pkgs or name in self.dep_pkgs or name in self.related_pkgs:
                continue

            base_pkg = self.base_packages.get(name)
            if not base_pkg:
                continue

            # 使用合并索引中的最高版本（by_name），而非目录列表中的版本
            best = self.multi_index.by_name.get(name)
            if not best:
                continue

            cmp = _compare_versions(best.version, base_pkg.version)
            if cmp > 0:
                best.base_version = base_pkg.version
                self.related_pkgs[name] = best
                found.append(name)
                self.log(
                    f"    [RELATED] {name}: 目录扫描发现 (base {base_pkg.version} → 补丁 {best.version})"
                )

        return found


    # Full Upgrade: scan all patch-repo packages, download those newer than base

    def full_upgrade(self) -> list["PatchPackage"]:
        """
        Scan ALL packages in the patch repository.
        Returns packages where:
          - Package exists in base AND patch version > base version
        This is the "全量升级" logic: pull everything from the patch repo
        that is newer than what the base already has.
        """
        self.log("=" * 60)
        self.log("全量升级模式：扫描所有补丁包")
        self.log("[配置信息]")
        if self.config_summary:
            for line in self.config_summary.splitlines():
                self.log(f"  {line}")
        self.log("=" * 60)

        result: list["PatchPackage"] = []
        skipped_not_in_base = 0
        skipped_same_or_lower = 0

        for name, pkg in self.multi_index.by_name.items():
            base_pkg = self.base_packages.get(name)

            # 情况1: base 中不存在此包 → 跳过（不在全量升级范围内）
            if not base_pkg:
                skipped_not_in_base += 1
                continue

            # 情况2: base 中存在，比较版本
            cmp = _compare_versions(pkg.version, base_pkg.version)
            if cmp <= 0:
                # 补丁版本 <= base 版本，不升级
                skipped_same_or_lower += 1
                continue

            # 情况3: 补丁版本 > base 版本 → 需要升级
            pkg.base_version = base_pkg.version
            result.append(pkg)
            self.log(
                f"  UP {name}: patch {pkg.version} > base {base_pkg.version}"
            )

        self.log("")
        self.log(f"全量升级：{len(result)} 个包待下载")
        if skipped_not_in_base:
            self.log(f"  (不在 base 中，跳过 {skipped_not_in_base} 个)")
        if skipped_same_or_lower:
            self.log(f"  (base 版本相同或更高，跳过 {skipped_same_or_lower} 个)")

        return result

    # ── Step 3: Download packages ─────────────────────────────────────────────

    def download_packages(
        self,
        packages: list[PatchPackage],
        output_dir: Path,
        retry: int = 1,
    ) -> dict[str, tuple[str, Any]]:
        """
        Download packages concurrently.
        Returns: {filename → (status, path_or_error)}
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        results: dict[str, tuple[str, Any]] = {}

        def _dl(pkg: PatchPackage) -> tuple[str, str, Any]:
            url = pkg.download_url()
            basename = Path(pkg.filename).name
            dest = output_dir / basename

            # 文件已存在则跳过（预览复用已有下载）
            if dest.exists():
                return (pkg.filename, "success", str(dest))

            last_err = ""
            for attempt in range(retry + 1):
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "patch-resolver/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        data = resp.read()

                    if pkg.size and len(data) != pkg.size:
                        raise ValueError(f"Size mismatch: expected {pkg.size}, got {len(data)}")

                    if pkg.sha256:
                        actual = hashlib.sha256(data).hexdigest()
                        if actual != pkg.sha256:
                            raise ValueError(f"SHA256 mismatch")

                    dest.write_bytes(data)
                    return (pkg.filename, "success", str(dest))

                except Exception as exc:
                    last_err = str(exc)
                    if attempt < retry:
                        time.sleep(1)

            return (pkg.filename, "failed", last_err)

        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = {ex.submit(_dl, p): p for p in packages}
            for future in as_completed(futures):
                fname, status, path_or_err = future.result()
                results[fname] = (status, path_or_err)

        return results


    # ── Step 4: Resolve deps via Packages index ──────────────────────────────────

    def _resolve_dep(self, pkg_name: str, raw_dep) -> "DepCacheEntry":
        """
        解析单个依赖项，结果缓存到 dep_cache。
        缓存 key 统一用包名（去冒号后缀），同一包只解析一次。
        对每个 dep（可能是 | 组），遍历所有 alternatives 查 base，
        遍历完都没有才查补丁仓库 / 虚拟包。
        """
        # 统一用包名作为缓存 key（不带版本约束），同一包只解析一次
        dep_key = self._strip_suffix(raw_dep.alternatives[0].strip())
        if dep_key in self.dep_cache:
            return self.dep_cache[dep_key]

        # ── 1. 遍历所有 alternatives，查 base 包 ──
        for alt_name in raw_dep.alternatives:
            alt_stripped = self._strip_suffix(alt_name.strip())
            base_pkg = self.base_packages.get(alt_stripped)
            if base_pkg:
                if raw_dep.relation and raw_dep.target_version:
                    if _dep_satisfied_by(raw_dep, base_pkg.version):
                        entry = DepCacheEntry(
                            status="ok",
                            base_version=base_pkg.version,
                            note=f"base 版本 {base_pkg.version} 满足约束 {raw_dep.relation} {raw_dep.target_version}"
                        )
                    else:
                        # base 有但不满足版本，查补丁仓库
                        candidates = [p for p in self.multi_index.by_name.values()
                                    if self._strip_suffix(p.name) == alt_stripped]
                        if candidates:
                            best = max(candidates, key=lambda p: _version_key(p.version))
                            best.base_version = base_pkg.version
                            entry = DepCacheEntry(
                                status="upgrade",
                                base_version=base_pkg.version,
                                patch_pkg=best,
                                note=f"base={base_pkg.version} 不满足 {raw_dep.relation} {raw_dep.target_version}，补丁版本 {best.version}"
                            )
                        else:
                            entry = DepCacheEntry(
                                status="missing",
                                base_version=base_pkg.version,
                                note=f"base={base_pkg.version} 不满足约束，补丁仓库中未找到"
                            )
                else:
                    entry = DepCacheEntry(
                        status="ok",
                        base_version=base_pkg.version,
                        note="base 中已有，无版本约束"
                    )
                self.dep_cache[dep_key] = entry
                return entry

        # ── 2. 所有 alternatives 都不在 base → 查补丁仓库 ──
        candidates = [p for p in self.multi_index.by_name.values()
                    if self._strip_suffix(p.name) == dep_key]
        if candidates:
            best = max(candidates, key=lambda p: _version_key(p.version))
            entry = DepCacheEntry(
                status="upgrade",
                patch_pkg=best,
                note=f"从补丁仓库下载 {best.version}"
            )
            self.dep_cache[dep_key] = entry
            return entry

        # ── 3. 既不在 base 也不在补丁仓库 → 判断是虚拟包还是真实缺失包 ──
        #    真实包：在补丁仓库的 by_name 中存在
        #    虚拟包：在补丁仓库的 by_name 中不存在，但在 Provides 中存在
        is_virtual = (dep_key in self.multi_index.provides)

        if not is_virtual:
            # ── 3a. 通过 apt-cache showpkg 动态查询提供者 ──
            provider_name = _query_apt_provider(dep_key)
            if provider_name:
                base_prov = self.base_packages.get(provider_name)
                if base_prov:
                    entry = DepCacheEntry(
                        status="virtual-provided",
                        base_version=base_prov.version,
                        provider_name=provider_name,
                        note=f"虚拟包 {dep_key}，由 base 中的 {provider_name} ({base_prov.version}) 提供"
                    )
                else:
                    entry = DepCacheEntry(
                        status="virtual-provided",
                        note=f"虚拟包 {dep_key}，由 apt 查询到 {provider_name} 提供（base 中暂无该提供者）"
                    )
                self.dep_cache[dep_key] = entry
                return entry

            # 真实包，仓库里没有它 → missing
            entry = DepCacheEntry(
                status="missing",
                note="base 和补丁仓库中均不存在"
            )
            self.dep_cache[dep_key] = entry
            return entry

        # ── 4. 虚拟包：尝试找提供者 ──
        # 3a. base 中有提供者 → ok
        providers = self.multi_index.provides.get(dep_key, [])
        for prov_name in providers:
            prov_stripped = self._strip_suffix(prov_name)
            base_prov = self.base_packages.get(prov_stripped)
            if base_prov:
                entry = DepCacheEntry(
                    status="virtual-provided",
                    base_version=base_prov.version,
                    provider_name=prov_stripped,
                    note=f"虚拟包 {dep_key}，由 base 中的 {prov_stripped} ({base_prov.version}) 提供"
                )
                self.dep_cache[dep_key] = entry
                return entry

        # 3b. 补丁仓库中有提供者 → 标记下载
        for prov_name in providers:
            prov_stripped = self._strip_suffix(prov_name)
            prov_candidates = [p for p in self.multi_index.by_name.values()
                            if self._strip_suffix(p.name) == prov_stripped]
            if prov_candidates:
                best = max(prov_candidates, key=lambda p: _version_key(p.version))
                entry = DepCacheEntry(
                    status="upgrade",
                    patch_pkg=best,
                    note=f"虚拟包 {dep_key}，补丁仓库中 {best.name} ({best.version}) 提供"
                )
                self.dep_cache[dep_key] = entry
                return entry

        # 3c. 找不到提供者 → 忽略（不加入 unresolved）
        # 虚拟包由安装环境的实际包提供，下载时无需处理
        entry = DepCacheEntry(
            status="ignored-virtual",
            note=f"虚拟包，未找到提供者，已忽略"
        )
        self.dep_cache[dep_key] = entry
        return entry
        return entry

    def resolve_deps(
        self,
        pkg_names: list,
        include_recommends: bool = False,
    ):
        """
        纯索引依赖解析：仅通过 Packages 索引的 by_depends 获取依赖信息。
        不下载 .deb，不调用 dpkg-deb。
        Results统一从 dep_cache 命中。
        Returns: (newly_resolved_names, unresolved_dep_tuples, ignored_virtuals, virtual_provided)
        """
        new_names = []
        unresolved = []
        ignored_virtuals = []
        virtual_provided = []

        # 只处理传入的包列表（pkg_names = 外层决定的新包）
        for pkg_name in pkg_names:
            # 从 Packages 索引获取依赖（已预解析）
            raw_deps = self.multi_index.by_depends.get(pkg_name)
            if raw_deps is None:
                # 索引中没有依赖信息，跳过
                continue
            for raw_dep in raw_deps:
                entry = self._resolve_dep(pkg_name, raw_dep)
                dep_key_for_log = raw_dep.package_name
                dep_name_display = raw_dep.package_name
                if entry.status == "ok":
                    self.log(f"    [DEP OK] {dep_name_display}: {entry.note}")
                elif entry.status == "virtual-provided":
                    self.log(f"    [DEP OK] {dep_name_display}: {entry.note}")
                    if not any(v[0] == dep_key_for_log for v in virtual_provided):
                        virtual_provided.append((dep_key_for_log, raw_dep.raw, entry.note))
                elif entry.status == "ignored-virtual":
                    self.log(f"    [DEP IGNORE] {dep_name_display}: {entry.note}")
                    if not any(v[0] == dep_key_for_log for v in ignored_virtuals):
                        ignored_virtuals.append((dep_key_for_log, raw_dep.raw))
                elif entry.status == "upgrade":
                    self.log(f"    [DEP UPGRADE] {dep_name_display}: {entry.note}")
                    dn = self._strip_suffix(entry.patch_pkg.name)
                    if dn not in self.dep_pkgs and dn not in self.patch_pkgs and dn not in self.related_pkgs:
                        self.dep_pkgs[dn] = entry.patch_pkg
                        new_names.append(dn)
                        self.log(f"      -> 标记下载依赖: {dn} ({entry.patch_pkg.version})")
                    else:
                        self.log(f"      -> 依赖已标记: {dn}")
                elif entry.status == "missing":
                    self.log(f"    [DEP MISSING] {dep_name_display}: {entry.note}")
                    if not any(u[0] == dep_key_for_log for u in unresolved):
                        unresolved.append((dep_key_for_log, raw_dep.raw))
        return new_names, unresolved, ignored_virtuals, virtual_provided


    # ── Full resolution pipeline ──────────────────────────────────────────────

    def resolve(
        self,
        requested: list[str],
        output_dir: Optional[Path] = None,
        include_recommends: bool = False,
        dry_run: bool = False,
        retry: int = 1,
        config_summary: str = "",
    ) -> "ResolutionResult":
        """
        Run the full patch-centric resolution pipeline.
        所有依赖解析均通过 Packages 索引完成，不下载 .deb 文件。
        下载仅在 /run 路由中单独执行。
        Returns a ResolutionResult with all details.
        """
        self.config_summary = config_summary
        self.log("=" * 60)
        self.log("[配置信息]")
        if self.config_summary:
            for line in self.config_summary.splitlines():
                self.log(f"  {line}")
        self.log("=" * 60)
        self.log("步骤 1: 解析补丁包（取版本最高）")
        self.log("=" * 60)
        patch_names = self.resolve_patches(requested)

        # 纯索引解析：不下载任何 .deb 文件
        self.log("")
        self.log("=" * 60)
        self.log("步骤 2: 纯索引依赖解析（Packages 元数据）")
        self.log("=" * 60)

        unresolved: list[tuple[str, str]] = []
        _unresolved_keys: set[str] = set()
        ignored_virtuals: list[tuple[str, str]] = []
        _ignored_virtual_keys: set[str] = set()
        virtual_provided: list[tuple[str, str, str]] = []   # (pkg, raw_dep, note)
        _virtual_provided_keys: set[str] = set()

        # ── 目录扫描：patch 包所在目录下的配套包 ──
        for name in patch_names:
            if name in self.patch_pkgs:
                self.scan_directory_upgrade(self.patch_pkgs[name])

        # ── 初始层：解析 patch 包，收集第一层依赖 ──
        init_names = list(self.patch_pkgs.keys())
        _, new_unresolved, new_ignored_virtuals, new_virtual_provided = self.resolve_deps(init_names, include_recommends)
        for item in new_unresolved:
            if item[0] not in _unresolved_keys:
                unresolved.append(item)
                _unresolved_keys.add(item[0])
        for item in new_ignored_virtuals:
            if item[0] not in _ignored_virtual_keys:
                ignored_virtuals.append(item)
                _ignored_virtual_keys.add(item[0])
        for item in new_virtual_provided:
            if item[0] not in _virtual_provided_keys:
                virtual_provided.append(item)
                _virtual_provided_keys.add(item[0])

        # 递归：解析新发现的 dep 包和 related 包的依赖，直到无新包
        depth = 0
        _resolved_so_far: set[str] = set(patch_names)
        while True:
            pending = [n for n in self.dep_pkgs if n not in _resolved_so_far]
            pending_related = [n for n in self.related_pkgs if n not in _resolved_so_far]
            if not pending and not pending_related:
                break
            if depth >= self.max_depth:
                break

            depth += 1
            all_to_resolve = pending + pending_related
            if pending:
                self.log(f"  ── 依赖层级 {depth}: {len(all_to_resolve)} 个新包 ──")

            # 目录扫描：对新发现的 dep 包扫描其目录下的配套包
            for n in pending:
                if n in self.dep_pkgs:
                    self.scan_directory_upgrade(self.dep_pkgs[n])

            # 对 related 包也做目录扫描
            for n in pending_related:
                if n in self.related_pkgs:
                    self.scan_directory_upgrade(self.related_pkgs[n])

            # 纯索引解析依赖（不下载）
            _, new_unresolved, new_ignored_virtuals, new_virtual_provided = self.resolve_deps(all_to_resolve, include_recommends)
            for item in new_unresolved:
                if item[0] not in _unresolved_keys:
                    unresolved.append(item)
                    _unresolved_keys.add(item[0])
            for item in new_ignored_virtuals:
                if item[0] not in _ignored_virtual_keys:
                    ignored_virtuals.append(item)
                    _ignored_virtual_keys.add(item[0])
            for item in new_virtual_provided:
                if item[0] not in _virtual_provided_keys:
                    virtual_provided.append(item)
                    _virtual_provided_keys.add(item[0])

            for n in all_to_resolve:
                _resolved_so_far.add(n)

        self.log("")
        self.log("=" * 60)
        self.log("步骤 3: 结果汇总")
        self.log("=" * 60)

        # Final result (no downloaded files in this phase)
        return self._build_result(
            output_dir or Path("."),
            patch_names,
            list(self.related_pkgs.keys()),  # 目录扫描发现的配套包
            list(self.dep_pkgs.keys()),       # 所有收集到的 dep 包
            unresolved,
            ignored_virtuals,
            virtual_provided,
        )

    def _build_result(
        self,
        output_dir: Path,
        patch_names: list[str],
        related_names: list[str],
        dep_names: list[str],
        unresolved: list[tuple[str, str]],
        ignored_virtuals: list[tuple[str, str]],
        virtual_provided: list[tuple[str, str, str]] = [],
    ) -> "ResolutionResult":
        patch_resolved = [self.patch_pkgs[n] for n in patch_names if n in self.patch_pkgs]
        related_resolved = [self.related_pkgs[n] for n in related_names if n in self.related_pkgs]
        dep_resolved = [self.dep_pkgs[n] for n in dep_names if n in self.dep_pkgs]

        return ResolutionResult(
            patch_packages=patch_resolved,
            related_packages=related_resolved,
            related_scans=[],   # 已移除 find_related，不再有 related_scans
            dep_packages=dep_resolved,
            not_found=self.not_found,
            unresolved_deps=unresolved,
            ignored_virtuals=ignored_virtuals,
            virtual_provided=virtual_provided,
            log="\n".join(self.log_lines),
            output_dir=output_dir,
            all_downloaded_files=[],  # 解析阶段不下载，下载在 /run 路由中单独执行
            config_summary=self.config_summary,
        )

    def log(self, msg: str):
        self.log_lines.append(msg)
        print(msg)

    @staticmethod
    def _strip_suffix(name: str) -> str:
        """Strip :amd64 / :arm64 suffix from package name."""
        return re.sub(r":(amd64|arm64|all)$", "", name, flags=re.IGNORECASE)


# ─── Result container ───────────────────────────────────────────────────────────

@dataclass
class RelatedScan:
    """Record of a single related-package scan."""
    directory: str
    pkg_name: str
    patch_version: str
    base_version: Optional[str]   # None = not in base
    action: str                   # "download" | "skip_not_in_base" | "skip_same_or_lower"
    reason: str                  # human-readable reason


@dataclass
class ResolutionResult:
    patch_packages: list[PatchPackage]
    related_packages: list[PatchPackage]           # packages confirmed to download
    related_scans: list[RelatedScan]             # all scanned related packages
    dep_packages: list[PatchPackage]
    not_found: list[tuple[str, str]]   # (name, base_version)
    unresolved_deps: list[tuple[str, str]]      # (name, raw_dep)
    ignored_virtuals: list[tuple[str, str]]     # (name, raw_dep) virtual pkgs w/o provider
    log: str
    output_dir: Path
    all_downloaded_files: list[Path]
    # config snapshot
    config_summary: str = ""                     # "Base: ... | Arch: ... | Repos: ..."
    virtual_provided: list[tuple[str, str, str]] = field(default_factory=list)  # (name, raw_dep, note)

    @property
    def all_packages(self) -> list[PatchPackage]:
        return self.patch_packages + self.related_packages + self.dep_packages

    def download_summary(self) -> str:
        lines = [
            f"补丁包 ({len(self.patch_packages)}):",
            *[f"  {p.name} {p.version}" for p in self.patch_packages],
            f"依赖包 ({len(self.dep_packages)}):",
            *[f"  {p.name} {p.version}" for p in self.dep_packages],
        ]
        if self.not_found:
            lines.append(f"\n未找到的包 ({len(self.not_found)}):")
            for name, base_ver in self.not_found:
                lines.append(
                    f"  {name}" + (f" (base 版本: {base_ver})" if base_ver else " (base 中也不存在)")
                )
        if self.unresolved_deps:
            lines.append(f"\n无法解决的依赖 ({len(self.unresolved_deps)}):")
            for name, raw_dep in self.unresolved_deps:
                lines.append(f"  {name}: {raw_dep}")
        return "\n".join(lines)


# ─── CLI ───────────────────────────────────────────────────────────────────────

def _cli_main():
    parser = argparse.ArgumentParser(
        description="Patch-centric APT package resolver — no local apt-get required."
    )
    parser.add_argument("--base-list", default="packages-full-x86_64.txt")
    parser.add_argument("--patch-list", default="patch-packages.txt")
    parser.add_argument("--output-dir", default="download")
    parser.add_argument(
        "--patch-repo", action="append", dest="patch_repos", default=[],
        metavar="URL DIST COMP PRIORITY",
        help="APT repository. Format: 'URL dist component priority'. "
             "Can be specified multiple times.",
    )
    parser.add_argument(
        "--architecture", default="amd64",
        choices=["amd64", "arm64", "all"],
        help="Target architecture (default: amd64, or auto-detected from base list)",
    )
    parser.add_argument("--include-recommends", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--retry", type=int, default=1)
    parser.add_argument("--max-depth", type=int, default=10)
    args = parser.parse_args()

    # Detect architecture from base list
    arch = args.architecture
    if arch == "amd64" and args.base_list:
        detected = detect_architecture_from_base(args.base_list)
        if detected:
            arch = detected
            print(f"[info] Architecture auto-detected from base list: {arch}")

    # Load base packages
    base_packages = load_base_packages(args.base_list)
    print(f"[info] Base packages loaded: {len(base_packages)}")

    # Parse repos
    multi_index = MultiRepoIndex()
    for repo_arg in args.patch_repos:
        parts = repo_arg.split()
        if len(parts) < 4:
            raise SystemExit(f"ERROR: --patch-repo requires 'URL dist comp priority', got: {repo_arg!r}")
        multi_index.add_repo(
            repo_url=parts[0],
            distribution=parts[1],
            components=[parts[2]],
            architecture=arch,
            priority=int(parts[3]),
        )

    if not multi_index.repos:
        raise SystemExit("ERROR: at least one --patch-repo is required")

    multi_index.repos.sort(key=lambda r: r["priority"])

    # Load patch packages
    patch_path = Path(args.patch_list)
    raw_text = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
    requested = [
        line.strip().split()[0]
        for line in raw_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not requested:
        raise SystemExit(f"ERROR: no patch packages in {args.patch_list}")

    print(f"[info] Requested patches: {requested}")

    # Fetch all repo indexes
    print(f"[info] Fetching Packages indexes from {len(multi_index.repos)} repository(ies)...")
    for repo_cfg in multi_index.repos:
        try:
            idx = _fetch_and_parse_index(
                base_url=repo_cfg["url"],
                distribution=repo_cfg["distribution"],
                components=repo_cfg["components"],
                architecture=arch,
            )
            # Populate by_name from by_directory
            for dir_, pkgs in idx.by_directory.items():
                for p in pkgs:
                    if p.name not in idx.by_name:
                        idx.by_name[p.name] = p
            _merge_indexes(multi_index, idx)
            print(f"  ✓ {repo_cfg['url']}: {len(idx.by_name)} packages, {len(idx.by_directory)} directories")
        except RepositoryFetchError as exc:
            print(f"  ✗ {repo_cfg['url']}: {exc}")
            raise SystemExit(1)

    print(f"[info] Combined index: {len(multi_index.by_name)} packages, "
          f"{len(multi_index.by_directory)} directories")

    # Run resolution
    resolver = PatchResolver(
        multi_index=multi_index,
        base_packages=base_packages,
        architecture=arch,
        max_workers=args.max_workers,
        max_depth=args.max_depth,
    )

    result = resolver.resolve(
        requested=requested,
        output_dir=Path(args.output_dir),
        include_recommends=args.include_recommends,
        dry_run=args.dry_run,
        retry=args.retry,
    )

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(result.download_summary())
    print(f"\nDownloaded files: {result.output_dir}")
    for f in result.all_downloaded_files:
        print(f"  {f}")


if __name__ == "__main__":
    _cli_main()
