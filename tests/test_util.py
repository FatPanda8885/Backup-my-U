from __future__ import annotations

import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

from myu import util


def _close_myu_handlers() -> None:
    logger = logging.getLogger("myu")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


class MyuErrorTest(unittest.TestCase):
    def test_message(self) -> None:
        self.assertTrue(issubclass(util.MyuError, Exception))
        self.assertEqual(str(util.MyuError("另一个备份进程正在运行")), "另一个备份进程正在运行")


class ParseSizeTest(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(util.parse_size("512m"), 536870912)
        self.assertEqual(util.parse_size("512M"), 536870912)
        self.assertEqual(util.parse_size("1k"), 1024)
        self.assertEqual(util.parse_size("2kb"), 2048)
        self.assertEqual(util.parse_size("3g"), 3 * 1024 ** 3)
        self.assertEqual(util.parse_size("1t"), 1024 ** 4)
        self.assertEqual(util.parse_size("1024"), 1024)
        self.assertEqual(util.parse_size(" 512m "), 536870912)

    def test_invalid(self) -> None:
        for text in ("", "abc", "12x", "-1m", "1.5g", "m", "12 m b"):
            with self.subTest(text=text):
                with self.assertRaises(util.MyuError):
                    util.parse_size(text)


class HumanBytesTest(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(util.human_bytes(1500000), "1.43 MB")
        self.assertEqual(util.human_bytes(0), "0 B")
        self.assertEqual(util.human_bytes(999), "999 B")
        self.assertEqual(util.human_bytes(1024), "1.00 KB")
        self.assertEqual(util.human_bytes(1024 ** 3), "1.00 GB")
        self.assertEqual(util.human_bytes(5 * 1024 ** 4), "5.00 TB")


class TimestampTest(unittest.TestCase):
    def test_utc_now_iso(self) -> None:
        text = util.utc_now_iso()
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_new_run_id(self) -> None:
        moment = datetime(2025, 3, 16, 10, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(util.new_run_id(moment), "20250316T101500Z")
        self.assertEqual(util.new_run_id(datetime(2025, 3, 16, 10, 15, 0)), "20250316T101500Z")
        shifted = datetime(2025, 3, 16, 18, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(util.new_run_id(shifted), "20250316T101500Z")
        self.assertRegex(util.new_run_id(), r"^\d{8}T\d{6}Z$")


class PathMatchesTest(unittest.TestCase):
    def test_star_does_not_cross_slash(self) -> None:
        self.assertTrue(util.path_matches("a.tmp", ["*.tmp"]))
        self.assertTrue(util.path_matches("a/b/x.tmp", ["*.tmp"]))
        self.assertFalse(util.path_matches("a/b/x.txt", ["*.tmp"]))
        self.assertTrue(util.path_matches("a/b/c.txt", ["a/*/c.txt"]))
        self.assertFalse(util.path_matches("a/b/c.txt", ["a/*"]))

    def test_double_star_crosses_levels(self) -> None:
        self.assertTrue(util.path_matches("a/b/c.txt", ["a/**"]))
        self.assertTrue(util.path_matches("a/b/c/d.txt", ["a/**"]))
        self.assertFalse(util.path_matches("a.txt", ["a/**"]))
        self.assertTrue(util.path_matches("System Volume Information/desktop.ini",
                                         ["System Volume Information/**"]))
        self.assertTrue(util.path_matches("foo", ["**/foo"]))
        self.assertTrue(util.path_matches("x/y/foo", ["**/foo"]))
        self.assertTrue(util.path_matches("anything/at/all", ["**"]))

    def test_without_leading_slash_matches_any_level(self) -> None:
        self.assertTrue(util.path_matches("pagefile.sys", ["pagefile.sys"]))
        self.assertTrue(util.path_matches("deep/dir/pagefile.sys", ["pagefile.sys"]))
        self.assertTrue(util.path_matches("_deleted/run/a.txt", ["_deleted/**"]))
        self.assertTrue(util.path_matches("x/.Trash-1000/a", [".Trash-*/**"]))

    def test_with_leading_slash_is_anchored(self) -> None:
        self.assertTrue(util.path_matches("x/a.txt", ["/x/*.txt"]))
        self.assertFalse(util.path_matches("y/x/a.txt", ["/x/*.txt"]))

    def test_question_mark_single_char(self) -> None:
        self.assertTrue(util.path_matches("abc.tmp", ["a?c.tmp"]))
        self.assertFalse(util.path_matches("ac.tmp", ["a?c.tmp"]))
        self.assertFalse(util.path_matches("a/c.tmp", ["a?c.tmp"]))

    def test_multiple_patterns_and_empty(self) -> None:
        patterns = ("*.tmp", "pagefile.sys")
        self.assertTrue(util.path_matches("x/y.tmp", patterns))
        self.assertTrue(util.path_matches("y/pagefile.sys", patterns))
        self.assertFalse(util.path_matches("y/keep.dat", patterns))
        self.assertFalse(util.path_matches("y/keep.dat", []))
        self.assertFalse(util.path_matches("y/keep.dat", None))
        self.assertTrue(util.path_matches("a/x.tmp", "*.tmp"))
        self.assertFalse(util.path_matches("a/x.tmp", ""))
        self.assertTrue(util.path_matches("a\\x.tmp", ["*.tmp"]))


class NormalizeRelpathTest(unittest.TestCase):
    def test_normalizes(self) -> None:
        self.assertEqual(util.normalize_relpath("a\\b\\c"), "a/b/c")
        self.assertEqual(util.normalize_relpath("/a/b"), "a/b")
        self.assertEqual(util.normalize_relpath("a//b/"), "a/b")
        self.assertEqual(util.normalize_relpath("./a/./b"), "a/b")
        self.assertEqual(util.normalize_relpath("文档/中文 名称.txt"), "文档/中文 名称.txt")

    def test_rejects_parent_segments(self) -> None:
        for text in ("..", "../a", "a/../../b", "a\\..\\b"):
            with self.subTest(text=text):
                with self.assertRaises(util.MyuError):
                    util.normalize_relpath(text)


class IterFilesTest(unittest.TestCase):
    def test_lists_files_and_applies_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "System Volume Information").mkdir()
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "skip.tmp").write_text("t", encoding="utf-8")
            (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
            (root / "sub" / "c.tmp").write_text("c", encoding="utf-8")
            (root / "System Volume Information" / "desktop.ini").write_text("d", encoding="utf-8")
            found = sorted(path.name for path in util.iter_files(root, ("*.tmp",)))
            self.assertEqual(found, ["a.txt", "b.txt", "desktop.ini"])
            found = sorted(
                path.name
                for path in util.iter_files(root, ("*.tmp", "System Volume Information/**"))
            )
            self.assertEqual(found, ["a.txt", "b.txt"])
            self.assertEqual(len(list(util.iter_files(root, None))), 5)

    def test_skips_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "real.txt"
            target.write_text("x", encoding="utf-8")
            try:
                os.symlink(target, root / "link.txt")
            except (OSError, NotImplementedError):
                self.skipTest("当前环境不允许创建符号链接")
            found = sorted(path.name for path in util.iter_files(root, None))
            self.assertEqual(found, ["real.txt"])

    def test_missing_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(util.MyuError):
                list(util.iter_files(Path(tmp) / "nope", None))


class AtomicFileTest(unittest.TestCase):
    def test_copy_file_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src.bin"
            payload = "内容 content".encode("utf-8") * 100
            src.write_bytes(payload)
            dst = root / "out" / "dst.bin"
            dst.parent.mkdir()
            self.assertEqual(util.copy_file_atomic(src, dst), len(payload))
            self.assertEqual(dst.read_bytes(), payload)
            self.assertFalse((dst.parent / "dst.bin.myu-tmp").exists())
            (root / "second.bin").write_bytes(b"abc")
            second = util.copy_file_atomic(src, root / "second.bin")
            self.assertEqual(second, len(payload))
            self.assertEqual((root / "second.bin").read_bytes(), payload)

    def test_copy_file_atomic_cleans_up_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dst = root / "missing-dir" / "dst.bin"
            with self.assertRaises(util.MyuError):
                util.copy_file_atomic(root / "nope.bin", dst)
            self.assertFalse((dst.parent / "dst.bin.myu-tmp").exists())

    def test_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "a.myu-tmp"
            src.write_text("new", encoding="utf-8")
            dst = root / "a.txt"
            dst.write_text("old", encoding="utf-8")
            util.atomic_replace(src, dst)
            self.assertEqual(dst.read_text(encoding="utf-8"), "new")
            self.assertFalse(src.exists())

    def test_atomic_replace_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(util.MyuError):
                util.atomic_replace(root / "nope.myu-tmp", root / "x.txt")


class PasswordTest(unittest.TestCase):
    def test_hash_and_verify(self) -> None:
        stored = util.hash_password("秘密口令")
        parts = stored.split("$")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "pbkdf2_sha256")
        self.assertEqual(int(parts[1]), 200000)
        self.assertTrue(util.verify_password(stored, "秘密口令"))
        self.assertFalse(util.verify_password(stored, "错误口令"))
        self.assertNotEqual(stored, util.hash_password("秘密口令"))

    def test_custom_iterations(self) -> None:
        stored = util.hash_password("pw", 1000)
        self.assertEqual(stored.split("$")[1], "1000")
        self.assertTrue(util.verify_password(stored, "pw"))

    def test_malformed_stored(self) -> None:
        for stored in ("", "abc", "md5$1$a$b", "pbkdf2_sha256$xx$a$b",
                       "pbkdf2_sha256$1$!!$!!", "pbkdf2_sha256$1$YQ==$Yg==",
                       "pbkdf2_sha256$0$YWJj$YWJj"):
            with self.subTest(stored=stored):
                self.assertFalse(util.verify_password(stored, "pw"))


class FileLockTest(unittest.TestCase):
    def test_single_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "myu.lock"
            with util.FileLock(path):
                self.assertTrue(path.exists())
                with self.assertRaises(util.MyuError) as caught:
                    with util.FileLock(path):
                        pass
                self.assertEqual(str(caught.exception), "另一个备份进程正在运行")
            with util.FileLock(path):
                pass

    def test_timeout_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "myu.lock"
            with util.FileLock(path):
                with self.assertRaises(util.MyuError):
                    with util.FileLock(path, timeout=0.1):
                        pass


class SetupLoggerTest(unittest.TestCase):
    def tearDown(self) -> None:
        _close_myu_handlers()

    def test_writes_to_file_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            stream = io.StringIO()
            with redirect_stderr(stream):
                logger = util.setup_logger(log_dir, run_id="20250316T101500Z")
                logger.info("开始备份 %s", "中文")
                self.assertEqual(len(logger.handlers), 2)
            _close_myu_handlers()
            log_file = log_dir / "20250316T101500Z.log"
            self.assertTrue(log_file.is_file())
            text = log_file.read_text(encoding="utf-8")
            self.assertIn("INFO    开始备份 中文", text)
            self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO")
            self.assertIn("开始备份 中文", stream.getvalue())

    def test_without_run_id_only_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            stream = io.StringIO()
            with redirect_stderr(stream):
                logger = util.setup_logger(log_dir)
                logger.warning("仅控制台")
                self.assertEqual(len(logger.handlers), 1)
            self.assertFalse(log_dir.exists())
            self.assertIn("仅控制台", stream.getvalue())

    def test_verbose_and_repeat_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            with redirect_stderr(io.StringIO()):
                logger = util.setup_logger(log_dir, run_id="a", verbose=True)
                self.assertEqual(logger.level, logging.DEBUG)
                logger = util.setup_logger(log_dir, run_id="b", verbose=False)
                self.assertEqual(len(logger.handlers), 2)
                self.assertEqual(logger.level, logging.INFO)
            _close_myu_handlers()
            self.assertTrue((log_dir / "b.log").is_file())
            self.assertEqual((log_dir / "a.log").read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
