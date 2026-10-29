from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from myu.config import Config
from myu.index import Store
from myu.repo import GitRepo
from myu.restore import restore
from myu.util import MyuError

RUN_ID = "20250316T101500Z"
SERIAL = "USB-SERIAL-01"
DELETED_FILES = ("_deleted/20250101T000000Z/gone.txt",)

FILES = {
    "a.txt": b"hello\n",
    "空格 文件.txt": "中文内容\n".encode("utf-8"),
    "sub/深层/b c.txt": b"nested\n",
    "sub/line.txt": b"line1\r\nline2\r\n",
    "no-mtime.txt": b"kept\n",
    "_deleted/20250101T000000Z/gone.txt": b"gone\n",
}

# 100ns 整数倍的值在 NTFS 上可逐位落盘；非整数倍会被四舍五入到 100ns 边界。
MTIMES = {
    "a.txt": 1711000000123456700,
    "空格 文件.txt": 1711000000123456789,
    "sub/深层/b c.txt": 1711000000000000000,
    "sub/line.txt": 1711000001000000000,
}


class RestoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.work_tree = self.root / "repo"
        self.git = GitRepo(self.work_tree)
        self.git.init()
        for relpath, data in FILES.items():
            path = self.work_tree / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git.add_all()
        self.commit = self.git.commit(f"run {RUN_ID}: +5 ~0 -0")
        self.assertIsNotNone(self.commit)
        self.git.tag(f"run-{RUN_ID}")
        self.state_dir = self.root / "state"
        self.store = Store(self.state_dir / "myu.db")
        self.addCleanup(self.store.close)
        self.store.upsert_device(SERIAL, label="测试盘")
        self.store.start_run(RUN_ID, SERIAL, "2025-03-16T10:15:00Z")
        self.restorable = {
            relpath: data for relpath, data in FILES.items() if not relpath.startswith("_deleted/")
        }
        self.store.finish_run(
            RUN_ID,
            "completed",
            added=len(self.restorable),
            bytes_copied=sum(len(data) for data in self.restorable.values()),
            git_commit=self.commit,
        )
        self.store.replace_entries(
            SERIAL,
            RUN_ID,
            [
                {"relpath": relpath, "size": len(FILES[relpath]), "mtime_ns": mtime_ns, "blob_sha": None}
                for relpath, mtime_ns in MTIMES.items()
            ],
        )
        self.cfg = Config(repo_root=self.work_tree, state_dir=self.state_dir, log_dir=self.root / "logs")
        self.dest = self.root / "restored"

    def names(self, dest: Path | None = None) -> list:
        base = self.dest if dest is None else dest
        return sorted(path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file())

    def total_bytes(self) -> int:
        return sum(len(data) for data in self.restorable.values())


class FullRestoreTest(RestoreTestCase):
    def test_restores_every_file_byte_by_byte(self) -> None:
        stats = restore(self.cfg, self.store, RUN_ID, self.dest)
        self.assertEqual(stats.files, len(self.restorable))
        self.assertEqual(stats.bytes, self.total_bytes())
        self.assertEqual(stats.skipped, 0)
        for relpath, data in self.restorable.items():
            with self.subTest(relpath=relpath):
                self.assertEqual((self.dest / relpath).read_bytes(), data)

    def test_crlf_content_survives_round_trip(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest)
        self.assertEqual((self.dest / "sub/line.txt").read_bytes(), b"line1\r\nline2\r\n")

    def test_unicode_and_space_names(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest)
        plain = self.dest / "空格 文件.txt"
        deep = self.dest / "sub" / "深层" / "b c.txt"
        self.assertTrue(plain.is_file())
        self.assertTrue(deep.is_file())
        self.assertEqual(plain.read_bytes(), FILES["空格 文件.txt"])
        self.assertEqual(deep.read_bytes(), FILES["sub/深层/b c.txt"])

    def test_deleted_graveyard_is_not_restored(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest)
        self.assertFalse((self.dest / "_deleted").exists())
        self.assertNotIn("_deleted/20250101T000000Z/gone.txt", self.names())

    def test_no_staging_leftovers(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest)
        leftovers = [path.name for path in self.dest.iterdir() if path.name.startswith(".myu-restore-")]
        self.assertEqual(leftovers, [])

    def test_dest_is_created(self) -> None:
        self.assertFalse(self.dest.exists())
        restore(self.cfg, self.store, RUN_ID, self.dest)
        self.assertTrue(self.dest.is_dir())


