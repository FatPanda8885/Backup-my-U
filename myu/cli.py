from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

from . import __version__, config, device, index, repo, report, restore, sync, util, web

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NO_DEVICE = 3
EXIT_LOCKED = 75

LOCK_NAME = "myu.lock"
MARKER_NAME = "last-run.marker"
DELETED_PREFIX = "_deleted/"
RUN_TAG_PREFIX = "run-"
PREVIEW_LIMIT = 20
PASSWORD_MIN_LENGTH = 8
PASSWORD_ATTEMPTS = 3
GC_META_KEY = "last_gc_commit"
UNKNOWN_TEXT = "—"
STATUS_TITLES = {
    "running": "进行中",
    "completed": "完成",
    "failed": "失败",
    "dryrun": "试运行",
}


@dataclass(frozen=True)
class _Target:
    """本次备份要用的 U 盘根目录及其身份信息。"""

    root: Path
    serial: str
    mountpoint: Path
    we_mounted: bool
    name: str = ""
    label: str = ""
    fs_type: str = ""
    marker: str = ""


# ---------------------------------------------------------------- 通用小工具


def _config_path(args) -> Path:
    given = getattr(args, "config", None)
    return Path(given).expanduser() if given else config.default_config_path()


def _load_config(args) -> config.Config:
    return config.load_config(_config_path(args))


def _same_path(first: object, second: object) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
            os.path.abspath(str(second))
        )
    except OSError:
        return str(first) == str(second)


def _status_text(status: object) -> str:
    if status is None:
        return UNKNOWN_TEXT
    text = str(status)
    return STATUS_TITLES.get(text, text)


def _size_text(size: object) -> str:
    if size is None or isinstance(size, bool):
        return UNKNOWN_TEXT
    try:
        return util.human_bytes(int(size))
    except (TypeError, ValueError):
        return UNKNOWN_TEXT


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
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes} 分"


def _canonical_run_id(value: object) -> str:
    text = str(value).strip()
    return text[len(RUN_TAG_PREFIX):] if text.startswith(RUN_TAG_PREFIX) else text


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - _display_width(text), 0)


