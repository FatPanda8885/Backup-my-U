"""场景级验证：把 README「安全边界」与 docs/DESIGN.md §3/§8 的承诺跑成可执行的证据。

每个用例对应一条对用户做出的承诺（用例名形如 test_promise_<字母>_...）。全部在
tempfile.TemporaryDirectory() 里用真实 CLI（python -m myu 子进程与直接调 myu.cli.main）
一周一周地插 U 盘，不复测单元行为。
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from myu import cli, config, index, util, web  # noqa: E402  （项目根必须先进入 sys.path）

DEVICE_SERIAL = "SCENARIO-USB-0001"
WEB_PASSWORD = "scenario-pass"
WEB_PASSWORD_ITERATIONS = 1000
SUBPROCESS_TIMEOUT = 30
MARKER_NAME = "last-run.marker"
DELETED_DIR = "_deleted"

# 口令哈希默认留空；需要网页的用例再用 util.hash_password 覆盖。
CONFIG_TEMPLATE = """[general]
repo_root = {repo_root}
state_dir = {state_dir}
log_dir   = {log_dir}
exclude = *.tmp
poll_guard_seconds = 3600

[device]
serial      = {serial}
marker_file = .myu-id
marker_value =

[git]
big_file_threshold = 512m
gc_after_runs      = 20

[web]
bind          = 127.0.0.1
port          = 8787
password_hash =
"""

# 不走系统代理：网页用例只连 127.0.0.1。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- 通用小工具


def _close_myu_logger() -> None:
    """关掉 util.setup_logger 留下的文件句柄，否则 Windows 删不掉临时目录。"""
    logger = logging.getLogger("myu")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - 清理路径不该让用例失败
            pass


def usb_snapshot(root: Path) -> dict:
    """整个目录的递归快照：相对路径 -> (字节数, mtime_ns, 内容 sha256)。"""
    base = Path(root)
    snapshot: dict = {}
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        snapshot[path.relative_to(base).as_posix()] = (
            stat.st_size,
            stat.st_mtime_ns,
            digest.hexdigest(),
        )
    return snapshot


def describe_snapshot_diff(before: dict, after: dict) -> str:
    """把两份快照的差异渲染成中文，断言失败时直接贴给人看。"""
    lines: list = []
    for relpath in sorted(set(before) | set(after)):
        old = before.get(relpath)
        new = after.get(relpath)
        if old == new:
            continue
        if old is None:
            lines.append("  + U 盘上多出了 %s：%s" % (relpath, new))
            continue
        if new is None:
            lines.append("  - U 盘上少了 %s：原来是 %s" % (relpath, old))
            continue
        for position, name in enumerate(("字节数", "mtime_ns", "内容 sha256")):
            if old[position] != new[position]:
                lines.append(
                    "  ~ %s 的%s: %s -> %s" % (relpath, name, old[position], new[position])
                )
    return "\n".join(lines) if lines else "  （两份快照没有差异）"


def markdown_section(text: str, title: str) -> str:
    """取 Markdown 里「## <title>…」到下一个「## 」之间的正文；没有该节时返回空串。"""
    lines = text.splitlines()
    for position, line in enumerate(lines):
        if line.startswith("## %s" % title):
            body: list = []
            for following in lines[position + 1:]:
                if following.startswith("## "):
                    break
                body.append(following)
            return "\n".join(body)
    return ""


def run_id_epoch(run_id: object) -> float | None:
    """run_id（20250316T101500Z）对应的 Unix 秒；格式不对返回 None。"""
    try:
        moment = datetime.strptime(str(run_id), "%Y%m%dT%H%M%SZ")
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- 场景基座


