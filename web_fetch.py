#!/usr/bin/env python3
"""web_fetch.py — oem_deb_fetch 的 Web 界面。

版本管理：每个版本保存基线清单与若干自定义仓库组，逐个仓库检查可升级包，
并按项目实时下载与验签。核心逻辑全部在 oem_deb_fetch 中。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import importlib.metadata
import io
import ipaddress
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import NotFound

from oem_deb_fetch import (
    ARCH_BASELINE,
    FetchError,
    Package,
    Upgrade,
    _download_one,
    baseline_filename,
    compare_versions,
    fetch_index,
    find_upgrades,
    load_baseline,
    normalize_arch,
    parse_repo_spec,
    resolve_baseline_source,
)
from notes import (
    NotesError as NotesFetchError,
    filter_report as notes_filter_report,
    is_configured as notes_is_configured,
    load_bundle as notes_load_bundle,
    merge_config as notes_merge_config,
    mirror_path as notes_mirror_path,
    period_label as notes_period_label,
    read_meta as notes_read_meta,
    sync_mirror as notes_sync_mirror,
    local_dir as notes_local_dir,
)

WORKDIR = Path(__file__).resolve().parent
CACHE_DIR = WORKDIR / "cache" / "fetch"
VERSION_CACHE = CACHE_DIR / "versions"
JOB_DIR = CACHE_DIR / "jobs"
NOTES_CACHE = WORKDIR / "cache" / "notes"
NOTES_LOCAL_FILE = WORKDIR / "config" / "notes.local.json"
DATA_DIR = WORKDIR / "config"
VERSIONS_FILE = DATA_DIR / "versions.json"
VERSION_FILE = WORKDIR / "VERSION"
APP_VERSION = (
    VERSION_FILE.read_text(encoding="utf-8").strip()
    if VERSION_FILE.is_file()
    else "0.0.0"
)
DEFAULT_UPDATE_URLS = (
    "https://github.com/fly602/oem-deb-resolver/raw/main/VERSION",
    "https://github.com/fly602/oem-deb-resolver/raw/refactor/simplified-fetch/VERSION",
    "https://api.github.com/repos/fly602/oem-deb-resolver/contents/VERSION?ref=main",
    "https://api.github.com/repos/fly602/oem-deb-resolver/contents/VERSION?ref=refactor/simplified-fetch",
)
GITHUB_URL = "https://github.com/fly602/oem-deb-resolver"

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024


def _ensure_dirs() -> None:
    for path in (VERSION_CACHE, JOB_DIR, DATA_DIR, NOTES_CACHE):
        path.mkdir(parents=True, exist_ok=True)


# ─── 通用 ──────────────────────────────────────────────────────────────────────


def _parse_filters(text: str) -> list[str]:
    return [part for part in re.split(r"[\s,]+", text.strip()) if part]


def _upgrade_to_dict(upgrade: Upgrade) -> dict:
    package = upgrade.package
    return {
        "name": package.name,
        "base_version": upgrade.base_version,
        "version": package.version,
        "architecture": package.architecture,
        "filename": package.filename,
        "size": package.size,
        "sha256": package.sha256,
        "repo_url": package.repo_url,
        "source": package.source or package.name,
    }


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@app.template_filter("filesize")
def filesize_filter(size: int) -> str:
    return _human_size(int(size))


@app.template_filter("repohost")
def repohost_filter(repo: dict) -> str:
    return _repo_label(repo)


def _verify_debs(root: Path) -> dict:
    """对下载目录中的 deb 逐个执行 deepin-deb-verify。"""
    binary = shutil.which("deepin-deb-verify")
    deb_files = sorted(path for path in root.rglob("*.deb") if path.is_file())
    entries: list[dict] = []
    if not deb_files:
        return {"available": bool(binary), "files": [], "passed": 0, "failed": 0}

    if not binary:
        for path in deb_files:
            entries.append(
                {
                    "name": str(path.relative_to(root)),
                    "size": path.stat().st_size,
                    "status": "skipped",
                    "detail": "未检测到 deepin-deb-verify",
                }
            )
        return {"available": False, "files": entries, "passed": 0, "failed": 0}

    def check(path: Path) -> dict:
        entry = {
            "name": str(path.relative_to(root)),
            "size": path.stat().st_size,
            "status": "ok",
            "detail": "",
        }
        try:
            result = subprocess.run(
                [binary, "-f", str(path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (result.stdout or "") + (result.stderr or "")
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            entry["detail"] = lines[-1] if lines else ""
            if result.returncode != 0:
                entry["status"] = "failed"
                entry["detail"] = entry["detail"] or f"退出码 {result.returncode}"
        except Exception as exc:  # noqa: BLE001 - 验签失败不应中断下载结果
            entry["status"] = "failed"
            entry["detail"] = str(exc)
        return entry

    workers = max(1, min(8, len(deb_files)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        entries = list(executor.map(check, deb_files))
    passed = sum(1 for entry in entries if entry["status"] == "ok")
    failed = sum(1 for entry in entries if entry["status"] == "failed")
    return {"available": True, "files": entries, "passed": passed, "failed": failed}


def _slug(value: str, fallback: str = "x") -> str:
    """目录名：保留中文等可读字符，只替换路径分隔符与空白。"""
    cleaned = re.sub(r"[\s\\/:*?\"<>|\x00-\x1f]+", "-", value).strip("-._ ")
    return cleaned or fallback


def _ascii_slug(value: str, fallback: str = "version") -> str:
    """版本 ID：只保留 ASCII。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    return cleaned or fallback


def _arch_list(arch: str) -> list[str]:
    return list(ARCH_BASELINE) if arch == "all" else [arch]


_BASELINE_ARCH_TOKENS = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "loongarch64": "loong64",
    "loong64": "loong64",
    "sw_64": "sw64",
    "sw64": "sw64",
    "mips64el": "mips64el",
}


def detect_arch_from_baseline(source: str) -> str | None:
    """从具体基线文件名中识别架构，如 packages-full-aarch64.txt → arm64。"""
    tail = source.strip().rstrip("/").rsplit("/", 1)[-1].lower()
    if not tail.endswith(".txt"):
        return None
    stem = tail[:-4]
    if stem.startswith("packages-full-"):
        return _BASELINE_ARCH_TOKENS.get(stem[len("packages-full-") :])
    for token, arch in _BASELINE_ARCH_TOKENS.items():
        if stem.endswith("-" + token) or stem.endswith("_" + token):
            return arch
    return None


def effective_arch(version: dict) -> str:
    return detect_arch_from_baseline(version.get("base_list", "")) or version.get(
        "arch", "amd64"
    )