def _table(headers: tuple, rows: list) -> str:
    grid = [list(row) for row in rows]
    widths = [
        max([_display_width(str(cell)) for cell in column])
        for column in zip(headers, *grid)
    ]
    lines = [
        "  ".join(_pad(str(cell), width) for cell, width in zip(headers, widths)).rstrip(),
        "  ".join("-" * width for width in widths),
    ]
    for row in grid:
        lines.append(
            "  ".join(_pad(str(cell), width) for cell, width in zip(row, widths)).rstrip()
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- init


def _warn_argument_conflicts(args, cfg: config.Config) -> None:
    for name, given in (
        ("repo-root", getattr(args, "repo_root", None)),
        ("state-dir", getattr(args, "state_dir", None)),
        ("log-dir", getattr(args, "log_dir", None)),
    ):
        if not given:
            continue
        actual = getattr(cfg, name.replace("-", "_"))
        if not _same_path(Path(given).expanduser(), actual):
            print(
                f"警告：参数 --{name} 与配置里的 {actual} 不一致，以配置文件为准",
                file=sys.stderr,
            )


def cmd_init(args) -> int:
    config_path = _config_path(args)
    if config_path.is_file():
        print(f"配置文件已存在，保持原样: {config_path}")
    else:
        config.write_template(
            config_path,
            repo_root=args.repo_root,
            state_dir=args.state_dir,
        )
        print(f"已生成配置模板: {config_path}")
    cfg = _load_config(args)
    _warn_argument_conflicts(args, cfg)
    for directory in (cfg.repo_root, cfg.state_dir, cfg.log_dir, cfg.reports_dir):
        directory.mkdir(parents=True, exist_ok=True)
    git = repo.GitRepo(cfg.repo_root)
    if git.exists():
        print(f"git 仓库已存在，跳过初始化: {cfg.repo_root}")
    else:
        git.init(cfg.big_file_threshold)
        print(f"已初始化 git 仓库: {cfg.repo_root}")
    print(f"仓库目录: {cfg.repo_root}")
    print(f"状态目录: {cfg.state_dir}（报告写入 {cfg.reports_dir}）")
    print(f"日志目录: {cfg.log_dir}")
    return EXIT_OK


# ---------------------------------------------------------------- device-id


def _find_by_mountpoint(devices: list, target: Path) -> object:
    for candidate in devices:
        mountpoint = candidate.mountpoint
        if mountpoint is None:
            continue
        if _same_path(mountpoint, target):
            return candidate
    return None


def cmd_device_id(args) -> int:
    devices = device.list_block_devices()
    if args.mountpoint is None:
        if not devices:
            print("未发现可挂载的块设备。")
            return EXIT_OK
        rows = [
            [
                item.name,
                item.serial or UNKNOWN_TEXT,
                item.label or UNKNOWN_TEXT,
                item.fs_type or UNKNOWN_TEXT,
                str(item.mountpoint) if item.mountpoint is not None else UNKNOWN_TEXT,
            ]
            for item in devices
        ]
        print(_table(("设备", "序列号", "标签", "文件系统", "挂载点"), rows))
        return EXIT_OK
    target = Path(args.mountpoint).expanduser()
    found = _find_by_mountpoint(devices, target)
    if found is None:
        print(f"错误：未找到挂载点 {target} 对应的块设备", file=sys.stderr)
        return EXIT_FAILURE
    print(found.serial or f"设备 {found.name} 没有序列号")
    return EXIT_OK


# ---------------------------------------------------------------- 设备解析


def _resolve_target(cfg: config.Config, args) -> _Target | None:
    """--mountpoint 直接当 U 盘根；否则走 device.find_target。找不到返回 None。"""
    mountpoint = getattr(args, "mountpoint", None)
    wanted_serial = getattr(args, "device", None)
    if mountpoint:
        root = Path(mountpoint).expanduser()
        if not root.is_dir():
            print(f"错误：挂载点不存在或不是目录: {root}", file=sys.stderr)
            return None
        marker = device.read_marker(root, cfg.marker_file) or ""
        serial = wanted_serial or cfg.device_serial or marker or f"mountpoint:{root.as_posix()}"
        return _Target(
            root=root, serial=serial, mountpoint=root, we_mounted=False, marker=marker
        )
    effective = cfg if not wanted_serial else dataclasses.replace(cfg, device_serial=wanted_serial)
    found = device.find_target(effective)
    if found is None:
        print("错误：未找到目标 U 盘（序列号与标记文件都不匹配）", file=sys.stderr)
        return None
    info, mounted_at, we_mounted = found
    root = Path(str(mounted_at))
    marker = device.read_marker(root, cfg.marker_file) or ""
    serial = info.serial or cfg.device_serial or marker or "unknown"
    return _Target(
        root=root,
        serial=serial,
        mountpoint=root,
        we_mounted=we_mounted,
        name=info.name,
        label=info.label,
        fs_type=info.fs_type,
        marker=marker,
    )


def _release_target(target: _Target) -> None:
    if target.we_mounted:
        device.unmount(target.mountpoint)


# ---------------------------------------------------------------- scan


def cmd_scan(args) -> int:
    cfg = _load_config(args)
    target = _resolve_target(cfg, args)
    if target is None:
        return EXIT_NO_DEVICE
    try:
        identity = target.serial
        if target.name:
            identity = f"{target.name}（序列号 {target.serial}）"
        print(f"目标设备: {identity}，挂载点 {target.root}")
        usb = sync.scan(target.root, cfg.excludes)
        worktree = sync.scan_worktree(cfg.repo_root, cfg.excludes)
        plan = sync.plan(usb, worktree, cfg.repo_root)
        pending = list(plan.added) + list(plan.modified)
        print("（试运行：未写入任何文件，未更新索引）")
        print(
            f"新增 {len(plan.added)} 个，修改 {len(plan.modified)} 个，"
            f"删除 {len(plan.deleted)} 个"
        )
        print(f"将复制 {len(pending)} 个文件，共 {util.human_bytes(plan.changed_bytes())}")
        for state in pending[:PREVIEW_LIMIT]:
            print(f"  {state.relpath}  ({util.human_bytes(state.size)})")
        if len(pending) > PREVIEW_LIMIT:
            print(f"  …另有 {len(pending) - PREVIEW_LIMIT} 个待备份文件")
        return EXIT_OK
    finally:
        _release_target(target)


# ---------------------------------------------------------------- backup


def _marker_is_fresh(marker: Path, guard_seconds: int) -> bool:
    try:
        age = time.time() - Path(marker).stat().st_mtime
    except OSError:
        return False
    return age < max(int(guard_seconds), 0)


def _touch_marker(marker: Path, logger) -> None:
    try:
        Path(marker).parent.mkdir(parents=True, exist_ok=True)
        Path(marker).touch()
    except OSError as exc:
        logger.warning("更新节流标记失败 %s: %s", marker, exc)


def _original_path(path: str) -> str:
    """_deleted/<run_id>/<relpath> 还原成 <relpath>。"""
    parts = path.split("/", 2)
    if len(parts) == 3 and parts[2]:
        return parts[2]
    return path


def _dedupe(items: list) -> list:
    seen: set = set()
    result: list = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _classify(changes: list) -> tuple:
    """把 git 的 (状态, 路径) 清单拆成三栏；墓碑路径从新增改为归入删除。"""
    added: list = []
    modified: list = []
    deleted: list = []
    for status, path in changes:
        if path.startswith(DELETED_PREFIX):
            deleted.append(_original_path(path))
            continue
        if status == "D":
            deleted.append(path)
        elif status == "M":
            modified.append(path)
        else:
            added.append(path)
    return _dedupe(added), _dedupe(modified), _dedupe(deleted)


def _file_item(relpath: str, usb: dict, tree: dict) -> dict:
    state = usb.get(relpath)
    blob_sha, size = tree.get(relpath, (None, state.size if state is not None else None))
    return {
        "relpath": relpath,
        "size": size,
        "mtime_ns": state.mtime_ns if state is not None else None,
        "blob_sha": blob_sha,
    }


def _entry_item(relpath: str, usb: dict, tree: dict) -> dict:
    blob_sha, size = tree[relpath]
    state = usb.get(relpath)
    return {
        "relpath": relpath,
        "size": int(size),
        "mtime_ns": int(state.mtime_ns) if state is not None else 0,
        "blob_sha": blob_sha,
    }


def _deleted_item(relpath: str, previous: dict, raw_tree: dict, run_id: str) -> dict:
    old = previous.get(relpath) or {}
    tombstone = raw_tree.get(f"{DELETED_PREFIX}{run_id}/{relpath}")
    size = old.get("size")
    if size is None and tombstone is not None:
        size = tombstone[1]
    return {
        "relpath": relpath,
        "size": size,
        "mtime_ns": old.get("mtime_ns"),
        "blob_sha": tombstone[0] if tombstone is not None else old.get("blob_sha"),
    }


def _maybe_gc(cfg: config.Config, git: repo.GitRepo, logger) -> None:
    interval = int(cfg.gc_after_runs)
    if interval <= 0:
        return
    count = git.rev_count()
    if count > 0 and count % interval == 0:
        logger.info("已累计 %d 次提交，执行 git gc", count)
        git.gc()


def _execute_run(
    cfg: config.Config,
    target: _Target,
    plan: sync.Plan,
    store: index.Store,
    git: repo.GitRepo,
    prev_commit: str | None,
    usb: dict,
    logger,
) -> int:
    run_id = util.new_run_id()
    logger = util.setup_logger(cfg.log_dir, run_id)
    logger.info(
        "开始备份 %s：设备 %s，新增 %d 个 / 修改 %d 个 / 删除 %d 个",
        run_id,
        target.serial,
        len(plan.added),
        len(plan.modified),
        len(plan.deleted),
    )
    store.upsert_device(
        target.serial,
        label=target.label or None,
        marker=target.marker or None,
        fs_type=target.fs_type or None,
    )
    store.start_run(run_id, target.serial, util.utc_now_iso())
    watch = time.monotonic()
    try:
        stats = sync.mirror(plan, cfg.repo_root, run_id)
        git.add_all()
        message = (
            f"{RUN_TAG_PREFIX} {run_id}: "
            f"+{len(plan.added)} ~{len(plan.modified)} -{len(plan.deleted)}"
        )
        commit = git.commit(message)
        if commit is None:
            raise util.MyuError("镜像结束后工作区没有可提交的变更，本次运行记为失败")
        git.tag(f"{RUN_TAG_PREFIX}{run_id}")
        added, modified, deleted = _classify(git.diff_name_status(prev_commit, commit))
        raw_tree = git.tree_entries(commit)
        tree = {
            path: value
            for path, value in raw_tree.items()
            if not path.startswith(DELETED_PREFIX)
        }
        previous = {entry["relpath"]: entry for entry in store.list_entries(target.serial)}
        store.add_run_items(run_id, "added", [_file_item(path, usb, tree) for path in added])
        store.add_run_items(
            run_id, "modified", [_file_item(path, usb, tree) for path in modified]
        )
        store.add_run_items(
            run_id,
            "deleted",
            [_deleted_item(path, previous, raw_tree, run_id) for path in deleted],
        )
        store.replace_entries(
            target.serial, run_id, [_entry_item(path, usb, tree) for path in tree]
        )
        items = {kind: store.list_run_items(run_id, kind) for kind in index.KINDS}
        duration_ms = int((time.monotonic() - watch) * 1000)
        # 报告在 finish_run 之前渲染，耗时得先塞进 run 字典，否则会渲染成「—」。
        run = dict(store.get_run(run_id) or {})
        run["duration_ms"] = duration_ms
        report_path = report.write_report(
            cfg.reports_dir, run_id, report.build_markdown(run, items, stats)
        )
        store.finish_run(
            run_id,
            "completed",
            added=len(added),
            modified=len(modified),
            deleted=len(deleted),
            bytes_copied=stats.bytes_copied,
            bytes_total=plan.total_bytes(),
            duration_ms=duration_ms,
            git_commit=commit,
        )
        logger.info(
            "备份完成 %s：+%d ~%d -%d，复制 %s，耗时 %s",
            run_id,
            len(added),
            len(modified),
            len(deleted),
            util.human_bytes(stats.bytes_copied),
            _duration_text(duration_ms),
        )
        logger.info("报告: %s", report_path)
        _maybe_gc(cfg, git, logger)
        return EXIT_OK
    except (Exception, KeyboardInterrupt) as exc:
        duration_ms = int((time.monotonic() - watch) * 1000)
        logger.error("备份失败 %s: %s", run_id, exc)
        try:
            store.finish_run(
                run_id,
                "failed",
                bytes_total=plan.total_bytes(),
                duration_ms=duration_ms,
                error=str(exc) or exc.__class__.__name__,
            )
        except util.MyuError as record_error:
            logger.error("写入失败记录时出错: %s", record_error)
        return EXIT_FAILURE


def _backup_body(
    cfg: config.Config, target: _Target, store: index.Store, logger
) -> int:
    git = repo.GitRepo(cfg.repo_root)
    if not git.exists():
        logger.info("git 仓库尚未初始化，正在初始化: %s", cfg.repo_root)
        git.init(cfg.big_file_threshold)
    prev_run_id = store.last_completed_run_id(target.serial)
    prev_commit = git.rev_parse(f"{RUN_TAG_PREFIX}{prev_run_id}") if prev_run_id else None
    if prev_run_id and prev_commit is None:
        logger.warning("上次运行 %s 的 tag 不存在，本次按首次运行处理", prev_run_id)
    usb = sync.scan(target.root, cfg.excludes)
    worktree = sync.scan_worktree(cfg.repo_root, cfg.excludes)
    plan = sync.plan(usb, worktree, cfg.repo_root)
    if not (plan.added or plan.modified or plan.deleted):
        logger.info("无变化，跳过")
        return EXIT_OK
    return _execute_run(cfg, target, plan, store, git, prev_commit, usb, logger)


def _backup_locked(cfg: config.Config, args, logger) -> int:
    marker = Path(cfg.state_dir) / MARKER_NAME
    guard = int(cfg.poll_guard_seconds)
    if args.poll and _marker_is_fresh(marker, guard):
        logger.debug("距上次运行不足 %d 秒，--poll 直接跳过（不做任何扫描）", guard)
        return EXIT_OK
    target = _resolve_target(cfg, args)
    if target is None:
        return EXIT_NO_DEVICE
    store = None
    try:
        store = index.Store(cfg.db_path)
        return _backup_body(cfg, target, store, logger)
    except (Exception, KeyboardInterrupt) as exc:
        logger.error("备份失败: %s", exc)
        return EXIT_FAILURE
    finally:
        # 清理阶段不再抛出：节流标记与卸载必须无条件执行。
        try:
            if store is not None:
                store.close()
        except Exception:
            pass
        _touch_marker(marker, logger)
        if target.we_mounted:
            try:
                device.unmount(target.mountpoint)
            except util.MyuError as exc:
                logger.error("卸载 %s 失败: %s", target.mountpoint, exc)


def cmd_backup(args) -> int:
    cfg = _load_config(args)
    logger = util.setup_logger(cfg.log_dir)
    lock = util.FileLock(cfg.state_dir / LOCK_NAME)
    try:
        lock.__enter__()
    except util.MyuError as exc:
        logger.error("%s，本次不执行（退出码 %d）", exc, EXIT_LOCKED)
        return EXIT_LOCKED
    try:
        return _backup_locked(cfg, args, logger)
    finally:
        lock.__exit__(None, None, None)


# ---------------------------------------------------------------- status / runs / show


def cmd_status(args) -> int:
    cfg = _load_config(args)
    store = index.Store(cfg.db_path)
    try:
        run = store.latest_run()
    finally:
        store.close()
    if run is None:
        print("还没有任何备份运行记录。")
        return EXIT_OK
    report_path = Path(cfg.reports_dir) / f"{run['run_id']}.md"
    print("最近一次运行")
    print(f"  运行 ID: {run['run_id']}")
    print(f"  开始时间（UTC）: {run.get('started_at') or UNKNOWN_TEXT}")
    print(f"  结束时间（UTC）: {run.get('finished_at') or UNKNOWN_TEXT}")
    print(f"  设备: {run.get('device_serial') or UNKNOWN_TEXT}")
    print(f"  状态: {_status_text(run.get('status'))}")
    print(
        f"  变更: 新增 {run.get('added', 0)} / 修改 {run.get('modified', 0)}"
        f" / 删除 {run.get('deleted', 0)}"
    )
    print(
        f"  字节: 复制 {_size_text(run.get('bytes_copied'))}，"
        f"盘上共 {_size_text(run.get('bytes_total'))}"
    )
    print(f"  耗时: {_duration_text(run.get('duration_ms'))}")
    missing = "" if report_path.is_file() else "（不存在）"
    print(f"  报告: {report_path}{missing}")
    if run.get("error"):
        print(f"  错误: {run['error']}")
    return EXIT_OK


def cmd_runs(args) -> int:
    cfg = _load_config(args)
    limit = int(args.limit)
    if limit < 1:
        print("错误：--limit 必须是正整数", file=sys.stderr)
        return EXIT_USAGE
    store = index.Store(cfg.db_path)
    try:
        rows = store.list_runs(limit=limit)
    finally:
        store.close()
    if not rows:
        print("还没有任何备份运行记录。")
        return EXIT_OK
    body = [
        [
            run["run_id"],
            run.get("started_at") or UNKNOWN_TEXT,
            run.get("device_serial") or UNKNOWN_TEXT,
            _status_text(run.get("status")),
            f"+{run.get('added', 0)}",
            f"~{run.get('modified', 0)}",
            f"-{run.get('deleted', 0)}",
            _size_text(run.get("bytes_copied")),
            _duration_text(run.get("duration_ms")),
        ]
        for run in rows
    ]
    print(
        _table(
            ("运行 ID", "开始时间（UTC）", "设备", "状态", "新增", "修改", "删除", "复制", "耗时"),
            body,
        )
    )
    return EXIT_OK


def cmd_show(args) -> int:
    cfg = _load_config(args)
    run_id = _canonical_run_id(args.run_id)
    store = index.Store(cfg.db_path)
    try:
        run = store.get_run(run_id)
        items = {
            kind: store.list_run_items(run_id, kind) for kind in index.KINDS
        } if run is not None else {}
    finally:
        store.close()
    if run is None:
        print(f"错误：找不到运行记录 {args.run_id}", file=sys.stderr)
        return EXIT_FAILURE
    print(f"运行 {run_id}")
    print(f"  设备: {run.get('device_serial') or UNKNOWN_TEXT}")
    print(f"  开始时间（UTC）: {run.get('started_at') or UNKNOWN_TEXT}")
    print(f"  结束时间（UTC）: {run.get('finished_at') or UNKNOWN_TEXT}")
    print(f"  状态: {_status_text(run.get('status'))}")
    print(f"  耗时: {_duration_text(run.get('duration_ms'))}")
    print(f"  git commit: {run.get('git_commit') or UNKNOWN_TEXT}")
    if run.get("error"):
        print(f"  错误: {run['error']}")
    for kind, title in (("added", "新增"), ("modified", "修改"), ("deleted", "删除")):
        entries = items.get(kind, [])
        print("")
        print(f"{title}（{len(entries)} 个）")
        if not entries:
            print("  （无）")
            continue
        for entry in entries:
            print(f"  {entry['relpath']}  {_size_text(entry.get('size'))}")
    deleted_count = len(items.get("deleted", []))
    if deleted_count:
        print("")
        print(
            f"提示：要取回本次运行删掉的 {deleted_count} 个文件，"
            f"执行 myu restore {args.run_id} <目标目录> --deleted"
        )
    return EXIT_OK


# ---------------------------------------------------------------- restore


def cmd_restore(args) -> int:
    cfg = _load_config(args)
    store = index.Store(cfg.db_path)
    try:
        stats = restore.restore(
            cfg,
            store,
            args.run_id,
            Path(args.dest).expanduser(),
            subpath=args.path,
            preserve_mtime=not args.no_mtime,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            include_deleted=args.deleted,
        )
    finally:
        store.close()
    title = "试运行结果" if args.dry_run else "恢复完成"
    print(
        f"{title}：{stats.files} 个文件，{util.human_bytes(stats.bytes)}，"
        f"跳过 {stats.skipped} 个"
    )
    if args.deleted:
        print("已启用墓碑还原：本次运行删掉的文件按原路径一并计入上述结果")
    return EXIT_OK


# ---------------------------------------------------------------- web


def cmd_web(args) -> int:
    cfg = _load_config(args)
    if args.port is not None and not 1 <= args.port <= 65535:
        print(f"错误：--port 必须在 1-65535 之间: {args.port}", file=sys.stderr)
        return EXIT_USAGE
    effective = dataclasses.replace(
        cfg,
        web_bind=args.bind or cfg.web_bind,
        web_port=args.port if args.port is not None else cfg.web_port,
    )
    store = index.Store(cfg.db_path)
    try:
        return web.serve(effective, store)
    except OSError as exc:
        print(f"错误：网页服务启动失败: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        store.close()


# ---------------------------------------------------------------- set-password


def cmd_set_password(args) -> int:
    cfg = _load_config(args)
    for _attempt in range(PASSWORD_ATTEMPTS):
        first = getpass("请输入新口令: ")
        second = getpass("请再次输入新口令: ")
        if first != second:
            print("两次输入不一致，请重试。", file=sys.stderr)
            continue
        if len(first) < PASSWORD_MIN_LENGTH:
            print(f"口令至少需要 {PASSWORD_MIN_LENGTH} 位，请重试。", file=sys.stderr)
            continue
        config.save_config(dataclasses.replace(cfg, web_password_hash=util.hash_password(first)))
        print(f"口令已写入配置: {_config_path(args)}")
        return EXIT_OK
    print(f"连续 {PASSWORD_ATTEMPTS} 次输入无效，口令未修改。", file=sys.stderr)
    return EXIT_USAGE


# ---------------------------------------------------------------- gc


def cmd_gc(args) -> int:
    cfg = _load_config(args)
    git = repo.GitRepo(cfg.repo_root)
    if not git.exists():
        print(f"错误：备份仓库不存在 {cfg.repo_root}", file=sys.stderr)
        return EXIT_FAILURE
    before = git.rev_count()
    git.gc()
    after = git.rev_count()
    head = git.head() or ""
    store = index.Store(cfg.db_path)
    try:
        store.set_meta(GC_META_KEY, head)
    finally:
        store.close()
    print(f"git gc 完成：rev_count {before} -> {after}")
    print(f"HEAD: {head or UNKNOWN_TEXT}")
    return EXIT_OK


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myu", description="Backup-my-U：把 U 盘的增量备份归档进 git 仓库"
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="配置文件路径（默认取环境变量 MYU_CONFIG 或 /etc/myu/config.ini）",
    )
    parser.add_argument("--version", action="version", version=f"myu {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="子命令")

    parser_init = subparsers.add_parser("init", help="建目录、初始化 git 仓库并生成配置模板")
    parser_init.add_argument("--repo-root", metavar="P", default=None)
    parser_init.add_argument("--state-dir", metavar="P", default=None)
    parser_init.add_argument("--log-dir", metavar="P", default=None)
    parser_init.set_defaults(handler=cmd_init)

    parser_device = subparsers.add_parser("device-id", help="列出块设备，或查询挂载点的序列号")
    parser_device.add_argument("mountpoint", nargs="?", default=None, metavar="MOUNTPOINT")
    parser_device.set_defaults(handler=cmd_device_id)

    parser_scan = subparsers.add_parser("scan", help="只比对不写盘，打印将要备份的文件")
    parser_scan.add_argument("--device", metavar="SERIAL", default=None)
    parser_scan.set_defaults(handler=cmd_scan)

    parser_backup = subparsers.add_parser("backup", help="执行一次增量备份")
    parser_backup.add_argument("--device", metavar="SERIAL", default=None)
    parser_backup.add_argument("--mountpoint", metavar="P", default=None)
    parser_backup.add_argument("--poll", action="store_true")
    parser_backup.set_defaults(handler=cmd_backup)

    subparsers.add_parser("status", help="查看最近一次运行").set_defaults(handler=cmd_status)

    parser_runs = subparsers.add_parser("runs", help="查看运行历史")
    parser_runs.add_argument("--limit", type=int, default=50, metavar="N")
    parser_runs.set_defaults(handler=cmd_runs)

    parser_show = subparsers.add_parser("show", help="查看某次运行的三栏明细")
    parser_show.add_argument("run_id", metavar="RUN_ID")
    parser_show.set_defaults(handler=cmd_show)

    parser_restore = subparsers.add_parser("restore", help="把某次备份恢复到指定目录")
    parser_restore.add_argument("run_id", metavar="RUN_ID")
    parser_restore.add_argument("dest", metavar="DEST")
    parser_restore.add_argument("--path", metavar="SUB", default=None)
    parser_restore.add_argument("--no-mtime", action="store_true")
    parser_restore.add_argument("--overwrite", action="store_true")
    parser_restore.add_argument("--dry-run", action="store_true")
    parser_restore.add_argument("--deleted", action="store_true")
    parser_restore.set_defaults(handler=cmd_restore)

    parser_web = subparsers.add_parser("web", help="启动只读网页服务")
    parser_web.add_argument("--port", type=int, default=None, metavar="N")
    parser_web.add_argument("--bind", metavar="ADDR", default=None)
    parser_web.set_defaults(handler=cmd_web)

    subparsers.add_parser("set-password", help="设置网页口令").set_defaults(
        handler=cmd_set_password
    )

    subparsers.add_parser("gc", help="手动执行 git gc 并记录 HEAD").set_defaults(handler=cmd_gc)
    return parser


def _exit_code(code: object) -> int:
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return int(code)
    return EXIT_USAGE


def main(argv: list | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc.code)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(handler(args))
    except util.MyuError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FAILURE
