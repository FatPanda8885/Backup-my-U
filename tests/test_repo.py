from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myu import repo


def _git_blob_sha(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.work_tree = self.root / "repo"
        self.repo = repo.GitRepo(self.work_tree)

    def write(self, relpath: str, text: str) -> Path:
        path = self.work_tree / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

class InitTest(RepoTestCase):
    def test_exists(self) -> None:
        self.assertFalse(self.repo.exists())
        self.assertEqual(self.repo.work_tree, self.work_tree)
        self.repo.init()
        self.assertTrue(self.repo.exists())
        self.assertTrue((self.work_tree / ".git").is_dir())

    def test_local_config(self) -> None:
        self.repo.init(big_file_threshold="64m")
        def config(name: str) -> str:
            return self.repo.run("config", name).stdout.decode("utf-8").strip()

        self.assertEqual(config("user.name"), "Backup-my-U")
        self.assertEqual(config("user.email"), "myu@localhost")
        self.assertEqual(config("core.autocrlf"), "false")
        self.assertEqual(config("core.quotepath"), "false")
        self.assertEqual(config("core.bigFileThreshold"), "64m")
        self.assertEqual(config("gc.auto"), "0")


class CommitTest(RepoTestCase):
    def test_commit_returns_sha_then_none(self) -> None:
        self.repo.init()
        self.assertIsNone(self.repo.commit("空的提交"))
        self.write("a.txt", "hello")
        self.assertFalse(self.repo.has_staged_changes())
        self.repo.add_all()
        self.assertTrue(self.repo.has_staged_changes())
        first = self.repo.commit("run 20250316T101500Z: +1 ~0 -0")
        self.assertIsNotNone(first)
        self.assertEqual(len(first), 40)
        self.assertTrue(all(char in "0123456789abcdef" for char in first))
        self.assertEqual(self.repo.head(), first)
        self.assertFalse(self.repo.has_staged_changes())
        self.assertIsNone(self.repo.commit("没有变化"))
        self.assertEqual(self.repo.head(), first)

    def test_commit_message_is_recorded(self) -> None:
        self.repo.init()
        self.write("中文 名称.txt", "内容")
        self.repo.add_all()
        self.repo.commit("run x: +1 ~0 -0")
        subject = self.repo.run("log", "-1", "--pretty=format:%s").stdout.decode("utf-8")
        self.assertEqual(subject, "run x: +1 ~0 -0")


class TagTest(RepoTestCase):
    def test_tag_and_rev_parse(self) -> None:
        self.repo.init()
        self.assertIsNone(self.repo.head())
        self.assertIsNone(self.repo.rev_parse("HEAD"))
        self.assertIsNone(self.repo.rev_parse("run-20250316T101500Z"))
        self.write("a.txt", "1")
        self.repo.add_all()
        first = self.repo.commit("run 1")
        self.repo.tag("run-20250316T101500Z")
        self.assertEqual(self.repo.rev_parse("run-20250316T101500Z"), first)

        self.write("a.txt", "2")
        self.repo.add_all()
        second = self.repo.commit("run 2")
        self.repo.tag("run-20250316T101500Z")
        self.assertEqual(self.repo.rev_parse("run-20250316T101500Z"), second)
        self.assertNotEqual(first, second)


class DiffTest(RepoTestCase):
    def test_first_commit_all_added(self) -> None:
        self.repo.init()
        self.write("a.txt", "a")
        self.write("中文 名称.txt", "b")
        self.repo.add_all()
        first = self.repo.commit("run 1")
        self.assertEqual(
            sorted(self.repo.diff_name_status(None, first)),
            [("A", "a.txt"), ("A", "中文 名称.txt")],
        )

    def test_statuses_between_commits(self) -> None:
        self.repo.init()
        self.write("a.txt", "a")
        self.write("b.txt", "b")
        self.write("中文 名称.txt", "c")
        self.repo.add_all()
        first = self.repo.commit("run 1")

        self.write("a.txt", "changed")
        (self.work_tree / "b.txt").unlink()
        self.write("c.txt", "new")
        self.repo.add_all()
        second = self.repo.commit("run 2")

        statuses = {path: status for status, path in self.repo.diff_name_status(first, second)}
        self.assertEqual(statuses, {"a.txt": "M", "b.txt": "D", "c.txt": "A"})


class TreeTest(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.init()
        self.write("plain.txt", "hello")
        self.write("中文 名称.txt", "内容内容")
        self.write("sub dir/内层 文件.txt", "y")
        self.repo.add_all()
        self.sha = self.repo.commit("run 1")

    def test_tree_entries(self) -> None:
        entries = self.repo.tree_entries(self.sha)
        data = (self.work_tree / "中文 名称.txt").read_bytes()
        self.assertEqual(
            entries,
            {
                "plain.txt": (_git_blob_sha(b"hello"), 5),
                "中文 名称.txt": (_git_blob_sha(data), len(data)),
                "sub dir/内层 文件.txt": (_git_blob_sha(b"y"), 1),
            },
        )

    def test_tracked_files(self) -> None:
        expected = sorted(["plain.txt", "中文 名称.txt", "sub dir/内层 文件.txt"])
        self.assertEqual(sorted(self.repo.tracked_files(self.sha)), expected)
        self.assertEqual(sorted(self.repo.tracked_files()), expected)


class ArchiveTest(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.init()
        self.write("top.txt", "top")
        self.write("sub/a.txt", "aa")
        self.write("sub/中文 名称.txt", "内层内容")
        self.repo.add_all()
        self.sha = self.repo.commit("run 1")

    def test_archive_whole_tree(self) -> None:
        dest = self.root / "out"
        files, total = self.repo.archive_to(self.sha, dest)
        self.assertEqual(files, 3)
        self.assertEqual(total, len(b"top") + len(b"aa") + len("内层内容".encode("utf-8")))
        self.assertEqual((dest / "top.txt").read_text(encoding="utf-8"), "top")
        self.assertEqual((dest / "sub" / "a.txt").read_text(encoding="utf-8"), "aa")
        self.assertEqual((dest / "sub" / "中文 名称.txt").read_text(encoding="utf-8"), "内层内容")

    def test_archive_subpath_directory(self) -> None:
        dest = self.root / "sub-out"
        files, total = self.repo.archive_to(self.sha, dest, subpath="sub")
        self.assertEqual(files, 2)
        self.assertEqual(total, len(b"aa") + len("内层内容".encode("utf-8")))
        self.assertEqual((dest / "a.txt").read_text(encoding="utf-8"), "aa")
        self.assertEqual((dest / "中文 名称.txt").read_text(encoding="utf-8"), "内层内容")

    def test_archive_subpath_with_backslashes(self) -> None:
        dest = self.root / "sub-out2"
        files, _ = self.repo.archive_to(self.sha, dest, subpath="sub\\")
        self.assertEqual(files, 2)

    def test_archive_subpath_single_file(self) -> None:
        dest = self.root / "file-out"
        files, total = self.repo.archive_to(self.sha, dest, subpath="top.txt")
        self.assertEqual(files, 1)
        self.assertEqual(total, len(b"top"))
        self.assertEqual((dest / "top.txt").read_text(encoding="utf-8"), "top")
        nested = self.root / "nested-out"
        files, total = self.repo.archive_to(self.sha, nested, subpath="sub/a.txt")
        self.assertEqual(files, 1)
        self.assertEqual((nested / "a.txt").read_text(encoding="utf-8"), "aa")

    def test_archive_streams_without_run(self) -> None:
        dest = self.root / "stream-out"

        def forbidden(*args, **kwargs):
            raise AssertionError("archive_to 不得走 run 或 subprocess.run")

        expected_total = len(b"top") + len(b"aa") + len("内层内容".encode("utf-8"))
        with mock.patch.object(repo.GitRepo, "run", forbidden):
            with mock.patch.object(repo.subprocess, "run", forbidden):
                files, total = self.repo.archive_to(self.sha, dest)
        self.assertEqual(files, 3)
        self.assertEqual(total, expected_total)
        self.assertEqual((dest / "sub" / "中文 名称.txt").read_text(encoding="utf-8"), "内层内容")

    def test_archive_rejects_member_escaping_dest(self) -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            info = tarfile.TarInfo("../evil.txt")
            data = b"boom"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        payload.seek(0)

        class _FakeProcess:
            returncode = 0
            stdout = payload
            stderr = io.BytesIO(b"")

            def wait(self) -> int:
                return 0

        dest = self.root / "escape-out"
        with mock.patch.object(repo.GitRepo, "popen", return_value=_FakeProcess()):
            with self.assertRaises(repo.MyuError):
                self.repo.archive_to(self.sha, dest)
        self.assertFalse((self.root / "evil.txt").exists())

    def test_archive_bad_rev_reports_git_error(self) -> None:
        with self.assertRaises(repo.MyuError) as caught:
            self.repo.archive_to("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", self.root / "bad-out")
        self.assertIn("git archive", str(caught.exception))
        self.assertIn("失败", str(caught.exception))

    def test_show_bytes(self) -> None:
        self.assertEqual(self.repo.show_bytes(self.sha, "sub/中文 名称.txt"),
                         "内层内容".encode("utf-8"))
        self.assertEqual(self.repo.show_bytes(self.sha, "top.txt"), b"top")


class LogTest(RepoTestCase):
    def test_log_and_rev_count(self) -> None:
        self.repo.init()
        self.assertEqual(self.repo.log(), [])
        self.assertEqual(self.repo.rev_count(), 0)
        self.write("a.txt", "1")
        self.repo.add_all()
        first = self.repo.commit("run 1")
        self.write("a.txt", "2")
        self.repo.add_all()
        second = self.repo.commit("run 2: +0 ~1 -0")
        entries = self.repo.log(limit=10)
        self.assertEqual([entry["commit"] for entry in entries], [second, first])
        self.assertEqual([entry["subject"] for entry in entries], ["run 2: +0 ~1 -0", "run 1"])
        self.assertRegex(entries[0]["date"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(len(self.repo.log(limit=1)), 1)
        self.assertEqual(self.repo.rev_count(), 2)


class MaintenanceTest(RepoTestCase):
    def test_gc_and_fsck(self) -> None:
        self.repo.init()
        self.write("a.txt", "1")
        self.repo.add_all()
        self.repo.commit("run 1")
        self.assertTrue(self.repo.fsck())
        self.repo.gc()
        self.assertTrue(self.repo.fsck())


class FailureTest(RepoTestCase):
    def test_failed_command_raises_myuerror(self) -> None:
        self.repo.init()
        with self.assertRaises(repo.MyuError) as caught:
            self.repo.run("show", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        self.assertIn("失败", str(caught.exception))
        self.assertIn("git show", str(caught.exception))

    def test_check_false_returns_result(self) -> None:
        self.repo.init()
        result = self.repo.run("show", "deadbeef", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr)


if __name__ == "__main__":
    unittest.main()
