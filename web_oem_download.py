#!/usr/bin/env python3
"""
Web interface for the patch-centric APT dependency resolver.

User workflow:
  1. Configure repositories, patch package list, base package list, target arch.
  2. Preview: runs resolution, shows what would be downloaded + problems.
  3. Run: reuses preview result, downloads deb packages.
  4. Full Upgrade: download ALL patch-repo packages newer than base.
"""

import json
import sys
import subprocess
import tempfile
import uuid
from pathlib import Path

# ─── Bootstrap: ensure Flask + packaging ────────────────────────────────────────

def _pip_install(pkgs):
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "-q", *pkgs],
        check=False,
    )

for pkg, import_name in [("Flask", "flask"), ("packaging", "packaging")]:
    try:
        __import__(import_name)
    except ImportError:
        _pip_install([pkg])

del _pip_install

# ─── Import resolver ─────────────────────────────────────────────────────────────
from flask import Flask, render_template, request, send_file, abort

from patch_resolver import (
    PatchResolver, MultiRepoIndex, load_base_packages,
    detect_architecture_from_base, _fetch_and_parse_index,
    _merge_indexes, RepositoryFetchError, _fetch_text,
    PatchPackage,
)

# ─── App setup ─────────────────────────────────────────────────────────────────

WORKDIR = Path(__file__).resolve().parent
CONFIG_DIR = WORKDIR / "config"
CACHE_DIR = WORKDIR / "cache"
PATCH_LIST = CONFIG_DIR / "patch-packages.txt"
DEFAULT_OUTPUT_DIR = CACHE_DIR / "download"
SOURCES_FILE = CONFIG_DIR / "oem-patch-sources.list"
BASE_LISTS_FILE = CONFIG_DIR / "base_lists.json"
BASE_LIST_FILE = CONFIG_DIR / "base_list.txt"
PREVIEW_CACHE = CACHE_DIR / "preview_cache"

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ─── Helpers ───────────────────────────────────────────────────────────────────

def read_patch_packages():
    if not PATCH_LIST.exists():
        return ""
    return PATCH_LIST.read_text(encoding="utf-8")


def write_patch_packages(content: str):
    PATCH_LIST.write_text(
        content.strip() + "\n" if content.strip() else "", encoding="utf-8"
    )


def save_sources_list(content: str):
    """Persist patch repo sources.list to local file."""
    SOURCES_FILE.write_text(content.strip() + "\n", encoding="utf-8")


