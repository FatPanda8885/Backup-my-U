from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myu import config


def _write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class DefaultConfigPathTest(unittest.TestCase):
    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"MYU_CONFIG": str(Path("a") / "b.ini")}):
            self.assertEqual(config.default_config_path(), Path("a") / "b.ini")

    def test_fallback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.default_config_path(), Path("/etc/myu/config.ini"))


class LoadConfigTest(unittest.TestCase):
    def test_template_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "cfg" / "config.ini"
            repo_root = root / "backup" / "repo"
            state_dir = root / "backup" / "state"
            config.write_template(cfg_path, repo_root=repo_root, state_dir=state_dir)
            self.assertTrue(cfg_path.is_file())
            cfg = config.load_config(cfg_path)
            self.assertEqual(cfg.repo_root, repo_root)
            self.assertEqual(cfg.state_dir, state_dir)
            self.assertEqual(cfg.log_dir, state_dir.parent / "logs")
            self.assertEqual(
                cfg.excludes,
                (
                    "$RECYCLE.BIN/**",
                    "System Volume Information/**",
                    ".Trashes/**",
                    ".Spotlight-V100/**",
                    ".fseventsd/**",
                    ".Trash-*/**",
                    "*.tmp",
                    "pagefile.sys",
                ),
            )
            self.assertEqual(cfg.device_serial, "")
            self.assertEqual(cfg.marker_file, ".myu-id")
            self.assertEqual(cfg.marker_value, "")
            self.assertEqual(cfg.big_file_threshold, "512m")
            self.assertEqual(cfg.gc_after_runs, 20)
            self.assertEqual(cfg.poll_guard_seconds, 3600)
            self.assertEqual(cfg.web_bind, "0.0.0.0")
            self.assertEqual(cfg.web_port, 8787)
            self.assertEqual(cfg.web_password_hash, "")
            self.assertEqual(Path(str(cfg.config_path)), cfg_path)

    def test_template_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.ini"
            config.write_template(cfg_path)
            text = cfg_path.read_text(encoding="utf-8")
            self.assertIn("repo_root = /srv/myu-backup/repo", text)
            self.assertIn("state_dir = /srv/myu-backup/state", text)
            self.assertIn("log_dir   = /srv/myu-backup/logs", text)
            self.assertIn("; 每行一个 glob，可缩进续写", text)
            self.assertIn("; 轮询兜底的最小间隔（秒）", text)
            self.assertIn("poll_guard_seconds = 3600", text)
            self.assertIn("password_hash =", text)
            self.assertNotIn("\r", text)

    def test_custom_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = _write_config(
                root / "config.ini",
                "[general]\n"
                f"repo_root = {root / 'r'}\n"
                f"state_dir = {root / 's'}\n"
                f"log_dir = {root / 'l'}\n"
                "exclude = *.tmp\n"
                "    *.log\n"
                "\n"
                "[device]\n"
                "serial = 0123456789ABCDEF\n"
                "marker_file = .other-id\n"
                "marker_value = abc\n"
                "\n"
                "[git]\n"
                "big_file_threshold = 64m\n"
                "gc_after_runs = 3\n"
                "\n"
                "[web]\n"
                "bind = 127.0.0.1\n"
                "port = 9000\n"
                "password_hash = pbkdf2_sha256$1000$c2FsdA==$aGFzaA==\n",
            )
            cfg = config.load_config(cfg_path)
            self.assertEqual(cfg.excludes, ("*.tmp", "*.log"))
            self.assertEqual(cfg.device_serial, "0123456789ABCDEF")
            self.assertEqual(cfg.marker_file, ".other-id")
            self.assertEqual(cfg.marker_value, "abc")
            self.assertEqual(cfg.big_file_threshold, "64m")
            self.assertEqual(cfg.gc_after_runs, 3)
            self.assertEqual(cfg.web_bind, "127.0.0.1")
            self.assertEqual(cfg.web_port, 9000)
            self.assertEqual(cfg.web_password_hash, "pbkdf2_sha256$1000$c2FsdA==$aGFzaA==")

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(config.MyuError):
                config.load_config(Path(tmp) / "nope.ini")

    def test_relative_paths_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for option in ("repo_root", "state_dir", "log_dir"):
                values = {
                    "repo_root": root / "repo",
                    "state_dir": root / "state",
                    "log_dir": root / "logs",
                }
                values[option] = "relative/path"
                text = (
                    "[general]\n"
                    f"repo_root = {values['repo_root']}\n"
                    f"state_dir = {values['state_dir']}\n"
                    f"log_dir = {values['log_dir']}\n"
                )
                cfg_path = _write_config(root / f"{option}.ini", text)
                with self.subTest(option=option):
                    with self.assertRaises(config.MyuError) as caught:
                        config.load_config(cfg_path)
                    self.assertIn(option, str(caught.exception))

    def test_port_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for port in ("0", "65536", "-1", "abc"):
                text = (
                    "[general]\n"
                    f"repo_root = {root / 'repo'}\n"
                    f"state_dir = {root / 'state'}\n"
                    f"log_dir = {root / 'logs'}\n"
                    "[web]\n"
                    f"port = {port}\n"
                )
                cfg_path = _write_config(root / f"port{port}.ini", text)
                with self.subTest(port=port):
                    with self.assertRaises(config.MyuError):
                        config.load_config(cfg_path)

    def test_unknown_keys_warn_and_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = (
                "[general]\n"
                f"repo_root = {root / 'repo'}\n"
                f"state_dir = {root / 'state'}\n"
                f"log_dir = {root / 'logs'}\n"
                "unknown_key = 1\n"
                "\n"
                "[mystery]\n"
                "foo = bar\n"
            )
            cfg_path = _write_config(root / "config.ini", text)
            with self.assertLogs("myu.config", level="WARNING") as captured:
                cfg = config.load_config(cfg_path)
            self.assertEqual(cfg.repo_root, root / "repo")
            joined = "\n".join(captured.output)
            self.assertIn("unknown_key", joined)
            self.assertIn("[mystery]", joined)

    def test_properties_and_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = config.Config(repo_root=root / "repo", state_dir=root / "state",
                                log_dir=root / "logs")
            self.assertEqual(cfg.db_path, root / "state" / "myu.db")
            self.assertEqual(cfg.reports_dir, root / "state" / "reports")
            self.assertEqual(cfg.work_tree, root / "repo")
            self.assertEqual(cfg.content_root, root / "repo")
            self.assertIsNone(cfg.config_path)
            self.assertEqual(cfg.excludes, ())
            with self.assertRaises(dataclasses.FrozenInstanceError):
                cfg.web_port = 1  # type: ignore[misc]


