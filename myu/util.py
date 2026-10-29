from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
CHUNK_SIZE = 1024 * 1024
POLL_INTERVAL = 0.05

SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024 ** 2,
    "mb": 1024 ** 2,
    "g": 1024 ** 3,
    "gb": 1024 ** 3,
    "t": 1024 ** 4,
    "tb": 1024 ** 4,
}
HUMAN_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 200000
PASSWORD_SALT_BYTES = 16


class MyuError(Exception):
    """可预期的运行错误；上层捕获后转成中文提示与退出码。"""


def _acquire_fd(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_fd(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """单实例锁；抢不到抛 MyuError，由 CLI 转成退出码 75。"""

    def __init__(self, path: Path, timeout: float = 0.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(self.timeout, 0.0)
        while True:
            try:
                _acquire_fd(fd)
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise MyuError("另一个备份进程正在运行") from None
                time.sleep(POLL_INTERVAL)
                continue
            self._fd = fd
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            _release_fd(fd)
        finally:
            os.close(fd)


def parse_size(text: str) -> int:
    """把 512m / 20g / 1024 这样的写法解析成字节数。"""
    if not isinstance(text, str):
        raise MyuError(f"大小必须是字符串: {text!r}")
    matched = re.fullmatch(r"(\d+)\s*([a-z]*)", text.strip().lower())
    if not matched:
        raise MyuError(f"无法解析大小: {text!r}")
    unit = matched.group(2)
    if unit not in SIZE_UNITS:
        raise MyuError(f"无法识别的大小单位: {text!r}")
    return int(matched.group(1)) * SIZE_UNITS[unit]


def human_bytes(n: int) -> str:
    """1024 进制、两位小数的人类可读大小。"""
    value = float(n)
    index = 0
    while value >= 1024.0 and index < len(HUMAN_UNITS) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return f"{int(n)} B"
    return f"{value:.2f} {HUMAN_UNITS[index]}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(now: datetime | None = None) -> str:
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def setup_logger(log_dir: Path, run_id: str | None = None, verbose: bool = False) -> logging.Logger:
    """stderr + log_dir/<run_id>.log 双输出；重复调用不会叠加 handler。"""
    logger = logging.getLogger("myu")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    if run_id is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(directory / f"{run_id}.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger


def _glob_to_regex(pattern: str) -> str:
    """* 不跨 /，** 跨层级，**/ 可匹配零层目录，? 匹配单个非 / 字符。"""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                if pattern[index + 2:index + 3] == "/":
                    parts.append("(?:.*/)?")
                    index += 3
                else:
                    parts.append(".*")
                    index += 2
                continue
            parts.append("[^/]*")
            index += 1
            continue
        if char == "?":
            parts.append("[^/]")
            index += 1
            continue
        parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def _match_one(target: str, pattern: str) -> bool:
    normalized = pattern.strip().replace("\\", "/")
    rooted = normalized.startswith("/")
    if rooted:
        normalized = normalized.lstrip("/")
    regex = _glob_to_regex(normalized)
    if not rooted:
        regex = f"(?:.*/)?{regex}"
    return re.fullmatch(regex, target) is not None


def path_matches(relpath: str, patterns: Iterable[str] | str | None) -> bool:
    """relpath 命中任一 glob 即返回 True；模式不以 / 开头时匹配任意层级。"""
    if patterns is None:
        return False
    if isinstance(patterns, str):
        patterns = (patterns,)
    target = str(relpath).replace("\\", "/").lstrip("/")
    for pattern in patterns:
        if pattern is None:
            continue
        text = str(pattern).strip()
        if not text:
            continue
        if _match_one(target, text):
            return True
    return False


def normalize_relpath(path: str) -> str:
    """反斜杠转 /、去前导 /、丢 . 段、拒绝 .. 段。"""
    if not isinstance(path, str):
        raise MyuError(f"路径必须是字符串: {path!r}")
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise MyuError(f"路径不允许出现 .. 段: {path!r}")
        parts.append(part)
    return "/".join(parts)


def iter_files(root: Path, excludes: Iterable[str] | str | None = None) -> Iterator[Path]:
    """按目录顺序遍历 root 下的普通文件，跳过符号链接与命中 excludes 的相对路径。"""
    base = Path(root)
    pending: list[tuple[Path, str]] = [(base, "")]
    index = 0
    while index < len(pending):
        directory, prefix = pending[index]
        index += 1
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise MyuError(f"无法读取目录 {directory}: {exc}") from exc
        for child in children:
            relpath = f"{prefix}{child.name}"
            try:
                if child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False):
                    pending.append((Path(child.path), f"{relpath}/"))
                    continue
                if not child.is_file(follow_symlinks=False):
                    continue
            except OSError as exc:
                raise MyuError(f"无法读取目录项 {child.path}: {exc}") from exc
            if path_matches(relpath, excludes):
                continue
            yield Path(child.path)


def atomic_replace(src: Path, dst: Path) -> None:
    source = Path(src)
    target = Path(dst)
    try:
        os.replace(str(source), str(target))
    except OSError as exc:
        if source.exists():
            try:
                source.unlink()
            except OSError:
                pass
        raise MyuError(f"替换文件失败 {target}: {exc}") from exc


def copy_file_atomic(src: Path, dst: Path) -> int:
    """写同目录 <name>.myu-tmp -> flush -> fsync -> rename，返回复制的字节数。"""
    source = Path(src)
    target = Path(dst)
    tmp = target.with_name(f"{target.name}.myu-tmp")
    written = 0
    try:
        with open(source, "rb") as reader, open(tmp, "wb") as writer:
            while True:
                chunk = reader.read(CHUNK_SIZE)
                if not chunk:
                    break
                writer.write(chunk)
                written += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise MyuError(f"复制文件失败 {source} -> {target}: {exc}") from exc
    atomic_replace(tmp, target)
    return written


def hash_password(password: str, iterations: int = PASSWORD_ITERATIONS) -> str:
    """生成 pbkdf2_sha256$迭代数$盐b64$哈希b64。"""
    if iterations < 1:
        raise MyuError(f"pbkdf2 迭代次数必须为正整数: {iterations}")
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (
            PASSWORD_ALGORITHM,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(stored: str, password: str) -> bool:
    """格式非法一律返回 False，比较走 hmac.compare_digest。"""
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != PASSWORD_ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except ValueError:
        return False
    if iterations < 1 or not salt or not expected:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)