class RunIdFormTest(RestoreTestCase):
    def test_plain_run_id(self) -> None:
        stats = restore(self.cfg, self.store, RUN_ID, self.dest)
        self.assertEqual(stats.files, len(self.restorable))

    def test_tag_form(self) -> None:
        stats = restore(self.cfg, self.store, f"run-{RUN_ID}", self.dest)
        self.assertEqual(stats.files, len(self.restorable))
        self.assertEqual(stats.bytes, self.total_bytes())

    def test_both_forms_give_same_tree(self) -> None:
        plain_dest = self.root / "plain"
        tagged_dest = self.root / "tagged"
        first = restore(self.cfg, self.store, RUN_ID, plain_dest)
        second = restore(self.cfg, self.store, f"run-{RUN_ID}", tagged_dest)
        self.assertEqual(first, second)
        self.assertEqual(self.names(plain_dest), self.names(tagged_dest))

    def test_unknown_run_id_raises(self) -> None:
        with self.assertRaises(MyuError) as caught:
            restore(self.cfg, self.store, "20250101T000000Z", self.dest)
        self.assertIn("找不到备份运行", str(caught.exception))
        self.assertFalse(self.dest.exists())

    def test_missing_repo_raises(self) -> None:
        cfg = Config(repo_root=self.root / "nope", state_dir=self.state_dir, log_dir=self.root / "logs")
        with self.assertRaises(MyuError):
            restore(cfg, self.store, RUN_ID, self.dest)


class SubpathTest(RestoreTestCase):
    def test_subpath_restores_only_that_directory(self) -> None:
        stats = restore(self.cfg, self.store, RUN_ID, self.dest, subpath="sub")
        self.assertEqual(self.names(), ["line.txt", "深层/b c.txt"])
        self.assertEqual(stats.files, 2)
        self.assertEqual(stats.bytes, len(FILES["sub/line.txt"]) + len(FILES["sub/深层/b c.txt"]))

    def test_subpath_trims_leading_and_trailing_slash(self) -> None:
        stats = restore(self.cfg, self.store, RUN_ID, self.dest, subpath="/sub/")
        self.assertEqual(stats.files, 2)
        self.assertEqual(self.names(), ["line.txt", "深层/b c.txt"])

    def test_subpath_pointing_at_one_file(self) -> None:
        stats = restore(self.cfg, self.store, RUN_ID, self.dest, subpath="sub/line.txt")
        self.assertEqual(stats.files, 1)
        self.assertEqual(self.names(), ["line.txt"])
        self.assertEqual((self.dest / "line.txt").read_bytes(), FILES["sub/line.txt"])

    def test_unknown_subpath_raises(self) -> None:
        with self.assertRaises(MyuError) as caught:
            restore(self.cfg, self.store, RUN_ID, self.dest, subpath="missing")
        self.assertIn("备份中不存在路径", str(caught.exception))


