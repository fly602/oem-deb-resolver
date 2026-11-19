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
import subprocess
import threading
import time
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
    compare_versions,
    fetch_index,
    find_upgrades,
    load_baseline,
    normalize_arch,
    parse_repo_spec,
    resolve_baseline_source,
)

WORKDIR = Path(__file__).resolve().parent
CACHE_DIR = WORKDIR / "cache" / "fetch"
VERSION_CACHE = CACHE_DIR / "versions"
JOB_DIR = CACHE_DIR / "jobs"
DATA_DIR = WORKDIR / "config"
VERSIONS_FILE = DATA_DIR / "versions.json"

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024


def _ensure_dirs() -> None:
    for path in (VERSION_CACHE, JOB_DIR, DATA_DIR):
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


def _version_form_data(version: dict | None) -> dict:
    if version is None:
        return {
            "name": "",
            "base_list": "",
            "arch": "amd64",
            "groups": [{"name": "", "repos": ""}],
        }
    return {
        "name": version["name"],
        "base_list": version["base_list"],
        "arch": version.get("arch", "amd64"),
        "groups": [
            {"name": group["name"], "repos": "\n".join(group["repos"])}
            for group in version.get("groups", [])
        ]
        or [{"name": "", "repos": ""}],
    }


def _submitted_form_data(form) -> dict:
    groups = [
        {"name": group_name, "repos": repo_text}
        for group_name, repo_text in zip(
            form.getlist("group_name"), form.getlist("group_repos")
        )
    ]
    return {
        "name": form.get("name", ""),
        "base_list": form.get("base_list", ""),
        "arch": form.get("architecture", "amd64"),
        "groups": groups or [{"name": "", "repos": ""}],
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
    groups: list[dict] = []
    for index, (group_name, repo_text) in enumerate(
        zip(group_names, group_repos), 1
    ):
        group_name = group_name.strip()
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
        groups.append({"name": group_name, "repos": repos})

    if not groups:
        raise ValueError("请至少添加一个补丁仓库组")
    detected = detect_arch_from_baseline(base_list)
    return {
        "name": name,
        "base_list": base_list,
        "arch": detected or arch,
        "groups": groups,
    }


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
        "base_list": data["base_list"],
        "arch": data["arch"],
        "groups": data["groups"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    versions.append(version)
    _save_versions(versions)
    return redirect(url_for("version_detail", version_id=version["id"]))


@app.route("/versions/<version_id>", methods=["GET"])
def version_detail(version_id: str):
    version = _get_version(version_id)
    return render_template(
        "version_detail.html",
        version=version,
        display_arch=effective_arch(version),
        detected_arch=detect_arch_from_baseline(version.get("base_list", "")),
        repo_count=sum(len(group.get("repos", [])) for group in version["groups"]),
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
    for arch in _arch_list(effective_arch(version)):
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
    for group in version["groups"]:
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


# ─── 版本检查与下载 ────────────────────────────────────────────────────────────


@app.route("/versions/<version_id>/check", methods=["POST"])
def version_check(version_id: str):
    _ensure_dirs()
    version = _get_version(version_id)
    filters = _parse_filters(request.form.get("filters", ""))
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
        for group in groups:
            for repo in group["repos"]:
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


def _package_job(job: dict) -> None:
    with job["package_lock"]:
        if job.get("zip_ready"):
            return
        root = Path(job["root"])
        _write_job_manifests(job, root)
        zip_path = JOB_DIR / f"{job['id']}.zip"
        _zip_tree(root, zip_path)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
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


def main() -> None:
    import argparse
    import webbrowser

    parser = argparse.ArgumentParser(description="启动 oem deb 升级包下载服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=51234)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    _ensure_dirs()
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            pass
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
