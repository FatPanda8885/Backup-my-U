from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .util import MyuError, atomic_replace, human_bytes, normalize_relpath

EMPTY_TEXT = "（无）"
UNKNOWN_TEXT = "—"
PATH_COLUMNS = "| 相对路径 | 大小 | 时间（UTC） |"
PATH_SEPARATOR = "| --- | --- | --- |"
STATUS_TITLES = {
    "running": "进行中",
    "completed": "完成",
    "failed": "失败",
    "dryrun": "试运行",
}


def _get(source: object, name: str, default: object = None) -> object:
    """条目既可能是 dict，也可能是带同名属性的对象。"""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _cell(value: object) -> str:
    """表格单元格：None 记作破折号，压平换行，转义反斜杠与竖线。"""
    text = UNKNOWN_TEXT if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _row(*cells: object) -> str:
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _size_text(size: object) -> str:
    if size is None or isinstance(size, bool):
        return UNKNOWN_TEXT
    try:
        return human_bytes(int(size))
    except (TypeError, ValueError):
        return UNKNOWN_TEXT


def _time_text(mtime_ns: object) -> str:
    if mtime_ns is None or isinstance(mtime_ns, bool):
        return UNKNOWN_TEXT
    try:
        moment = datetime.fromtimestamp(int(mtime_ns) // 1_000_000_000, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return UNKNOWN_TEXT
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _duration_text(duration_ms: object) -> str:
    if duration_ms is None or isinstance(duration_ms, bool):
        return UNKNOWN_TEXT
    try:
        milliseconds = int(duration_ms)
    except (TypeError, ValueError):
        return UNKNOWN_TEXT
    if milliseconds < 0:
        return UNKNOWN_TEXT
    if milliseconds < 1000:
        return f"{milliseconds} 毫秒"
    seconds = milliseconds / 1000.0
    if seconds < 60:
        return f"{seconds:.2f} 秒"
    total_seconds = int(round(seconds))
    minutes, rest = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes} 分"


def _status_text(status: object) -> str:
    if status is None:
        return UNKNOWN_TEXT
    text = str(status)
    return STATUS_TITLES.get(text, text)


def _listed(items: object, kind: str) -> list:
    if isinstance(items, Mapping):
        value = items.get(kind)
    else:
        value = getattr(items, kind, None)
    return [] if value is None else list(value)


def _count(run: object, items: object, kind: str) -> int:
    """有条目列表时以列表长度为准，保证概览与下方章节一致；否则回落到 runs 行。"""
    if isinstance(items, Mapping) and kind in items:
        return len(_listed(items, kind))
    stored = _get(run, kind)
    if stored is None:
        return 0
    try:
        return int(stored)
    except (TypeError, ValueError):
        return 0


def _file_rows(entries: list) -> list:
    return [
        _row(_get(entry, "relpath"), _size_text(_get(entry, "size")), _time_text(_get(entry, "mtime_ns")))
        for entry in entries
    ]


def _section(title: str, rows: list) -> list:
    block = [f"## {title}", ""]
    if rows:
        block.extend([PATH_COLUMNS, PATH_SEPARATOR, *rows])
    else:
        block.append(EMPTY_TEXT)
    return block


def _overview(run: object, items: object) -> list:
    fields = (
        ("运行 ID", _get(run, "run_id")),
        ("设备序列号", _get(run, "device_serial")),
        ("开始时间", _get(run, "started_at")),
        ("结束时间", _get(run, "finished_at")),
        ("耗时", _duration_text(_get(run, "duration_ms"))),
        ("新增", f"{_count(run, items, 'added')} 个"),
        ("修改", f"{_count(run, items, 'modified')} 个"),
        ("删除", f"{_count(run, items, 'deleted')} 个"),
        ("复制字节数", _size_text(_get(run, "bytes_copied"))),
        ("git commit", _get(run, "git_commit")),
        ("状态", _status_text(_get(run, "status"))),
    )
    return [
        "## 概览",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        *(_row(name, value) for name, value in fields),
    ]


def _error_lines(run: object, stats: object) -> list:
    texts: list = []
    for source in (run, stats):
        if source is None:
            continue
        single = _get(source, "error")
        if single:
            texts.append(str(single))
        many = _get(source, "errors")
        if many:
            texts.extend(str(item) for item in many)
    return [f"- {_cell(text)}" for text in texts]


def build_markdown(run: dict, items: dict, stats: object = None) -> str:
    """把一次运行渲染成中文 Markdown：标题 / 概览表 / 新增 / 修改 / 删除 / 异常。"""
    lines: list = [f"# 备份运行 {_cell(_get(run, 'run_id'))}", ""]
    lines.extend(_overview(run, items))
    lines.append("")
    for kind, title in (("added", "新增"), ("modified", "修改"), ("deleted", "删除")):
        rows = _file_rows(_listed(items, kind))
        lines.extend(_section(f"{title}（{_count(run, items, kind)} 个）", rows))
        lines.append("")
    errors = _error_lines(run, stats)
    lines.extend([f"## 异常（{len(errors)} 条）", ""])
    lines.extend(errors if errors else [EMPTY_TEXT])
    return "\n".join(lines) + "\n"


def write_report(reports_dir: Path, run_id: object, text: str) -> Path:
    """写 reports_dir/<run_id>.md：自动建目录，先写 .myu-tmp 再原子替换。"""
    name = normalize_relpath(str(run_id))
    if not name or "/" in name:
        raise MyuError(f"非法的运行 ID: {run_id!r}")
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.md"
    tmp = target.with_name(f"{target.name}.myu-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise MyuError(f"写报告失败 {target}: {exc}") from exc
    atomic_replace(tmp, target)
    return target
