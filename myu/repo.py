from __future__ import annotations

import os
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from .util import MyuError, normalize_relpath

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
GIT_CONFIG_ARGS = ("-c", "core.quotepath=false")
CHUNK_SIZE = 1024 * 1024
_STDERR_LIMIT = 500


def _summarize_stderr(text: str, returncode: int) -> str:
    body = text.strip()
    if not body:
        return f"退出码 {returncode}"
    if len(body) > _STDERR_LIMIT:
        return body[:_STDERR_LIMIT] + "…"
    return body


def _stderr_summary(result: subprocess.CompletedProcess) -> str:
    text = result.stderr.decode("utf-8", "replace") if result.stderr else ""
    return _summarize_stderr(text, result.returncode)


def _safe_member_path(name: str) -> str:
    """归档成员名先归一化；绝对路径与 .. 段一律拒绝。"""
    text = name.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise MyuError(f"归档成员路径非法: {name!r}")
    return normalize_relpath(text)


def _close_process(process: subprocess.Popen) -> tuple:
    """关管道再 wait（避免 git 写满管道卡死），返回 (退出码, stderr 原文)。"""
    if process.stdout is not None:
        process.stdout.close()
    returncode = process.wait()
    stderr = ""
    if process.stderr is not None:
        stderr = process.stderr.read().decode("utf-8", "replace")
        process.stderr.close()
    return returncode, stderr


def _extract_tar(stream: BinaryIO, target: Path, prefix: str) -> tuple:
    """tarfile 的 r| 模式顺次解包，整条流不落内存。"""
    files = 0
    total = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if not member.isfile():
                continue
            name = _safe_member_path(member.name)
            if prefix:
                if name == prefix:
                    name = name.rsplit("/", 1)[-1]
                elif name.startswith(f"{prefix}/"):
                    name = name[len(prefix) + 1:]
                else:
                    continue
            if not name:
                continue
            out_path = target / name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            size = 0
            with open(out_path, "wb") as handle:
                while True:
                    chunk = extracted.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    size += len(chunk)
            files += 1
            total += size
    return files, total


