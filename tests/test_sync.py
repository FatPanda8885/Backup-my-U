from __future__ import annotations

import errno
import filecmp
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myu import sync, util
from myu.sync import DELETED_DIR, FileState, MirrorStats, Plan, scan, scan_worktree

DEEP_CHINESE = "相册/2024 年 春节/第 1 天/照片 001.jpg"
RUN_ID = "20240101T000000Z"


def write_file(path: Path, data: bytes | str, mtime_ns: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    path.write_bytes(payload)
    if mtime_ns is not None:
        os.utime(str(path), ns=(mtime_ns, mtime_ns))
    return path


def deep_path(root: Path, relpath: str) -> Path:
    return root.joinpath(*relpath.split("/"))


class TempTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.usb = self.root / "usb"
        self.work = self.root / "work"
        self.usb.mkdir()
        self.work.mkdir()
        filecmp.clear_cache()

    def make_plan(self, usb_excludes=None, work_excludes=None) -> Plan:
        return sync.plan(
            scan(self.usb, usb_excludes),
            scan_worktree(self.work, work_excludes),
            self.work,
        )


class ScanTest(TempTreeTest):
    def test_collects_files_with_size_and_mtime(self) -> None:
        write_file(self.usb / "a.txt", "abc")
        write_file(self.usb / "sub" / "b.txt", "12345")
        states = scan(self.usb)

        self.assertEqual(sorted(states), ["a.txt", "sub/b.txt"])
        self.assertIsInstance(states["a.txt"], FileState)
        self.assertEqual(states["a.txt"].size, 3)
        self.assertEqual(states["sub/b.txt"].size, 5)
        self.assertEqual(states["a.txt"].relpath, "a.txt")
        self.assertEqual(states["a.txt"].path, self.usb / "a.txt")
        self.assertEqual(states["a.txt"].mtime_ns, os.stat(str(self.usb / "a.txt")).st_mtime_ns)

    def test_skips_excluded_files(self) -> None:
        write_file(self.usb / "a.txt", "a")
        write_file(self.usb / "a.tmp", "a")
        write_file(self.usb / "System Volume Information" / "desktop.ini", "x")
        states = scan(self.usb, ("*.tmp", "System Volume Information/**"))

        self.assertEqual(sorted(states), ["a.txt"])

    def test_missing_root_raises(self) -> None:
        with self.assertRaises(util.MyuError):
            scan(self.root / "nope")

    def test_skips_symlink(self) -> None:
        write_file(self.usb / "target.txt", "t")
        try:
            os.symlink(str(self.usb / "target.txt"), str(self.usb / "link.txt"))
        except (OSError, NotImplementedError):
            self.skipTest("当前平台不允许创建符号链接")
        states = scan(self.usb)

        self.assertNotIn("link.txt", states)
        self.assertIn("target.txt", states)


class ScanWorktreeTest(TempTreeTest):
    def test_prunes_top_level_git_and_deleted(self) -> None:
        write_file(self.work / ".git" / "HEAD", "ref: refs/heads/main")
        write_file(self.work / ".git" / "objects" / "ab" / "cdef0123", "obj")
        write_file(self.work / DELETED_DIR / RUN_ID / "old.txt", "old")
        write_file(self.work / "keep.txt", "k")
        write_file(self.work / "sub" / "nested.txt", "n")
        write_file(self.work / "sub" / DELETED_DIR / "real.txt", "r")

        states = scan_worktree(self.work)

        self.assertEqual(sorted(states), ["keep.txt", "sub/_deleted/real.txt", "sub/nested.txt"])

    def test_does_not_descend_into_pruned_trees(self) -> None:
        write_file(self.work / ".git" / "objects" / "pack" / "x.pack", "obj")
        write_file(self.work / DELETED_DIR / "run1" / "y.txt", "y")
        write_file(self.work / "keep.txt", "k")
        visited: list = []
        original = os.scandir

        def spy(path):
            visited.append(os.path.abspath(str(path)))
            return original(path)

        with mock.patch.object(os, "scandir", side_effect=spy):
            states = scan_worktree(self.work)

        self.assertEqual(sorted(states), ["keep.txt"])
        names = {os.path.basename(path) for path in visited}
        self.assertNotIn(".git", names)
        self.assertNotIn(DELETED_DIR, names)

    def test_missing_worktree_returns_empty(self) -> None:
        self.assertEqual(scan_worktree(self.root / "nope"), {})

    def test_skips_excluded_files(self) -> None:
        write_file(self.work / "keep.txt", "k")
        write_file(self.work / "skip.tmp", "s")

        states = scan_worktree(self.work, ("*.tmp",))

        self.assertEqual(sorted(states), ["keep.txt"])


class PlanTest(TempTreeTest):
    def test_added_when_worktree_missing_file(self) -> None:
        write_file(self.usb / "new.txt", "new")
        result = self.make_plan()

        self.assertEqual([item.relpath for item in result.added], ["new.txt"])
        self.assertEqual(result.modified, [])
        self.assertEqual(result.unchanged, [])
        self.assertEqual(result.deleted, [])

    def test_modified_when_size_differs(self) -> None:
        write_file(self.usb / "a.txt", "0123456789", 1_600_000_000_000_000_000)
        write_file(self.work / "a.txt", "0123", 1_600_000_000_000_000_000)
        result = self.make_plan()

        self.assertEqual([item.relpath for item in result.modified], ["a.txt"])
        self.assertEqual(result.unchanged, [])

    def test_unchanged_when_size_and_mtime_equal(self) -> None:
        write_file(self.usb / "a.txt", "same", 1_600_000_000_000_000_000)
        write_file(self.work / "a.txt", "same", 1_600_000_000_000_000_000)
        result = self.make_plan()

        self.assertEqual([item.relpath for item in result.unchanged], ["a.txt"])
        self.assertEqual(result.modified, [])
        self.assertEqual(result.added, [])

    def test_unchanged_when_content_equal_but_mtime_differs(self) -> None:
        write_file(self.usb / "a.txt", "same bytes", 1_600_000_000_000_000_000)
        write_file(self.work / "a.txt", "same bytes", 1_700_000_000_000_000_000)
        result = self.make_plan()

        self.assertEqual([item.relpath for item in result.unchanged], ["a.txt"])
        self.assertEqual(result.modified, [])

        with mock.patch("myu.sync.copy_file_atomic", wraps=util.copy_file_atomic) as spy:
            stats = sync.mirror(result, self.work, RUN_ID)

        self.assertEqual(spy.call_count, 0)
        self.assertEqual(stats.copied, 0)
        self.assertEqual(stats.bytes_copied, 0)
        self.assertEqual(os.stat(str(self.work / "a.txt")).st_mtime_ns,
                         1_700_000_000_000_000_000)

    def test_modified_when_content_differs(self) -> None:
        write_file(self.usb / "a.txt", "aaaa", 1_600_000_000_000_000_000)
        write_file(self.work / "a.txt", "bbbb", 1_700_000_000_000_000_000)
        result = self.make_plan()

        self.assertEqual([item.relpath for item in result.modified], ["a.txt"])
        self.assertEqual(result.unchanged, [])

    def test_deleted_when_missing_on_usb(self) -> None:
        write_file(self.work / "gone.txt", "x")
        write_file(self.work / "sub" / "gone 2.txt", "y")
        result = self.make_plan()

        self.assertEqual(sorted(result.deleted), ["gone.txt", "sub/gone 2.txt"])
        self.assertEqual(result.added, [])

    def test_excluded_files_are_neither_backed_up_nor_deleted(self) -> None:
        write_file(self.usb / "a.txt", "a")
        write_file(self.usb / "skip.tmp", "s")
        write_file(self.work / "old.tmp", "o")
        result = self.make_plan(("*.tmp",), ("*.tmp",))

        self.assertEqual([item.relpath for item in result.added], ["a.txt"])
        self.assertEqual(result.deleted, [])

        stats = sync.mirror(result, self.work, RUN_ID)

        self.assertEqual(stats.copied, 1)
        self.assertFalse((self.work / "skip.tmp").exists())
        self.assertEqual((self.work / "old.tmp").read_text(encoding="utf-8"), "o")

    def test_changed_and_total_bytes(self) -> None:
        write_file(self.usb / "added.txt", "0123456789")
        write_file(self.usb / "modified.txt", "abcd", 1_600_000_000_000_000_000)
        write_file(self.work / "modified.txt", "ab", 1_600_000_000_000_000_000)
        write_file(self.usb / "same.txt", "xyz", 1_600_000_000_000_000_000)
        write_file(self.work / "same.txt", "xyz", 1_600_000_000_000_000_000)
        result = self.make_plan()

        self.assertEqual([item.relpath for item in result.added], ["added.txt"])
        self.assertEqual([item.relpath for item in result.modified], ["modified.txt"])
        self.assertEqual([item.relpath for item in result.unchanged], ["same.txt"])
        self.assertEqual(result.changed_bytes(), 10 + 4)
        self.assertEqual(result.total_bytes(), 10 + 4 + 3)


class MirrorTest(TempTreeTest):
    def test_copies_added_with_chinese_space_and_deep_dirs(self) -> None:
        payload = b"\x00\x01binary"
        write_file(deep_path(self.usb, DEEP_CHINESE), payload)
        write_file(self.usb / "simple.txt", "s")
        result = self.make_plan()

        stats = sync.mirror(result, self.work, RUN_ID)

        target = deep_path(self.work, DEEP_CHINESE)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), payload)
        self.assertTrue((self.work / "simple.txt").is_file())
        self.assertEqual(stats.copied, 2)
        self.assertEqual(stats.deleted_moved, 0)
        self.assertEqual(stats.bytes_copied, len(payload) + 1)
        self.assertEqual(stats.errors, [])
        self.assertEqual(list(self.work.rglob("*.myu-tmp")), [])

    def test_mirror_creates_missing_worktree_root(self) -> None:
        write_file(self.usb / "a.txt", "a")
        fresh = self.root / "fresh" / "repo"
        result = sync.plan(scan(self.usb), {}, fresh)

        stats = sync.mirror(result, fresh, RUN_ID)

        self.assertEqual(stats.copied, 1)
        self.assertEqual((fresh / "a.txt").read_text(encoding="utf-8"), "a")

    def test_overwrites_modified_file(self) -> None:
        write_file(self.usb / "a.txt", "new content")
        write_file(self.work / "a.txt", "old")
        result = self.make_plan()

        stats = sync.mirror(result, self.work, RUN_ID)

        self.assertEqual(stats.copied, 1)
        self.assertEqual((self.work / "a.txt").read_text(encoding="utf-8"), "new content")

    def test_moves_deleted_into_run_directory(self) -> None:
        write_file(self.work / "gone.txt", "old")
        write_file(self.work / "sub" / "dir" / "gone 中文.txt", "old2")
        result = self.make_plan()

        stats = sync.mirror(result, self.work, RUN_ID)

        self.assertEqual(stats.deleted_moved, 2)
        self.assertEqual(stats.errors, [])
        self.assertFalse((self.work / "gone.txt").exists())
        self.assertFalse((self.work / "sub" / "dir" / "gone 中文.txt").exists())
        archived = self.work / DELETED_DIR / RUN_ID / "gone.txt"
        self.assertEqual(archived.read_text(encoding="utf-8"), "old")
        archived_deep = self.work / DELETED_DIR / RUN_ID / "sub" / "dir" / "gone 中文.txt"
        self.assertEqual(archived_deep.read_text(encoding="utf-8"), "old2")

    def test_delete_falls_back_to_copy_on_cross_device(self) -> None:
        write_file(self.work / "gone.txt", "payload")
        result = self.make_plan()
        original = os.replace
        calls = {"count": 0}

        def flaky(source, target):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError(errno.EXDEV, "invalid cross-device link")
            return original(source, target)

        with mock.patch.object(os, "replace", side_effect=flaky):
            stats = sync.mirror(result, self.work, "runx")

        self.assertEqual(stats.deleted_moved, 1)
        self.assertFalse((self.work / "gone.txt").exists())
        archived = self.work / DELETED_DIR / "runx" / "gone.txt"
        self.assertEqual(archived.read_text(encoding="utf-8"), "payload")
        self.assertGreaterEqual(calls["count"], 2)

    def test_single_failure_is_collected_and_raised(self) -> None:
        write_file(self.usb / "ok.txt", "ok")
        write_file(self.usb / "bad.txt", "bad")
        result = self.make_plan()
        original = util.copy_file_atomic

        def failing(source, target):
            if str(source).endswith("bad.txt"):
                raise util.MyuError("模拟写入失败")
            return original(source, target)

        with mock.patch("myu.sync.copy_file_atomic", side_effect=failing):
            with self.assertRaises(util.MyuError) as ctx:
                sync.mirror(result, self.work, "run-fail")

        message = str(ctx.exception)
        self.assertIn("bad.txt", message)
        self.assertIn("1 个文件处理失败", message)
        self.assertIn("模拟写入失败", message)
        self.assertTrue((self.work / "ok.txt").is_file())
        self.assertFalse((self.work / "bad.txt").exists())

    def test_delete_failure_is_collected_and_raised(self) -> None:
        write_file(self.work / "gone.txt", "keep me")
        result = self.make_plan()

        with mock.patch.object(os, "replace", side_effect=OSError(errno.EXDEV, "cross-device")):
            with mock.patch("myu.sync.copy_file_atomic", side_effect=util.MyuError("磁盘只读")):
                with self.assertRaises(util.MyuError) as ctx:
                    sync.mirror(result, self.work, "run-del-fail")

        message = str(ctx.exception)
        self.assertIn("gone.txt", message)
        self.assertIn("磁盘只读", message)
        self.assertTrue((self.work / "gone.txt").is_file())

    def test_progress_and_stats(self) -> None:
        write_file(self.usb / "a.txt", "1")
        write_file(self.work / "b.txt", "2")
        result = self.make_plan()
        seen: list = []

        stats = sync.mirror(
            result,
            self.work,
            RUN_ID,
            progress=lambda done, total, relpath: seen.append((done, total, relpath)),
        )

        self.assertEqual(seen, [(1, 2, "a.txt"), (2, 2, "b.txt")])
        self.assertEqual(stats.copied, 1)
        self.assertEqual(stats.deleted_moved, 1)
        self.assertEqual(stats.bytes_copied, 1)
        self.assertEqual(stats.errors, [])

    def test_plan_defaults(self) -> None:
        result = Plan()

        self.assertEqual(result.added, [])
        self.assertEqual(result.modified, [])
        self.assertEqual(result.unchanged, [])
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.changed_bytes(), 0)
        self.assertEqual(result.total_bytes(), 0)

    def test_empty_run_id_raises(self) -> None:
        write_file(self.usb / "a.txt", "a")
        result = self.make_plan()

        with self.assertRaises(util.MyuError):
            sync.mirror(result, self.work, "   ")
        self.assertFalse((self.work / "a.txt").exists())

    def test_mirror_stats_defaults(self) -> None:
        stats = MirrorStats()

        self.assertEqual(stats.copied, 0)
        self.assertEqual(stats.deleted_moved, 0)
        self.assertEqual(stats.bytes_copied, 0)
        self.assertEqual(stats.errors, [])


if __name__ == "__main__":
    unittest.main()
