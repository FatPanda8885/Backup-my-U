from __future__ import annotations

import filecmp
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from myu.util import MyuError, copy_file_atomic, path_matches

DELETED_DIR = "_deleted"
PRUNED_DIRS = (".git", DELETED_DIR)
MAX_ERROR_LINES = 5


@dataclass(frozen=True)
class FileState:
    """扫描到的一个普通文件；path 是它在磁盘上的真实路径。"""

    relpath: str
    size: int
    mtime_ns: int
    path: Path


@dataclass
class Plan:
    added: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    deleted: list = field(default_factory=list)

    def changed_bytes(self) -> int:
        return sum(item.size for item in self.added) + sum(
            item.size for item in self.modified
        )

    def total_bytes(self) -> int:
        return self.changed_bytes() + sum(item.size for item in self.unchanged)


@dataclass
class MirrorStats:
    copied: int = 0
    deleted_moved: int = 0
    bytes_copied: int = 0
    errors: list = field(default_factory=list)


def _walk(root: Path, excludes: Iterable[str] | str | None, pruned: tuple) -> Iterator:
    """按目录顺序产出 (path, relpath)；pruned 里的顶层目录整棵跳过，不进子树。"""
    pending: list = [(Path(root), "")]
    index = 0
    while index < len(pending):
        directory, prefix = pending[index]
        index += 1
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise MyuError(f"无法读取目录 {directory}: {exc}") from exc
        for child in children:
            relpath = f"{prefix}{child.name}"
            try:
                if child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False):
                    if not prefix and child.name in pruned:
                        continue
                    pending.append((Path(child.path), f"{relpath}/"))
                    continue
                if not child.is_file(follow_symlinks=False):
                    continue
            except OSError as exc:
                raise MyuError(f"无法读取目录项 {child.path}: {exc}") from exc
            if path_matches(relpath, excludes):
                continue
            yield Path(child.path), relpath


def _scan(root: Path, excludes: Iterable[str] | str | None, pruned: tuple) -> dict:
    result: dict = {}
    for path, relpath in _walk(root, excludes, pruned):
        try:
            stat = path.stat()
        except OSError as exc:
            raise MyuError(f"无法读取文件 {path}: {exc}") from exc
        result[relpath] = FileState(relpath, stat.st_size, stat.st_mtime_ns, path)
    return result


def scan(root: Path, excludes: Iterable[str] | str | None = None) -> dict:
    """扫描 U 盘侧文件树：relpath -> FileState，跳过符号链接与命中 excludes 的文件。"""
    base = Path(root)
    if not base.is_dir():
        raise MyuError(f"待扫描目录不存在: {base}")
    return _scan(base, excludes, ())


def scan_worktree(worktree: Path, excludes: Iterable[str] | str | None = None) -> dict:
    """扫描仓库工作区：额外整棵跳过顶层 .git 与 _deleted（不进子树）。"""
    base = Path(worktree)
    if not base.is_dir():
        return {}
    return _scan(base, excludes, PRUNED_DIRS)


def _same_bytes(first: Path, second: Path) -> bool:
    try:
        return filecmp.cmp(str(first), str(second), shallow=False)
    except OSError:
        return False


def plan(usb: dict, worktree: dict, worktree_root: Path) -> Plan:
    """按 §7.6 的四条判据（顺序不可调换）算出 added/modified/unchanged/deleted。"""
    root = Path(worktree_root)
    result = Plan()
    for relpath in sorted(usb):
        source = usb[relpath]
        current = worktree.get(relpath)
        if current is None:
            result.added.append(source)
            continue
        if current.size != source.size:
            result.modified.append(source)
            continue
        if current.mtime_ns == source.mtime_ns:
            result.unchanged.append(source)
            continue
        if _same_bytes(source.path, root / relpath):
            result.unchanged.append(source)
            continue
        result.modified.append(source)
    for relpath in sorted(worktree):
        if relpath not in usb:
            result.deleted.append(relpath)
    return result


def _move_to_deleted(source: Path, target: Path) -> None:
    """同盘 rename；跨盘 OSError 回退成 copy_file_atomic + unlink。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(str(source), str(target))
    except OSError:
        copy_file_atomic(source, target)
        try:
            Path(source).unlink()
        except OSError as exc:
            raise MyuError(f"删除源文件失败 {source}: {exc}") from exc


def _notify(progress, done: int, total: int, relpath: str) -> None:
    if progress is None:
        return
    progress(done, total, relpath)


def _error_summary(stats: MirrorStats) -> str:
    shown = stats.errors[:MAX_ERROR_LINES]
    text = "；".join(shown)
    remaining = len(stats.errors) - len(shown)
    if remaining > 0:
        text = f"{text}；…另有 {remaining} 个错误"
    return f"镜像阶段有 {len(stats.errors)} 个文件处理失败: {text}"


def mirror(plan: Plan, worktree: Path, run_id: str, progress=None) -> MirrorStats:
    """把计划落到工作区：新增/修改原子复制，删除移入 _deleted/<run_id>/。

    单个文件失败不中断，记入 stats.errors，全部处理完后统一抛 MyuError。
    progress 形如 progress(done, total, relpath)，每处理完一个条目回调一次。
    """
    root = Path(worktree)
    label = str(run_id).strip()
    if not label:
        raise MyuError("run_id 不能为空")
    deleted_root = root / DELETED_DIR / label
    stats = MirrorStats()
    total = len(plan.added) + len(plan.modified) + len(plan.deleted)
    done = 0
    for source in list(plan.added) + list(plan.modified):
        target = root / source.relpath
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            stats.bytes_copied += copy_file_atomic(source.path, target)
            stats.copied += 1
        except (OSError, MyuError) as exc:
            stats.errors.append(f"{source.relpath}: {exc}")
        done += 1
        _notify(progress, done, total, source.relpath)
    for relpath in plan.deleted:
        try:
            _move_to_deleted(root / relpath, deleted_root / relpath)
            stats.deleted_moved += 1
        except (OSError, MyuError) as exc:
            stats.errors.append(f"{relpath}: {exc}")
        done += 1
        _notify(progress, done, total, relpath)
    if stats.errors:
        raise MyuError(_error_summary(stats))
    return stats