def _zip_tree(root: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.name.endswith(".part"):
                archive.write(path, path.relative_to(root))


# ─── 更新注记 ──────────────────────────────────────────────────────────────────

NOTE_FIELDS = ("url", "ref", "dir", "sources")
_NOTES_STATE_MEMO: dict[str, tuple[float, dict]] = {}


def _notes_local() -> dict:
    try:
        data = json.loads(NOTES_LOCAL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _notes_token() -> str:
    token = os.environ.get("OEM_GITLAB_TOKEN", "").strip()
    if token:
        return token
    local = _notes_local()
    return str(local.get("token", "")).strip()


def _notes_global_config() -> dict:
    local = _notes_local()
    return notes_merge_config(
        {
            "url": local.get("url", ""),
            "ref": local.get("ref", ""),
            "dir": local.get("dir", ""),
            "sources": local.get("sources", []),
        }
    )


def _notes_has_fields(raw) -> bool:
    if not isinstance(raw, dict):
        return False
    for field in NOTE_FIELDS:
        value = raw.get(field)
        if isinstance(value, (list, tuple)):
            if any(str(item).strip() for item in value):
                return True
        elif str(value or "").strip():
            return True
    return False


def _repo_notes(group: dict) -> list[dict | None]:
    raw = group.get("repo_notes")
    repos = group.get("repos", [])
    items = raw if isinstance(raw, list) else []
    result: list[dict | None] = []
    for index in range(len(repos)):
        item = items[index] if index < len(items) else None
        result.append(item if isinstance(item, dict) and item else None)
    return result


def _notes_config(version: dict, group: dict | None = None, repo_index: int | None = None) -> dict:
    """按「组填了用组、组空则按仓库」解析一处的注记配置。"""
    layers: list[dict | None] = [_notes_global_config(), version.get("notes")]
    group_notes = group.get("notes") if isinstance(group, dict) else None
    if _notes_has_fields(group_notes):
        layers.append(group_notes)
    elif isinstance(group, dict) and repo_index is not None:
        repo_items = _repo_notes(group)
        if repo_index < len(repo_items):
            layers.append(repo_items[repo_index])
    return notes_merge_config(*layers)


def _notes_mirror_override() -> str:
    local = _notes_local()
    return str(local.get("mirror_dir", "")).strip()


def _notes_bundle(config: dict, refresh: bool = False):
    return notes_load_bundle(
        config,
        NOTES_CACHE,
        _notes_token(),
        override=_notes_mirror_override(),
        refresh=refresh,
    )


def _notes_mirror_status(config: dict) -> dict:
    url = config.get("url", "")
    if not url or notes_local_dir(url):
        return {}
    path = notes_mirror_path(url, NOTES_CACHE, _notes_mirror_override())
    meta = notes_read_meta(path)
    return {
        "path": str(path),
        "exists": (path / ".git").is_dir(),
        "commit": meta.get("commit", ""),
        "updated_at": meta.get("updated_at", ""),
        "last_error": meta.get("last_error", ""),
    }


def _notes_configs_for_version(version: dict) -> list[dict]:
    """该版本会用到注记源（按组/仓库规则解析后去重）。"""
    configs: list[dict] = []
    seen: set[str] = set()

    def add(config: dict) -> None:
        if not notes_is_configured(config):
            return
        key = json.dumps(config, sort_keys=True, ensure_ascii=False)
        if key in seen:
            return
        seen.add(key)
        configs.append(config)

    for group in version.get("groups", []):
        if _notes_has_fields(group.get("notes")):
            add(_notes_config(version, group))
            continue
        for repo_index in range(len(group.get("repos", []))):
            add(_notes_config(version, group, repo_index))
    if not configs:
        add(_notes_config(version))
    return configs


def refresh_notes_mirrors(version: dict, timeout: int = 60) -> list[dict]:
    """检查更新时调用：把该版本的注记镜像同步到最新，失败只记录不抛。"""
    results: list[dict] = []
    for config in _notes_configs_for_version(version):
        entry = {"url": config.get("url", ""), "ok": True, "message": ""}
        try:
            _, action = notes_sync_mirror(
                config["url"],
                config.get("ref", ""),
                _notes_token(),
                NOTES_CACHE,
                _notes_mirror_override(),
                timeout=timeout,
            )
            entry["message"] = action
            _NOTES_STATE_MEMO.clear()
        except NotesFetchError as exc:
            entry["ok"] = False
            entry["message"] = str(exc)
        results.append(entry)
    return results


def _notes_state(config: dict) -> dict:
    if not notes_is_configured(config):
        return {"available": False, "reason": "未配置更新注记源", "label": "", "latest": ""}
    url = config.get("url", "")
    if not notes_local_dir(url):
        mirror = notes_mirror_path(url, NOTES_CACHE, _notes_mirror_override())
        if not (mirror / ".git").is_dir():
            return {
                "available": False,
                "reason": "注记仓库尚未同步（点「检查更新」时会自动拉取）",
                "label": "",
                "latest": "",
            }
    key = json.dumps(config, sort_keys=True, ensure_ascii=False)
    now = time.time()
    cached = _NOTES_STATE_MEMO.get(key)
    if cached and now - cached[0] < 300:
        return cached[1]
    try:
        bundle = _notes_bundle(config)
        periods = bundle.periods()
        state = {
            "available": True,
            "reason": "",
            "latest": periods[0],
            "label": notes_period_label(periods[0]),
        }
    except NotesFetchError as exc:
        state = {"available": False, "reason": str(exc), "latest": "", "label": ""}
    _NOTES_STATE_MEMO[key] = (now, state)
    return state


# ─── 版本持久化 ────────────────────────────────────────────────────────────────


def _load_versions() -> list[dict]:
    if not VERSIONS_FILE.is_file():
        return []
    try:
        data = json.loads(VERSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    versions = data.get("versions", []) if isinstance(data, dict) else []
    return [
        version
        for version in versions
        if isinstance(version, dict) and version.get("id") and version.get("name")
    ]


def _save_versions(versions: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = VERSIONS_FILE.with_name(VERSIONS_FILE.name + ".tmp")
    tmp.write_text(
        json.dumps({"schema": 1, "versions": versions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, VERSIONS_FILE)


def _get_version(version_id: str) -> dict:
    for version in _load_versions():
        if version.get("id") == version_id:
            return version
    raise NotFound()


def _version_id(name: str, versions: list[dict]) -> str:
    base = _ascii_slug(name, "version")
    existing = {version["id"] for version in versions}
    candidate = base
    index = 2
    while candidate in existing:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _notes_form_data(raw) -> dict:
    merged = notes_merge_config(raw if isinstance(raw, dict) else None)
    return {
        "url": merged["url"],
        "ref": merged["ref"],
        "dir": merged["dir"],
        "sources": ", ".join(merged["sources"]),
    }


def _version_form_data(version: dict | None) -> dict:
    if version is None:
        return {
            "name": "",
            "description": "",
            "admin": "",
            "base_list": "",
            "arch": "amd64",
            "notes": _notes_form_data(None),
            "groups": [_empty_group_form()],
        }
    return {
        "name": version["name"],
        "description": version.get("description", ""),
        "admin": version.get("admin", ""),
        "base_list": version["base_list"],
        "arch": version.get("arch", "amd64"),
        "notes": _notes_form_data(version.get("notes")),
        "groups": [
            {
                "name": group["name"],
                "description": group.get("description", ""),
                "maintainer": group.get("maintainer", ""),
                "repos": "\n".join(group["repos"]),
                "notes": _notes_form_data(group.get("notes")),
                "repo_notes": _repo_notes_form_text(group),
            }
            for group in version.get("groups", [])
        ]
        or [_empty_group_form()],
    }


def _repo_notes_form_text(group: dict) -> str:
    lines: list[str] = []
    for item in _repo_notes(group):
        lines.append(json.dumps(item, ensure_ascii=False) if item else "")
    return "\n".join(lines)


def _empty_group_form() -> dict:
    return {
        "name": "",
        "description": "",
        "maintainer": "",
        "repos": "",
        "notes": _notes_form_data(None),
        "repo_notes": "",
    }


def _submitted_form_data(form) -> dict:
    names = form.getlist("group_name")
    descriptions = form.getlist("group_description")
    maintainers = form.getlist("group_maintainer")
    repos = form.getlist("group_repos")
    notes_urls = form.getlist("group_notes_url")
    notes_refs = form.getlist("group_notes_ref")
    notes_dirs = form.getlist("group_notes_dir")
    notes_sources = form.getlist("group_notes_sources")
    repo_notes = form.getlist("group_repo_notes")

    def pick(values: list[str], index: int) -> str:
        return values[index] if index < len(values) else ""

    groups = [
        {
            "name": name,
            "description": pick(descriptions, index),
            "maintainer": pick(maintainers, index),
            "repos": pick(repos, index),
            "notes": {
                "url": pick(notes_urls, index),
                "ref": pick(notes_refs, index),
                "dir": pick(notes_dirs, index),
                "sources": pick(notes_sources, index),
            },
            "repo_notes": pick(repo_notes, index),
        }
        for index, name in enumerate(names)
    ]
    return {
        "name": form.get("name", ""),
        "description": form.get("description", ""),
        "admin": form.get("admin", ""),
        "base_list": form.get("base_list", ""),
        "arch": form.get("architecture", "amd64"),
        "notes": {
            "url": form.get("notes_url", ""),
            "ref": form.get("notes_ref", ""),
            "dir": form.get("notes_dir", ""),
            "sources": form.get("notes_sources", ""),
        },
        "groups": groups or [_empty_group_form()],
    }


def _version_from_form(form) -> dict:
    name = form.get("name", "").strip()
    base_list = form.get("base_list", "").strip()
    arch_raw = form.get("architecture", "amd64").strip()
    if not name:
        raise ValueError("请填写版本名称")
    if not base_list:
        raise ValueError("请填写基线仓库/清单地址")
    try:
        arch = normalize_arch(arch_raw)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    group_names = form.getlist("group_name")
    group_repos = form.getlist("group_repos")
    group_descriptions = form.getlist("group_description")
    group_maintainers = form.getlist("group_maintainer")
    group_notes_urls = form.getlist("group_notes_url")
    group_notes_refs = form.getlist("group_notes_ref")
    group_notes_dirs = form.getlist("group_notes_dir")
    group_notes_sources = form.getlist("group_notes_sources")
    group_repo_notes = form.getlist("group_repo_notes")

    def pick(values: list[str], index: int) -> str:
        return values[index] if index < len(values) else ""

    groups: list[dict] = []
    for index, (group_name, repo_text) in enumerate(
        zip(group_names, group_repos), 1
    ):
        group_name = group_name.strip()
        group_description = (
            group_descriptions[index - 1].strip()
            if index - 1 < len(group_descriptions)
            else ""
        )[:500]
        group_maintainer = (
            group_maintainers[index - 1].strip()
            if index - 1 < len(group_maintainers)
            else ""
        )[:200]
        repos = [
            line.strip()
            for line in repo_text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not group_name and not repos:
            continue
        if not group_name:
            raise ValueError(f"第 {index} 个仓库组缺少名称")
        if not repos:
            raise ValueError(f"仓库组「{group_name}」至少需要一个补丁仓库")
        for repo in repos:
            parse_repo_spec(repo)  # 只做格式校验
        entry = {
            "name": group_name,
            "description": group_description,
            "maintainer": group_maintainer,
            "repos": repos,
        }
        group_notes = _notes_from_fields(
            pick(group_notes_urls, index - 1),
            pick(group_notes_refs, index - 1),
            pick(group_notes_dirs, index - 1),
            pick(group_notes_sources, index - 1),
        )
        if group_notes:
            entry["notes"] = group_notes
        repo_notes = _repo_notes_from_text(
            pick(group_repo_notes, index - 1), len(repos), group_name
        )
        if repo_notes:
            entry["repo_notes"] = repo_notes
        groups.append(entry)

    if not groups:
        raise ValueError("请至少添加一个补丁仓库组")
    detected = detect_arch_from_baseline(base_list)
    version = {
        "name": name,
        "description": form.get("description", "").strip()[:500],
        "admin": form.get("admin", "").strip()[:200],
        "base_list": base_list,
        "arch": detected or arch,
        "groups": groups,
    }
    version_notes = _notes_from_fields(
        form.get("notes_url", ""),
        form.get("notes_ref", ""),
        form.get("notes_dir", ""),
        form.get("notes_sources", ""),
    )
    if version_notes:
        version["notes"] = version_notes
    return version


def _notes_from_fields(url: str, ref: str, dir_name: str, sources: str) -> dict:
    merged = notes_merge_config(
        {"url": url, "ref": ref, "dir": dir_name, "sources": sources}
    )
    if not any(merged[field] for field in NOTE_FIELDS):
        return {}
    return merged


def _repo_notes_from_text(text: str, repo_count: int, group_name: str) -> list[dict | None]:
    lines = text.splitlines()
    items: list[dict | None] = []
    for line in lines[:repo_count]:
        stripped = line.strip()
        if not stripped:
            items.append(None)
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"仓库组「{group_name}」第 {len(items) + 1} 行注记配置不是合法 JSON：{exc.msg}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(
                f"仓库组「{group_name}」第 {len(items) + 1} 行注记配置必须是 JSON 对象"
            )
        merged = notes_merge_config(data)
        items.append(merged if any(merged[field] for field in NOTE_FIELDS) else None)
    while len(items) < repo_count:
        items.append(None)
    if not any(items):
        return []
    return items


def _cleanup_checks(version_id: str) -> None:
    directory = VERSION_CACHE / version_id
    checks = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for stale in checks[:-10]:
        stale.unlink(missing_ok=True)


def _load_check(version_id: str, check_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", check_id):
        abort(404)
    path = VERSION_CACHE / version_id / f"{check_id}.json"
    if not path.is_file():
        abort(404)
    return json.loads(path.read_text(encoding="utf-8"))


def _payload_size(payload: dict) -> int:
    return sum(
        int(item["size"])
        for group in payload.get("groups", [])
        for repo in group.get("repos", [])
        for info in repo.get("by_arch", {}).values()
        for item in info.get("upgrades", [])
    )


def _repo_label(repo: dict) -> str:
    try:
        url = parse_repo_spec(repo["spec"]).url
        host = urlsplit(url).hostname or url
        return host
    except ValueError:
        return "repo"


def _repo_rows(repo: dict, arch_order: list[str]) -> list[dict]:
    """把一个仓库各架构的升级包合并成一行一个包。"""
    rows: dict[str, dict] = {}
    for arch in arch_order:
        info = repo.get("by_arch", {}).get(arch)
        if not info or info.get("error"):
            continue
        for item in info.get("upgrades", []):
            row = rows.setdefault(
                item["name"],
                {
                    "name": item["name"],
                    "project": item.get("source") or item["name"],
                    "base_versions": [],
                    "versions": [],
                    "arches": [],
                    "size": 0,
                },
            )
            if item["base_version"] not in row["base_versions"]:
                row["base_versions"].append(item["base_version"])
            if item["version"] not in row["versions"]:
                row["versions"].append(item["version"])
            row["arches"].append(arch)
            row["size"] += int(item["size"])
    return sorted(rows.values(), key=lambda item: item["name"])


def _group_projects(rows: list[dict]) -> list[dict]:
    projects: dict[str, dict] = {}
    for row in rows:
        project = projects.setdefault(
            row["project"],
            {
                "name": row["project"],
                "packages": [],
                "arches": [],
                "size": 0,
                "base_versions": [],
                "versions": [],
            },
        )
        project["packages"].append(row)
        project["size"] += int(row["size"])
        for arch in row["arches"]:
            if arch not in project["arches"]:
                project["arches"].append(arch)
        for version in row["base_versions"]:
            if version not in project["base_versions"]:
                project["base_versions"].append(version)
        for version in row["versions"]:
            if version not in project["versions"]:
                project["versions"].append(version)
    return sorted(projects.values(), key=lambda item: item["name"])


def _merge_group_by_arch(group: dict, arch_order: list[str]) -> dict:
    """合并组内多个仓库，同一包同一架构取最高版本。"""
    merged: dict[str, dict] = {}
    for arch in arch_order:
        best: dict[str, dict] = {}
        for repo in group["repos"]:
            info = repo.get("by_arch", {}).get(arch)
            if not info or info.get("error"):
                continue
            for item in info.get("upgrades", []):
                previous = best.get(item["name"])
                if previous is None or compare_versions(
                    item["version"], previous["version"]
                ) > 0:
                    best[item["name"]] = item
        merged[arch] = {
            "packages": len(best),
            "upgrades": [best[name] for name in sorted(best)],
            "error": "",
        }
    return merged


def _payload_package_count(payload: dict) -> int:
    total = 0
    for group in payload.get("groups", []):
        projects = group.get("merged_projects")
        if projects is not None:
            total += sum(len(project["packages"]) for project in projects)
        else:
            total += sum(len(repo.get("rows", [])) for repo in group.get("repos", []))
    return total


def _selected_arches(payload: dict, arch: str) -> list[str]:
    available = [item["arch"] for item in payload["arches"] if not item.get("error")]
    if arch == "all":
        return available
    if arch not in available:
        raise NotFound()
    return [arch]


# ─── 首页：版本列表 ────────────────────────────────────────────────────────────


@app.route("/", methods=["GET"])
def index():
    versions = _load_versions()
    for version in versions:
        version["display_arch"] = effective_arch(version)
    return render_template("versions_index.html", versions=versions)


# ─── 版本增删改查 ──────────────────────────────────────────────────────────────


@app.route("/versions/new", methods=["GET"])
def version_new():
    return render_template(
        "version_form.html",
        mode="create",
        action=url_for("version_create"),
        form=_version_form_data(None),
        version=None,
        arches=list(ARCH_BASELINE),
        error="",
    )


@app.route("/versions", methods=["POST"])
def version_create():
    try:
        data = _version_from_form(request.form)
    except ValueError as exc:
        return render_template(
            "version_form.html",
            mode="create",
            action=url_for("version_create"),
            form=_submitted_form_data(request.form),
            version=None,
            arches=list(ARCH_BASELINE),
            error=str(exc),
        )
    versions = _load_versions()
    version = {
        "id": _version_id(data["name"], versions),
        "name": data["name"],
        "description": data.get("description", ""),
        "admin": data.get("admin", ""),
        "base_list": data["base_list"],
        "arch": data["arch"],
        "groups": data["groups"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if data.get("notes"):
        version["notes"] = data["notes"]
    versions.append(version)
    _save_versions(versions)
    return redirect(url_for("version_detail", version_id=version["id"]))


def _nominal_baseline_url(source: str, arch: str) -> str:
    if source.rstrip("/").lower().endswith(".txt"):
        return source
    return (
        f"{source.rstrip('/')}/report/iso-package-list/"
        f"{baseline_filename(arch)}"
    )


def _repo_short_label(url: str) -> str:
    parsed = urlsplit(url)
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    host = parsed.hostname or url
    if tail and len(tail) >= 24:
        return tail
    return f"{host}/{tail}" if tail else host


def _friendly_baseline_label(source: str) -> str:
    tail = source.rstrip("/").rsplit("/", 1)[-1]
    if tail.lower().endswith(".txt"):
        arch = detect_arch_from_baseline(source)
        if arch:
            return f"基线包清单（{arch}）"
        return f"基线包清单（{tail}）"
    match = re.search(r"/([^/]*stable[^/]*)/?$", source, re.IGNORECASE)
    if match:
        return f"{match.group(1)} 基线清单"
    return "发布基线清单"


def _source_page_url(
    version_id: str,
    kind: str,
    arch: str,
    q: str,
    per_page: str,
    page: int,
    group_index: int | None = None,
    repo_index: int | None = None,
) -> str:
    params = {
        "version_id": version_id,
        "kind": kind,
        "arch": arch,
        "per_page": per_page,
        "page": page,
    }
    if q:
        params["q"] = q
    if kind == "repo":
        params["group"] = group_index
        params["repo"] = repo_index
    return url_for("version_source_detail", **params)


@app.route("/versions/<version_id>", methods=["GET"])
def version_detail(version_id: str):
    version = _get_version(version_id)
    repo_groups = []
    repo_count = 0
    for group_index, group in enumerate(version["groups"]):
        repos = []
        group_notes_mode = _notes_has_fields(group.get("notes"))
        group_state = _notes_state(_notes_config(version, group)) if group_notes_mode else None
        for repo_index, spec in enumerate(group["repos"]):
            repo_count += 1
            try:
                repo = parse_repo_spec(spec)
                url = repo.url
                short = _repo_short_label(url)
            except ValueError:
                url = spec
                short = spec
            entry = {
                "serial": repo_index + 1,
                "group_index": group_index,
                "repo_index": repo_index,
                "url": url,
                "short": short,
                "notes_state": None,
                "notes_url": "",
            }
            if not group_notes_mode:
                state = _notes_state(_notes_config(version, group, repo_index))
                entry["notes_state"] = state
                if state["available"]:
                    entry["notes_url"] = url_for(
                        "version_notes",
                        version_id=version_id,
                        group=group_index,
                        repo=repo_index,
                    )
            repos.append(entry)
        repo_groups.append(
            {
                "index": group_index,
                "name": group["name"],
                "description": group.get("description", ""),
                "maintainer": group.get("maintainer", ""),
                "repos": repos,
                "notes_state": group_state,
                "notes_url": url_for(
                    "version_notes", version_id=version_id, group=group_index
                )
                if group_state and group_state["available"]
                else "",
            }
        )
    baseline_rows = []
    for index, arch in enumerate(_arch_list(effective_arch(version))):
        try:
            url = resolve_baseline_source(version["base_list"], arch)
            error = ""
        except (FetchError, ValueError) as exc:
            url = _nominal_baseline_url(version["base_list"], arch)
            error = str(exc)
        baseline_rows.append(
            {
                "serial": index + 1,
                "arch": arch,
                "url": url,
                "label": url.rstrip("/").rsplit("/", 1)[-1] or url,
                "error": error,
            }
        )
    return render_template(
        "version_detail.html",
        version=version,
        display_arch=effective_arch(version),
        detected_arch=detect_arch_from_baseline(version.get("base_list", "")),
        repo_count=repo_count,
        baseline_rows=baseline_rows,
        repo_groups=repo_groups,
        error=request.args.get("error", ""),
    )


@app.route("/versions/<version_id>/source", methods=["GET"])
def version_source_detail(version_id: str):
    version = _get_version(version_id)
    kind = request.args.get("kind", "baseline")
    if kind not in ("baseline", "repo"):
        kind = "baseline"

    arch_options = _arch_list(effective_arch(version))
    arch = request.args.get("arch", "")
    if arch not in arch_options:
        arch = arch_options[0] if arch_options else "amd64"

    q = request.args.get("q", "").strip()[:100]
    per_page_raw = request.args.get("per_page", "100")
    per_page_map = {"50": 50, "100": 100, "500": 500, "1000": 1000, "all": None}
    if per_page_raw not in per_page_map:
        per_page_raw = "100"
    per_page = per_page_map[per_page_raw]

    try:
        page = max(1, int(request.args.get("page", "1") or 1))
    except ValueError:
        page = 1

    label = ""
    url = ""
    rows: list[dict] = []
    error = ""
    group_index: int | None = None
    repo_index: int | None = None

    if kind == "baseline":
        label = _friendly_baseline_label(version["base_list"])
        try:
            source = resolve_baseline_source(version["base_list"], arch)
            baseline = load_baseline(source, arch)
            rows = [
                {"name": name, "version": version_text}
                for name, version_text in baseline.items()
            ]
            url = source
        except (FetchError, ValueError) as exc:
            error = str(exc)
            url = version["base_list"]
    else:
        try:
            group_index = int(request.args.get("group", "0"))
            repo_index = int(request.args.get("repo", "0"))
            group = version["groups"][group_index]
            spec = group["repos"][repo_index]
            repo = parse_repo_spec(spec)
            host = urlsplit(repo.url).hostname or repo.url
            label = f"{group['name']} · {host}"
            url = repo.url
            packages = fetch_index(repo, arch)
            rows = [
                {
                    "name": package.name,
                    "version": package.version,
                    "architecture": package.architecture,
                    "size": package.size,
                    "filename": package.filename,
                }
                for package in packages.values()
            ]
        except (IndexError, ValueError, FetchError) as exc:
            error = str(exc)
            if not label:
                label = "补丁仓库"

    if q:
        needle = q.lower()
        rows = [
            row
            for row in rows
            if needle in row["name"].lower() or needle in row["version"].lower()
        ]
    rows.sort(key=lambda row: row["name"])

    total = len(rows)
    if per_page is None:
        total_pages = 1
        page = 1
        shown = rows
    else:
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        shown = rows[start : start + per_page]

    if total == 0:
        start_index = end_index = 0
    elif per_page is None:
        start_index, end_index = 1, total
    else:
        start_index = (page - 1) * per_page + 1
        end_index = min(total, page * per_page)

    page_links = []
    if total_pages > 1:
        first_page = max(1, page - 2)
        last_page = min(total_pages, page + 2)
        for number in range(first_page, last_page + 1):
            page_links.append(
                {
                    "number": number,
                    "url": _source_page_url(
                        version_id,
                        kind,
                        arch,
                        q,
                        per_page_raw,
                        number,
                        group_index,
                        repo_index,
                    ),
                }
            )
    prev_url = (
        _source_page_url(
            version_id,
            kind,
            arch,
            q,
            per_page_raw,
            page - 1,
            group_index,
            repo_index,
        )
        if page > 1
        else ""
    )
    next_url = (
        _source_page_url(
            version_id,
            kind,
            arch,
            q,
            per_page_raw,
            page + 1,
            group_index,
            repo_index,
        )
        if page < total_pages
        else ""
    )
    return render_template(
        "source_detail.html",
        version=version,
        kind=kind,
        label=label,
        url=url,
        arch=arch,
        arch_options=arch_options,
        q=q,
        per_page=per_page_raw,
        page=page,
        total=total,
        total_pages=total_pages,
        shown=shown,
        error=error,
        start_index=start_index,
        end_index=end_index,
        page_links=page_links,
        prev_url=prev_url,
        next_url=next_url,
        group_index=group_index,
        repo_index=repo_index,
    )


def _notes_target(version: dict) -> tuple[dict | None, dict]:
    group_index = request.args.get("group", type=int)
    repo_index = request.args.get("repo", type=int)
    groups = version.get("groups", [])
    if group_index is None:
        abort(404)
    if group_index < 0 or group_index >= len(groups):
        abort(404)
    group = groups[group_index]
    if repo_index is not None:
        if not _notes_has_fields(group.get("notes")):
            if repo_index < 0 or repo_index >= len(group.get("repos", [])):
                abort(404)
        elif repo_index < 0 or repo_index >= len(group.get("repos", [])):
            abort(404)
    config = _notes_config(version, group, repo_index)
    return group, {
        "group_index": group_index,
        "repo_index": repo_index,
        "config": config,
    }


def _notes_scope_label(version: dict, target: dict) -> str:
    group = version["groups"][target["group_index"]]
    label = f"仓库组 {group['name']}"
    if target["repo_index"] is not None:
        spec = group["repos"][target["repo_index"]]
        try:
            label += f" · {_repo_short_label(parse_repo_spec(spec).url)}"
        except ValueError:
            label += f" · {spec[:40]}"
    return label


@app.route("/versions/<version_id>/notes", methods=["GET"])
def version_notes(version_id: str):
    version = _get_version(version_id)
    group, target = _notes_target(version)
    period = request.args.get("period", "").strip()
    config = target["config"]
    base = {
        "version": version,
        "group": group,
        "scope": _notes_scope_label(version, target),
        "config": config,
        "args": {
            "group": group["name"],
            "group_index": target["group_index"],
            "repo_index": target["repo_index"],
        },
        "export_url": url_for(
            "version_notes_export",
            version_id=version_id,
            group=target["group_index"],
            **(
                {"repo": target["repo_index"]}
                if target["repo_index"] is not None
                else {}
            ),
        ),
        "notes_url": url_for(
            "version_notes",
            version_id=version_id,
            group=target["group_index"],
            **(
                {"repo": target["repo_index"]}
                if target["repo_index"] is not None
                else {}
            ),
        ),
        "mirror": _notes_mirror_status(config),
        "sync_message": request.args.get("sync", ""),
    }
    if not notes_is_configured(config):
        return (
            render_template(
                "notes.html",
                data=None,
                error="未配置更新注记源：请在版本或仓库组里填写「注记来源 URL」。",
                **base,
            ),
            200,
        )
    try:
        bundle = _notes_bundle(config, refresh=request.args.get("refresh") == "1")
        data = bundle.load(period or None)
    except NotesFetchError as exc:
        return render_template("notes.html", data=None, error=str(exc), **base), 200
    return render_template("notes.html", data=data, error="", **base)


@app.route("/versions/<version_id>/notes/refresh", methods=["POST"])
def version_notes_refresh(version_id: str):
    version = _get_version(version_id)
    _, target = _notes_target(version)
    config = target["config"]
    if not notes_is_configured(config):
        abort(404)
    try:
        _, action = notes_sync_mirror(
            config["url"],
            config.get("ref", ""),
            _notes_token(),
            NOTES_CACHE,
            _notes_mirror_override(),
            timeout=120,
        )
        message = action
    except NotesFetchError as exc:
        message = f"同步失败：{exc}"
    _NOTES_STATE_MEMO.clear()
    return redirect(
        url_for(
            "version_notes",
            version_id=version_id,
            group=target["group_index"],
            sync=message,
            **(
                {"repo": target["repo_index"]}
                if target["repo_index"] is not None
                else {}
            ),
        )
    )


@app.route("/versions/<version_id>/notes/export", methods=["GET"])
def version_notes_export(version_id: str):
    version = _get_version(version_id)
    _, target = _notes_target(version)
    config = target["config"]
    if not notes_is_configured(config):
        abort(404)
    try:
        bundle = _notes_bundle(config)
        payload = bundle.export_zip(version["name"])
    except (NotesFetchError, OSError) as exc:
        abort(404, description=str(exc))
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    slug = _ascii_slug(version["name"], "version")
    folder = config.get("dir") or "notes"
    return send_file(
        payload,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"oem-notes-{slug}-{_ascii_slug(folder, 'notes')}-{stamp}.zip",
    )


@app.route("/versions/<version_id>/edit", methods=["GET"])
def version_edit(version_id: str):
    version = _get_version(version_id)
    return render_template(
        "version_form.html",
        mode="edit",
        action=url_for("version_update", version_id=version_id),
        form=_version_form_data(version),
        version=version,
        arches=list(ARCH_BASELINE),
        error="",
    )


@app.route("/versions/<version_id>", methods=["POST"])
def version_update(version_id: str):
    version = _get_version(version_id)
    try:
        data = _version_from_form(request.form)
    except ValueError as exc:
        return render_template(
            "version_form.html",
            mode="edit",
            action=url_for("version_update", version_id=version_id),
            form=_submitted_form_data(request.form),
            version=version,
            arches=list(ARCH_BASELINE),
            error=str(exc),
        )
    versions = _load_versions()
    for item in versions:
        if item.get("id") == version_id:
            item.update(data)
            if "notes" not in data:
                item.pop("notes", None)
            break
    _save_versions(versions)
    return redirect(url_for("version_detail", version_id=version_id))


@app.route("/versions/<version_id>/delete", methods=["POST"])
def version_delete(version_id: str):
    version = _get_version(version_id)
    versions = [item for item in _load_versions() if item.get("id") != version_id]
    _save_versions(versions)
    shutil.rmtree(VERSION_CACHE / version["id"], ignore_errors=True)
    return redirect(url_for("index"))


@app.route("/versions/<version_id>/validate/baseline", methods=["POST"])
def version_validate_baseline(version_id: str):
    version = _get_version(version_id)
    checks: list[dict] = []
    arches = _arch_list(effective_arch(version))
    arch_filter = request.values.get("arch")
    if arch_filter in arches:
        arches = [arch_filter]
    for arch in arches:
        try:
            source = resolve_baseline_source(version["base_list"], arch)
            baseline = load_baseline(source, arch)
            checks.append(
                {
                    "arch": arch,
                    "title": arch,
                    "subtitle": source,
                    "ok": True,
                    "message": f"{len(baseline)} 个包",
                }
            )
        except (FetchError, ValueError) as exc:
            checks.append(
                {
                    "arch": arch,
                    "title": arch,
                    "subtitle": version["base_list"],
                    "ok": False,
                    "message": str(exc),
                }
            )
    return jsonify(
        {
            "kind": "baseline",
            "checks": checks,
            "failed": sum(1 for check in checks if not check["ok"]),
        }
    )


@app.route("/versions/<version_id>/validate/repos", methods=["POST"])
def version_validate_repos(version_id: str):
    version = _get_version(version_id)
    checks: list[dict] = []

    group_filter = request.values.get("group_index")
    repo_filter = request.values.get("repo_index")
    try:
        group_filter_index = int(group_filter) if group_filter is not None else None
        repo_filter_index = int(repo_filter) if repo_filter is not None else None
    except ValueError:
        group_filter_index = repo_filter_index = None

    filtered_groups: list[dict] = []
    for group_index, group in enumerate(version["groups"]):
        if group_filter_index is not None and group_index != group_filter_index:
            continue
        repos = [
            spec
            for repo_index, spec in enumerate(group["repos"])
            if repo_filter_index is None or repo_index == repo_filter_index
        ]
        if repos:
            filtered_groups.append({"name": group["name"], "repos": repos})

    for group in filtered_groups:
        for spec in group["repos"]:
            try:
                repo = parse_repo_spec(spec)
            except ValueError as exc:
                checks.append(
                    {
                        "arch": "—",
                        "title": group["name"],
                        "subtitle": spec,
                        "ok": False,
                        "message": str(exc),
                    }
                )
                continue
            for arch in _arch_list(effective_arch(version)):
                try:
                    packages = fetch_index(repo, arch)
                    checks.append(
                        {
                            "arch": arch,
                            "title": group["name"],
                            "subtitle": repo.url,
                            "ok": True,
                            "message": f"{len(packages)} 个包",
                        }
                    )
                except (FetchError, ValueError) as exc:
                    checks.append(
                        {
                            "arch": arch,
                            "title": group["name"],
                            "subtitle": repo.url,
                            "ok": False,
                            "message": str(exc),
                        }
                    )
    return jsonify(
        {
            "kind": "repos",
            "checks": checks,
            "failed": sum(1 for check in checks if not check["ok"]),
        }
    )



def _repo_notes_map(group: dict) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for spec, item in zip(group.get("repos", []), _repo_notes(group)):
        if item:
            mapping[spec] = item
    return mapping


def _merge_version_config(existing: dict, imported: dict) -> dict:
    """同名版本合并：保留现有配置，只追加缺失的仓库组和仓库。"""
    merged = dict(existing)
    for key in ("name", "description", "admin", "base_list", "arch", "created_at"):
        if not merged.get(key) and imported.get(key):
            merged[key] = imported[key]
    if not _notes_has_fields(merged.get("notes")) and _notes_has_fields(imported.get("notes")):
        merged["notes"] = imported["notes"]

    groups: list[dict] = []
    group_index: dict[str, dict] = {}
    repo_notes_map: dict[str, dict[str, dict]] = {}
    for group in existing.get("groups", []):
        copy = {
            "name": group["name"],
            "description": group.get("description", ""),
            "maintainer": group.get("maintainer", ""),
            "repos": list(dict.fromkeys(group.get("repos", []))),
        }
        if _notes_has_fields(group.get("notes")):
            copy["notes"] = group["notes"]
        groups.append(copy)
        group_index[copy["name"]] = copy
        repo_notes_map[copy["name"]] = _repo_notes_map(group)
    for group in imported.get("groups", []):
        target = group_index.get(group["name"])
        if target is None:
            target = {
                "name": group["name"],
                "description": group.get("description", ""),
                "maintainer": group.get("maintainer", ""),
                "repos": [],
            }
            if _notes_has_fields(group.get("notes")):
                target["notes"] = group["notes"]
            groups.append(target)
            group_index[group["name"]] = target
            repo_notes_map.setdefault(group["name"], {})
        else:
            for key in ("description", "maintainer"):
                if not target.get(key) and group.get(key):
                    target[key] = group[key]
            if not _notes_has_fields(target.get("notes")) and _notes_has_fields(group.get("notes")):
                target["notes"] = group["notes"]
        imported_notes = _repo_notes_map(group)
        for spec in imported_notes:
            repo_notes_map.setdefault(group["name"], {}).setdefault(spec, imported_notes[spec])
        for repo in group["repos"]:
            if repo not in target["repos"]:
                target["repos"].append(repo)
    for group in groups:
        mapping = repo_notes_map.get(group["name"]) or {}
        items = [mapping.get(spec) for spec in group["repos"]]
        if any(items):
            group["repo_notes"] = items
    merged["groups"] = groups
    return merged


def _parse_imported_versions(payload) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("versions"), list):
        items = payload["versions"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        raise ValueError("配置格式无效：需要 versions 数组或版本对象")
    if not items:
        raise ValueError("配置中没有版本")

    versions: list[dict] = []
    for index, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个版本不是对象")
        name = str(raw.get("name", "")).strip()
        base_list = str(raw.get("base_list", "")).strip()
        if not name:
            raise ValueError(f"第 {index} 个版本缺少 name")
        if not base_list:
            raise ValueError(f"版本 {name} 缺少 base_list")
        try:
            arch = normalize_arch(str(raw.get("arch", "amd64")))
        except ValueError as exc:
            raise ValueError(f"版本 {name}: {exc}") from exc
        detected = detect_arch_from_baseline(base_list)
        if detected:
            arch = detected
        version_notes = notes_merge_config(
            raw.get("notes") if isinstance(raw.get("notes"), dict) else None
        )

        groups_raw = raw.get("groups")
        if not isinstance(groups_raw, list) or not groups_raw:
            raise ValueError(f"版本 {name} 缺少仓库组")
        groups: list[dict] = []
        for group_index, group in enumerate(groups_raw, 1):
            if not isinstance(group, dict):
                raise ValueError(f"版本 {name} 第 {group_index} 个仓库组格式错误")
            group_name = str(group.get("name", "")).strip()
            repos_raw = group.get("repos", [])
            if isinstance(repos_raw, str):
                repos_raw = repos_raw.splitlines()
            if not isinstance(repos_raw, list):
                raise ValueError(f"版本 {name} 仓库组 {group_name} 的 repos 格式错误")
            repos = [str(repo).strip() for repo in repos_raw if str(repo).strip()]
            if not group_name or not repos:
                raise ValueError(f"版本 {name} 第 {group_index} 个仓库组缺少名称或仓库")
            for repo in repos:
                parse_repo_spec(repo)
            entry = {
                "name": group_name,
                "description": str(group.get("description", "")).strip()[:500],
                "maintainer": str(group.get("maintainer", "")).strip()[:200],
                "repos": repos,
            }
            group_notes = notes_merge_config(
                group.get("notes") if isinstance(group.get("notes"), dict) else None
            )
            if any(group_notes[field] for field in NOTE_FIELDS):
                entry["notes"] = group_notes
            repo_notes_raw = group.get("repo_notes")
            if isinstance(repo_notes_raw, list):
                items: list[dict | None] = []
                for item in repo_notes_raw[: len(repos)]:
                    if not isinstance(item, dict):
                        items.append(None)
                        continue
                    merged_item = notes_merge_config(item)
                    items.append(
                        merged_item
                        if any(merged_item[field] for field in NOTE_FIELDS)
                        else None
                    )
                while len(items) < len(repos):
                    items.append(None)
                if any(items):
                    entry["repo_notes"] = items
            groups.append(entry)

        version_id = re.sub(
            r"[^A-Za-z0-9._-]+", "-", str(raw.get("id", "")).strip()
        ).strip("-._")
        if not version_id:
            version_id = _ascii_slug(name, "version")
        entry = {
            "id": version_id,
            "name": name,
            "description": str(raw.get("description", "")).strip()[:500],
            "admin": str(raw.get("admin", "")).strip()[:200],
            "base_list": base_list,
            "arch": arch,
            "groups": groups,
            "created_at": str(
                raw.get("created_at")
                or datetime.now(timezone.utc).isoformat(timespec="seconds")
            ),
        }
        if any(version_notes[field] for field in NOTE_FIELDS):
            entry["notes"] = version_notes
        versions.append(entry)
    return versions


@app.route("/versions/import", methods=["GET", "POST"])
def version_import():
    error = ""
    result = None
    if request.method == "POST":
        raw_text = ""
        upload = request.files.get("config_file")
        if upload and upload.filename:
            try:
                raw_text = upload.read().decode("utf-8")
            except UnicodeDecodeError:
                error = "配置文件不是 UTF-8 文本"
        if not error and not raw_text:
            raw_text = request.form.get("json_text", "").strip()
        if not error and not raw_text:
            error = "请选择 versions.json 文件或粘贴配置内容"
        if not error:
            try:
                imported = _parse_imported_versions(json.loads(raw_text))
            except json.JSONDecodeError as exc:
                error = f"JSON 解析失败: {exc}"
            except ValueError as exc:
                error = str(exc)
        if not error:
            versions = _load_versions()
            added = updated = 0
            names: list[str] = []
            for item in imported:
                existing = next(
                    (version for version in versions if version["id"] == item["id"]),
                    None,
                )
                if existing is None:
                    versions.append(item)
                    added += 1
                else:
                    existing.update(_merge_version_config(existing, item))
                    updated += 1
                names.append(item["name"])
            _save_versions(versions)
            result = {"added": added, "updated": updated, "names": names}
    return render_template("version_import.html", error=error, result=result)


# ─── 版本检查与下载 ────────────────────────────────────────────────────────────


@app.route("/versions/<version_id>/check", methods=["POST"])
def version_check(version_id: str):
    _ensure_dirs()
    version = _get_version(version_id)
    try:
        refresh_notes_mirrors(version, timeout=60)
    except Exception:  # noqa: BLE001 - 注记同步失败不影响检查
        pass
    filters = _parse_filters(request.form.get("filters", ""))
    manual_scope = request.form.get("scope") == "selected"
    selected_repos: set[tuple[int, int]] = set()
    for raw in request.form.getlist("repos"):
        parts = raw.split(":", 1)
        if len(parts) != 2:
            continue
        try:
            selected_repos.add((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    if manual_scope and not selected_repos:
        return redirect(
            url_for(
                "version_detail",
                version_id=version_id,
                error="请至少勾选一个补丁仓库",
            )
        )
    groups = [
        {
            "name": group["name"],
            "repos": [
                {"spec": spec, "by_arch": {}} for spec in group["repos"]
            ],
        }
        for group in version["groups"]
    ]
    arches_meta: list[dict] = []
    errors: list[str] = []

    for arch in _arch_list(effective_arch(version)):
        source = resolve_baseline_source(version["base_list"], arch)
        try:
            baseline = load_baseline(source, arch)
        except FetchError as exc:
            arches_meta.append(
                {"arch": arch, "source": source, "base_count": 0, "error": str(exc)}
            )
            errors.append(f"{arch}: {exc}")
            continue
        arches_meta.append(
            {"arch": arch, "source": source, "base_count": len(baseline), "error": ""}
        )
        for group_index, group in enumerate(groups):
            for repo_index, repo in enumerate(group["repos"]):
                if manual_scope and (group_index, repo_index) not in selected_repos:
                    continue
                try:
                    parsed = parse_repo_spec(repo["spec"])
                    packages = fetch_index(parsed, arch)
                    upgrades = find_upgrades(packages, baseline, filters)
                    repo["by_arch"][arch] = {
                        "packages": len(packages),
                        "upgrades": [_upgrade_to_dict(item) for item in upgrades],
                        "error": "",
                    }
                except (FetchError, ValueError) as exc:
                    repo["by_arch"][arch] = {
                        "packages": 0,
                        "upgrades": [],
                        "error": str(exc),
                    }
                    errors.append(f"{arch}/{group['name']}: {exc}")

    arch_order = [item["arch"] for item in arches_meta if not item["error"]]
    for group in groups:
        for repo in group["repos"]:
            repo["rows"] = _repo_rows(repo, arch_order)
            repo["projects"] = _group_projects(repo["rows"])
            repo["upgrade_arches"] = [
                arch
                for arch in arch_order
                if repo["by_arch"].get(arch, {}).get("upgrades")
            ]
            repo["repo_errors"] = [
                f"{arch}: {info['error']}"
                for arch in arch_order
                for info in [repo["by_arch"].get(arch, {})]
                if info.get("error")
            ]
        group["merged_by_arch"] = _merge_group_by_arch(group, arch_order)
        group["merged_rows"] = _repo_rows(
            {"by_arch": group["merged_by_arch"]}, arch_order
        )
        group["merged_projects"] = _group_projects(group["merged_rows"])
        group["package_count"] = len(
            {
                item["name"]
                for arch in arch_order
                for item in group["merged_by_arch"][arch]["upgrades"]
            }
        )
        group["repo_errors"] = [
            f"{_repo_label(repo)}: {message}"
            for repo in group["repos"]
            for message in repo["repo_errors"]
        ]

    payload = {
        "version_id": version["id"],
        "version_name": version["name"],
        "filters": filters,
        "arches": arches_meta,
        "groups": groups,
    }
    check_id = uuid.uuid4().hex[:12]
    directory = VERSION_CACHE / version_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{check_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    _cleanup_checks(version_id)

    total_packages = sum(
        len(project["packages"])
        for group in groups
        for project in group["merged_projects"]
    )
    return render_template(
        "version_results.html",
        version=version,
        payload=payload,
        check_id=check_id,
        total_packages=total_packages,
        total_size=_payload_size(payload),
        errors=errors,
    )


@app.route("/versions/<version_id>/download-all/<check_id>", methods=["POST"])
def version_download_all(version_id: str, check_id: str):
    _ensure_dirs()
    version = _get_version(version_id)
    payload = _load_check(version_id, check_id)
    arch = request.form.get("arch", "all").strip() or "all"
    selected_arches = _selected_arches(payload, arch)

    selections: dict[tuple[int, int], set[str]] = {}

    def select_names(group_index: int, repo_index: int, names: set[str]) -> None:
        if group_index < 0 or repo_index < 0 or not names:
            return
        selections.setdefault((group_index, repo_index), set()).update(names)

    for raw in request.form.getlist("projects"):
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            group_index = int(parts[0])
            repo_index = int(parts[1])
        except ValueError:
            continue
        try:
            repo = payload["groups"][group_index]["repos"][repo_index]
        except IndexError:
            continue
        project = next(
            (item for item in repo.get("projects", []) if item["name"] == parts[2]),
            None,
        )
        if project:
            select_names(
                group_index,
                repo_index,
                {package["name"] for package in project["packages"]},
            )

    for raw in request.form.getlist("packages"):
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            group_index = int(parts[0])
            repo_index = int(parts[1])
        except ValueError:
            continue
        select_names(group_index, repo_index, {parts[2]})

    total_packages = _payload_package_count(payload)

    group_choices: set[tuple[int, str]] = set()
    for raw in request.form.getlist("group_projects"):
        parts = raw.split(":", 1)
        if len(parts) != 2:
            continue
        try:
            group_index = int(parts[0])
        except ValueError:
            continue
        if 0 <= group_index < len(payload["groups"]):
            group_choices.add((group_index, parts[1]))

    if not selections and not group_choices:
        return render_template(
            "version_results.html",
            version=version,
            payload=payload,
            check_id=check_id,
            total_packages=total_packages,
            total_size=_payload_size(payload),
            errors=["未选择任何包"],
        )

    tasks: list[dict] = []
    seen_tasks: set[tuple[int, int, str, str]] = set()

    def add_task(
        group_index: int,
        repo_index: int,
        group: dict,
        repo: dict,
        current_arch: str,
        item: dict,
    ) -> None:
        key = (group_index, repo_index, current_arch, item["name"])
        if key in seen_tasks:
            return
        seen_tasks.add(key)
        group_dir = f"{group_index + 1:02d}-{_slug(group['name'])}"
        repo_dir = f"{repo_index + 1:02d}-{_slug(_repo_label(repo), 'repo')}"
        basename = item["filename"].rsplit("/", 1)[-1]
        tasks.append(
            {
                "name": item["name"],
                "version": item["version"],
                "base_version": item["base_version"],
                "pkg_arch": item["architecture"],
                "filename": item["filename"],
                "size": int(item["size"]),
                "sha256": item["sha256"],
                "repo_url": item["repo_url"],
                "project": item.get("source") or item["name"],
                "group_name": group["name"],
                "repo_label": _repo_label(repo),
                "arch": current_arch,
                "group_index": group_index,
                "repo_index": repo_index,
                "group_dir": group_dir,
                "repo_dir": repo_dir,
                "basename": basename,
                "dest": f"{current_arch}/{group_dir}/{repo_dir}/{basename}",
                "download": "pending",
                "verify": "pending",
                "detail": "",
            }
        )

    for (group_index, repo_index), names in sorted(selections.items()):
        try:
            group = payload["groups"][group_index]
            repo = group["repos"][repo_index]
        except IndexError:
            continue
        for current_arch in selected_arches:
            info = repo.get("by_arch", {}).get(current_arch)
            if not info:
                continue
            for item in info.get("upgrades", []):
                if item["name"] in names:
                    add_task(
                        group_index, repo_index, group, repo, current_arch, item
                    )

    for group_index, project_name in sorted(group_choices):
        group = payload["groups"][group_index]
        best: dict[tuple[str, str], tuple[dict, int]] = {}
        for repo_index, repo in enumerate(group["repos"]):
            for current_arch in selected_arches:
                info = repo.get("by_arch", {}).get(current_arch)
                if not info:
                    continue
                for item in info.get("upgrades", []):
                    if (item.get("source") or item["name"]) != project_name:
                        continue
                    key = (current_arch, item["name"])
                    previous = best.get(key)
                    if previous is None or compare_versions(
                        item["version"], previous[0]["version"]
                    ) > 0:
                        best[key] = (item, repo_index)
        for (current_arch, _name), (item, repo_index) in sorted(best.items()):
            add_task(
                group_index,
                repo_index,
                group,
                group["repos"][repo_index],
                current_arch,
                item,
            )

    if not tasks:
        return render_template(
            "version_results.html",
            version=version,
            payload=payload,
            check_id=check_id,
            total_packages=total_packages,
            total_size=_payload_size(payload),
            errors=[f"所选包在架构 {arch} 下没有可下载项"],
        )

    _cleanup_jobs()
    job_id = uuid.uuid4().hex[:12]
    root = JOB_DIR / job_id / "upgrade-debs"
    root.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "version_id": version_id,
        "version_name": version["name"],
        "version_config": dict(version),
        "check_id": check_id,
        "arch": arch,
        "status": "running",
        "tasks": tasks,
        "root": str(root),
        "created": time.time(),
        "completed": 0,
        "verified": 0,
        "stage": "downloading",
        "zip_ready": False,
        "zip_name": "",
        "auto_zip": False,
        "verify_available": bool(shutil.which("deepin-deb-verify")),
        "error": "",
        "stop_event": threading.Event(),
        "package_lock": threading.Lock(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return redirect(url_for("version_job", version_id=version_id, job_id=job_id))


def _get_job(job_id: str, version_id: str | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise NotFound()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or (version_id and job["version_id"] != version_id):
            raise NotFound()
        return job


def _cleanup_jobs() -> None:
    cutoff = time.time() - 7200
    with JOBS_LOCK:
        stale = [
            job_id
            for job_id, job in JOBS.items()
            if job.get("created", 0) < cutoff
            and job.get("status") not in ("running", "stopping")
        ]
        for job_id in stale:
            JOBS.pop(job_id, None)
    for job_id in stale:
        shutil.rmtree(JOB_DIR / job_id, ignore_errors=True)
        (JOB_DIR / f"{job_id}.zip").unlink(missing_ok=True)


def _write_job_manifests(job: dict, root: Path) -> None:
    groups: dict[tuple[str, int, int], list[dict]] = {}
    for task in job["tasks"]:
        if task["download"] not in ("ok", "cached"):
            continue
        key = (task["arch"], task["group_index"], task["repo_index"])
        groups.setdefault(key, []).append(task)

    for tasks in groups.values():
        tasks.sort(key=lambda item: item["name"])
        first = tasks[0]
        directory = root / first["arch"] / first["group_dir"] / first["repo_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "manifest.tsv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            handle.write(
                "Package\tArchitecture\tBaseVersion\tUpgradeVersion\t"
                "Size\tSHA256\tURL\tFile\n"
            )
            for task in tasks:
                url = f"{task['repo_url'].rstrip('/')}/{task['filename'].lstrip('/')}"
                handle.write(
                    f"{task['name']}\t{task['pkg_arch']}\t{task['base_version']}\t"
                    f"{task['version']}\t{task['size']}\t{task['sha256']}\t"
                    f"{url}\t{task['basename']}\n"
                )
        with (directory / "SHA256SUMS").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            for task in tasks:
                handle.write(f"{task['sha256']}  {task['basename']}\n")


_DOWNLOAD_LABELS = {
    "pending": "未开始",
    "downloading": "下载中",
    "ok": "完成",
    "cached": "已存在",
    "failed": "失败",
}
_VERIFY_LABELS = {
    "pending": "未验签",
    "ok": "通过",
    "failed": "未签名",
    "skipped": "跳过",
}


def _write_job_export(job: dict, root: Path, timestamp: str) -> None:
    """写入可直接导入的版本配置、打包时间与版本对比信息。"""
    tasks = job["tasks"]
    config = job.get("version_config") or {}
    (root / "versions.json").write_text(
        json.dumps(
            {"schema": 1, "versions": [config] if config else []},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    projects = {
        (task["group_index"], task["repo_index"], task["project"])
        for task in tasks
    }
    packages = {
        (
            task["group_index"],
            task["repo_index"],
            task["project"],
            task["name"],
        )
        for task in tasks
    }
    downloaded = sum(1 for task in tasks if task["download"] in ("ok", "cached"))
    unsigned = sum(1 for task in tasks if task["verify"] == "failed")
    skipped = sum(1 for task in tasks if task["verify"] == "skipped")

    lines = [
        f"版本: {job.get('version_name', '')}",
        f"架构: {config.get('arch', job.get('arch', ''))}",
        f"打包时间: {timestamp}",
        f"基线清单: {config.get('base_list', '')}",
        "仓库组:",
    ]
    for group in config.get("groups", []):
        lines.append(f"  {group.get('name', '')}:")
        for repo in group.get("repos", []):
            lines.append(f"    - {repo}")
    lines.extend(
        [
            f"项目数: {len(projects)}",
            f"包数: {len(packages)}",
            f"文件数: {len(tasks)}",
            f"下载成功: {downloaded}",
            f"未签名: {unsigned}",
            f"验签跳过: {skipped}",
        ]
    )
    (root / "info.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = sorted(
        tasks,
        key=lambda task: (task["project"], task["name"], task["arch"]),
    )
    with (root / "comparison.tsv").open("w", encoding="utf-8", newline="") as handle:
        handle.write(
            "项目\t包名\t架构\t基础版本\t升级版本\t大小\tSHA256\t文件名"
            "\t下载状态\t验签状态\n"
        )
        for task in rows:
            handle.write(
                f"{task['project']}\t{task['name']}\t{task['arch']}\t"
                f"{task['base_version']}\t{task['version']}\t{task['size']}\t"
                f"{task['sha256']}\t{task['basename']}\t"
                f"{_DOWNLOAD_LABELS.get(task['download'], task['download'])}\t"
                f"{_VERIFY_LABELS.get(task['verify'], task['verify'])}\n"
            )


def _package_job(job: dict) -> None:
    with job["package_lock"]:
        if job.get("zip_ready"):
            return
        root = Path(job["root"])
        _write_job_manifests(job, root)
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        _write_job_export(job, root, now.strftime("%Y-%m-%d %H:%M:%S"))
        zip_path = JOB_DIR / f"{job['id']}.zip"
        _zip_tree(root, zip_path)
        safe_name = re.sub(r'[\\/:*?"<>|]+', "-", job["version_name"]).strip()
        with JOBS_LOCK:
            job["zip_name"] = f"{safe_name or 'upgrade-debs'}-{timestamp}.zip"
            job["zip_ready"] = True


def _run_job(job: dict) -> None:
    root = Path(job["root"])
    binary = shutil.which("deepin-deb-verify")
    stop_event = job["stop_event"]
    tasks = job["tasks"]

    def run_phase(worker) -> None:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, task) for task in tasks]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    with JOBS_LOCK:
                        job["error"] = str(exc)

    def download(task: dict) -> None:
        if stop_event.is_set():
            return
        with JOBS_LOCK:
            task["download"] = "downloading"
        package = Package(
            name=task["name"],
            version=task["version"],
            architecture=task["pkg_arch"],
            filename=task["filename"],
            size=task["size"],
            sha256=task["sha256"],
            repo_url=task["repo_url"],
            source=task["project"],
        )
        upgrade = Upgrade(package=package, base_version=task["base_version"])
        try:
            result = _download_one(upgrade, root / task["dest"], retries=1)
            with JOBS_LOCK:
                task["download"] = "cached" if result == "cached" else "ok"
        except Exception as exc:  # noqa: BLE001 - 单文件失败不中断任务
            with JOBS_LOCK:
                task["download"] = "failed"
                task["detail"] = str(exc)
        finally:
            with JOBS_LOCK:
                job["completed"] = job.get("completed", 0) + 1

    def verify(task: dict) -> None:
        if stop_event.is_set():
            return
        if task["download"] not in ("ok", "cached"):
            with JOBS_LOCK:
                task["verify"] = "skipped"
                task["detail"] = task["detail"] or "下载失败，未验签"
        elif binary:
            try:
                proc = subprocess.run(
                    [binary, "-f", str(root / task["dest"])],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                output = (proc.stdout or "") + (proc.stderr or "")
                lines = [line.strip() for line in output.splitlines() if line.strip()]
                detail = lines[-1] if lines else ""
                with JOBS_LOCK:
                    if proc.returncode == 0:
                        task["verify"] = "ok"
                        task["detail"] = detail or "验签通过"
                    else:
                        task["verify"] = "failed"
                        task["detail"] = detail or f"退出码 {proc.returncode}"
            except Exception as exc:  # noqa: BLE001
                with JOBS_LOCK:
                    task["verify"] = "failed"
                    task["detail"] = str(exc)
        else:
            with JOBS_LOCK:
                task["verify"] = "skipped"
                task["detail"] = "未检测到 deepin-deb-verify"
        with JOBS_LOCK:
            job["verified"] = job.get("verified", 0) + 1

    with JOBS_LOCK:
        job["stage"] = "downloading"
    run_phase(download)
    if stop_event.is_set():
        with JOBS_LOCK:
            job["status"] = "stopped"
            job["stage"] = "stopped"
        return

    with JOBS_LOCK:
        job["stage"] = "verifying"
    run_phase(verify)
    if stop_event.is_set():
        with JOBS_LOCK:
            job["status"] = "stopped"
            job["stage"] = "stopped"
        return

    with JOBS_LOCK:
        success = sum(1 for task in tasks if task["download"] in ("ok", "cached"))
        unsigned = sum(1 for task in tasks if task["verify"] == "failed")
        skipped = sum(1 for task in tasks if task["verify"] == "skipped")
        download_failed = sum(1 for task in tasks if task["download"] == "failed")

    if success and unsigned == 0:
        with JOBS_LOCK:
            job["stage"] = "packaging"
        try:
            _package_job(job)
        except Exception as exc:  # noqa: BLE001
            with JOBS_LOCK:
                job["error"] = f"打包失败: {exc}"
        else:
            with JOBS_LOCK:
                job["auto_zip"] = skipped == 0 and download_failed == 0
                job["stage"] = "ready"
    elif unsigned:
        with JOBS_LOCK:
            job["stage"] = "gated"
    else:
        with JOBS_LOCK:
            job["error"] = job.get("error") or "没有成功下载任何文件"
            job["stage"] = "error"

    with JOBS_LOCK:
        job["status"] = "stopped" if stop_event.is_set() else "done"
        if stop_event.is_set():
            job["stage"] = "stopped"


def _job_snapshot(job: dict) -> dict:
    with JOBS_LOCK:
        tasks = [dict(task) for task in job["tasks"]]
        status = job["status"]
        stage = job.get("stage", "")
        zip_ready = job.get("zip_ready", False)
        zip_name = job.get("zip_name", "")
        auto_zip = job.get("auto_zip", False)
        error = job.get("error", "")

    projects: dict[tuple[int, int, str], dict] = {}
    for task in tasks:
        key = (task["group_index"], task["repo_index"], task["project"])
        project = projects.setdefault(
            key,
            {
                "name": task["project"],
                "repo": f"{task['group_name']} / {task['repo_label']}",
                "arches": [],
                "packages": set(),
                "total": 0,
                "downloaded": 0,
                "download_failed": 0,
                "verified": 0,
                "unsigned": 0,
                "skipped": 0,
            },
        )
        project["packages"].add(task["name"])
        project["total"] += 1
        if task["arch"] not in project["arches"]:
            project["arches"].append(task["arch"])
        if task["download"] in ("ok", "cached"):
            project["downloaded"] += 1
        elif task["download"] == "failed":
            project["download_failed"] += 1
        if task["verify"] == "ok":
            project["verified"] += 1
        elif task["verify"] == "failed":
            project["unsigned"] += 1
            project["verified"] += 1
        elif task["verify"] == "skipped":
            project["skipped"] += 1

    rows = []
    for project in projects.values():
        row = dict(project)
        row["arches"] = sorted(row["arches"])
        row["package_count"] = len(row["packages"])
        row.pop("packages", None)
        rows.append(row)
    rows.sort(key=lambda item: (0 if item["unsigned"] else 1, item["name"]))

    return {
        "status": status,
        "stage": stage,
        "running": status in ("running", "stopping"),
        "total": len(tasks),
        "completed": sum(
            1 for task in tasks if task["download"] not in ("pending", "downloading")
        ),
        "verified": sum(
            1 for task in tasks if task["verify"] not in ("pending",)
        ),
        "success": sum(1 for task in tasks if task["download"] in ("ok", "cached")),
        "download_failed": sum(1 for task in tasks if task["download"] == "failed"),
        "unsigned": sum(1 for task in tasks if task["verify"] == "failed"),
        "verify_skipped": sum(1 for task in tasks if task["verify"] == "skipped"),
        "zip_ready": zip_ready,
        "zip_name": zip_name,
        "auto_zip": auto_zip,
        "zip_url": url_for("job_zip", job_id=job["id"]) if zip_ready else "",
        "error": error,
        "projects": rows,
    }


@app.route("/versions/<version_id>/jobs/<job_id>", methods=["GET"])
def version_job(version_id: str, job_id: str):
    version = _get_version(version_id)
    job = _get_job(job_id, version_id)
    return render_template("version_job.html", version=version, job=job)


@app.route("/jobs/<job_id>/status", methods=["GET"])
def job_status(job_id: str):
    return jsonify(_job_snapshot(_get_job(job_id)))


@app.route("/jobs/<job_id>/stop", methods=["POST"])
def job_stop(job_id: str):
    job = _get_job(job_id)
    with JOBS_LOCK:
        if job["status"] == "running":
            job["status"] = "stopping"
            job["stop_event"].set()
        status = job["status"]
    return jsonify({"status": status})


@app.route("/jobs/<job_id>/package", methods=["POST"])
def job_package(job_id: str):
    job = _get_job(job_id)
    with JOBS_LOCK:
        running = job["status"] in ("running", "stopping")
        unsigned = any(task["verify"] == "failed" for task in job["tasks"])
    if running:
        return jsonify({"error": "任务尚未结束"}), 409
    if unsigned and request.form.get("ack") != "1":
        return jsonify({"error": "存在未签名包，需要确认"}), 400
    try:
        _package_job(job)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    return jsonify(
        {"zip_url": url_for("job_zip", job_id=job_id), "zip_name": job.get("zip_name", "")}
    )


@app.route("/jobs/<job_id>/zip", methods=["GET"])
def job_zip(job_id: str):
    job = _get_job(job_id)
    with JOBS_LOCK:
        ready = job.get("zip_ready")
    zip_path = JOB_DIR / f"{job_id}.zip"
    if not ready or not zip_path.is_file():
        abort(404)
    with JOBS_LOCK:
        zip_name = job.get("zip_name") or f"upgrade-debs-{job_id}.zip"
    return send_file(
        str(zip_path),
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name,
    )


def _client_is_local() -> bool:
    """判断当前请求是否来自本机（回环地址或本机网卡地址）。"""
    remote = request.remote_addr or ""
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    try:
        host_addresses = {
            ipaddress.ip_address(item)
            for item in socket.gethostbyname_ex(socket.gethostname())[2]
        }
    except Exception:  # noqa: BLE001 - 取不到本机地址时按远程处理
        host_addresses = set()
    return address in host_addresses


def _build_deploy_zip() -> io.BytesIO:
    buffer = io.BytesIO()
    files = [
        "oem_deb_fetch.py",
        "web_fetch.py",
        "notes.py",
        "run-fetch-web.sh",
        "oem-launcher.sh",
        "install-desktop.sh",
        "self-update.sh",
        "requirements.txt",
        "VERSION",
        "README.md",
        "install.sh",
    ]
    legacy_templates = {"index.html", "preview.html", "result.html"}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            path = WORKDIR / relative
            if path.is_file():
                archive.write(path, f"oem-deb-resolver/{relative}")
        for dirname in ("static", "templates"):
            base = WORKDIR / dirname
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                if dirname == "templates" and path.name in legacy_templates:
                    continue
                archive.write(
                    path, f"oem-deb-resolver/{path.relative_to(WORKDIR)}"
                )
        if VERSIONS_FILE.is_file():
            archive.write(VERSIONS_FILE, "oem-deb-resolver/config/versions.json")
    buffer.seek(0)
    return buffer


def _fetch_latest_version() -> tuple[str | None, str]:
    env_url = os.environ.get("OEM_UPDATE_URL", "").strip()
    if env_url:
        urls = [env_url]
    else:
        urls = list(DEFAULT_UPDATE_URLS)

    errors: list[str] = []
    best_version: str | None = None
    best_url = ""
    for url in urls:
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "oem-deb-fetcher/update-check",
                    "Accept": "application/vnd.github.raw",
                },
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                text = response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - 汇总后返回给页面
            errors.append(f"{url}: {exc}")
            continue
        latest = text.strip().splitlines()[0].strip() if text.strip() else ""
        latest = latest.lstrip("vV")
        if not latest or latest.startswith("{"):
            continue
        if best_version is None or compare_versions(latest, best_version) > 0:
            best_version = latest
            best_url = url
    if best_version:
        return best_version, best_url
    return None, "; ".join(errors[-2:])


@app.route("/about", methods=["GET"])
def about():
    try:
        flask_version = importlib.metadata.version("flask")
    except importlib.metadata.PackageNotFoundError:
        flask_version = "未知"
    return render_template(
        "about.html",
        app_version=APP_VERSION,
        python_version=sys.version.split()[0],
        flask_version=flask_version,
        verifier=shutil.which("deepin-deb-verify") or "",
        is_local=_client_is_local(),
        github_url=GITHUB_URL,
    )


@app.route("/health", methods=["GET"])
def health():
    response = jsonify(
        {"status": "ok", "app": "oem-deb-resolver", "version": APP_VERSION}
    )
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/deploy", methods=["GET"])
def deploy():
    return render_template(
        "deploy.html",
        app_version=APP_VERSION,
        github_url=GITHUB_URL,
        public_url=request.host_url.rstrip("/"),
        has_config=VERSIONS_FILE.is_file(),
    )


@app.route("/deploy/package.zip", methods=["GET"])
def deploy_package():
    return send_file(
        _build_deploy_zip(),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"oem-deb-resolver-{APP_VERSION}.zip",
    )


@app.route("/about/check-update", methods=["POST"])
def about_check_update():
    latest, source = _fetch_latest_version()
    if not latest:
        return jsonify(
            {
                "status": "error",
                "message": "检查更新失败：无法获取远程版本",
                "detail": source,
                "current": APP_VERSION,
                "url": "",
            }
        )
    comparison = compare_versions(latest, APP_VERSION)
    if comparison > 0:
        status = "update"
        message = f"发现新版本 v{latest}（当前 v{APP_VERSION}）"
    elif comparison == 0:
        status = "latest"
        message = f"已是最新版本 v{APP_VERSION}"
    else:
        status = "latest"
        message = f"当前版本 v{APP_VERSION}，远程版本 v{latest}"
    return jsonify(
        {
            "status": status,
            "message": message,
            "current": APP_VERSION,
            "latest": latest,
            "source": source,
            "url": GITHUB_URL,
        }
    )


def _update_script() -> Path:
    return WORKDIR / "self-update.sh"


def _update_log() -> Path:
    return WORKDIR / "cache" / "self-update.log"


def _update_pidfile() -> Path:
    return WORKDIR / "cache" / "self-update.pid"


def _update_running() -> int | None:
    try:
        pid = int(_update_pidfile().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


@app.route("/about/update", methods=["POST"])
def about_update():
    if not _client_is_local():
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "只允许在本机触发更新（请在服务器上打开本页面或通过 SSH 隧道访问）",
                }
            ),
            403,
        )
    script = _update_script()
    if not script.is_file():
        return jsonify(
            {
                "status": "error",
                "message": f"缺少更新脚本 {script}，请重新执行 install.sh 部署",
            }
        )
    running = _update_running()
    if running:
        return jsonify(
            {"status": "running", "message": f"更新已在进行中（pid {running}）"}
        )
    log_path = _update_log()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = log_path.open("ab")
        process = subprocess.Popen(
            ["bash", str(script)],
            cwd=str(WORKDIR),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return jsonify({"status": "error", "message": f"无法启动更新：{exc}"})
    finally:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - 句柄可能未创建
            pass
    _update_pidfile().write_text(str(process.pid), encoding="utf-8")
    return jsonify(
        {
            "status": "started",
            "message": f"已开始更新（当前 v{APP_VERSION}），服务将在完成后自动重启",
            "pid": process.pid,
        }
    )


@app.route("/about/update/status", methods=["GET"])
def about_update_status():
    lines: list[str] = []
    try:
        text = _update_log().read_text(encoding="utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()][-60:]
    except OSError:
        pass
    running = _update_running()
    state = "idle"
    if running:
        state = "running"
    else:
        joined = "\n".join(lines)
        if "更新完成" in joined:
            state = "done"
        elif "更新失败" in joined or "错误：" in joined:
            state = "failed"
    return jsonify(
        {
            "running": bool(running),
            "state": state,
            "pid": running,
            "version": APP_VERSION,
            "lines": lines,
        }
    )


def main() -> None:
    import argparse
    import webbrowser

    parser = argparse.ArgumentParser(description="启动 oem deb 升级包下载服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=51234)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    _ensure_dirs()
    browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{browser_host}:{args.port}/"
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            pass
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