class PollGuardSecondsTest(unittest.TestCase):
    def _load(self, general_lines: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = (
                "[general]\n"
                f"repo_root = {root / 'repo'}\n"
                f"state_dir = {root / 'state'}\n"
                f"log_dir = {root / 'logs'}\n"
                f"{general_lines}"
            )
            cfg_path = _write_config(root / "config.ini", text)
            return config.load_config(cfg_path)

    def test_default_when_missing(self) -> None:
        cfg = self._load("")
        self.assertEqual(cfg.poll_guard_seconds, 3600)

    def test_empty_value_falls_back_to_default(self) -> None:
        cfg = self._load("poll_guard_seconds =\n")
        self.assertEqual(cfg.poll_guard_seconds, 3600)

    def test_zero_is_accepted(self) -> None:
        cfg = self._load("poll_guard_seconds = 0\n")
        self.assertEqual(cfg.poll_guard_seconds, 0)

    def test_override_without_unknown_key_warning(self) -> None:
        with mock.patch.object(config._LOG, "warning") as warned:
            cfg = self._load("poll_guard_seconds = 10\n")
        self.assertEqual(cfg.poll_guard_seconds, 10)
        warned.assert_not_called()

    def test_invalid_values_rejected(self) -> None:
        for raw in ("abc", "-1", "1.5", "3600s"):
            with self.subTest(raw=raw):
                with self.assertRaises(config.MyuError) as caught:
                    self._load(f"poll_guard_seconds = {raw}\n")
                self.assertIn("poll_guard_seconds", str(caught.exception))

    def test_template_roundtrip_keeps_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.ini"
            config.write_template(cfg_path, repo_root=root / "repo", state_dir=root / "state")
            text = cfg_path.read_text(encoding="utf-8")
            general_block = text.split("[device]")[0]
            self.assertIn("poll_guard_seconds = 3600", general_block)
            self.assertLess(general_block.index("pagefile.sys"),
                            general_block.index("poll_guard_seconds"))
            self.assertEqual(config.load_config(cfg_path).poll_guard_seconds, 3600)


class SaveConfigTest(unittest.TestCase):
    def test_rewrites_only_password_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.ini"
            config.write_template(cfg_path, repo_root=root / "repo", state_dir=root / "state")
            cfg = config.load_config(cfg_path)
            updated = dataclasses.replace(cfg, web_password_hash="pbkdf2_sha256$2000$c2FsdA==$aGFzaA==")
            config.save_config(updated)
            text = cfg_path.read_text(encoding="utf-8")
            self.assertIn("password_hash = pbkdf2_sha256$2000$c2FsdA==$aGFzaA==", text)
            self.assertIn("; 每行一个 glob，可缩进续写", text)
            self.assertIn("; U 盘的 ID_SERIAL；留空则只靠标记文件识别", text)
            self.assertIn("; 由 myu set-password 写入", text)
            reloaded = config.load_config(cfg_path)
            self.assertEqual(reloaded.web_password_hash, "pbkdf2_sha256$2000$c2FsdA==$aGFzaA==")
            self.assertEqual(reloaded.repo_root, cfg.repo_root)
            self.assertEqual(reloaded.excludes, cfg.excludes)
            self.assertEqual(text.count("[web]"), 1)

    def test_inserts_key_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = _write_config(
                root / "config.ini",
                "[general]\n"
                f"repo_root = {root / 'repo'}\n"
                f"state_dir = {root / 'state'}\n"
                f"log_dir = {root / 'logs'}\n"
                "\n"
                "[web]\n"
                "port = 8787\n",
            )
            cfg = config.load_config(cfg_path)
            config.save_config(dataclasses.replace(cfg, web_password_hash="a$b$c$d"))
            reloaded = config.load_config(cfg_path)
            self.assertEqual(reloaded.web_password_hash, "a$b$c$d")
            self.assertEqual(reloaded.web_port, 8787)

    def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = config.Config(repo_root=root / "repo", state_dir=root / "state",
                                log_dir=root / "logs", config_path=root / "nope.ini")
            with self.assertRaises(config.MyuError):
                config.save_config(cfg)

    def test_keeps_crlf_style(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = (
                "[general]\r\n"
                f"repo_root = {root / 'repo'}\r\n"
                f"state_dir = {root / 'state'}\r\n"
                f"log_dir = {root / 'logs'}\r\n"
                "[web]\r\n"
                "port = 8787\r\n"
                "password_hash =\r\n"
            )
            cfg_path = root / "config.ini"
            with open(cfg_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            cfg = config.load_config(cfg_path)
            config.save_config(dataclasses.replace(cfg, web_password_hash="h"))
            with open(cfg_path, "r", encoding="utf-8", newline="") as handle:
                saved = handle.read()
            self.assertEqual(saved.count("\r\n"), text.count("\r\n"))
            self.assertIn("password_hash = h\r\n", saved)


if __name__ == "__main__":
    unittest.main()