class GitRepo:
    """备份仓库的 git 封装；所有调用都带 core.quotepath=false。"""

    def __init__(self, work_tree: Path) -> None:
        self._work_tree = Path(work_tree)

    @property
    def work_tree(self) -> Path:
        return self._work_tree

    def exists(self) -> bool:
        return (self._work_tree / ".git").exists()

    def _command(self, args: Sequence[str]) -> list:
        return ["git", *GIT_CONFIG_ARGS, *args]

    def _environment(self) -> dict:
        env = dict(os.environ)
        env.update(GIT_ENV)
        return env

    def run(self, *args, check: bool = True, stdin: object = None) -> subprocess.CompletedProcess:
        payload = None
        if stdin is not None:
            payload = stdin.encode("utf-8") if isinstance(stdin, str) else bytes(stdin)
        command = self._command([str(arg) for arg in args])
        try:
            result = subprocess.run(
                command,
                cwd=str(self._work_tree),
                env=self._environment(),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise MyuError(f"执行 git 失败: {exc}") from exc
        if check and result.returncode != 0:
            raise MyuError(f"git {' '.join(command[3:])} 失败: {_stderr_summary(result)}")
        return result

    def popen(self, *args) -> subprocess.Popen:
        """流式出口：stdout=PIPE，仅 archive_to 使用，调用方负责 wait()。"""
        command = self._command([str(arg) for arg in args])
        try:
            return subprocess.Popen(
                command,
                cwd=str(self._work_tree),
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise MyuError(f"执行 git 失败: {exc}") from exc

    def init(self, big_file_threshold: str = "512m") -> None:
        self._work_tree.mkdir(parents=True, exist_ok=True)
        self.run("init", "-q")
        self.run("config", "user.name", "Backup-my-U")
        self.run("config", "user.email", "myu@localhost")
        self.run("config", "core.autocrlf", "false")
        self.run("config", "core.quotepath", "false")
        self.run("config", "core.bigFileThreshold", str(big_file_threshold))
        self.run("config", "gc.auto", "0")

    def add_all(self) -> None:
        self.run("add", "-A")

    def has_staged_changes(self) -> bool:
        result = self.run("diff", "--cached", "--quiet", check=False)
        if result.returncode not in (0, 1):
            raise MyuError(f"git diff --cached 失败: {_stderr_summary(result)}")
        return result.returncode == 1

    def commit(self, message: str) -> str | None:
        if not self.has_staged_changes():
            return None
        self.run("commit", "-q", "-m", message)
        return self.head()

    def tag(self, name: str) -> None:
        self.run("tag", "-f", name)

    def head(self) -> str | None:
        return self.rev_parse("HEAD")

    def rev_parse(self, rev: str) -> str | None:
        result = self.run("rev-parse", "--verify", "--quiet", rev, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", "replace").strip() or None

    def diff_name_status(self, old: object, new: str) -> list:
        if old is None:
            return [("A", path) for path in self.tracked_files(new)]
        result = self.run("diff", "--no-renames", "--name-status", str(old), new)
        entries: list = []
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            status, _, path = line.partition("\t")
            if not path:
                continue
            entries.append((status.strip(), path))
        return entries

    def tree_entries(self, rev: str) -> dict:
        result = self.run("ls-tree", "-r", "-l", rev)
        entries: dict = {}
        for line in result.stdout.decode("utf-8", "replace").split("\n"):
            if not line:
                continue
            meta, separator, path = line.partition("\t")
            if not separator:
                continue
            fields = meta.split()
            if len(fields) < 4 or fields[1] != "blob":
                continue
            try:
                size = int(fields[3])
            except ValueError:
                continue
            entries[path] = (fields[2], size)
        return entries

    def tracked_files(self, rev: str = "HEAD") -> list:
        result = self.run("ls-tree", "-r", "--name-only", rev)
        return [line for line in result.stdout.decode("utf-8", "replace").split("\n") if line]

    def archive_to(self, rev: str, dest: Path, subpath: object = None) -> tuple:
        """rev 或 rev:subpath 的树打到 dest；subpath 剥前缀，单文件落 basename。"""
        prefix = normalize_relpath(str(subpath)) if subpath else ""
        args = ["archive", "--format=tar", rev]
        if prefix:
            args.extend(["--", prefix])
        target = Path(dest)
        target.mkdir(parents=True, exist_ok=True)
        process = self.popen(*args)
        counts: tuple = (0, 0)
        failure: Exception | None = None
        try:
            counts = _extract_tar(process.stdout, target, prefix)
        except (tarfile.TarError, MyuError) as exc:
            failure = exc
        finally:
            returncode, stderr = _close_process(process)
        if returncode != 0:
            raise MyuError(f"git archive {rev} 失败: {_summarize_stderr(stderr, returncode)}") from failure
        if failure is not None:
            raise failure
        return counts

    def show_bytes(self, rev: str, relpath: str) -> bytes:
        path = normalize_relpath(relpath)
        if not path:
            raise MyuError("文件路径为空")
        return self.run("show", f"{rev}:{path}").stdout

    def log(self, limit: int = 50) -> list:
        result = self.run("log", f"-n{int(limit)}", "--pretty=format:%H%x1f%aI%x1f%s", check=False)
        if result.returncode != 0:
            return []
        entries: list = []
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\x1f")
            if len(parts) != 3:
                continue
            entries.append({"commit": parts[0], "date": parts[1], "subject": parts[2]})
        return entries

    def rev_count(self) -> int:
        result = self.run("rev-list", "--count", "HEAD", check=False)
        if result.returncode != 0:
            return 0
        try:
            return int(result.stdout.decode("utf-8", "replace").strip())
        except ValueError:
            return 0

    def gc(self) -> None:
        self.run("gc", "--quiet")

    def fsck(self) -> bool:
        return self.run("fsck", "--no-progress", check=False).returncode == 0
