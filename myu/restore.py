from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .index import Store
from .repo import GitRepo
from .util import MyuError, normalize_relpath

DELETED_PREFIX = "_deleted/"
STAGING_PREFIX = ".myu-restore-"


@dataclass
class RestoreStats:
    files: int
    bytes: int
    skipped: int


def _canonical_run_id(run_id: object) -> str:
    text = str(run_id).strip()
    return text[4:] if text.startswith("run-") else text


def _resolve_commit(repo: GitRepo, run_id: object) -> str:
    """先按原样找 rev（tag run_id），找不到再加 run- 前缀。"""
    text = str(run_id).strip()
    for candidate in (text, f"run-{text}"):
        commit = repo.rev_parse(candidate)
        if commit:
            return commit
    raise MyuError(f"找不到备份运行 {text}：git tag {text} 与 run-{text} 都不存在")


def _subtree_plan(tree: dict, prefix: str) -> dict:
    """返回 {目标内相对路径: (仓库内相对路径, 字节数)}，剥掉 prefix；prefix 不在树里时返回空表。"""
    if not prefix:
        return {path: (path, int(entry[1])) for path, entry in tree.items()}
    mapping: dict = {}
    if prefix in tree:
        mapping[prefix.rsplit("/", 1)[-1]] = (prefix, int(tree[prefix][1]))
        return mapping
    marker = f"{prefix}/"
    for path, entry in tree.items():
        if path.startswith(marker):
            mapping[path[len(marker):]] = (path, int(entry[1]))
    return mapping


def _plan(tree: dict, prefix: str, required: bool = True) -> dict:
    """普通文件计划：剥掉 prefix 并丢弃 _deleted/；required=False 时缺路径不报错（留给墓碑判定）。"""
    mapping = _subtree_plan(tree, prefix)
    if required and prefix and not mapping:
        raise MyuError(f"备份中不存在路径: {prefix}")
    return {
        relpath: value
        for relpath, value in mapping.items()
        if not value[0].startswith(DELETED_PREFIX)
    }


def _tombstone_root(run_id: str, prefix: str) -> str:
    """本次运行墓碑在树里的根：_deleted/<run_id>/，有 subpath 时再往下钻一层。"""
    root = f"{DELETED_PREFIX}{run_id}"
    return f"{root}/{prefix}" if prefix else root


def _tombstone_plan(tree: dict, run_id: str, prefix: str) -> dict:
    """只认自己那一份：_deleted/<run_id>/ 下的条目按原相对路径还原，没有墓碑时返回空表。"""
    if not run_id:
        return {}
    return _subtree_plan(tree, _tombstone_root(run_id, prefix))


def _planned_stats(plan: dict, dest: Path, overwrite: bool) -> RestoreStats:
    stats = RestoreStats(files=0, bytes=0, skipped=0)
    for relpath, (_source, size) in sorted(plan.items()):
        if not overwrite and (dest / relpath).exists():
            stats.skipped += 1
            continue
        stats.files += 1
        stats.bytes += int(size)
    return stats


def _mtime_map(store: Store, run_id: str) -> dict:
    """仓库相对路径 -> mtime_ns；运行记录或条目缺失时返回空表，不回填也不报错。"""
    run = store.get_run(run_id)
    if not run:
        return {}
    serial = run.get("device_serial") if isinstance(run, Mapping) else None
    if not serial:
        return {}
    mapping: dict = {}
    for entry in store.list_entries(str(serial)):
        if not isinstance(entry, Mapping):
            continue
        relpath = entry.get("relpath")
        mtime_ns = entry.get("mtime_ns")
        if relpath is None or mtime_ns is None:
            continue
        mapping[str(relpath)] = int(mtime_ns)
    return mapping


def _materialize(
    staging: Path,
    plan: dict,
    target: Path,
    overwrite: bool,
    mtimes: dict,
    stats: RestoreStats,
) -> None:
    """把 staging 里命中 plan 的文件搬到 target；mtime 按仓库内原路径回填。"""
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        relpath = path.relative_to(staging).as_posix()
        planned = plan.get(relpath)
        if planned is None:
            continue
        source_relpath, _size = planned
        final = target / relpath
        if not overwrite and final.exists():
            stats.skipped += 1
            continue
        size = path.stat().st_size
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(path), str(final))
        mtime_ns = mtimes.get(source_relpath)
        if mtime_ns is not None:
            os.utime(str(final), ns=(mtime_ns, mtime_ns))
        stats.files += 1
        stats.bytes += size


def restore(
    cfg: Config,
    store: Store,
    run_id: str,
    dest: Path,
    subpath: str | None = None,
    preserve_mtime: bool = True,
    overwrite: bool = False,
    dry_run: bool = False,
    include_deleted: bool = False,
) -> RestoreStats:
    """把某次备份恢复到 dest；run_id 接受 run_id 或 tag run-<run_id> 两种写法。

    include_deleted=True 时额外把本次运行产生的墓碑（树里 _deleted/<本次 run_id>/ 下的条目）
    按原相对路径还原；其它运行的墓碑一律忽略。
    """
    repo_root = Path(cfg.repo_root)
    repo = GitRepo(repo_root)
    if not repo.exists():
        raise MyuError(f"备份仓库不存在: {repo_root}")
    commit = _resolve_commit(repo, run_id)
    canonical = _canonical_run_id(run_id)
    prefix = normalize_relpath(str(subpath)) if subpath else ""
    tree = repo.tree_entries(commit)
    plan = _plan(tree, prefix, required=not include_deleted)
    tombstones: dict = {}
    if include_deleted:
        # 同一目标路径既有活文件又有墓碑时活文件优先：本次运行的树里两者并存说明该路径被删后又被建了回来
        tombstones = {
            relpath: value
            for relpath, value in _tombstone_plan(tree, canonical, prefix).items()
            if relpath not in plan
        }
        # subpath 只命中墓碑时也要能恢复，因此存在性判定放在两份计划都算完之后
        if prefix and not plan and not tombstones:
            raise MyuError(f"备份中不存在路径: {prefix}")
    target = Path(dest)
    if dry_run:
        return _planned_stats({**plan, **tombstones}, target, overwrite)
    target.mkdir(parents=True, exist_ok=True)
    mtimes = _mtime_map(store, canonical) if preserve_mtime else {}
    stats = RestoreStats(files=0, bytes=0, skipped=0)
    # archive_to 会直接覆盖写，因此先解到 dest 内的临时目录，再按计划逐个搬到位
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=str(target)))
    try:
        if plan:
            # plan 为空时不必解包：subpath 若只存在于墓碑里，git archive 会报 pathspec 不匹配
            repo.archive_to(commit, staging, subpath=prefix or None)
            _materialize(staging, plan, target, overwrite, mtimes, stats)
        if tombstones:
            # 墓碑单独解一份：archive_to 会剥掉 subpath 前缀，混在同一目录里会与活文件撞名
            deleted_staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=str(target)))
            try:
                repo.archive_to(
                    commit, deleted_staging, subpath=_tombstone_root(canonical, prefix)
                )
                _materialize(deleted_staging, tombstones, target, overwrite, mtimes, stats)
            finally:
                shutil.rmtree(deleted_staging, ignore_errors=True)
        return stats
    finally:
        shutil.rmtree(staging, ignore_errors=True)
