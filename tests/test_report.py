from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from myu import report
from myu.util import MyuError, human_bytes

RUN_ID = "20250316T101500Z"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
HEADINGS = ("# 备份运行", "## 概览", "## 新增", "## 修改", "## 删除", "## 异常")


def _run(**overrides: object) -> dict:
    run = {
        "run_id": RUN_ID,
        "device_serial": "USB-SERIAL-01",
        "started_at": "2025-03-16T10:15:00Z",
        "finished_at": "2025-03-16T10:15:45Z",
        "status": "completed",
        "added": 2,
        "modified": 1,
        "deleted": 1,
        "bytes_copied": 1536,
        "bytes_total": 4096,
        "duration_ms": 45000,
        "git_commit": COMMIT,
        "error": None,
    }
    run.update(overrides)
    return run


def _item(relpath: str, size: object = 10, mtime_ns: object = 1711000000123456700) -> dict:
    return {"relpath": relpath, "size": size, "mtime_ns": mtime_ns, "blob_sha": "0" * 40}


def _items(**overrides: object) -> dict:
    items = {
        "added": [_item("a.txt"), _item("管道|名字.txt", size=2048)],
        "modified": [_item("sub/b b.txt", size=1024)],
        "deleted": [_item("gone.txt", size=None, mtime_ns=None)],
    }
    items.update(overrides)
    return items


def _cell_count(line: str) -> int:
    """一行表格里有几列：按未被转义的竖线切分。"""
    return len(re.split(r"(?<!\\)\|", line)) - 2


def _data_rows(text: str, heading: str) -> list:
    """取某个二级标题下表格的数据行，跳过表头与分隔线。"""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    rows = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.startswith("| ") and not line.startswith("| 相对路径") and not line.startswith("| ---"):
            rows.append(line)
    return rows


