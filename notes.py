#!/usr/bin/env python3
"""notes.py — 读取 CRP「整机适配更新报告」仓库，提供更新注记。

报告仓库结构（来自自动化扫描）：

    <dir>/<YYYYMMDD_HHMMSS>/
        更新注记_<ts>.md
        CRP-<id>-变更报告_<ts>.md
        baseline_snapshot_<id>_<date>.json

配置可以是 GitLab 仓库地址（HTTPS，可带令牌），也可以是本地已克隆目录。
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

PERIOD_RE = re.compile(r"^\d{8}_\d{6}$")
CACHE_TTL = 300.0
GIT_TIMEOUT = 120

NOTE_FIELDS = ("url", "ref", "dir", "sources")


class NotesError(Exception):
    """注记读取失败，消息面向页面展示。"""


# ─── 配置合并 ──────────────────────────────────────────────────────────────────


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _clean_sources(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[\s,，]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


def merge_config(*layers: dict | None) -> dict:
    """按顺序合并注记配置，后者优先；sources 整体覆盖而不是拼接。"""
    merged: dict = {"url": "", "ref": "", "dir": "", "sources": []}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for field in ("url", "ref", "dir"):
            value = _clean(layer.get(field))
            if value:
                merged[field] = value
        sources = _clean_sources(layer.get("sources"))
        if sources:
            merged["sources"] = sources
    return merged


def is_configured(config: dict) -> bool:
    return bool(config.get("url"))


# ─── 仓库定位（clone / 本地目录） ──────────────────────────────────────────────


def _local_dir(url: str) -> Path | None:
    candidate = url[7:] if url.startswith("file://") else url
    if candidate.startswith(("http://", "https://", "git@", "ssh://")):
        return None
    path = Path(candidate).expanduser()
    return path if path.is_dir() else None


def _clone_url(url: str, token: str) -> str:
    if not token:
        return url
    if not url.startswith("https://"):
        return url
    rest = url[len("https://"):]
    if "@" in rest.split("/", 1)[0]:
        return url
    return f"https://oauth2:{token}@{rest}"


def _run_git(args: list[str], cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as exc:
        raise NotesError("系统缺少 git，无法拉取注记仓库") from exc
    except subprocess.TimeoutExpired as exc:
        raise NotesError("拉取注记仓库超时（120s）") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        message = re.sub(r"https://[^@\s]+@", "https://", message)
        tail = message.splitlines()[-1] if message else f"git 退出码 {result.returncode}"
        raise NotesError(f"拉取注记仓库失败：{tail}")


def _checkout(url: str, ref: str, token: str, cache_root: Path) -> Path:
    local = _local_dir(url)
    if local is not None:
        return local
    key = hashlib.sha1(f"{url}|{ref}".encode("utf-8")).hexdigest()[:16]
    target = cache_root / key
    meta = cache_root / f"{key}.json"
    now = time.time()
    if target.is_dir() and meta.is_file():
        try:
            fetched_at = float(json.loads(meta.read_text(encoding="utf-8"))["fetched_at"])
        except (OSError, ValueError, KeyError):
            fetched_at = 0.0
        if now - fetched_at < CACHE_TTL:
            return target
        _run_git(["-C", str(target), "fetch", "--depth=1", "origin", ref or "HEAD"])
        _run_git(["-C", str(target), "reset", "--hard", "FETCH_HEAD"])
        meta.write_text(json.dumps({"fetched_at": now}), encoding="utf-8")
        return target
    shutil.rmtree(target, ignore_errors=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    clone_url = _clone_url(url, token)
    args = ["clone", "--depth=1", "--quiet"]
    if ref:
        args += ["--branch", ref]
    args += [clone_url, str(target)]
    _run_git(args)
    meta.write_text(json.dumps({"fetched_at": now}), encoding="utf-8")
    return target


# ─── 读取某一期 ────────────────────────────────────────────────────────────────


def _pick(directory: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[-1]
    return None


def period_label(period: str) -> str:
    try:
        return f"{period[0:4]}-{period[4:6]}-{period[6:8]} {period[9:11]}:{period[11:13]}"
    except IndexError:  # pragma: no cover - 目录名已由正则约束
        return period


class ReportBundle:
    """一个注记源的快照：可列期次、读某一期、全量导出。"""

    def __init__(self, root: Path, config: dict):
        self.root = root
        self.config = config
        self.dir = config.get("dir", "")

    # 注记所在的子目录（含期次目录）
    def base_dir(self) -> Path:
        if not self.dir:
            return self.root
        candidate = (self.root / self.dir).resolve()
        if self.root.resolve() not in candidate.parents and candidate != self.root.resolve():
            raise NotesError(f"注记目录越界：{self.dir}")
        if not candidate.is_dir():
            raise NotesError(f"注记目录不存在：{self.dir}")
        return candidate

    def periods(self) -> list[str]:
        base = self.base_dir()
        items = [
            child.name
            for child in base.iterdir()
            if child.is_dir() and PERIOD_RE.match(child.name)
        ]
        if not items:
            raise NotesError(f"{self.dir or '仓库根目录'} 下没有可用的注记期次")
        return sorted(items, reverse=True)

    def period_dir(self, period: str) -> Path:
        if not PERIOD_RE.match(period or ""):
            raise NotesError("期次格式无效")
        path = self.base_dir() / period
        if not path.is_dir():
            raise NotesError(f"期次不存在：{period}")
        return path

    def load(self, period: str | None = None) -> dict:
        periods = self.periods()
        current = period or periods[0]
        if current not in periods:
            raise NotesError(f"期次不存在：{current}")
        directory = self.period_dir(current)
        note = _pick(directory, ["更新注记_*.md", "*注记*.md"])
        report = _pick(directory, ["CRP-*-变更报告_*.md", "*变更报告*.md"])
        snapshot = _pick(directory, ["baseline_snapshot_*.json"])
        if note is None:
            raise NotesError(f"期次 {current} 缺少更新注记文件")
        note_text = note.read_text(encoding="utf-8", errors="replace")
        report_text = report.read_text(encoding="utf-8", errors="replace") if report else ""
        meta = {"crp_subject": "", "mode": "", "snapshot_date": "", "changed": [], "total": 0}
        if snapshot is not None:
            try:
                data = json.loads(snapshot.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                data = {}
            packages = data.get("packages") if isinstance(data, dict) else {}
            if isinstance(packages, dict):
                changed = [
                    name
                    for name, info in packages.items()
                    if isinstance(info, dict) and int(info.get("change_count") or 0) > 0
                ]
                meta = {
                    "crp_subject": str(data.get("crp_subject", "")),
                    "mode": str(data.get("mode", "")),
                    "snapshot_date": str(data.get("snapshot_date", "")),
                    "changed": sorted(changed),
                    "total": len(packages),
                }
        sources = _clean_sources(self.config.get("sources"))
        return {
            "period": current,
            "period_label": period_label(current),
            "periods": [{"id": item, "label": period_label(item)} for item in periods],
            "note_text": note_text,
            "note_html": render_markdown(note_text),
            "report_text": report_text,
            "report_html": render_markdown(filter_report(report_text, sources)) if report_text else "",
            "sources": sources,
            "meta": meta,
            "files": [
                path.name
                for path in (note, report, snapshot)
                if path is not None
            ],
        }

    # 全量导出：所有期次的原始文件
    def export_zip(self, version_name: str) -> io.BytesIO:
        periods = self.periods()
        sources = _clean_sources(self.config.get("sources"))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            base = self.base_dir()
            prefix = f"{self.dir or 'notes'}"
            summary_rows: list[str] = []
            for period in periods:
                directory = base / period
                for path in sorted(directory.iterdir()):
                    if not path.is_file():
                        continue
                    arcname = f"{prefix}/{period}/{path.name}"
                    if path.name.endswith("变更报告.md") or "变更报告" in path.name:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        archive.writestr(arcname, text)
                        if sources:
                            filtered = filter_report(text, sources)
                            archive.writestr(f"{prefix}/{period}/变更报告_{period}.filtered.md", filtered)
                        continue
                    archive.write(path, arcname)
                snapshot = _pick(directory, ["baseline_snapshot_*.json"])
                changed = []
                if snapshot is not None:
                    try:
                        data = json.loads(snapshot.read_text(encoding="utf-8", errors="replace"))
                        packages = data.get("packages") or {}
                        changed = [
                            name
                            for name, info in packages.items()
                            if isinstance(info, dict) and int(info.get("change_count") or 0) > 0
                        ]
                    except json.JSONDecodeError:
                        pass
                summary_rows.append(
                    f"{period}\t{period_label(period)}\t{len(changed)}\t{', '.join(sorted(changed))}"
                )
            info_lines = [
                f"版本: {version_name}",
                f"注记来源: {self.config.get('url', '')}",
                f"分支: {self.config.get('ref', '')}",
                f"目录: {self.dir}",
                f"过滤仓库: {', '.join(sources) if sources else '（不过滤，整篇导出）'}",
                f"期次数: {len(periods)}",
                f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "期次\t时间\t变更仓库数\t变更仓库",
                *summary_rows,
            ]
            archive.writestr(f"{prefix}/info.txt", "\n".join(info_lines) + "\n")
        buffer.seek(0)
        return buffer


# ─── Markdown 渲染（只支持报告用到的子集，全部先转义） ────────────────────────


def _render_inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def render_markdown(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    list_open = False

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            close_list()
            index += 1
            continue
        if set(stripped) <= {"-"} and len(stripped) >= 3:
            close_list()
            out.append("<hr>")
            index += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            index += 1
            continue
        if stripped.startswith("|"):
            close_list()
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = _table_cells(lines[index])
                if not all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
                    rows.append(cells)
                index += 1
            out.append("<table>")
            for row_index, cells in enumerate(rows):
                tag = "th" if row_index == 0 else "td"
                out.append(
                    "<tr>"
                    + "".join(f"<{tag}>{_render_inline(cell)}</{tag}>" for cell in cells)
                    + "</tr>"
                )
            out.append("</table>")
            continue
        if stripped.startswith(">"):
            close_list()
            quoted: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip()[1:].strip())
                index += 1
            out.append("<blockquote>" + "<br>".join(_render_inline(item) for item in quoted) + "</blockquote>")
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{_render_inline(bullet.group(1))}</li>")
            index += 1
            continue
        close_list()
        paragraph: list[str] = [stripped]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if not nxt or nxt.startswith(("|", "#", ">", "-", "*")):
                break
            paragraph.append(nxt)
            index += 1
        out.append("<p>" + _render_inline(" ".join(paragraph)) + "</p>")
    close_list()
    return "\n".join(out)


def filter_report(text: str, sources: list[str]) -> str:
    """只保留与 sources 相关的汇总行与仓库小节。sources 为空时原样返回。"""
    if not sources or not text:
        return text
    wanted = {source.lower() for source in sources}
    out: list[str] = []
    keep_section = True
    for line in text.splitlines():
        heading = re.match(r"^###\s+(.*)$", line.strip())
        if heading:
            keep_section = heading.group(1).strip().lower() in wanted
            if keep_section:
                out.append(line)
            continue
        if line.strip().startswith("|"):
            cells = _table_cells(line)
            name = cells[0].strip("`* ").lower() if cells else ""
            if not name or name in {"仓库", "字段"} or set(name) <= {"-", ":"}:
                out.append(line)
            elif name in wanted:
                out.append(line)
            continue
        if not keep_section and line.strip() and not line.strip().startswith("#"):
            continue
        out.append(line)
    filtered = "\n".join(out)
    filtered = re.sub(r"\n{3,}", "\n\n", filtered)
    return filtered


# ─── 供 Web 层使用的入口 ───────────────────────────────────────────────────────


def load_bundle(config: dict, cache_root: Path, token: str = "") -> ReportBundle:
    if not is_configured(config):
        raise NotesError("未配置更新注记源")
    root = _checkout(config["url"], config.get("ref", ""), token, cache_root)
    return ReportBundle(root, config)
