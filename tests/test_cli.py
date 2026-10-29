from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from myu import cli, device, index, repo, util, web
from myu.config import load_config

SERIAL = "USB-SERIAL-01"
PASSWORD = "correct-horse-battery"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _remove_tree(root: Path) -> None:
    """git 的 pack 文件在 Windows 上是只读的，直接 rmtree 会失败。"""

    def handle(func, target, _exc):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    shutil.rmtree(str(root), onerror=handle)


def make_temp_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="myu-cli-"))


def reset_logging() -> None:
    logger = logging.getLogger("myu")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def run_cli(config_path: Path, *argv) -> tuple:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--config", str(config_path), *argv])
    return code, out.getvalue(), err.getvalue()


def run_cli_raw(*argv) -> tuple:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write_config(
    path: Path,
    root: Path,
    serial: str = SERIAL,
    general_extra: str = "",
    password_hash: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[general]\n"
        f"repo_root = {(root / 'repo').as_posix()}\n"
        f"state_dir = {(root / 'state').as_posix()}\n"
        f"log_dir = {(root / 'logs').as_posix()}\n"
        "exclude = *.tmp\n"
        f"{general_extra}"
        "\n"
        "[device]\n"
        f"serial = {serial}\n"
        "marker_file = .myu-id\n"
        "marker_value =\n"
        "\n"
        "[git]\n"
        "big_file_threshold = 512m\n"
        "gc_after_runs = 20\n"
        "\n"
        "[web]\n"
        "bind = 127.0.0.1\n"
        "port = 8787\n"
        f"password_hash = {password_hash}\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def snapshot(root: Path) -> set:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def settle_second() -> None:
    """run_id 只有秒精度：等到下一个整秒，避免两次运行撞同一个 run_id。"""
    time.sleep(max(0.0, 1.0 - (time.time() % 1.0)) + 0.02)


def write_files(base: Path, files: dict) -> None:
    for relpath, data in files.items():
        path = base / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class CliTestCase(unittest.TestCase):
    """每个用例一个临时根目录：配置、伪 U 盘、仓库、状态目录都在里面。"""

    root: Path
    usb: Path
    config_path: Path
    repo_root: Path
    state_dir: Path
    logs_dir: Path
    marker: Path

    def setUp(self) -> None:
        self.root = make_temp_root()
        self.addCleanup(_remove_tree, self.root)
        self.usb = self.root / "usb"
        self.usb.mkdir()
        self.config_path = write_config(self.root / "config.ini", self.root)
        self.repo_root = self.root / "repo"
        self.state_dir = self.root / "state"
        self.logs_dir = self.root / "logs"
        self.marker = self.state_dir / "last-run.marker"

    def tearDown(self) -> None:
        reset_logging()

    def cli(self, *argv) -> tuple:
        return run_cli(self.config_path, *argv)

    def backup(self, *extra) -> tuple:
        return self.cli("backup", "--mountpoint", str(self.usb), *extra)

    def open_store(self) -> index.Store:
        store = index.Store(self.state_dir / "myu.db")
        self.addCleanup(store.close)
        return store

    def fake_device(self) -> device.DeviceInfo:
        return device.DeviceInfo(
            name="sdb1",
            serial=SERIAL,
            label="测试盘",
            fs_type="vfat",
            uuid="ABCD-1234",
            mountpoint=self.usb,
            size=16 * 1024 ** 3,
        )


# --------------------------------------------------------------------- 入口


class EntranceTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_logging()

    def test_version_flag(self) -> None:
        code, out, _err = run_cli_raw("--version")
        self.assertEqual(code, 0)
        self.assertIn("myu 0.1.0", out)

    def test_no_subcommand_is_usage_error(self) -> None:
        code, out, _err = run_cli_raw()
        self.assertEqual(code, 2)
        self.assertIn("子命令", out)

    def test_unknown_subcommand_is_usage_error(self) -> None:
        code, _out, _err = run_cli_raw("explode")
        self.assertEqual(code, 2)

    def test_missing_config_returns_failure(self) -> None:
        root = make_temp_root()
        self.addCleanup(_remove_tree, root)
        code, _out, err = run_cli(root / "absent.ini", "status")
        self.assertEqual(code, 1)
        self.assertIn("配置文件不存在", err)

    def test_module_entry_point(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "myu", "--version"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("myu 0.1.0", completed.stdout)


# --------------------------------------------------------------------- init


class InitTest(CliTestCase):
    def fresh_config(self) -> Path:
        return self.root / "etc" / "myu" / "config.ini"

    def init(self, *extra) -> tuple:
        return run_cli(
            self.fresh_config(),
            "init",
            "--repo-root",
            str(self.repo_root),
            "--state-dir",
            str(self.state_dir),
            "--log-dir",
            str(self.logs_dir),
            *extra,
        )

    def test_init_creates_template_dirs_and_repo(self) -> None:
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertTrue(self.fresh_config().is_file())
        self.assertIn("已生成配置模板", out)
        for directory in (self.repo_root, self.state_dir, self.logs_dir, self.state_dir / "reports"):
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir())
        self.assertTrue(repo.GitRepo(self.repo_root).exists())
        self.assertTrue((self.repo_root / ".git" / "config").is_file())

    def test_init_is_idempotent_and_keeps_existing_config(self) -> None:
        self.assertEqual(self.init()[0], 0)
        first_text = self.fresh_config().read_text(encoding="utf-8")
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.fresh_config().read_text(encoding="utf-8"), first_text)

        # 手工改一个键，再跑一次 init：内容必须原样保留（不被模板覆盖）
        edited = "".join(
            "gc_after_runs      = 7\n" if line.startswith("gc_after_runs") else line
            for line in first_text.splitlines(keepends=True)
        )
        self.assertNotEqual(edited, first_text)
        self.fresh_config().write_text(edited, encoding="utf-8", newline="\n")
        (self.repo_root / "keep.txt").write_bytes(b"keep")

        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertIn("配置文件已存在", out)
        self.assertEqual(self.fresh_config().read_text(encoding="utf-8"), edited)
        self.assertEqual((self.repo_root / "keep.txt").read_bytes(), b"keep")

    def test_init_warns_when_arguments_conflict_with_file(self) -> None:
        self.assertEqual(self.init()[0], 0)
        before = self.fresh_config().read_text(encoding="utf-8")
        other_root = self.root / "other-repo"
        other_state = self.root / "other-state"
        other_logs = self.root / "other-logs"
        code, _out, err = run_cli(
            self.fresh_config(),
            "init",
            "--repo-root",
            str(other_root),
            "--state-dir",
            str(other_state),
            "--log-dir",
            str(other_logs),
        )
        self.assertEqual(code, 0)
        self.assertIn("警告", err)
        self.assertIn("以配置文件为准", err)
        self.assertEqual(self.fresh_config().read_text(encoding="utf-8"), before)
        self.assertFalse(other_root.exists())
        self.assertFalse(other_state.exists())
        self.assertFalse(other_logs.exists())


