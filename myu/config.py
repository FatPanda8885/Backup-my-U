from __future__ import annotations

import configparser
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .util import MyuError

_LOG = logging.getLogger("myu.config")

DEFAULT_CONFIG_PATH = Path("/etc/myu/config.ini")
DEFAULT_BACKUP_ROOT = Path("/srv/myu-backup")
DEFAULT_MARKER_FILE = ".myu-id"
DEFAULT_BIG_FILE_THRESHOLD = "512m"
DEFAULT_GC_AFTER_RUNS = 20
DEFAULT_POLL_GUARD_SECONDS = 3600
DEFAULT_WEB_BIND = "0.0.0.0"
DEFAULT_WEB_PORT = 8787

_KNOWN_OPTIONS = {
    "general": ("repo_root", "state_dir", "log_dir", "exclude", "poll_guard_seconds"),
    "device": ("serial", "marker_file", "marker_value"),
    "git": ("big_file_threshold", "gc_after_runs"),
    "web": ("bind", "port", "password_hash"),
}

_TEMPLATE = """[general]
repo_root = {repo_root}
state_dir = {state_dir}
log_dir   = {log_dir}
; 每行一个 glob，可缩进续写
exclude = $RECYCLE.BIN/**
    System Volume Information/**
    .Trashes/**
    .Spotlight-V100/**
    .fseventsd/**
    .Trash-*/**
    *.tmp
    pagefile.sys
; 轮询兜底的最小间隔（秒）：任何一次运行结束后的该时长内，带 --poll 的调用直接跳过
poll_guard_seconds = 3600

[device]
; U 盘的 ID_SERIAL；留空则只靠标记文件识别
serial      =
; 序列号缺失时的兜底识别：盘内该文件存在（且内容匹配 marker_value）即认
marker_file = .myu-id
marker_value =

[git]
big_file_threshold = 512m
gc_after_runs      = 20

[web]
bind          = 0.0.0.0
port          = 8787
; 由 myu set-password 写入：pbkdf2_sha256$迭代数$盐b64$哈希b64
password_hash =
"""

_OPTION_LINE = re.compile(r"^\s*([^=:#;\s][^=:]*?)\s*[=:]\s*(.*)$")


@dataclass(frozen=True)
class Config:
    repo_root: Path
    state_dir: Path
    log_dir: Path
    excludes: tuple = ()
    device_serial: str = ""
    marker_file: str = DEFAULT_MARKER_FILE
    marker_value: str = ""
    big_file_threshold: str = DEFAULT_BIG_FILE_THRESHOLD
    gc_after_runs: int = DEFAULT_GC_AFTER_RUNS
    poll_guard_seconds: int = DEFAULT_POLL_GUARD_SECONDS
    web_bind: str = DEFAULT_WEB_BIND
    web_port: int = DEFAULT_WEB_PORT
    web_password_hash: str = ""
    config_path: object = None

    @property
    def db_path(self) -> Path:
        return Path(self.state_dir) / "myu.db"

    @property
    def reports_dir(self) -> Path:
        return Path(self.state_dir) / "reports"

    @property
    def work_tree(self) -> Path:
        return Path(self.repo_root)

    @property
    def content_root(self) -> Path:
        return Path(self.repo_root)


def default_config_path() -> Path:
    override = os.environ.get("MYU_CONFIG", "").strip()
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH


def _section(parser: configparser.ConfigParser, name: str) -> dict:
    if not parser.has_section(name):
        return {}
    return {key: parser.get(name, key) for key in parser.options(name)}


def _text(section: dict, option: str, default: str) -> str:
    raw = section.get(option)
    if raw is None:
        return default
    return raw.strip()


def _absolute_path(section: dict, option: str, default: Path) -> Path:
    raw = _text(section, option, "")
    if not raw:
        path = default
    else:
        path = Path(raw)
    if not path.is_absolute():
        raise MyuError(f"{option} 必须是绝对路径: {raw or str(default)}")
    return path