class OverwriteTest(RestoreTestCase):
    def test_existing_file_is_skipped_and_untouched(self) -> None:
        self.dest.mkdir(parents=True)
        keep = self.dest / "a.txt"
        keep.write_bytes(b"do not touch")
        stats = restore(self.cfg, self.store, RUN_ID, self.dest)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.files, len(self.restorable) - 1)
        self.assertEqual(stats.bytes, self.total_bytes() - len(FILES["a.txt"]))
        self.assertEqual(keep.read_bytes(), b"do not touch")

    def test_overwrite_true_replaces_file(self) -> None:
        self.dest.mkdir(parents=True)
        keep = self.dest / "a.txt"
        keep.write_bytes(b"old")
        stats = restore(self.cfg, self.store, RUN_ID, self.dest, overwrite=True)
        self.assertEqual(stats.skipped, 0)
        self.assertEqual(stats.files, len(self.restorable))
        self.assertEqual(keep.read_bytes(), FILES["a.txt"])

    def test_skip_is_per_file(self) -> None:
        self.dest.mkdir(parents=True)
        (self.dest / "a.txt").write_bytes(b"old-a")
        (self.dest / "sub").mkdir()
        (self.dest / "sub" / "line.txt").write_bytes(b"old-line")
        stats = restore(self.cfg, self.store, RUN_ID, self.dest)
        self.assertEqual(stats.skipped, 2)
        self.assertEqual(stats.files, len(self.restorable) - 2)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"old-a")
        self.assertEqual((self.dest / "sub" / "line.txt").read_bytes(), b"old-line")
        self.assertEqual((self.dest / "no-mtime.txt").read_bytes(), FILES["no-mtime.txt"])


class MtimeTest(RestoreTestCase):
    def test_mtime_is_restored(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest)
        actual = os.stat(self.dest / "a.txt").st_mtime_ns
        # 1711000000123456700 是 100ns 的整数倍，NTFS 时间戳粒度为 100ns，可逐位落盘。
        self.assertEqual(actual, MTIMES["a.txt"])

    def test_mtime_within_ntfs_granularity(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest)
        actual = os.stat(self.dest / "空格 文件.txt").st_mtime_ns
        # 该值不是 100ns 整数倍，NTFS 只能精确到 100ns：误差应远小于 1 秒。
        self.assertLessEqual(abs(actual - MTIMES["空格 文件.txt"]), 1000)

    def test_mtime_restored_with_subpath(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest, subpath="sub")
        # subpath 会把前缀剥掉，但回填仍要按仓库内原路径 sub/line.txt 查到条目。
        self.assertEqual(os.stat(self.dest / "line.txt").st_mtime_ns, MTIMES["sub/line.txt"])

    def test_missing_entry_keeps_extraction_time(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest)
        actual = os.stat(self.dest / "no-mtime.txt").st_mtime_ns
        self.assertNotIn(actual, MTIMES.values())
        self.assertLess(abs(time.time_ns() - actual), 300 * 10 ** 9)

    def test_preserve_mtime_false_leaves_now(self) -> None:
        restore(self.cfg, self.store, RUN_ID, self.dest, preserve_mtime=False)
        actual = os.stat(self.dest / "a.txt").st_mtime_ns
        self.assertNotEqual(actual, MTIMES["a.txt"])
        self.assertLess(abs(time.time_ns() - actual), 300 * 10 ** 9)

    def test_store_without_run_row_does_not_fail(self) -> None:
        empty = Store(self.root / "empty-state" / "myu.db")
        self.addCleanup(empty.close)
        stats = restore(self.cfg, empty, RUN_ID, self.dest)
        self.assertEqual(stats.files, len(self.restorable))
        self.assertNotEqual(os.stat(self.dest / "a.txt").st_mtime_ns, MTIMES["a.txt"])