# ---------------------------------------------------------------- device-id


class DeviceIdTest(CliTestCase):
    def test_lists_all_block_devices(self) -> None:
        devices = [
            self.fake_device(),
            device.DeviceInfo(
                name="sdc1",
                serial="",
                label="",
                fs_type="ext4",
                uuid="U-2",
                mountpoint=None,
                size=10,
            ),
        ]
        with mock.patch.object(device, "list_block_devices", return_value=devices):
            code, out, err = run_cli_raw("device-id")
        self.assertEqual(code, 0, err)
        self.assertIn("sdb1", out)
        self.assertIn(SERIAL, out)
        self.assertIn("sdc1", out)
        self.assertIn("挂载点", out)

    def test_prints_serial_for_mountpoint(self) -> None:
        with mock.patch.object(device, "list_block_devices", return_value=[self.fake_device()]):
            code, out, err = run_cli_raw("device-id", str(self.usb))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), SERIAL)

    def test_empty_device_list(self) -> None:
        with mock.patch.object(device, "list_block_devices", return_value=[]):
            code, out, err = run_cli_raw("device-id")
        self.assertEqual(code, 0, err)
        self.assertIn("未发现可挂载的块设备", out)

    def test_unknown_mountpoint_returns_failure(self) -> None:
        with mock.patch.object(device, "list_block_devices", return_value=[self.fake_device()]):
            code, _out, err = run_cli_raw("device-id", str(self.root / "not-mounted"))
        self.assertEqual(code, 1)
        self.assertIn("未找到挂载点", err)

    def test_missing_lsblk_command_returns_failure(self) -> None:
        with mock.patch.object(
            device, "list_block_devices", side_effect=util.MyuError("读取块设备列表失败：系统里找不到命令 lsblk")
        ):
            code, _out, err = run_cli_raw("device-id")
        self.assertEqual(code, 1)
        self.assertIn("错误：", err)
        self.assertIn("lsblk", err)


# --------------------------------------------------------------------- scan


class ScanTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        write_files(
            self.usb,
            {
                "a.txt": b"aaa",
                "sub/b.txt": b"bbb",
                "c 中文.txt": "中文内容".encode("utf-8"),
                "skip.tmp": b"excluded",
            },
        )

    def scan(self, *extra) -> tuple:
        found = (self.fake_device(), self.usb, False)
        with mock.patch.object(device, "find_target", return_value=found):
            return self.cli("scan", *extra)

    def test_scan_reports_plan_and_writes_nothing(self) -> None:
        before = snapshot(self.root)
        code, out, err = self.scan()
        after = snapshot(self.root)
        self.assertEqual(code, 0, err)
        self.assertEqual(after, before)
        self.assertIn("试运行：未写入任何文件", out)
        self.assertIn("新增 3 个，修改 0 个，删除 0 个", out)
        self.assertIn("将复制 3 个文件", out)
        for relpath in ("a.txt", "sub/b.txt", "c 中文.txt"):
            self.assertIn(relpath, out)
        self.assertNotIn("skip.tmp", out)
        self.assertFalse((self.root / "repo").exists())
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.logs_dir.exists())
        self.assertFalse(any(p.suffix == ".db" for p in self.root.rglob("*")))

    def test_scan_preview_stops_at_twenty_files(self) -> None:
        extra = {f"many/{index_name:02d}.txt" for index_name in range(23)}
        write_files(self.usb, {relpath: b"x" for relpath in extra})
        code, out, err = self.scan()
        self.assertEqual(code, 0, err)
        self.assertIn("新增 26 个", out)
        self.assertIn("…另有 6 个待备份文件", out)
        preview = [
            line
            for line in out.splitlines()
            if line.startswith("  ") and not line.startswith("  …")
        ]
        self.assertEqual(len(preview), 20)

    def test_scan_without_target_device_exits_3(self) -> None:
        with mock.patch.object(device, "find_target", return_value=None):
            code, _out, err = self.cli("scan")
        self.assertEqual(code, 3)
        self.assertIn("未找到目标 U 盘", err)

    def test_scan_accepts_device_override(self) -> None:
        captured = {}

        def fake_find(cfg):
            captured["serial"] = cfg.device_serial
            return (self.fake_device(), self.usb, False)

        with mock.patch.object(device, "find_target", side_effect=fake_find):
            code, _out, err = self.cli("scan", "--device", "OVERRIDE-9")
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["serial"], "OVERRIDE-9")


# ------------------------------------------------------- backup 端到端三次