class ScenarioTestCase(unittest.TestCase):
    """每个用例一套独立的假 U 盘 + 备份根目录，全程不碰真实磁盘位置。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(_close_myu_logger)
        self.root = Path(self._tmp.name)
        self.usb = self.root / "usb"
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.logs = self.root / "logs"
        for directory in (self.usb, self.repo, self.state, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.root / "config.ini"
        self.cfg_path.write_text(
            CONFIG_TEMPLATE.format(
                repo_root=self.repo.as_posix(),
                state_dir=self.state.as_posix(),
                log_dir=self.logs.as_posix(),
                serial=DEVICE_SERIAL,
            ),
            encoding="utf-8",
            newline="\n",
        )

    # ------------------------------------------------------------ 假 U 盘

    def write_usb(self, relpath: str, text: str, mtime_ns: int | None = None) -> Path:
        path = self.usb / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        if mtime_ns is not None:
            os.utime(str(path), ns=(mtime_ns, mtime_ns))
        return path

    def remove_usb(self, relpath: str) -> None:
        (self.usb / relpath).unlink()

    def usb_snapshot(self) -> dict:
        return usb_snapshot(self.usb)

    # ------------------------------------------------------------ CLI

    def myu(self, *args: str) -> tuple:
        """进程内直接调 cli.main；返回 (退出码, 合并后的 stdout+stderr)。"""
        argv = ["--config", str(self.cfg_path), *args]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        finally:
            _close_myu_logger()
        return code, out.getvalue() + err.getvalue()

    def myu_subprocess(self, *args: str) -> subprocess.CompletedProcess:
        """真起一个 python -m myu 子进程，证明命令行入口真的可用。"""
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(PROJECT_ROOT) + (
            os.pathsep + existing if existing else ""
        )
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        command = [sys.executable, "-m", "myu", "--config", str(self.cfg_path), *args]
        return subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
        )

    def backup(self, *extra: str) -> tuple:
        """进程内跑一次 backup；--mountpoint 直接把假 U 盘当已挂载根。"""
        self._reserve_run_slot()
        return self.myu("backup", "--mountpoint", str(self.usb), *extra)

    def backup_subprocess(self, *extra: str) -> subprocess.CompletedProcess:
        self._reserve_run_slot()
        return self.myu_subprocess("backup", "--mountpoint", str(self.usb), *extra)

    def _reserve_run_slot(self) -> None:
        """run_id 是秒级时间戳；同一秒内跑两次会撞 UNIQUE 约束，先错开一秒。"""
        latest = self.latest_run()
        if not latest:
            return
        used = run_id_epoch(latest.get("run_id"))
        if used is None:
            return
        now = time.time()
        if now < used + 1.0:
            time.sleep(used + 1.0 - now + 0.02)

    # ------------------------------------------------------------ 状态与 git

    def load_config(self, password_hash: str = "") -> config.Config:
        loaded = config.load_config(self.cfg_path)
        if password_hash:
            return dataclasses.replace(loaded, web_password_hash=password_hash)
        return loaded

    def latest_run(self) -> dict | None:
        with index.Store(self.state / "myu.db") as store:
            return store.latest_run()

    def run_ids(self) -> list:
        """runs 表里的 run_id，最新在前。"""
        with index.Store(self.state / "myu.db") as store:
            return [str(run["run_id"]) for run in store.list_runs(limit=100)]

    def run_items(self, run_id: str, kind: str | None = None) -> list:
        with index.Store(self.state / "myu.db") as store:
            return store.list_run_items(run_id, kind)

    def git(self, *args: str) -> subprocess.CompletedProcess:
        command = ["git", "-C", str(self.repo), "-c", "core.quotepath=false", *args]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
        )

    def rev_count(self) -> int:
        result = self.git("rev-list", "--count", "HEAD")
        self.assertEqual(
            result.returncode, 0, msg="git rev-list 失败，场景基座不成立\n" + result.stderr
        )
        return int(result.stdout.strip() or 0)

    def tags(self) -> list:
        return self.git("tag", "--list", "run-*").stdout.split()

    def assert_backup_ok(self, outcome: tuple, label: str) -> None:
        code, output = outcome
        self.assertEqual(code, 0, msg="%s 应该成功，实际退出码 %d\n%s" % (label, code, output))

    def http_get(self, port: int, path: str, password: str | None) -> tuple:
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
        if password is not None:
            token = base64.b64encode(
                ("myu:%s" % password).encode("utf-8")
            ).decode("ascii")
            request.add_header("Authorization", "Basic " + token)
        try:
            with OPENER.open(request, timeout=15) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            finally:
                exc.close()


# ---------------------------------------------------------------- 承诺 A


class PromiseATest(ScenarioTestCase):
    """README「安全边界」第 1 条 / DESIGN §3.1：绝不写 U 盘。"""

    def test_promise_a_usb_never_written(self) -> None:
        self.write_usb("a b c.txt", "abc-payload")
        self.write_usb("相册/2024 年 春节/第 1 天/照片 001.jpg", "jpeg-001-body")
        self.write_usb("docs/report final.txt", "week-one")

        before_first = self.usb_snapshot()
        self.assertTrue(before_first, msg="承诺 A：假 U 盘快照是空的，快照函数本身不成立")
        self.assertIn(
            "相册/2024 年 春节/第 1 天/照片 001.jpg",
            before_first,
            msg="承诺 A：快照没有递归到深层目录",
        )

        first = self.backup_subprocess()
        self.assertEqual(
            first.returncode,
            0,
            msg="承诺 A 前提不成立：首次备份失败\n" + first.stdout + first.stderr,
        )
        after_first = self.usb_snapshot()
        self.assertEqual(
            before_first,
            after_first,
            msg="承诺 A 被破坏：备份写动了 U 盘（路径/字节数/mtime/内容任一变化都算）\n"
            + describe_snapshot_diff(before_first, after_first),
        )

        # 第二周：改内容 + 新增文件，再验一次整盘快照
        self.write_usb("docs/report final.txt", "week-two-longer")
        self.write_usb("相册/2024 年 春节/第 2 天/照片 002.jpg", "jpeg-002-body")
        before_second = self.usb_snapshot()
        second = self.backup_subprocess()
        self.assertEqual(
            second.returncode,
            0,
            msg="承诺 A 前提不成立：第二次备份失败\n" + second.stdout + second.stderr,
        )
        after_second = self.usb_snapshot()
        self.assertEqual(
            before_second,
            after_second,
            msg="承诺 A 被破坏：第二次备份写动了 U 盘\n"
            + describe_snapshot_diff(before_second, after_second),
        )

        # 自证：快照真的比较了内容 sha256 与 mtime_ns，不是只比文件名
        victim = self.usb / "a b c.txt"
        original = victim.stat()
        baseline = self.usb_snapshot()
        victim.write_text("XYZ-payload", encoding="utf-8", newline="\n")  # 同字节数、不同内容
        os.utime(str(victim), ns=(original.st_atime_ns, original.st_mtime_ns))
        same_size = self.usb_snapshot()
        self.assertEqual(
            baseline["a b c.txt"][:2],
            same_size["a b c.txt"][:2],
            msg="承诺 A 自证失败：造出的两份字节数/mtime 不一致，无法单独验证 sha256",
        )
        self.assertNotEqual(
            baseline["a b c.txt"][2],
            same_size["a b c.txt"][2],
            msg="承诺 A 自证失败：快照没有比较内容 sha256",
        )
        victim.write_text("abc-payload", encoding="utf-8", newline="\n")
        os.utime(str(victim), ns=(original.st_atime_ns, original.st_mtime_ns + 10 ** 9))
        moved_mtime = self.usb_snapshot()
        self.assertNotEqual(
            baseline["a b c.txt"][1],
            moved_mtime["a b c.txt"][1],
            msg="承诺 A 自证失败：快照没有比较 mtime_ns",
        )


# ---------------------------------------------------------------- 承诺 B


class PromiseBTest(ScenarioTestCase):
    """README「安全边界」第 2 条 / DESIGN §3.2：数据只增不删。"""

    RELPATH = "资料/笔记 2025.txt"
    PAYLOAD = "被删掉的那一周的内容 2025"

    def _two_weeks(self) -> tuple:
        self.write_usb("keep.txt", "keep-v1")
        self.write_usb(self.RELPATH, self.PAYLOAD)
        self.assert_backup_ok(self.backup(), "承诺 B 前提：第一周备份")
        first = self.latest_run()
        self.remove_usb(self.RELPATH)
        self.assert_backup_ok(self.backup(), "承诺 B 前提：删掉文件后的第二周备份")
        second = self.latest_run()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(
            first["run_id"],
            second["run_id"],
            msg="承诺 B 前提：两次备份应各自产生一条 runs 行",
        )
        return first, second

    def test_promise_b_1_deletion_moved_to_tombstone(self) -> None:
        first, second = self._two_weeks()
        run_id = str(second["run_id"])

        self.assertFalse(
            (self.repo / self.RELPATH).exists(),
            msg="承诺 B 被破坏：U 盘删掉的文件还留在工作区原位",
        )
        self.assertEqual(
            int(second["deleted"]), 1, msg="承诺 B 被破坏：删除没有记进 runs 行"
        )
        tombstone = self.repo / DELETED_DIR / run_id / self.RELPATH
        self.assertTrue(
            tombstone.is_file(),
            msg="承诺 B 被破坏：_deleted/%s/%s 不存在" % (run_id, self.RELPATH),
        )
        self.assertEqual(
            tombstone.read_bytes(),
            self.PAYLOAD.encode("utf-8"),
            msg="承诺 B 被破坏：_deleted 里的内容与原文件不是逐字节相等",
        )
        self.assertEqual(
            [str(item["relpath"]) for item in self.run_items(run_id, "deleted")],
            [self.RELPATH],
            msg="承诺 B 被破坏：run_items 的删除栏没有这个文件",
        )
        tree = self.git("ls-tree", "-r", "--name-only", "run-" + run_id)
        self.assertEqual(tree.returncode, 0, msg="承诺 B：git ls-tree 失败\n" + tree.stderr)
        self.assertIn(
            "%s/%s/%s" % (DELETED_DIR, run_id, self.RELPATH),
            tree.stdout,
            msg="承诺 B 被破坏：墓碑没有进 git 历史",
        )

    def test_promise_b_2_deleted_file_recoverable_from_run_tag(self) -> None:
        first, second = self._two_weeks()
        first_tag = "run-%s" % first["run_id"]
        second_tag = "run-%s" % second["run_id"]

        # 数据只增不删：删除前那一周的 tag 一定能把文件整份拿回来
        dest_first = self.root / "restore-before-delete"
        self.assert_backup_ok(
            self.myu("restore", first_tag, str(dest_first)),
            "承诺 B：restore %s" % first_tag,
        )
        restored = dest_first / self.RELPATH
        self.assertTrue(
            restored.is_file(),
            msg="承诺 B 被破坏：用 tag %s 恢复后拿不回被删的文件" % first_tag,
        )
        self.assertEqual(
            restored.read_bytes(),
            self.PAYLOAD.encode("utf-8"),
            msg="承诺 B 被破坏：恢复出来的被删文件内容不正确",
        )

        # DESIGN §7.8：restore 还原的是「U 盘当时的样子」，_deleted/ 下的内容永不落到 dest。
        # 所以「删除发生那次」的 tag 恢复结果里没有这个文件 —— 上面的 tag 才是取回入口。
        dest_second = self.root / "restore-delete-week"
        self.assert_backup_ok(
            self.myu("restore", second_tag, str(dest_second)),
            "承诺 B：restore %s" % second_tag,
        )
        self.assertFalse(
            (dest_second / self.RELPATH).exists(),
            msg="承诺 B：删除当周的恢复结果里出现了当时 U 盘上已不存在的文件（与 §7.8 不符）",
        )
        self.assertTrue(
            (dest_second / "keep.txt").is_file(),
            msg="承诺 B：删除当周的恢复结果不完整（keep.txt 都没恢复出来）",
        )
        self.assertEqual(
            list(dest_second.rglob(DELETED_DIR)),
            [],
            msg="承诺 B 被破坏：_deleted/ 下的墓碑被恢复到了目标目录",
        )


# ---------------------------------------------------------------- 承诺 C


class PromiseCTest(ScenarioTestCase):
    """README「为什么用 git 做存储引擎」/ DESIGN §3.2：任意一周的状态都能还原。"""

    def test_promise_c_every_week_restores_its_own_content(self) -> None:
        relpath = "周记/版本.txt"
        versions = [
            ("第 1 周", "这是第一周的内容 v1"),
            ("第 2 周", "这是第二周改过的内容 v2，长度也不一样"),
            ("第 3 周", "第三周又改了一次 v3，第三版"),
        ]
        runs: list = []
        base_mtime = 1_700_000_000_000_000_000
        for position, version in enumerate(versions):
            self.write_usb(relpath, version[1], mtime_ns=base_mtime + position * 10 ** 9)
            self.assert_backup_ok(self.backup(), "承诺 C 前提：%s 备份" % version[0])
            runs.append(self.latest_run())

        self.assertEqual(
            len({run["run_id"] for run in runs}),
            3,
            msg="承诺 C 前提：三周内容应该各产生一次运行",
        )
        self.assertEqual(
            len(self.rev_count() and self.tags()),
            3,
            msg="承诺 C 前提：三周应该各有一个 tag",
        )

        for position, version in enumerate(versions):
            label, expected = version[0], version[1]
            run = runs[position]
            dest = self.root / ("restore-%s" % run["run_id"])
            self.assert_backup_ok(
                self.myu("restore", str(run["run_id"]), str(dest)),
                "承诺 C：restore %s" % run["run_id"],
            )
            restored = dest / relpath
            self.assertTrue(
                restored.is_file(),
                msg="承诺 C 被破坏：恢复 %s（%s）后文件不存在" % (label, run["run_id"]),
            )
            self.assertEqual(
                restored.read_text(encoding="utf-8"),
                expected,
                msg="承诺 C 被破坏：恢复 %s（%s）拿到的不是当周内容"
                % (label, run["run_id"]),
            )


# ---------------------------------------------------------------- 承诺 D


class PromiseDTest(ScenarioTestCase):
    """DESIGN §8 第 5 步：无变化就什么都不做。"""

    def test_promise_d_unchanged_usb_does_nothing(self) -> None:
        self.write_usb("keep.txt", "keep-v1")
        self.write_usb("docs/roster.txt", "roster-v1")
        self.assert_backup_ok(self.backup(), "承诺 D 前提：首次备份")
        first = self.latest_run()
        self.assertIsNotNone(first)

        runs_before = self.run_ids()
        commits_before = self.rev_count()
        tags_before = self.tags()

        marker = self.state / MARKER_NAME
        self.assertTrue(
            marker.is_file(), msg="承诺 D 被破坏：备份结束后没有生成 last-run.marker"
        )
        stale = time.time() - 3600
        os.utime(str(marker), (stale, stale))
        stale_mtime_ns = marker.stat().st_mtime_ns

        outcome = self.backup()
        self.assert_backup_ok(outcome, "承诺 D 前提：无变化的第二次备份")

        self.assertEqual(
            self.run_ids(),
            runs_before,
            msg="承诺 D 被破坏：无变化的运行新增了 runs 行（轮询会把历史表灌满）",
        )
        self.assertEqual(
            self.rev_count(),
            commits_before,
            msg="承诺 D 被破坏：无变化也产生了新 commit",
        )
        self.assertEqual(
            self.tags(), tags_before, msg="承诺 D 被破坏：无变化也打了新 tag"
        )
        self.assertGreater(
            marker.stat().st_mtime_ns,
            stale_mtime_ns,
            msg="承诺 D 被破坏：无变化时 last-run.marker 的 mtime 没有被更新",
        )
        self.assertIn("跳过", outcome[1], msg="承诺 D：日志里没有「无变化，跳过」")


# ---------------------------------------------------------------- 承诺 E


class PromiseETest(ScenarioTestCase):
    """README「日常命令」：多空格 / 中文 / 深层目录在备份、查询、恢复里都不出错。"""

    FILES = {
        "相册/2024 年 春节/第 1 天/照片 001.jpg": "照片内容 001",
        "文档/预算 2025.xlsx": "预算表格内容",
        "a b c.txt": "abc-body",
    }

    def test_promise_e_unicode_spaces_and_deep_dirs(self) -> None:
        for relpath, text in self.FILES.items():
            self.write_usb(relpath, text)
        self.assert_backup_ok(self.backup(), "承诺 E 前提：备份中文/空格/深层路径")
        run = self.latest_run()
        self.assertIsNotNone(run)
        run_id = str(run["run_id"])

        for relpath, text in self.FILES.items():
            path = self.repo / relpath
            self.assertTrue(
                path.is_file(), msg="承诺 E 被破坏：工作区里没有 %s" % relpath
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                text,
                msg="承诺 E 被破坏：工作区里 %s 的内容不对" % relpath,
            )

        tree = self.git("ls-tree", "-r", "--name-only", "HEAD")
        self.assertEqual(tree.returncode, 0, msg="承诺 E：git ls-tree 失败\n" + tree.stderr)
        for relpath in self.FILES:
            self.assertIn(
                relpath,
                tree.stdout,
                msg="承诺 E 被破坏：git 历史里的路径被转义/破坏 %s" % relpath,
            )

        proc = self.myu_subprocess("runs")
        self.assertEqual(
            proc.returncode, 0, msg="承诺 E 被破坏：myu runs 子进程失败\n" + proc.stderr
        )
        self.assertIn(
            run_id, proc.stdout, msg="承诺 E 被破坏：myu runs 里看不到这次运行"
        )

        proc = self.myu_subprocess("show", run_id)
        self.assertEqual(
            proc.returncode, 0, msg="承诺 E 被破坏：myu show 子进程失败\n" + proc.stderr
        )
        for relpath in self.FILES:
            self.assertIn(
                relpath, proc.stdout, msg="承诺 E 被破坏：myu show 里看不到 %s" % relpath
            )

        dest = self.root / "restore-unicode"
        self.assert_backup_ok(
            self.myu("restore", run_id, str(dest)), "承诺 E：restore 中文路径"
        )
        for relpath, text in self.FILES.items():
            restored = dest / relpath
            self.assertTrue(
                restored.is_file(), msg="承诺 E 被破坏：恢复后路径丢失 %s" % relpath
            )
            self.assertEqual(
                restored.read_text(encoding="utf-8"),
                text,
                msg="承诺 E 被破坏：恢复后 %s 的内容不对" % relpath,
            )


# ---------------------------------------------------------------- 承诺 F


class PromiseFTest(ScenarioTestCase):
    """README「安全边界」第 5 条 / DESIGN §3.5：失败不污染历史。"""

    def test_promise_f_failed_backup_leaves_no_dirty_state(self) -> None:
        self.write_usb("keep.txt", "keep-v1")
        self.write_usb("docs/roster.txt", "roster-v1")
        self.assert_backup_ok(self.backup(), "承诺 F 前提：先有一次成功的备份")
        commits_before = self.rev_count()
        rows_before = self.run_ids()

        # 制造必然失败：用空目录占住新增文件的落地路径，原子 rename 一定失败。
        # 目录是空的，工作区扫描不会产生删除计划，失败路径因此干净可判断。
        self.write_usb("boom.txt", "boom-payload")
        (self.repo / "boom.txt").mkdir()

        code, output = self.backup()
        self.assertEqual(
            code,
            1,
            msg="承诺 F 前提不成立：构造的失败没有让 backup 退出 1（实际 %d）\n%s"
            % (code, output),
        )
        failed = self.latest_run()
        self.assertIsNotNone(failed)
        self.assertNotIn(
            failed["run_id"],
            rows_before,
            msg="承诺 F 前提：失败的运行没有新增 runs 行，无法验证 failed 记录",
        )
        self.assertEqual(
            str(failed["status"]),
            "failed",
            msg="承诺 F 被破坏：失败的运行没有记为 failed",
        )
        self.assertTrue(
            str(failed.get("error") or "").strip(),
            msg="承诺 F 被破坏：failed 行的 error 是空的",
        )
        self.assertFalse(
            failed.get("git_commit"),
            msg="承诺 F 被破坏：失败的运行居然记下了 git_commit",
        )
        self.assertEqual(
            self.rev_count(),
            commits_before,
            msg="承诺 F 被破坏：失败的运行产生了半截 commit",
        )
        self.assertEqual(
            self.run_items(str(failed["run_id"])),
            [],
            msg="承诺 F 被破坏：失败的运行写进了 run_items 明细",
        )

        for relpath, text in (("keep.txt", "keep-v1"), ("docs/roster.txt", "roster-v1")):
            path = self.repo / relpath
            self.assertTrue(
                path.is_file(),
                msg="承诺 F 被破坏：失败的运行弄丢了已提交的 %s" % relpath,
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                text,
                msg="承诺 F 被破坏：失败的运行改动了已提交的 %s" % relpath,
            )
        status = self.git("status", "--porcelain")
        self.assertEqual(status.returncode, 0, msg="承诺 F：git status 失败\n" + status.stderr)
        self.assertEqual(
            status.stdout.strip(),
            "",
            msg="承诺 F 被破坏：失败的运行把工作区弄脏了\n" + status.stdout,
        )
        leftovers = sorted(path.name for path in self.repo.rglob("*.myu-tmp"))
        self.assertEqual(
            leftovers, [], msg="承诺 F 被破坏：留下了半成品临时文件 %s" % (leftovers,)
        )
        self.assertFalse(
            (self.repo / DELETED_DIR / str(failed["run_id"])).exists(),
            msg="承诺 F 被破坏：失败的运行留下了 _deleted 墓碑",
        )

        # 障碍解除后历史仍然能继续往前走
        (self.repo / "boom.txt").rmdir()
        self.assert_backup_ok(self.backup(), "承诺 F：失败之后应能继续正常备份")
        self.assertTrue(
            (self.repo / "boom.txt").is_file(),
            msg="承诺 F：恢复后的备份没有落地 boom.txt",
        )
        self.assertEqual(
            self.rev_count(),
            commits_before + 1,
            msg="承诺 F：失败之后的成功备份没有恰好新增一个 commit",
        )


# ---------------------------------------------------------------- 承诺 G


class PromiseGTest(ScenarioTestCase):
    """DESIGN §3.2 / §9：全部场景跑完后仓库完好。"""

    def test_promise_g_repository_intact_after_full_cycle(self) -> None:
        self.write_usb("相册/照片 001.jpg", "photo-v1")
        self.write_usb("notes.txt", "note-1")
        self.assert_backup_ok(self.backup(), "承诺 G 前提：第 1 周")

        self.write_usb("相册/照片 001.jpg", "photo-v2-更长的内容")
        self.write_usb("文档/预算 2025.xlsx", "xlsx-1")
        self.assert_backup_ok(self.backup(), "承诺 G 前提：第 2 周")

        self.remove_usb("notes.txt")
        self.assert_backup_ok(self.backup(), "承诺 G 前提：第 3 周（删除一个文件）")

        self.write_usb("boom.txt", "boom-payload")
        (self.repo / "boom.txt").mkdir()
        code, output = self.backup()
        self.assertEqual(code, 1, msg="承诺 G 前提：构造的失败运行没有失败\n" + output)
        (self.repo / "boom.txt").rmdir()

        fsck = self.git("fsck", "--no-progress")
        self.assertEqual(
            fsck.returncode,
            0,
            msg="承诺 G 被破坏：git fsck 退出码非 0\n" + fsck.stdout + fsck.stderr,
        )
        combined = (fsck.stdout + fsck.stderr).lower()
        self.assertNotIn("error", combined, msg="承诺 G 被破坏：fsck 输出含 error\n" + combined)
        self.assertNotIn(
            "missing", combined, msg="承诺 G 被破坏：fsck 输出含 missing\n" + combined
        )

        tags = self.tags()
        self.assertGreaterEqual(len(tags), 3, msg="承诺 G：历史里应有三次成功备份的 tag")
        for tag in tags:
            resolved = self.git("rev-parse", "--verify", tag + "^{commit}")
            self.assertEqual(
                resolved.returncode,
                0,
                msg="承诺 G 被破坏：tag %s 指向的提交不存在" % tag,
            )
            self.assertEqual(
                len(resolved.stdout.strip()), 40, msg="承诺 G：%s 解析结果异常" % tag
            )
        log = self.git("log", "--format=%H %s")
        self.assertEqual(log.returncode, 0, msg="承诺 G：git log 失败\n" + log.stderr)
        self.assertEqual(
            len(log.stdout.strip().splitlines()),
            self.rev_count(),
            msg="承诺 G 被破坏：git log 与 rev-list 的提交数不一致",
        )


# ---------------------------------------------------------------- 承诺 H


class PromiseHTest(ScenarioTestCase):
    """README「网页」/ DESIGN §7.7 §7.9：报告与网页可见。"""

    def _prepare_week(self) -> dict:
        self.write_usb("keep.txt", "keep-v1")
        self.write_usb("相册/照片 001.jpg", "jpeg-1")
        self.assert_backup_ok(self.backup(), "承诺 H 前提：第 1 周")
        self.write_usb("keep.txt", "keep-v2-更长的内容")
        self.remove_usb("相册/照片 001.jpg")
        self.write_usb("新增 文件.txt", "brand-new")
        self.assert_backup_ok(self.backup(), "承诺 H 前提：第 2 周")
        run = self.latest_run()
        self.assertIsNotNone(run)
        return run

    def test_promise_h_1_report_markdown_exists(self) -> None:
        run = self._prepare_week()
        run_id = str(run["run_id"])
        report = self.state / "reports" / (run_id + ".md")
        self.assertTrue(
            report.is_file(),
            msg="承诺 H 被破坏：state_dir/reports/%s.md 不存在" % run_id,
        )
        text = report.read_text(encoding="utf-8")
        self.assertTrue(
            text.startswith("# 备份运行 "),
            msg="承诺 H 被破坏：报告没有中文标题\n" + text.splitlines()[0],
        )
        for title in ("新增", "修改", "删除"):
            self.assertIn(
                "## " + title, text, msg="承诺 H 被破坏：报告缺少「%s」一节" % title
            )
        self.assertIn(
            "新增 文件.txt",
            markdown_section(text, "新增"),
            msg="承诺 H 被破坏：新增一节里没有这周新增的文件",
        )
        self.assertIn(
            "keep.txt",
            markdown_section(text, "修改"),
            msg="承诺 H 被破坏：修改一节里没有这周改过的文件",
        )
        self.assertIn(
            "相册/照片 001.jpg",
            markdown_section(text, "删除"),
            msg="承诺 H 被破坏：删除一节里没有这周删掉的文件",
        )

    def test_promise_h_2_web_pages_show_run_files(self) -> None:
        run = self._prepare_week()
        run_id = str(run["run_id"])
        cfg = self.load_config(
            util.hash_password(WEB_PASSWORD, iterations=WEB_PASSWORD_ITERATIONS)
        )

        with index.Store(cfg.db_path) as store:
            server = web.create_server(cfg, store, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            try:
                thread.start()
                port = int(server.server_address[1])
                self.assertGreater(port, 0, msg="承诺 H：网页服务没有拿到真实端口")

                status, body = self.http_get(port, "/", WEB_PASSWORD)
                self.assertEqual(status, 200, msg="承诺 H 被破坏：带口令请求 / 没有返回 200")
                self.assertIn(
                    run_id, body, msg="承诺 H 被破坏：首页运行历史里看不到这次运行"
                )

                status, body = self.http_get(port, "/run/" + run_id, WEB_PASSWORD)
                self.assertEqual(
                    status,
                    200,
                    msg="承诺 H 被破坏：带口令请求 /run/<run_id> 没有返回 200",
                )
                for relpath in ("keep.txt", "新增 文件.txt", "相册/照片 001.jpg"):
                    self.assertIn(
                        relpath,
                        body,
                        msg="承诺 H 被破坏：/run/%s 页面里看不到该 run 的文件 %s"
                        % (run_id, relpath),
                    )

                # 无口令必须被挡在门外；顺手把这条 warning 收进断言，别弄脏测试输出
                with self.assertLogs("myu.web", level="WARNING") as captured:
                    status, _body = self.http_get(port, "/", None)
                self.assertEqual(
                    status, 401, msg="承诺 H 被破坏：不带口令也拿到了 200"
                )
                self.assertIn(
                    "认证失败",
                    "\n".join(captured.output),
                    msg="承诺 H：无口令请求没有被记进日志",
                )
            finally:
                server.shutdown()
                thread.join(timeout=10)
                server.server_close()


# ---------------------------------------------------------------- 入口可用性


class EntryPointTest(ScenarioTestCase):
    """README「日常命令」：python -m myu 这个入口本身真的可用。"""

    def test_entrypoint_python_dash_m_myu_is_usable(self) -> None:
        proc = self.myu_subprocess("--version")
        self.assertEqual(
            proc.returncode, 0, msg="python -m myu --version 失败\n" + proc.stderr
        )
        self.assertIn("myu", proc.stdout, msg="承诺：--version 没有打印程序名")

        proc = self.myu_subprocess("--help")
        self.assertEqual(proc.returncode, 0, msg="python -m myu --help 失败\n" + proc.stderr)
        for subcommand in ("backup", "restore", "runs", "show", "web"):
            self.assertIn(
                subcommand, proc.stdout, msg="承诺：帮助里没有子命令 %s" % subcommand
            )

        proc = self.myu_subprocess()
        self.assertEqual(
            proc.returncode,
            2,
            msg="承诺：不带子命令时应按用法错误退出 2，实际 %d" % proc.returncode,
        )
        self.assertIn(
            "backup", proc.stdout + proc.stderr, msg="承诺：无子命令时没有打印帮助"
        )


if __name__ == "__main__":
    unittest.main()