class DryRunTest(RestoreTestCase):
    def test_dry_run_creates_nothing(self) -> None:
        stats = restore(self.cfg, self.store, RUN_ID, self.dest, dry_run=True)
        self.assertFalse(self.dest.exists())
        self.assertEqual(stats.files, len(self.restorable))
        self.assertEqual(stats.bytes, self.total_bytes())
        self.assertEqual(stats.skipped, 0)

    def test_dry_run_does_not_touch_existing_tree(self) -> None:
        self.dest.mkdir(parents=True)
        (self.dest / "a.txt").write_bytes(b"old")
        before = sorted(path.name for path in self.dest.iterdir())
        stats = restore(self.cfg, self.store, RUN_ID, self.dest, dry_run=True)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.files, len(self.restorable) - 1)
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), before)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"old")

    def test_dry_run_matches_real_run(self) -> None:
        dry = restore(self.cfg, self.store, RUN_ID, self.root / "dry-dest", dry_run=True)
        real = restore(self.cfg, self.store, RUN_ID, self.root / "real-dest")
        self.assertEqual((dry.files, dry.bytes, dry.skipped), (real.files, real.bytes, real.skipped))

    def test_dry_run_with_subpath(self) -> None:
        stats = restore(self.cfg, self.store, RUN_ID, self.dest, subpath="sub", dry_run=True)
        self.assertEqual(stats.files, 2)
        self.assertFalse(self.dest.exists())


