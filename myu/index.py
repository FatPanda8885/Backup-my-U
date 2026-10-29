from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import TracebackType

from .util import MyuError, utc_now_iso

SCHEMA_VERSION = "1"
KINDS = ("added", "modified", "deleted")

SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    serial     TEXT PRIMARY KEY,
    label      TEXT,
    marker     TEXT,
    fs_type    TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL UNIQUE,
    device_serial TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    added         INTEGER NOT NULL DEFAULT 0,
    modified      INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0,
    bytes_copied  INTEGER NOT NULL DEFAULT 0,
    bytes_total   INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER,
    git_commit    TEXT,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS entries (
    device_serial TEXT NOT NULL,
    relpath       TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    blob_sha      TEXT,
    last_run_id   TEXT NOT NULL,
    PRIMARY KEY (device_serial, relpath)
);

CREATE TABLE IF NOT EXISTS run_items (
    run_id   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    relpath  TEXT NOT NULL,
    size     INTEGER,
    mtime_ns INTEGER,
    blob_sha TEXT,
    PRIMARY KEY (run_id, kind, relpath)
);

CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id, kind);
"""

_MISSING = object()


def _field(item: object, name: str, default: object = _MISSING) -> object:
    """条目既可能是 dict，也可能是带同名属性的对象。"""
    if isinstance(item, Mapping):
        if name in item:
            return item[name]
    else:
        try:
            return item[name]  # type: ignore[index]
        except (TypeError, KeyError, IndexError):
            pass
        if hasattr(item, name):
            return getattr(item, name)
    if default is _MISSING:
        raise MyuError(f"条目缺少字段 {name}: {item!r}")
    return default


def _row(row: sqlite3.Row | None) -> dict | None:
    return None if row is None else {key: row[key] for key in row.keys()}


class Store:
    """SQLite 索引；DDL 与 DESIGN.md 第 6 节逐字一致。"""

    def __init__(self, db_path: object) -> None:
        self.db_path = Path(str(db_path))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def upsert_device(
        self,
        serial: str,
        label: str | None = None,
        marker: str | None = None,
        fs_type: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._conn:
            row = self._conn.execute(
                "SELECT serial FROM devices WHERE serial = ?", (serial,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO devices (serial, label, marker, fs_type, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (serial, label, marker, fs_type, now, now),
                )
                return
            self._conn.execute(
                "UPDATE devices SET label = COALESCE(?, label), marker = COALESCE(?, marker),"
                " fs_type = COALESCE(?, fs_type), last_seen = ? WHERE serial = ?",
                (label, marker, fs_type, now, serial),
            )

    def get_device(self, serial: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM devices WHERE serial = ?", (serial,)).fetchone()
        return _row(row)

    def list_devices(self) -> list:
        rows = self._conn.execute("SELECT * FROM devices ORDER BY first_seen, serial").fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def start_run(self, run_id: str, device_serial: str, started_at: str) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO runs (run_id, device_serial, started_at, status)"
                " VALUES (?, ?, ?, 'running')",
                (run_id, device_serial, started_at),
            )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: str,
        status: str,
        added: int = 0,
        modified: int = 0,
        deleted: int = 0,
        bytes_copied: int = 0,
        bytes_total: int = 0,
        duration_ms: int | None = None,
        git_commit: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, added = ?, modified = ?,"
                " deleted = ?, bytes_copied = ?, bytes_total = ?, duration_ms = ?,"
                " git_commit = ?, error = ? WHERE run_id = ?",
                (
                    utc_now_iso(),
                    status,
                    added,
                    modified,
                    deleted,
                    bytes_copied,
                    bytes_total,
                    duration_ms,
                    git_commit,
                    error,
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                raise MyuError(f"运行记录不存在: {run_id}")

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _row(row)

    def list_runs(self, limit: int = 50, device_serial: str | None = None) -> list:
        if device_serial is None:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE device_serial = ? ORDER BY id DESC LIMIT ?",
                (device_serial, int(limit)),
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def latest_run(self, device_serial: str | None = None) -> dict | None:
        if device_serial is None:
            row = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE device_serial = ? ORDER BY id DESC LIMIT 1",
                (device_serial,),
            ).fetchone()
        return _row(row)

    def last_completed_run_id(self, device_serial: str) -> str | None:
        row = self._conn.execute(
            "SELECT run_id FROM runs WHERE device_serial = ? AND status = 'completed'"
            " ORDER BY id DESC LIMIT 1",
            (device_serial,),
        ).fetchone()
        return None if row is None else str(row["run_id"])

    def list_entries(self, device_serial: str) -> list:
        rows = self._conn.execute(
            "SELECT relpath, size, mtime_ns, blob_sha FROM entries"
            " WHERE device_serial = ? ORDER BY relpath",
            (device_serial,),
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def replace_entries(self, device_serial: str, run_id: str, entries: Iterable) -> None:
        rows = [
            (
                device_serial,
                str(_field(entry, "relpath")),
                int(_field(entry, "size")),
                int(_field(entry, "mtime_ns")),
                _field(entry, "blob_sha", None),
                run_id,
            )
            for entry in entries
        ]
        with self._conn:
            self._conn.execute("DELETE FROM entries WHERE device_serial = ?", (device_serial,))
            self._conn.executemany(
                "INSERT INTO entries (device_serial, relpath, size, mtime_ns, blob_sha, last_run_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def add_run_items(self, run_id: str, kind: str, items: Iterable) -> None:
        rows = [
            (
                run_id,
                kind,
                str(_field(item, "relpath")),
                _field(item, "size", None),
                _field(item, "mtime_ns", None),
                _field(item, "blob_sha", None),
            )
            for item in items
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO run_items (run_id, kind, relpath, size, mtime_ns, blob_sha)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def list_run_items(self, run_id: str, kind: str | None = None) -> list:
        if kind is None:
            rows = self._conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? ORDER BY kind, relpath", (run_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? AND kind = ? ORDER BY relpath",
                (run_id, kind),
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def count_run_items(self, run_id: str) -> dict:
        counts = {kind: 0 for kind in KINDS}
        rows = self._conn.execute(
            "SELECT kind, COUNT(*) FROM run_items WHERE run_id = ? GROUP BY kind", (run_id,)
        ).fetchall()
        for kind, total in rows:
            counts[str(kind)] = int(total)
        return counts

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: object) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
            )