def load_base_lists() -> list[str]:
    """Load saved base package list paths for datalist."""
    if not BASE_LISTS_FILE.exists():
        return []
    try:
        return json.loads(BASE_LISTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_base_lists(paths: list[str]):
    """Save base package list paths to JSON file."""
    BASE_LISTS_FILE.write_text(json.dumps(paths, ensure_ascii=False, indent=2), encoding="utf-8")


def load_base_list() -> str:
    """Load saved base_list path."""
    if not BASE_LIST_FILE.exists():
        return str(CONFIG_DIR / "packages-full-x86_64.txt")
    return BASE_LIST_FILE.read_text(encoding="utf-8").strip()


def save_base_list(path: str):
    """Save base_list path to file."""
    BASE_LIST_FILE.write_text(path.strip() + "\n", encoding="utf-8")


def ensure_base_list(path: str):
    """Add path to saved base lists datalist, move to top if already present."""
    paths = load_base_lists()
    if path in paths:
        paths.remove(path)
    paths.insert(0, path)
    save_base_lists(paths)


def reset_config():
    """Clear all user config files (base list, sources, patch packages). Cache is NOT cleared."""
    for f in [BASE_LIST_FILE, SOURCES_FILE, PATCH_LIST]:
        if f.exists():
            f.unlink()
    if BASE_LISTS_FILE.exists():
        BASE_LISTS_FILE.unlink()


# ─── Preview result serialization ─────────────────────────────────────────────

def _pkg_to_dict(p) -> dict:
    """Serialize a PatchPackage to a plain dict."""
    return {
        "name": p.name,
        "version": p.version,
        "architecture": p.architecture,
        "filename": p.filename,
        "size": p.size,
        "sha256": p.sha256,
        "source_repo": p.source_repo,
        "base_version": p.base_version,
    }


def _dict_to_pkg(d: dict) -> PatchPackage:
    """Deserialize a dict back to a PatchPackage."""
    return PatchPackage(
        name=d["name"],
        version=d["version"],
        architecture=d.get("architecture", ""),
        filename=d["filename"],
        size=d.get("size", 0),
        sha256=d.get("sha256"),
        source_repo=d["source_repo"],
        base_version=d.get("base_version"),
    )


def save_preview_result(result, form_data: dict) -> str:
    """Save preview resolution result to disk. Returns result_id."""
    PREVIEW_CACHE.mkdir(parents=True, exist_ok=True)
    result_id = str(uuid.uuid4())[:8]
    data = {
        "patch_packages": [_pkg_to_dict(p) for p in result.patch_packages],
        "related_packages": [_pkg_to_dict(p) for p in result.related_packages],
        "dep_packages": [_pkg_to_dict(p) for p in result.dep_packages],
        "not_found": result.not_found,
        "unresolved_deps": result.unresolved_deps,
        "ignored_virtuals": result.ignored_virtuals,
        "log": result.log,
        "config_summary": result.config_summary,
        "form_data": form_data,
    }
    path = PREVIEW_CACHE / f"{result_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_id


def load_preview_result(result_id: str):
    """Load saved preview result. Returns dict or None."""
    path = PREVIEW_CACHE / f"{result_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ─── Form / repo helpers ───────────────────────────────────────────────────────

def _parse_sources_list(text: str) -> list[tuple[str, str, list[str]]]:
    """Parse APT sources.list format: 'deb [trusted=yes] URL dist comp...'"""
    import re
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.match(r"^deb\s", line):
            continue
        parts = re.split(r'\s+', line)
        if len(parts) < 3:
            continue
        idx = 1
        while idx < len(parts) and parts[idx].startswith("["):
            idx += 1
        if idx >= len(parts) - 1:
            continue
        url = parts[idx]
        dist = parts[idx + 1]
        components = [c for c in parts[idx + 2:] if c]
        results.append((url, dist, components))
    return results


def _parse_repos(form, arch: str) -> MultiRepoIndex:
    """Build MultiRepoIndex from sources.list textarea."""
    repo_lines = form.get("patch_repo_lines", "").strip()
    save_sources_list(repo_lines)
    mri = MultiRepoIndex()
    for priority, (url, dist, components) in enumerate(_parse_sources_list(repo_lines)):
        mri.add_repo(repo_url=url, distribution=dist,
                      components=components, architecture=arch, priority=priority)
    return mri


def _fetch_indexes(mri, arch: str) -> list[str]:
    """Fetch Packages index from every repo. Returns error messages."""
    errors = []
    for cfg in mri.repos:
        try:
            idx = _fetch_and_parse_index(
                base_url=cfg["url"], distribution=cfg["distribution"],
                components=cfg["components"], architecture=arch,
            )
            for _, pkgs in idx.by_directory.items():
                for p in pkgs:
                    if p.name not in idx.by_name:
                        idx.by_name[p.name] = p
            _merge_indexes(mri, idx)
        except RepositoryFetchError as exc:
            errors.append(f"{cfg['url']}: {exc}")
    return errors


def _parse_patch_names(text: str) -> list[str]:
    return [
        line.strip().split()[0]
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _make_temp_dir() -> Path:
    """Create a unique temp directory under cache/deb-temp/"""
    DEB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    sub = DEB_TEMP_DIR / str(uuid.uuid4())[:8]
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def _form_to_dict(form) -> dict:
    """Convert Flask form to a plain dict (for serialization)."""
    return {k: v for k, v in form.items()}


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    base_path = load_base_list()
    arch = detect_architecture_from_base(base_path) or "amd64"
    base_count = 0
    try:
        base_text = _fetch_text(base_path)
        base_count = sum(
            1 for line in base_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    except Exception:
        pass

    try:
        patch_repo_lines = SOURCES_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        patch_repo_lines = ""

    return render_template(
        "index.html",
        base_list=base_path,
        base_count=base_count,
        patch_packages=read_patch_packages(),
        patch_repo_lines=patch_repo_lines,
        output_dir=str(DEFAULT_OUTPUT_DIR),
        default_arch=arch,
        base_lists=load_base_lists(),
    )


@app.route("/preview", methods=["POST"])
def preview():
    patch_text = request.form.get("patch_packages", "").strip()
    write_patch_packages(patch_text)

    if not patch_text:
        return render_template("result.html", success=False, output="请先填写补丁包列表。")

    arch = request.form.get("architecture", "amd64").strip()
    base_path = request.form.get("base_list", str(CONFIG_DIR / "packages-full-x86_64.txt")).strip()
    save_base_list(base_path)
    ensure_base_list(base_path)

    mri = _parse_repos(request.form, arch)
    if not mri.repos:
        return render_template("result.html", success=False, output="请先配置至少一个补丁仓库。")

    fetch_errors = _fetch_indexes(mri, arch)
    if fetch_errors:
        return render_template("result.html", success=False,
                              output="仓库索引获取失败:\n" + "\n".join(fetch_errors))

    base_packages = load_base_packages(base_path)
    base_count = len(base_packages)
    requested = _parse_patch_names(patch_text)
    raw_repo_lines = request.form.get("patch_repo_lines", "").strip()

    config_summary = (
        f"Base Package List: {base_path}  (共 {base_count} 个包)\n"
        f"目标架构: {arch}\n"
        f"补丁仓库:\n{raw_repo_lines}"
    )

    resolver = PatchResolver(
        multi_index=mri, base_packages=base_packages,
        architecture=arch, max_workers=8, max_depth=10,
    )

    try:
        tmp_dir = _make_temp_dir()
        result = resolver.resolve(
            requested=requested,
            output_dir=tmp_dir,
            include_recommends=bool(request.form.get("include_recommends")),
            dry_run=False,
            retry=0,
            config_summary=config_summary,
        )
        # 预览用完即删，仅保留元数据用于显示
        import shutil
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
    except Exception as exc:
        import traceback
        return render_template("result.html", success=False,
                              output=f"解析出错:\n{exc}\n\n{traceback.format_exc()}")

    # 保存解析结果供下载使用
    result_id = save_preview_result(result, _form_to_dict(request.form))

    has_errors = bool(result.not_found) or bool(result.unresolved_deps)

    return render_template(
        "preview.html",
        patch_packages=result.patch_packages,
        related_packages=result.related_packages,
        dep_packages=result.dep_packages,
        not_found=result.not_found,
        unresolved_deps=result.unresolved_deps,
        ignored_virtuals=result.ignored_virtuals,
        log=result.log,
        form=request.form,
        arch=arch,
        repo_count=len(mri.repos),
        requested=requested,
        config_summary=config_summary,
        base_path=base_path,
        result_id=result_id,
        has_errors=has_errors,
    )


@app.route("/run", methods=["POST"])
def run():
    # 从预览保存的结果中读取，不再重新解析
    result_id = request.form.get("result_id", "").strip()
    ignore_errors = bool(request.form.get("ignore_errors"))

    if not result_id:
        return render_template("result.html", success=False,
                              output="缺少预览结果，请先执行预览。")

    cached = load_preview_result(result_id)
    if not cached:
        return render_template("result.html", success=False,
                              output="预览结果已过期，请重新执行预览。")

    # 从 JSON 恢复包列表
    patch_pkgs = [_dict_to_pkg(d) for d in cached["patch_packages"]]
    related_pkgs = [_dict_to_pkg(d) for d in cached["related_packages"]]
    dep_pkgs = [_dict_to_pkg(d) for d in cached["dep_packages"]]
    not_found = cached["not_found"]
    unresolved_deps = cached.get("unresolved_deps", [])
    ignored_virtuals = cached.get("ignored_virtuals", [])

    # 有错误且未勾选忽略 → 阻止下载
    if (not_found or unresolved_deps) and not ignore_errors:
        lines = ["存在以下问题，请先处理或勾选「忽略错误」再下载：\n"]
        if not_found:
            lines.append(f"未找到的包 ({len(not_found)}):")
            for name, base_ver in not_found:
                lines.append(f"  ✗ {name}" + (f"  (base: {base_ver})" if base_ver else ""))
        if unresolved_deps:
            lines.append(f"\n无法解决的依赖 ({len(unresolved_deps)}):")
            for name, raw_dep in unresolved_deps:
                lines.append(f"  ⚠ {name}: {raw_dep}")
        return render_template("result.html", success=False, output="\n".join(lines))

    # 下载到临时目录
    all_pkgs = patch_pkgs + related_pkgs + dep_pkgs
    output_dir = _make_temp_dir()
    tmp_id = output_dir.name

    # 重建 resolver 仅用于 download_packages
    form_data = cached.get("form_data", {})
    arch = form_data.get("architecture", "amd64")
    base_path = form_data.get("base_list", "")
    mri = _parse_repos(form_data, arch)

    resolver = PatchResolver(
        multi_index=mri, base_packages={},
        architecture=arch, max_workers=8, max_depth=10,
    )

    results = resolver.download_packages(all_pkgs, output_dir, retry=1)
    success_files = [v[1] for v in results.values() if v[0] == "success"]
    failed_items = [(k, v[1]) for k, v in results.items() if v[0] == "failed"]

    if not success_files:
        return f"下载失败，没有成功获取任何包文件。", 400

    # 打包为 zip
    import zipfile, shutil
    zip_path = CACHE_DIR / f"packages_{tmp_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in success_files:
            zf.write(fpath, Path(fpath).name)
    zip_path.chmod(0o644)

    # 清理临时下载目录
    if output_dir.exists():
        shutil.rmtree(output_dir)

    return send_file(
        str(zip_path),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"packages_{tmp_id}.zip",
    )


@app.route("/full-upgrade-preview", methods=["POST"])
def full_upgrade_preview():
    arch = request.form.get("architecture", "amd64").strip()
    base_path = request.form.get("base_list", str(CONFIG_DIR / "packages-full-x86_64.txt")).strip()
    save_base_list(base_path)
    ensure_base_list(base_path)

    mri = _parse_repos(request.form, arch)
    if not mri.repos:
        return render_template("result.html", success=False, output="请先配置至少一个补丁仓库。")

    fetch_errors = _fetch_indexes(mri, arch)
    if fetch_errors:
        return render_template("result.html", success=False,
                            output="仓库索引获取失败:\n" + "\n".join(fetch_errors))

    base_packages = load_base_packages(base_path)
    base_count = len(base_packages)
    raw_repo_lines = request.form.get("patch_repo_lines", "").strip()
    config_summary = (
        f"Base Package List: {base_path}  (共 {base_count} 个包)\n"
        f"目标架构: {arch}\n"
        f"补丁仓库:\n{raw_repo_lines}"
    )

    resolver = PatchResolver(
        multi_index=mri, base_packages=base_packages,
        architecture=arch, max_workers=8, max_depth=10,
    )

    try:
        upgrade_pkgs = resolver.full_upgrade()
    except Exception as exc:
        import traceback
        return render_template("result.html", success=False,
                            output=f"解析出错:\n{exc}\n\n{traceback.format_exc()}")

    if not upgrade_pkgs:
        return render_template("result.html", success=True,
                            output="全量升级：没有找到需要升级的包。")

    # 将升级包名作为 requested，调用 resolve() 做完整依赖解析
    requested_names = [p.name for p in upgrade_pkgs]
    tmp_dir = _make_temp_dir()
    try:
        result = resolver.resolve(
            requested=requested_names,
            output_dir=tmp_dir,
            include_recommends=False,
            dry_run=False,
            retry=0,
            config_summary=config_summary,
        )
        import shutil
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
    except Exception as exc:
        import traceback
        return render_template("result.html", success=False,
                            output=f"解析出错:\n{exc}\n\n{traceback.format_exc()}")

    result_id = save_preview_result(result, _form_to_dict(request.form))
    has_errors = bool(result.not_found) or bool(result.unresolved_deps)

    return render_template(
        "preview.html",
        patch_packages=result.patch_packages,
        related_packages=result.related_packages,
        dep_packages=result.dep_packages,
        not_found=result.not_found,
        unresolved_deps=result.unresolved_deps,
        ignored_virtuals=result.ignored_virtuals,
        log=result.log,
        form=request.form,
        arch=arch,
        repo_count=len(mri.repos),
        requested=["(全量升级)"],
        config_summary=config_summary,
        base_path=base_path,
        result_id=result_id,
        has_errors=has_errors,
    )


@app.route("/full-upgrade-run", methods=["POST"])
def full_upgrade_run():
    # 与 /run 类似，读取保存的结果
    result_id = request.form.get("result_id", "").strip()
    if not result_id:
        return render_template("result.html", success=False, output="缺少预览结果。")

    cached = load_preview_result(result_id)
    if not cached:
        return render_template("result.html", success=False, output="预览结果已过期。")

    upgrade_pkgs = [_dict_to_pkg(d) for d in cached["patch_packages"]]
    output_dir = _make_temp_dir()
    tmp_id = output_dir.name

    form_data = cached.get("form_data", {})
    arch = form_data.get("architecture", "amd64")
    mri = _parse_repos(form_data, arch)
    resolver = PatchResolver(multi_index=mri, base_packages={}, architecture=arch, max_workers=8, max_depth=10)

    results = resolver.download_packages(upgrade_pkgs, output_dir, retry=1)
    success_files = [v[1] for v in results.values() if v[0] == "success"]
    failed = [(k, v[1]) for k, v in results.items() if v[0] == "failed"]

    if not success_files:
        return f"下载失败，没有成功获取任何包文件。", 400

    # 打包为 zip
    import zipfile, shutil
    zip_path = CACHE_DIR / f"packages_{tmp_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in success_files:
            zf.write(fpath, Path(fpath).name)
    zip_path.chmod(0o644)

    # 清理临时下载目录
    if output_dir.exists():
        shutil.rmtree(output_dir)

    return send_file(
        str(zip_path),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"packages_{tmp_id}.zip",
    )


# ─── Zip download endpoint (GET，触发浏览器保存对话框) ──────────────────────────

@app.route("/download-zip/<zip_id>")
def download_zip(zip_id: str):
    """Serve a zip archive for browser download."""
    zip_path = CACHE_DIR / f"packages_{zip_id}.zip"
    if not zip_path.exists() or not zip_path.is_file():
        abort(404)
    return send_file(
        str(zip_path),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"packages_{zip_id}.zip",
    )


# ─── Temp download directory (cache/deb-temp/) ──────────────────────────────────

DEB_TEMP_DIR = CACHE_DIR / "deb-temp"

def _get_deb_temp_dir() -> Path:
    """Get or create a temp download directory under cache/deb-temp/."""
    DEB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return DEB_TEMP_DIR


# ─── Export log endpoint ────────────────────────────────────────────────────────────

@app.route("/export-log/<result_id>.txt")
def export_log(result_id: str):
    """Download the resolution log as a text file."""
    cached = load_preview_result(result_id)
    if not cached:
        abort(404)
    log_text = cached.get("log", "无解析日志")
    return log_text, 200, {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": f"attachment; filename=resolve-log-{result_id}.txt",
    }


# ─── Temp file download endpoint (for File System Access API) ──────────────────

@app.route("/download-tmp/<tmp_id>/<filename>")
def download_tmp(tmp_id: str, filename: str):
    """Serve a single file from a temp download directory."""
    tmp_base = DEB_TEMP_DIR / tmp_id
    if not tmp_base.exists():
        abort(404)
    file_path = tmp_base / filename
    if not file_path.exists() or not file_path.is_file():
        abort(404)
    return send_file(str(file_path), as_attachment=True, download_name=filename)


# ─── Reset APIs ────────────────────────────────────────────────────────────────

@app.route("/add-base-list", methods=["POST"])
def add_base_list():
    """Add a base list path to history, moving it to top (dedup + bring to front)."""
    from flask import request
    path = request.form.get("path", "").strip()
    if path:
        ensure_base_list(path)
    return ("OK", 200)


@app.route("/reset-base-list", methods=["POST"])
def reset_base_list():
    """Clear saved base list path and remove from history."""
    if BASE_LIST_FILE.exists():
        BASE_LIST_FILE.unlink()
    return ("OK", 200)


@app.route("/reset-config", methods=["POST"])
def reset_config_route():
    """Clear all config files."""
    reset_config()
    return ("OK", 200)


if __name__ == "__main__":
    import webbrowser
    url = "http://127.0.0.1:51234/"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host="127.0.0.1", port=51234, debug=True, use_reloader=False)