def _integer(section: dict, option: str, default: int) -> int:
    raw = _text(section, option, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise MyuError(f"{option} 必须是整数: {raw}") from exc


def _non_negative_integer(section: dict, option: str, default: int) -> int:
    raw = _text(section, option, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise MyuError(f"{option} 必须是非负整数（秒），当前为 {raw!r}") from exc
    if value < 0:
        raise MyuError(f"{option} 必须是非负整数（秒），当前为 {value}")
    return value


def _excludes(section: dict) -> tuple:
    raw = section.get("exclude", "")
    return tuple(line.strip() for line in raw.splitlines() if line.strip())


def _warn_unknown(parser: configparser.ConfigParser) -> None:
    for section_name in parser.sections():
        known = _KNOWN_OPTIONS.get(section_name.lower())
        if known is None:
            _LOG.warning("忽略未知配置节: [%s]", section_name)
            continue
        for option in parser.options(section_name):
            if option not in known:
                _LOG.warning("忽略未知配置项: [%s] %s", section_name, option)


def load_config(path: object = None) -> Config:
    config_path = Path(path) if path is not None else default_config_path()
    if not config_path.is_file():
        raise MyuError(f"配置文件不存在: {config_path}")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as exc:
        raise MyuError(f"配置文件解析失败 {config_path}: {exc}") from exc
    _warn_unknown(parser)
    general = _section(parser, "general")
    device = _section(parser, "device")
    git = _section(parser, "git")
    web = _section(parser, "web")
    web_port = _integer(web, "port", DEFAULT_WEB_PORT)
    if not 1 <= web_port <= 65535:
        raise MyuError(f"port 必须在 1-65535 之间: {web_port}")
    return Config(
        repo_root=_absolute_path(general, "repo_root", DEFAULT_BACKUP_ROOT / "repo"),
        state_dir=_absolute_path(general, "state_dir", DEFAULT_BACKUP_ROOT / "state"),
        log_dir=_absolute_path(general, "log_dir", DEFAULT_BACKUP_ROOT / "logs"),
        excludes=_excludes(general),
        device_serial=_text(device, "serial", ""),
        marker_file=_text(device, "marker_file", DEFAULT_MARKER_FILE),
        marker_value=_text(device, "marker_value", ""),
        big_file_threshold=_text(git, "big_file_threshold", DEFAULT_BIG_FILE_THRESHOLD),
        gc_after_runs=_integer(git, "gc_after_runs", DEFAULT_GC_AFTER_RUNS),
        poll_guard_seconds=_non_negative_integer(
            general, "poll_guard_seconds", DEFAULT_POLL_GUARD_SECONDS
        ),
        web_bind=_text(web, "bind", DEFAULT_WEB_BIND),
        web_port=web_port,
        web_password_hash=_text(web, "password_hash", ""),
        config_path=config_path,
    )


def write_template(path: Path, repo_root: object = None, state_dir: object = None) -> None:
    target = Path(path)
    root = Path(repo_root) if repo_root is not None else DEFAULT_BACKUP_ROOT / "repo"
    state = Path(state_dir) if state_dir is not None else DEFAULT_BACKUP_ROOT / "state"
    log_dir = state.parent / "logs"
    text = _TEMPLATE.format(
        repo_root=root.as_posix(),
        state_dir=state.as_posix(),
        log_dir=log_dir.as_posix(),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _split_option(line: str) -> tuple:
    """拆出 (键, 值)，值里保留行内 ; / # 注释。"""
    body = line.rstrip("\r\n")
    matched = _OPTION_LINE.match(body)
    if not matched:
        return None, body
    key = matched.group(1).strip()
    value = matched.group(2)
    for marker in (";", "#"):
        position = value.find(marker)
        if position >= 0:
            value = value[:position]
            break
    return key, value.strip()


def _with_newline(line: str, newline: str) -> str:
    return line if line.endswith(("\n", "\r")) else line + newline


def save_config(cfg: Config) -> None:
    """仅回写 password_hash，保留其余行、注释与原有换行风格。"""
    config_path = Path(cfg.config_path) if cfg.config_path is not None else default_config_path()
    if not config_path.is_file():
        raise MyuError(f"配置文件不存在: {config_path}")
    with open(config_path, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    header = None
    in_web = False
    body_end = len(lines)
    key_index = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip().lower()
            if in_web and body_end == len(lines):
                body_end = index
            in_web = name == "web"
            if in_web and header is None:
                header = index
            continue
        if not in_web or stripped.startswith((";", "#")):
            continue
        key, _ = _split_option(line)
        if key is not None and key.lower() == "password_hash":
            key_index = index
            break

    value = cfg.web_password_hash
    if key_index is not None:
        original = lines[key_index]
        matched = _OPTION_LINE.match(original.rstrip("\r\n"))
        prefix = matched.group(1) if matched else "password_hash"
        raw_value = matched.group(2) if matched else ""
        comment = ""
        for marker in (";", "#"):
            position = raw_value.find(marker)
            if position >= 0:
                comment = " " + raw_value[position:].strip()
                break
        indent = original[:len(original) - len(original.lstrip())]
        lines[key_index] = f"{indent}{prefix} = {value}{comment}{newline}"
    elif header is not None:
        insert_at = body_end
        if insert_at > 0:
            lines[insert_at - 1] = _with_newline(lines[insert_at - 1], newline)
        lines.insert(insert_at, f"password_hash = {value}{newline}")
    else:
        block = ["[web]", "", f"password_hash = {value}", ""]
        if lines:
            lines[-1] = _with_newline(lines[-1], newline)
            if lines[-1].strip():
                block.insert(0, "")
        lines.extend(f"{line}{newline}" for line in block)

    with open(config_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))