class BuildMarkdownTest(unittest.TestCase):
    def test_section_order(self) -> None:
        text = report.build_markdown(_run(), _items())
        positions = [text.index(heading) for heading in HEADINGS]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(text.count("# 备份运行"), 1)

    def test_overview_required_fields(self) -> None:
        text = report.build_markdown(_run(), _items())
        self.assertIn(f"| 运行 ID | {RUN_ID} |", text)
        self.assertIn("| 设备序列号 | USB-SERIAL-01 |", text)
        self.assertIn("| 开始时间 | 2025-03-16T10:15:00Z |", text)
        self.assertIn("| 结束时间 | 2025-03-16T10:15:45Z |", text)
        self.assertIn("| 耗时 | 45.00 秒 |", text)
        self.assertIn("| 新增 | 2 个 |", text)
        self.assertIn("| 修改 | 1 个 |", text)
        self.assertIn("| 删除 | 1 个 |", text)
        self.assertIn(f"| 复制字节数 | {human_bytes(1536)} |", text)
        self.assertIn(f"| git commit | {COMMIT} |", text)
        self.assertEqual(human_bytes(1536), "1.50 KB")

    def test_missing_fields_render_dash(self) -> None:
        text = report.build_markdown({"run_id": "x"}, {})
        self.assertIn("| 设备序列号 | — |", text)
        self.assertIn("| 结束时间 | — |", text)
        self.assertIn("| 耗时 | — |", text)
        self.assertIn("| git commit | — |", text)
        self.assertIn("| 状态 | — |", text)

    def test_counts_fall_back_to_run_row(self) -> None:
        text = report.build_markdown(_run(added=7), {})
        self.assertIn("| 新增 | 7 个 |", text)
        self.assertIn("## 新增（7 个）", text)

    def test_duration_text(self) -> None:
        cases = ((500, "500 毫秒"), (45000, "45.00 秒"), (90000, "1 分 30 秒"), (3723000, "1 时 2 分"))
        for duration_ms, expected in cases:
            with self.subTest(duration_ms=duration_ms):
                text = report.build_markdown(_run(duration_ms=duration_ms), {})
                self.assertIn(f"| 耗时 | {expected} |", text)

    def test_file_rows_have_three_columns(self) -> None:
        text = report.build_markdown(_run(), _items())
        for heading in ("## 新增", "## 修改", "## 删除"):
            rows = _data_rows(text, heading)
            self.assertTrue(rows, heading)
            for row in rows:
                self.assertEqual(_cell_count(row), 3, row)

    def test_file_row_content(self) -> None:
        text = report.build_markdown(_run(), _items())
        rows = _data_rows(text, "## 修改")
        self.assertEqual(rows, ["| sub/b b.txt | 1.00 KB | 2024-03-21 05:46:40 |"])

    def test_pipe_in_filename_is_escaped(self) -> None:
        text = report.build_markdown(_run(), _items())
        self.assertIn("管道\\|名字.txt", text)
        rows = _data_rows(text, "## 新增")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(_cell_count(row), 3, row)
        shipped = [row for row in rows if "管道" in row][0]
        self.assertEqual(_cell_count(shipped), 3, shipped)
        self.assertEqual(_data_rows(text, "## 新增"), rows)

    def test_newline_in_filename_does_not_break_table(self) -> None:
        items = _items(added=[_item("坏\n名字.txt")])
        text = report.build_markdown(_run(), items)
        rows = _data_rows(text, "## 新增")
        self.assertEqual(len(rows), 1)
        self.assertEqual(_cell_count(rows[0]), 3)

    def test_empty_sections_show_none(self) -> None:
        text = report.build_markdown(_run(added=0, modified=0, deleted=0), {"added": [], "modified": [], "deleted": []})
        for heading in ("## 新增", "## 修改", "## 删除"):
            self.assertIn(heading, text)
        self.assertEqual(text.count("（无）"), 4)
        self.assertIn("## 新增（0 个）\n\n（无）", text)

    def test_deleted_without_size_or_time(self) -> None:
        text = report.build_markdown(_run(), _items())
        self.assertEqual(_data_rows(text, "## 删除"), ["| gone.txt | — | — |"])

    def test_errors_section(self) -> None:
        stats = SimpleNamespace(errors=["复制失败: b.txt 已损坏"])
        text = report.build_markdown(_run(error="U 盘读取失败"), _items(), stats)
        self.assertIn("## 异常（2 条）", text)
        self.assertIn("- U 盘读取失败", text)
        self.assertIn("- 复制失败: b.txt 已损坏", text)

    def test_errors_section_from_mapping(self) -> None:
        text = report.build_markdown(_run(), _items(), {"errors": ["x"]})
        self.assertIn("## 异常（1 条）", text)
        self.assertIn("- x", text)

    def test_no_errors_shows_none(self) -> None:
        text = report.build_markdown(_run(), _items())
        tail = text[text.index("## 异常"):]
        self.assertIn("（无）", tail)

    def test_pipe_in_title_is_escaped(self) -> None:
        text = report.build_markdown(_run(run_id="run|1"), {})
        self.assertIn("# 备份运行 run\\|1", text)

    def test_output_is_lf_terminated(self) -> None:
        text = report.build_markdown(_run(), _items())
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\r", text)


class WriteReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_writes_file_and_creates_dir(self) -> None:
        reports = self.root / "state" / "reports"
        text = report.build_markdown(_run(), _items())
        path = report.write_report(reports, RUN_ID, text)
        self.assertEqual(path, reports / f"{RUN_ID}.md")
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))
        self.assertEqual([item.name for item in reports.iterdir()], [f"{RUN_ID}.md"])

    def test_utf8_without_bom_and_lf(self) -> None:
        reports = self.root / "reports"
        path = report.write_report(reports, RUN_ID, "中文\n第二行\n")
        raw = path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw, "中文\n第二行\n".encode("utf-8"))
        self.assertNotIn(b"\r", raw)

    def test_overwrites_existing_file(self) -> None:
        reports = self.root / "reports"
        report.write_report(reports, RUN_ID, "第一版\n")
        path = report.write_report(reports, RUN_ID, "第二版\n")
        self.assertEqual(path.read_bytes(), "第二版\n".encode("utf-8"))
        self.assertEqual([item.name for item in reports.iterdir()], [f"{RUN_ID}.md"])

    def test_rejects_bad_run_id(self) -> None:
        reports = self.root / "reports"
        for bad in ("", "../evil", "a/b", ".."):
            with self.subTest(bad=bad):
                with self.assertRaises(MyuError):
                    report.write_report(reports, bad, "x")


if __name__ == "__main__":
    unittest.main()