class BackupCycleTest(unittest.TestCase):
    """三次有变化的备份 + 一次无变化备份，全部走真 git / 真 SQLite。"""

    root: Path
    usb: Path
    config_path: Path
    repo_root: Path
    state_dir: Path
    marker: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = make_temp_root()
        cls.addClassCleanup(_remove_tree, cls.root)
        cls.addClassCleanup(reset_logging)
        cls.usb = cls.root / "usb"
        cls.usb.mkdir()
        cls.config_path = write_config(cls.root / "config.ini", cls.root)
        cls.repo_root = cls.root / "repo"
        cls.state_dir = cls.root / "state"
        cls.marker = cls.state_dir / "last-run.marker"
        cls.initial = {
            "a.txt": b"aaa",
            "sub/b.txt": b"bbb",
            "c 中文.txt": "中文内容".encode("utf-8"),
        }
        write_files(cls.usb, cls.initial)

        cls.results = {}
        cls.results["first"] = run_cli(
            cls.config_path, "backup", "--mountpoint", str(cls.usb)
        )
        # 改内容（长度不变 -> 走 filecmp 判据）并把 mtime 顶到未来
        changed = cls.usb / "a.txt"
        changed.write_bytes(b"zzz")
        stamp = time.time() + 120
        os.utime(changed, (stamp, stamp))
        settle_second()
        cls.results["second"] = run_cli(
            cls.config_path, "backup", "--mountpoint", str(cls.usb)
        )
        (cls.usb / "sub" / "b.txt").unlink()
        settle_second()
        cls.results["third"] = run_cli(
            cls.config_path, "backup", "--mountpoint", str(cls.usb)
        )
        settle_second()
        cls.results["unchanged"] = run_cli(
            cls.config_path, "backup", "--mountpoint", str(cls.usb)
        )

        store = index.Store(cls.state_dir / "myu.db")
        try:
            cls.runs = store.list_runs(limit=50)
            cls.items = {
                run["run_id"]: {
                    kind: store.list_run_items(run["run_id"], kind) for kind in index.KINDS
                }
                for run in cls.runs
            }
            cls.entries = store.list_entries(SERIAL)
            cls.gc_meta = store.get_meta("last_gc_commit")
        finally:
            store.close()
        cls.chronological = list(reversed(cls.runs))

    def tearDown(self) -> None:
        reset_logging()

    def relpaths(self, run_id: str, kind: str) -> list:
        return sorted(item["relpath"] for item in self.items[run_id][kind])

    # -- 三次运行的三栏计数 ------------------------------------------------

    def test_first_run_records_three_added(self) -> None:
        code, _out, err = self.results["first"]
        self.assertEqual(code, 0, err)
        first = self.chronological[0]
        self.assertEqual(first["status"], "completed")
        self.assertEqual((first["added"], first["modified"], first["deleted"]), (3, 0, 0))
        self.assertEqual(self.relpaths(first["run_id"], "added"), sorted(self.initial))
        self.assertEqual(self.relpaths(first["run_id"], "modified"), [])
        self.assertEqual(self.relpaths(first["run_id"], "deleted"), [])
        self.assertEqual(first["bytes_copied"], sum(len(data) for data in self.initial.values()))
        sizes = {
            item["relpath"]: item["size"]
            for item in self.items[first["run_id"]]["added"]
        }
        self.assertEqual(sizes, {relpath: len(data) for relpath, data in self.initial.items()})

    def test_second_run_records_exactly_one_modified(self) -> None:
        code, _out, err = self.results["second"]
        self.assertEqual(code, 0, err)
        second = self.chronological[1]
        self.assertEqual(second["status"], "completed")
        self.assertEqual((second["added"], second["modified"], second["deleted"]), (0, 1, 0))
        self.assertEqual(self.relpaths(second["run_id"], "modified"), ["a.txt"])
        self.assertEqual(self.relpaths(second["run_id"], "added"), [])
        self.assertEqual(self.relpaths(second["run_id"], "deleted"), [])
        self.assertEqual((self.repo_root / "a.txt").read_bytes(), b"zzz")

    def test_third_run_records_exactly_one_deleted_with_tombstone(self) -> None:
        code, _out, err = self.results["third"]
        self.assertEqual(code, 0, err)
        third = self.chronological[2]
        self.assertEqual(third["status"], "completed")
        self.assertEqual((third["added"], third["modified"], third["deleted"]), (0, 0, 1))
        self.assertEqual(self.relpaths(third["run_id"], "deleted"), ["sub/b.txt"])
        self.assertEqual(self.relpaths(third["run_id"], "added"), [])
        self.assertEqual(self.relpaths(third["run_id"], "modified"), [])
        self.assertFalse((self.repo_root / "sub" / "b.txt").exists())
        tombstone = self.repo_root / "_deleted" / third["run_id"] / "sub" / "b.txt"
        self.assertTrue(tombstone.is_file())
        self.assertEqual(tombstone.read_bytes(), b"bbb")

    def test_runs_table_holds_exactly_three_completed_rows(self) -> None:
        self.assertEqual(len(self.runs), 3)
        self.assertEqual([run["status"] for run in self.runs], ["completed"] * 3)
        self.assertEqual(
            [(run["added"], run["modified"], run["deleted"]) for run in self.chronological],
            [(3, 0, 0), (0, 1, 0), (0, 0, 1)],
        )
        self.assertEqual(sorted(entry["relpath"] for entry in self.entries), ["a.txt", "c 中文.txt"])

    def test_unchanged_run_does_not_add_a_row(self) -> None:
        code, _out, err = self.results["unchanged"]
        self.assertEqual(code, 0, err)
        self.assertIn("无变化，跳过", err)
        self.assertEqual(len(self.runs), 3)
        self.assertTrue(self.marker.is_file())

    # -- 报告、状态、历史、明细 --------------------------------------------

    def test_reports_are_written_for_each_run(self) -> None:
        for run in self.chronological:
            report_path = self.state_dir / "reports" / f"{run['run_id']}.md"
            with self.subTest(run_id=run["run_id"]):
                self.assertTrue(report_path.is_file())
                text = report_path.read_text(encoding="utf-8")
                self.assertIn(f"# 备份运行 {run['run_id']}", text)
                self.assertIn("## 概览", text)
                self.assertIn("## 删除", text)

    def test_status_shows_latest_run_and_report_path(self) -> None:
        code, out, err = run_cli(self.config_path, "status")
        self.assertEqual(code, 0, err)
        latest = self.chronological[-1]
        self.assertIn(latest["run_id"], out)
        self.assertIn("完成", out)
        self.assertIn("报告", out)
        self.assertIn(f"{latest['run_id']}.md", out)
        self.assertIn("删除 1", out)

    def test_runs_lists_history(self) -> None:
        code, out, err = run_cli(self.config_path, "runs")
        self.assertEqual(code, 0, err)
        for run in self.chronological:
            self.assertIn(run["run_id"], out)
        self.assertIn("运行 ID", out)
        self.assertIn("+3", out)

    def test_runs_limit_and_usage_error(self) -> None:
        code, out, err = run_cli(self.config_path, "runs", "--limit", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("20")]), 2)
        code, _out, err = run_cli(self.config_path, "runs", "--limit", "0")
        self.assertEqual(code, 2)
        self.assertIn("--limit", err)

    def test_show_prints_three_columns(self) -> None:
        third = self.chronological[2]
        code, out, err = run_cli(self.config_path, "show", third["run_id"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"运行 {third['run_id']}", out)
        self.assertIn("新增（0 个）", out)
        self.assertIn("修改（0 个）", out)
        self.assertIn("删除（1 个）", out)
        self.assertIn("sub/b.txt", out)

    def test_show_accepts_tag_prefixed_id(self) -> None:
        first = self.chronological[0]
        code, out, err = run_cli(self.config_path, "show", f"run-{first['run_id']}")
        self.assertEqual(code, 0, err)
        self.assertIn("新增（3 个）", out)

    def test_show_unknown_run_returns_failure(self) -> None:
        code, _out, err = run_cli(self.config_path, "show", "20200101T000000Z")
        self.assertEqual(code, 1)
        self.assertIn("找不到运行记录", err)

    def test_show_hints_deleted_restore_when_run_deleted_files(self) -> None:
        third = self.chronological[2]
        code, out, err = run_cli(self.config_path, "show", third["run_id"])
        self.assertEqual(code, 0, err)
        self.assertIn(
            f"提示：要取回本次运行删掉的 1 个文件，"
            f"执行 myu restore {third['run_id']} <目标目录> --deleted",
            out,
        )
        self.assertTrue(out.rstrip().endswith("--deleted"))

    def test_show_hint_echoes_run_id_as_typed(self) -> None:
        third = self.chronological[2]
        code, out, err = run_cli(self.config_path, "show", f"run-{third['run_id']}")
        self.assertEqual(code, 0, err)
        self.assertIn(f"myu restore run-{third['run_id']} <目标目录> --deleted", out)

    def test_show_without_deletions_has_no_hint(self) -> None:
        first = self.chronological[0]
        code, out, err = run_cli(self.config_path, "show", first["run_id"])
        self.assertEqual(code, 0, err)
        self.assertIn("删除（0 个）", out)
        self.assertNotIn("提示：", out)

    # -- 恢复 --------------------------------------------------------------

    def test_restore_third_run(self) -> None:
        third = self.chronological[2]
        dest = self.root / "restored"
        code, out, err = run_cli(self.config_path, "restore", third["run_id"], str(dest))
        self.assertEqual(code, 0, err)
        self.assertIn("恢复完成", out)
        self.assertIn("2 个文件", out)
        names = sorted(path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file())
        self.assertEqual(names, ["a.txt", "c 中文.txt"])
        self.assertEqual((dest / "a.txt").read_bytes(), b"zzz")
        self.assertEqual((dest / "c 中文.txt").read_bytes(), "中文内容".encode("utf-8"))
        self.assertFalse((dest / "_deleted").exists())
        self.assertFalse((dest / "sub" / "b.txt").exists())

    def test_restore_skips_existing_without_overwrite(self) -> None:
        third = self.chronological[2]
        dest = self.root / "restored-skip"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"locally-edited")
        code, out, err = run_cli(self.config_path, "restore", third["run_id"], str(dest))
        self.assertEqual(code, 0, err)
        self.assertIn("跳过 1 个", out)
        self.assertEqual((dest / "a.txt").read_bytes(), b"locally-edited")
        code, out, err = run_cli(
            self.config_path, "restore", third["run_id"], str(dest), "--overwrite"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual((dest / "a.txt").read_bytes(), b"zzz")

    def test_restore_dry_run_creates_nothing(self) -> None:
        third = self.chronological[2]
        dest = self.root / "restored-dry"
        code, out, err = run_cli(
            self.config_path, "restore", third["run_id"], str(dest), "--dry-run"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("试运行结果", out)
        self.assertFalse(dest.exists())

    def test_restore_unknown_run_returns_failure(self) -> None:
        code, _out, err = run_cli(
            self.config_path, "restore", "20200101T000000Z", str(self.root / "nowhere")
        )
        self.assertEqual(code, 1)
        self.assertIn("找不到备份运行", err)

    def test_restore_deleted_brings_tombstone_back(self) -> None:
        third = self.chronological[2]
        kept = self.root / "restored-deleted"
        code, out, err = run_cli(
            self.config_path, "restore", third["run_id"], str(kept), "--deleted"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("恢复完成", out)
        self.assertIn("已启用墓碑还原", out)
        self.assertIn("3 个文件", out)
        self.assertEqual((kept / "sub" / "b.txt").read_bytes(), b"bbb")
        # 墓碑落位即「原路径」：_deleted/<run_id>/ 这一层不能跟着到 dest 里
        self.assertFalse((kept / "_deleted").exists())

        plain = self.root / "restored-no-flag"
        code, out, err = run_cli(self.config_path, "restore", third["run_id"], str(plain))
        self.assertEqual(code, 0, err)
        self.assertNotIn("已启用墓碑还原", out)
        self.assertIn("2 个文件", out)
        self.assertFalse((plain / "sub" / "b.txt").exists())

    def test_restore_deleted_with_dry_run_counts_without_writing(self) -> None:
        third = self.chronological[2]
        dest = self.root / "restored-deleted-dry"
        code, out, err = run_cli(
            self.config_path, "restore", third["run_id"], str(dest), "--deleted", "--dry-run"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("试运行结果", out)
        self.assertIn("已启用墓碑还原", out)
        # 2 个活文件 + 1 个墓碑：试运行也必须把墓碑算进计数
        self.assertIn("3 个文件", out)
        self.assertFalse(dest.exists())

    def test_restore_deleted_passes_subpath_through(self) -> None:
        third = self.chronological[2]
        dest = self.root / "restored-deleted-sub"
        code, out, err = run_cli(
            self.config_path,
            "restore",
            third["run_id"],
            str(dest),
            "--path",
            "sub",
            "--deleted",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("已启用墓碑还原", out)
        # 第三次运行里活文件已无 sub/，subpath 只命中墓碑
        self.assertIn("1 个文件", out)
        names = sorted(
            path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()
        )
        self.assertEqual(names, ["b.txt"])
        self.assertEqual((dest / "b.txt").read_bytes(), b"bbb")

    # -- gc ----------------------------------------------------------------

    def test_gc_records_head_and_keeps_history(self) -> None:
        git = repo.GitRepo(self.repo_root)
        head = git.head()
        before = git.rev_count()
        code, out, err = run_cli(self.config_path, "gc")
        self.assertEqual(code, 0, err)
        self.assertIn(f"rev_count {before} -> {before}", out)
        self.assertEqual(git.rev_count(), before)
        store = index.Store(self.state_dir / "myu.db")
        try:
            self.assertEqual(store.get_meta("last_gc_commit"), head)
        finally:
            store.close()
        self.assertTrue(git.fsck())


# ------------------------------------------------------------ --poll 节流


class PollGuardTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        write_files(self.usb, {"a.txt": b"aaa"})

    def first_backup(self) -> None:
        code, _out, err = self.backup()
        self.assertEqual(code, 0, err)
        self.assertTrue(self.marker.is_file())
        self.assertEqual(len(self.open_store().list_runs()), 1)

    def age_marker(self, seconds: int) -> None:
        stamp = time.time() - seconds
        os.utime(self.marker, (stamp, stamp))

    def test_fresh_marker_skips_without_scanning(self) -> None:
        self.first_backup()
        code, _out, err = self.backup("--poll")
        self.assertEqual(code, 0, err)
        self.assertNotIn("无变化", err)
        self.assertEqual(len(self.open_store().list_runs()), 1)

    def test_fresh_marker_logs_one_debug_line(self) -> None:
        self.first_backup()
        original = util.setup_logger

        def verbose_logger(log_dir, run_id=None, verbose=False):
            returned = original(log_dir, run_id, verbose)
            returned.setLevel(logging.DEBUG)
            return returned

        with mock.patch.object(util, "setup_logger", side_effect=verbose_logger):
            code, _out, err = self.backup("--poll")
        self.assertEqual(code, 0, err)
        debug_lines = [line for line in err.splitlines() if " DEBUG " in line]
        self.assertEqual(len(debug_lines), 1)
        self.assertIn("--poll 直接跳过", debug_lines[0])

    def test_stale_marker_really_executes(self) -> None:
        self.first_backup()
        self.age_marker(7200)
        code, _out, err = self.backup("--poll")
        self.assertEqual(code, 0, err)
        self.assertIn("无变化，跳过", err)
        self.assertEqual(len(self.open_store().list_runs()), 1)

    def test_stale_marker_executes_real_backup(self) -> None:
        self.first_backup()
        self.age_marker(7200)
        write_files(self.usb, {"b.txt": b"bbb"})
        settle_second()
        code, _out, err = self.backup("--poll")
        self.assertEqual(code, 0, err)
        runs = self.open_store().list_runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["added"], 1)
        self.assertEqual(runs[0]["status"], "completed")

    def test_guard_window_is_configurable(self) -> None:
        self.config_path = write_config(
            self.root / "config.ini", self.root, general_extra="poll_guard_seconds = 10\n"
        )
        self.first_backup()
        self.assertEqual(load_config(self.config_path).poll_guard_seconds, 10)
        # 5 秒前 -> 仍在 10 秒窗口内，跳过
        self.age_marker(5)
        code, _out, err = self.backup("--poll")
        self.assertEqual(code, 0, err)
        self.assertNotIn("无变化", err)
        self.assertEqual(len(self.open_store().list_runs()), 1)
        # 30 秒前 -> 超出窗口，真的执行
        self.age_marker(30)
        code, _out, err = self.backup("--poll")
        self.assertEqual(code, 0, err)
        self.assertIn("无变化，跳过", err)
        self.assertEqual(len(self.open_store().list_runs()), 1)


# ------------------------------------------------------------- 锁与卸载


class LockTest(CliTestCase):
    def test_backup_exits_75_when_lock_is_held(self) -> None:
        write_files(self.usb, {"a.txt": b"aaa"})
        holder = util.FileLock(self.state_dir / "myu.lock")
        holder.__enter__()
        self.addCleanup(holder.__exit__, None, None, None)
        code, _out, err = self.backup()
        self.assertEqual(code, 75)
        self.assertIn("另一个备份进程正在运行", err)
        self.assertEqual(self.open_store().list_runs(), [])
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.repo_root.exists())


class MountLifecycleTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        write_files(self.usb, {"a.txt": b"aaa"})

    def test_mountpoint_option_skips_device_probing(self) -> None:
        with mock.patch.object(
            device, "find_target", side_effect=AssertionError("--mountpoint 不得探测设备")
        ):
            code, _out, err = self.backup()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.open_store().list_runs()), 1)

    def test_mountpoint_option_never_mounts_or_unmounts(self) -> None:
        with mock.patch.object(device, "unmount") as unmount:
            code, _out, err = self.backup()
        self.assertEqual(code, 0, err)
        unmount.assert_not_called()

    def test_unmount_runs_when_this_process_mounted(self) -> None:
        found = (self.fake_device(), self.usb, True)
        with mock.patch.object(device, "find_target", return_value=found), mock.patch.object(
            device, "unmount"
        ) as unmount:
            code, _out, err = self.cli("backup")
        self.assertEqual(code, 0, err)
        unmount.assert_called_once_with(self.usb)
        self.assertEqual(len(self.open_store().list_runs()), 1)

    def test_unmount_runs_even_when_backup_fails(self) -> None:
        self.repo_root.write_bytes(b"not a directory")
        found = (self.fake_device(), self.usb, True)
        with mock.patch.object(device, "find_target", return_value=found), mock.patch.object(
            device, "unmount"
        ) as unmount:
            code, _out, err = self.cli("backup")
        self.assertEqual(code, 1)
        unmount.assert_called_once_with(self.usb)
        self.assertTrue(self.marker.is_file())
        self.assertIn("备份失败", err)


class BackupFailureTest(CliTestCase):
    def test_mirror_failure_records_failed_run(self) -> None:
        write_files(self.usb, {"ok.txt": b"ok"})
        code, _out, err = self.backup()
        self.assertEqual(code, 0, err)
        # 让 _deleted 之外的镜像目标变成目录，逼 copy_file_atomic 失败
        (self.repo_root / "bad.txt").mkdir()
        write_files(self.usb, {"bad.txt": b"payload"})
        settle_second()
        code, _out, err = self.backup()
        self.assertEqual(code, 1)
        self.assertIn("备份失败", err)
        store = self.open_store()
        runs = store.list_runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertTrue(runs[0]["error"])
        self.assertEqual(runs[1]["status"], "completed")
        self.assertIn("bad.txt", runs[0]["error"])
        self.assertEqual(store.get_run(runs[0]["run_id"])["added"], 0)


class BackupNoDeviceTest(CliTestCase):
    def test_backup_without_target_device_exits_3(self) -> None:
        with mock.patch.object(device, "find_target", return_value=None):
            code, _out, err = self.cli("backup")
        self.assertEqual(code, 3)
        self.assertIn("未找到目标 U 盘", err)
        self.assertFalse((self.state_dir / "myu.db").exists())
        self.assertFalse(self.marker.exists())

    def test_backup_with_missing_mountpoint_exits_3(self) -> None:
        code, _out, err = self.cli("backup", "--mountpoint", str(self.root / "absent"))
        self.assertEqual(code, 3)
        self.assertIn("挂载点不存在", err)

    def test_backup_creates_git_repo_when_missing(self) -> None:
        write_files(self.usb, {"a.txt": b"aaa"})
        self.assertFalse((self.repo_root / ".git").exists())
        code, _out, err = self.backup()
        self.assertEqual(code, 0, err)
        self.assertTrue((self.repo_root / ".git").exists())
        self.assertIn("git 仓库尚未初始化", err)


# --------------------------------------------------------------- 空库行为


class EmptyStateTest(CliTestCase):
    def test_status_on_empty_state(self) -> None:
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("还没有任何备份运行记录", out)

    def test_runs_on_empty_state(self) -> None:
        code, out, err = self.cli("runs")
        self.assertEqual(code, 0, err)
        self.assertIn("还没有任何备份运行记录", out)

    def test_show_on_empty_state(self) -> None:
        code, _out, err = self.cli("show", "20200101T000000Z")
        self.assertEqual(code, 1)
        self.assertIn("找不到运行记录", err)

    def test_gc_without_repo_returns_failure(self) -> None:
        code, _out, err = self.cli("gc")
        self.assertEqual(code, 1)
        self.assertIn("备份仓库不存在", err)


# ------------------------------------------------------------ set-password


class SetPasswordTest(CliTestCase):
    def config_text(self) -> str:
        return self.config_path.read_text(encoding="utf-8")

    def test_matching_passwords_are_written(self) -> None:
        with mock.patch.object(cli, "getpass", side_effect=[PASSWORD, PASSWORD]) as reader:
            code, out, err = self.cli("set-password")
        self.assertEqual(code, 0, err)
        self.assertIn("口令已写入配置", out)
        self.assertEqual(reader.call_count, 2)
        cfg = load_config(self.config_path)
        self.assertTrue(util.verify_password(cfg.web_password_hash, PASSWORD))
        text = self.config_text()
        self.assertIn("[general]", text)
        self.assertIn("gc_after_runs = 20", text)
        self.assertIn("repo_root = ", text)

    def test_mismatch_returns_2_and_keeps_config(self) -> None:
        before = self.config_text()
        with mock.patch.object(
            cli, "getpass", side_effect=["aaaaaaaa", "bbbbbbbb"] * cli.PASSWORD_ATTEMPTS
        ):
            code, _out, err = self.cli("set-password")
        self.assertEqual(code, 2)
        self.assertIn("两次输入不一致", err)
        self.assertIn("口令未修改", err)
        self.assertEqual(self.config_text(), before)

    def test_short_password_returns_2(self) -> None:
        before = self.config_text()
        with mock.patch.object(cli, "getpass", side_effect=["short"] * 2 * cli.PASSWORD_ATTEMPTS):
            code, _out, err = self.cli("set-password")
        self.assertEqual(code, 2)
        self.assertIn("至少需要 8 位", err)
        self.assertEqual(self.config_text(), before)

    def test_retries_then_succeeds(self) -> None:
        attempts = ["aaaaaaaa", "bbbbbbbb", "short", "short", PASSWORD, PASSWORD]
        with mock.patch.object(cli, "getpass", side_effect=attempts):
            code, out, err = self.cli("set-password")
        self.assertEqual(code, 0, err)
        self.assertIn("口令已写入配置", out)
        stored = load_config(self.config_path).web_password_hash
        self.assertTrue(util.verify_password(stored, PASSWORD))

    def test_missing_config_returns_failure(self) -> None:
        code, _out, err = run_cli(self.root / "absent.ini", "set-password")
        self.assertEqual(code, 1)
        self.assertIn("配置文件不存在", err)


# --------------------------------------------------------------------- web


class WebCommandTest(CliTestCase):
    def test_web_refuses_without_password(self) -> None:
        code, _out, err = self.cli("web", "--bind", "127.0.0.1", "--port", "8787")
        self.assertEqual(code, 2)
        self.assertIn("口令", err)

    def test_web_rejects_bad_port(self) -> None:
        code, _out, err = self.cli("web", "--port", "70000")
        self.assertEqual(code, 2)
        self.assertIn("--port", err)

    def test_web_passes_bind_and_port_to_serve(self) -> None:
        with mock.patch.object(web, "serve", return_value=0) as serve:
            code, _out, err = self.cli("web", "--bind", "127.0.0.1", "--port", "9911")
        self.assertEqual(code, 0, err)
        effective, store = serve.call_args[0]
        self.assertEqual((effective.web_bind, effective.web_port), ("127.0.0.1", 9911))
        self.assertEqual(Path(store.db_path), self.state_dir / "myu.db")

    def test_web_without_options_uses_config_values(self) -> None:
        with mock.patch.object(web, "serve", return_value=0) as serve:
            code, _out, err = self.cli("web")
        self.assertEqual(code, 0, err)
        effective = serve.call_args[0][0]
        self.assertEqual((effective.web_bind, effective.web_port), ("127.0.0.1", 8787))


if __name__ == "__main__":
    unittest.main()