class MultiRunCase(unittest.TestCase):
    """按顺序造多次运行：每次提交就是工作区当时的快照，被删掉的文件留在 _deleted/<run_id>/ 里。"""

    RUN1 = "20250309T101500Z"
    RUN2 = "20250316T101500Z"
    RUN3 = "20250323T101500Z"
    RUN4 = "20250330T101500Z"
    LEGACY_RUN = "20250101T000000Z"
    SERIAL = "USB-SERIAL-02"
    BASE = {
        "gone.txt": "run2 删掉我\n".encode("utf-8"),
        "sub/nested.txt": b"run3 nested\n",
        "solong.txt": b"run4 solong\n",
        "keep.txt": b"keep\n",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.work_tree = self.root / "repo"
        self.git = GitRepo(self.work_tree)
        self.git.init()
        self.state_dir = self.root / "state"
        self.store = Store(self.state_dir / "myu.db")
        self.addCleanup(self.store.close)
        self.store.upsert_device(self.SERIAL, label="测试盘")
        self.cfg = Config(
            repo_root=self.work_tree, state_dir=self.state_dir, log_dir=self.root / "logs"
        )
        self.dest = self.root / "restored"
        snap1 = dict(self.BASE)
        # 更早某次运行留下的墓碑：任何一次 restore 都不该把它翻出来
        snap2 = {path: data for path, data in snap1.items() if path != "gone.txt"}
        snap2[self.grave(self.RUN2, "gone.txt")] = snap1["gone.txt"]
        snap2[self.grave(self.LEGACY_RUN, "legacy.txt")] = b"legacy grave\n"
        snap3 = {path: data for path, data in snap2.items() if path != "sub/nested.txt"}
        snap3[self.grave(self.RUN3, "sub/nested.txt")] = snap2["sub/nested.txt"]
        snap4 = {path: data for path, data in snap3.items() if path != "solong.txt"}
        snap4[self.grave(self.RUN4, "solong.txt")] = snap3["solong.txt"]
        self.trees = {}
        for run_id, snapshot in (
            (self.RUN1, snap1),
            (self.RUN2, snap2),
            (self.RUN3, snap3),
            (self.RUN4, snap4),
        ):
            self.trees[run_id] = self.commit_run(run_id, snapshot)

    @staticmethod
    def grave(run_id: str, relpath: str) -> str:
        return f"_deleted/{run_id}/{relpath}"

    @staticmethod
    def live(files: dict) -> dict:
        return {
            path: data for path, data in files.items() if not path.startswith("_deleted/")
        }

    def commit_run(self, run_id: str, files: dict) -> dict:
        for child in list(self.work_tree.iterdir()):
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for relpath, data in files.items():
            path = self.work_tree / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git.add_all()
        commit = self.git.commit(f"run {run_id}: snapshot")
        self.assertIsNotNone(commit)
        self.git.tag(f"run-{run_id}")
        self.store.start_run(run_id, self.SERIAL, "2025-03-09T10:15:00Z")
        self.store.finish_run(run_id, "completed", added=len(files), git_commit=commit)
        return files

    def names(self, dest: Path | None = None) -> list:
        base = self.dest if dest is None else dest
        return sorted(path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file())

    @staticmethod
    def size_of(files: dict) -> int:
        return sum(len(data) for data in files.values())


class IncludeDeletedTest(MultiRunCase):
    """默认语义不变，只有显式 include_deleted 才把本次运行删掉的文件按原路径捞回来。"""

    def test_default_restore_does_not_bring_tombstones(self) -> None:
        stats = restore(self.cfg, self.store, self.RUN2, self.dest)
        live = self.live(self.trees[self.RUN2])
        self.assertNotIn("gone.txt", self.names())
        self.assertEqual(self.names(), sorted(live))
        self.assertEqual(stats.files, len(live))
        self.assertEqual(stats.bytes, self.size_of(live))
        self.assertEqual(stats.skipped, 0)
        self.assertFalse((self.dest / "_deleted").exists())

    def test_include_deleted_restores_file_byte_by_byte(self) -> None:
        stats = restore(self.cfg, self.store, self.RUN2, self.dest, include_deleted=True)
        live = self.live(self.trees[self.RUN2])
        self.assertEqual((self.dest / "gone.txt").read_bytes(), self.BASE["gone.txt"])
        self.assertEqual(stats.files, len(live) + 1)
        self.assertEqual(stats.bytes, self.size_of(live) + len(self.BASE["gone.txt"]))
        self.assertEqual(stats.skipped, 0)
        self.assertFalse((self.dest / "_deleted").exists())

    def test_run_without_own_tombstones_restores_nothing_extra(self) -> None:
        # RUN1 的树里一条 _deleted/ 都没有：include_deleted 退化成 no-op，不能报错
        stats = restore(self.cfg, self.store, self.RUN1, self.dest, include_deleted=True)
        live = self.live(self.trees[self.RUN1])
        self.assertEqual(stats.files, len(live))
        self.assertEqual(stats.bytes, self.size_of(live))
        self.assertEqual(self.names(), sorted(live))
        self.assertEqual(stats.skipped, 0)

    def test_other_runs_tombstones_are_ignored(self) -> None:
        # 树里躺着更早运行的墓碑，restore 本次运行不该把它带出来
        restore(self.cfg, self.store, self.RUN2, self.dest, include_deleted=True)
        self.assertNotIn("legacy.txt", self.names())
        self.assertEqual((self.dest / "gone.txt").read_bytes(), self.BASE["gone.txt"])

    def test_only_own_tombstones_are_restored(self) -> None:
        # RUN3 删了 sub/nested.txt；RUN2 删的 gone.txt 也在树里，但只该带回自己的那份
        stats = restore(self.cfg, self.store, self.RUN3, self.dest, include_deleted=True)
        live = self.live(self.trees[self.RUN3])
        self.assertEqual((self.dest / "sub/nested.txt").read_bytes(), self.BASE["sub/nested.txt"])
        self.assertNotIn("gone.txt", self.names())
        self.assertFalse((self.dest / "gone.txt").exists())
        self.assertEqual(stats.files, len(live) + 1)
        self.assertEqual(stats.skipped, 0)

    def test_three_runs_each_deleted_one_file(self) -> None:
        # RUN4 的树里同时有 RUN2/RUN3/RUN4 三份墓碑，只有 RUN4 那份落地
        stats = restore(self.cfg, self.store, self.RUN4, self.dest, include_deleted=True)
        names = self.names()
        self.assertIn("solong.txt", names)
        self.assertEqual((self.dest / "solong.txt").read_bytes(), self.BASE["solong.txt"])
        self.assertNotIn("gone.txt", names)
        self.assertNotIn("sub/nested.txt", names)
        self.assertEqual(stats.files, len(self.live(self.trees[self.RUN4])) + 1)

    def test_tag_form_also_uses_own_tombstones(self) -> None:
        stats = restore(self.cfg, self.store, f"run-{self.RUN2}", self.dest, include_deleted=True)
        self.assertEqual((self.dest / "gone.txt").read_bytes(), self.BASE["gone.txt"])
        self.assertEqual(stats.files, len(self.live(self.trees[self.RUN2])) + 1)

    def test_existing_tombstone_target_is_skipped_and_untouched(self) -> None:
        self.dest.mkdir(parents=True)
        keep = self.dest / "gone.txt"
        keep.write_bytes(b"do not touch")
        stats = restore(self.cfg, self.store, self.RUN2, self.dest, include_deleted=True)
        live = self.live(self.trees[self.RUN2])
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.files, len(live))
        self.assertEqual(stats.bytes, self.size_of(live))
        self.assertEqual(keep.read_bytes(), b"do not touch")

    def test_overwrite_true_replaces_tombstone_target(self) -> None:
        self.dest.mkdir(parents=True)
        keep = self.dest / "gone.txt"
        keep.write_bytes(b"old")
        stats = restore(
            self.cfg, self.store, self.RUN2, self.dest, include_deleted=True, overwrite=True
        )
        self.assertEqual(stats.skipped, 0)
        self.assertEqual(keep.read_bytes(), self.BASE["gone.txt"])

    def test_subpath_filters_tombstones_by_original_path(self) -> None:
        # RUN3 的活文件里已经没有 sub/ 了，subpath 只命中墓碑时也要能恢复
        stats = restore(
            self.cfg, self.store, self.RUN3, self.dest, subpath="sub", include_deleted=True
        )
        self.assertEqual(self.names(), ["nested.txt"])
        self.assertEqual((self.dest / "nested.txt").read_bytes(), self.BASE["sub/nested.txt"])
        self.assertEqual(stats.files, 1)
        self.assertEqual(stats.bytes, len(self.BASE["sub/nested.txt"]))

    def test_subpath_excludes_tombstones_outside_it(self) -> None:
        # gone.txt 的墓碑在根上，subpath=sub 不该把它带出来
        restore(
            self.cfg, self.store, self.RUN2, self.dest, subpath="sub", include_deleted=True
        )
        self.assertEqual(self.names(), ["nested.txt"])
        self.assertFalse((self.dest / "gone.txt").exists())

    def test_dry_run_counts_tombstones_without_writing(self) -> None:
        dry = restore(
            self.cfg, self.store, self.RUN3, self.dest, dry_run=True, include_deleted=True
        )
        self.assertFalse(self.dest.exists())
        real = restore(self.cfg, self.store, self.RUN3, self.dest, include_deleted=True)
        self.assertEqual(
            (dry.files, dry.bytes, dry.skipped), (real.files, real.bytes, real.skipped)
        )
        self.assertEqual(dry.files, len(self.live(self.trees[self.RUN3])) + 1)

    def test_tombstone_mtime_follows_entry_when_present(self) -> None:
        # preserve_mtime 的口径与普通文件一致：按仓库内原路径（_deleted/<run_id>/...）查条目
        mtime_ns = 1711000000123456700
        self.store.replace_entries(
            self.SERIAL,
            self.RUN2,
            [
                {
                    "relpath": self.grave(self.RUN2, "gone.txt"),
                    "size": len(self.BASE["gone.txt"]),
                    "mtime_ns": mtime_ns,
                    "blob_sha": None,
                }
            ],
        )
        kept = self.root / "kept-mtime"
        restore(self.cfg, self.store, self.RUN2, kept, include_deleted=True)
        self.assertEqual(os.stat(kept / "gone.txt").st_mtime_ns, mtime_ns)
        now_dest = self.root / "now-mtime"
        restore(
            self.cfg, self.store, self.RUN2, now_dest, include_deleted=True, preserve_mtime=False
        )
        self.assertNotEqual(os.stat(now_dest / "gone.txt").st_mtime_ns, mtime_ns)


if __name__ == "__main__":
    unittest.main()
