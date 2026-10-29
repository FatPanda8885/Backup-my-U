from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from myu import index

FENCE = "\x60\x60\x60"


def _design_statements() -> dict:
    design = Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md"
    if not design.is_file():
        raise unittest.SkipTest("缺少 docs/DESIGN.md")
    text = design.read_text(encoding="utf-8")
    start = text.index(FENCE + "sql") + len(FENCE + "sql")
    block = text[start:text.index(FENCE, start)]
    statements = {}
    for part in block.split(";"):
        normalized = " ".join(part.split())
        if not normalized:
            continue
        name = normalized.split(" ")[5]
        # SQLite 不把 IF NOT EXISTS 写进 sqlite_master.sql，比较前统一去掉。
        statements[name] = normalized.replace(" IF NOT EXISTS", "")
    return statements


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "state" / "myu.db"
        self.store = index.Store(self.db_path)
        self.addCleanup(self.store.close)


class SchemaTest(StoreTestCase):
    def test_ddl_matches_design_verbatim(self) -> None:
        expected = _design_statements()
        self.store.close()
        connection = sqlite3.connect(str(self.db_path))
        try:
            rows = connection.execute(
                "SELECT name, sql FROM sqlite_master"
                " WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        finally:
            connection.close()
        actual = {name: " ".join(sql.split()) for name, sql in rows}
        self.assertEqual(actual, expected)

    def test_ddl_constant_matches_design_block(self) -> None:
        design = Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md"
        if not design.is_file():
            raise unittest.SkipTest("缺少 docs/DESIGN.md")
        text = design.read_text(encoding="utf-8")
        start = text.index(FENCE + "sql") + len(FENCE + "sql")
        block = text[start:text.index(FENCE, start)]
        self.assertEqual(index.SCHEMA_SQL, block.strip("\n") + "\n")

    def test_table_columns(self) -> None:
        connection = sqlite3.connect(str(self.db_path))
        try:
            columns = {
                table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                for table in ("meta", "devices", "runs", "entries", "run_items")
            }
        finally:
            connection.close()
        self.assertEqual(columns["meta"], ["key", "value"])
        self.assertEqual(
            columns["runs"],
            ["id", "run_id", "device_serial", "started_at", "finished_at", "status",
             "added", "modified", "deleted", "bytes_copied", "bytes_total",
             "duration_ms", "git_commit", "error"],
        )
        self.assertEqual(
            columns["run_items"], ["run_id", "kind", "relpath", "size", "mtime_ns", "blob_sha"]
        )

    def test_pragmas(self) -> None:
        self.assertEqual(self.store._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store._conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        self.assertEqual(self.store._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_schema_version_meta(self) -> None:
        self.assertEqual(self.store.get_meta("schema_version"), "1")

    def test_reopen_keeps_data(self) -> None:
        self.store.set_meta("last_gc_commit", "abc")
        self.store.close()
        reopened = index.Store(self.db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_meta("last_gc_commit"), "abc")
        self.assertEqual(reopened.get_meta("schema_version"), "1")


class DeviceTest(StoreTestCase):
    def test_upsert_and_fetch(self) -> None:
        self.assertIsNone(self.store.get_device("S1"))
        self.store.upsert_device("S1", label="U盘", marker=".myu-id", fs_type="vfat")
        device = self.store.get_device("S1")
        self.assertEqual(device["serial"], "S1")
        self.assertEqual(device["label"], "U盘")
        self.assertEqual(device["marker"], ".myu-id")
        self.assertEqual(device["fs_type"], "vfat")
        first_seen = device["first_seen"]
        self.assertRegex(first_seen, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(device["last_seen"], first_seen)

        self.store.upsert_device("S1", label="新标签")
        updated = self.store.get_device("S1")
        self.assertEqual(updated["label"], "新标签")
        self.assertEqual(updated["marker"], ".myu-id")
        self.assertEqual(updated["fs_type"], "vfat")
        self.assertEqual(updated["first_seen"], first_seen)

    def test_list_devices(self) -> None:
        self.assertEqual(self.store.list_devices(), [])
        self.store.upsert_device("S2")
        self.store.upsert_device("S1")
        serials = [item["serial"] for item in self.store.list_devices()]
        self.assertEqual(sorted(serials), ["S1", "S2"])


class RunTest(StoreTestCase):
    def test_run_lifecycle(self) -> None:
        row_id = self.store.start_run("20250316T101500Z", "S1", "2025-03-16T10:15:00Z")
        self.assertIsInstance(row_id, int)
        run = self.store.get_run("20250316T101500Z")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["device_serial"], "S1")
        self.assertIsNone(run["finished_at"])
        self.assertEqual(run["added"], 0)

        self.store.finish_run(
            "20250316T101500Z",
            "completed",
            added=2,
            modified=1,
            deleted=3,
            bytes_copied=100,
            bytes_total=200,
            duration_ms=1500,
            git_commit="a" * 40,
        )
        done = self.store.get_run("20250316T101500Z")
        self.assertEqual(done["status"], "completed")
        self.assertEqual((done["added"], done["modified"], done["deleted"]), (2, 1, 3))
        self.assertEqual(done["bytes_copied"], 100)
        self.assertEqual(done["bytes_total"], 200)
        self.assertEqual(done["duration_ms"], 1500)
        self.assertEqual(done["git_commit"], "a" * 40)
        self.assertIsNone(done["error"])
        self.assertRegex(done["finished_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_finish_unknown_run(self) -> None:
        with self.assertRaises(index.MyuError):
            self.store.finish_run("nope", "failed", error="x")

    def test_list_latest_and_last_completed(self) -> None:
        self.assertIsNone(self.store.latest_run())
        self.assertIsNone(self.store.last_completed_run_id("S1"))
        self.store.start_run("r1", "S1", "2025-03-16T10:00:00Z")
        self.store.finish_run("r1", "failed", error="炸了")
        self.store.start_run("r2", "S1", "2025-03-16T11:00:00Z")
        self.store.finish_run("r2", "completed")
        self.store.start_run("r3", "S2", "2025-03-16T12:00:00Z")
        self.store.finish_run("r3", "completed")

        self.assertEqual(self.store.last_completed_run_id("S1"), "r2")
        self.assertEqual(self.store.last_completed_run_id("S2"), "r3")
        self.assertIsNone(self.store.last_completed_run_id("S9"))
        self.assertEqual(self.store.latest_run()["run_id"], "r3")
        self.assertEqual(self.store.latest_run("S1")["run_id"], "r2")
        self.assertEqual([run["run_id"] for run in self.store.list_runs()], ["r3", "r2", "r1"])
        self.assertEqual([run["run_id"] for run in self.store.list_runs(limit=1)], ["r3"])
        self.assertEqual([run["run_id"] for run in self.store.list_runs(device_serial="S1")],
                         ["r2", "r1"])
        self.assertEqual(self.store.get_run("missing"), None)


class EntryTest(StoreTestCase):
    def test_replace_entries(self) -> None:
        self.assertEqual(self.store.list_entries("S1"), [])
        self.store.replace_entries(
            "S1",
            "r1",
            [
                {"relpath": "b.txt", "size": 2, "mtime_ns": 20, "blob_sha": "sha-b"},
                {"relpath": "a/中文 名.txt", "size": 1, "mtime_ns": 10, "blob_sha": "sha-a"},
            ],
        )
        entries = self.store.list_entries("S1")
        self.assertEqual([entry["relpath"] for entry in entries], ["a/中文 名.txt", "b.txt"])
        self.assertEqual(entries[0]["size"], 1)
        self.assertEqual(entries[0]["mtime_ns"], 10)
        self.assertEqual(entries[0]["blob_sha"], "sha-a")

        self.store.replace_entries("S1", "r2", [{"relpath": "c.txt", "size": 3, "mtime_ns": 30}])
        self.assertEqual([entry["relpath"] for entry in self.store.list_entries("S1")], ["c.txt"])
        self.assertIsNone(self.store.list_entries("S1")[0]["blob_sha"])

    def test_replace_entries_accepts_objects(self) -> None:
        class _Item:
            def __init__(self, relpath: str, size: int, mtime_ns: int) -> None:
                self.relpath = relpath
                self.size = size
                self.mtime_ns = mtime_ns

        self.store.replace_entries("S1", "r1", [_Item("x.txt", 7, 70)])
        entries = self.store.list_entries("S1")
        self.assertEqual(entries[0]["relpath"], "x.txt")
        self.assertEqual(entries[0]["size"], 7)
        self.assertEqual(entries[0]["mtime_ns"], 70)


class RunItemTest(StoreTestCase):
    def test_add_list_count(self) -> None:
        self.store.add_run_items(
            "r1", "added", [{"relpath": "a.txt", "size": 1, "mtime_ns": 2, "blob_sha": "s"}]
        )
        self.store.add_run_items("r1", "deleted", [{"relpath": "b.txt"}])
        self.store.add_run_items("r1", "modified", [])
        items = self.store.list_run_items("r1")
        self.assertEqual([(item["kind"], item["relpath"]) for item in items],
                         [("added", "a.txt"), ("deleted", "b.txt")])
        self.assertEqual(self.store.list_run_items("r1", "deleted")[0]["relpath"], "b.txt")
        self.assertEqual(self.store.count_run_items("r1"),
                         {"added": 1, "modified": 0, "deleted": 1})
        self.assertEqual(self.store.count_run_items("missing"),
                         {"added": 0, "modified": 0, "deleted": 0})

    def test_entry_without_relpath_is_rejected(self) -> None:
        with self.assertRaises(index.MyuError):
            self.store.add_run_items("r1", "added", [{"size": 1}])


class MetaTest(StoreTestCase):
    def test_get_set(self) -> None:
        self.assertIsNone(self.store.get_meta("last_gc_commit"))
        self.store.set_meta("last_gc_commit", "abc123")
        self.assertEqual(self.store.get_meta("last_gc_commit"), "abc123")
        self.store.set_meta("last_gc_commit", "def456")
        self.assertEqual(self.store.get_meta("last_gc_commit"), "def456")

    def test_context_manager(self) -> None:
        store = index.Store(self.root / "other.db")
        with store as handle:
            self.assertIs(handle, store)
            handle.set_meta("k", "v")
        with self.assertRaises(sqlite3.ProgrammingError):
            store.get_meta("k")


if __name__ == "__main__":
    unittest.main()
