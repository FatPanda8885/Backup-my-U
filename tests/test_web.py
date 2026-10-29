from __future__ import annotations

import ast
import base64
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from myu import util, web
from myu.config import Config
from myu.index import Store

PASSWORD = "hunter2"
ITERATIONS = 1000
CHALLENGE = 'Basic realm="Backup-my-U"'
RUN_ID = "20250316T101500Z"
FAILED_RUN_ID = "20250316T111500Z"
MISSING_RUN_ID = "20200101T000000Z"

# 不走系统代理，测试只连本机 127.0.0.1。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

WORK_TREE_FILES = {
    "a.txt": "alpha",
    "docs/readme.txt": "readme-body",
    "nested/b.txt": "bee-body",
    "notes/日记.md": "# 日记\n",
    "photos/海边 照片.jpg": "jpeg-body",
    "a&b.txt": "escape-me",
    ".git/config": "[core]\n\trepositoryformatversion = 0\n",
    "_deleted/20200101T000000Z/old/tmp.txt": "已经删除",
}


def _basic_auth(password: str, user: str = "myu") -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _header(headers: dict, name: str):
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _make_config(root: Path, password_hash: str) -> Config:
    repo = root / "repo"
    state = root / "state"
    logs = root / "logs"
    for directory in (repo, state, logs):
        directory.mkdir(parents=True, exist_ok=True)
    return Config(
        repo_root=repo,
        state_dir=state,
        log_dir=logs,
        web_password_hash=password_hash,
    )


class WebTestCase(unittest.TestCase):
    """真起一个绑 127.0.0.1:0 的服务，本模块的用例共用它。"""

    cfg: Config

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.root = Path(cls._tmp.name)
        cls.cfg = _make_config(cls.root, util.hash_password(PASSWORD, iterations=ITERATIONS))
        for relpath, text in WORK_TREE_FILES.items():
            path = cls.cfg.work_tree / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (cls.root / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
        (cls.cfg.log_dir / f"{RUN_ID}.log").write_text(
            "2025-03-16 10:15:00 INFO    备份完成\n", encoding="utf-8"
        )
        cls.store = Store(cls.cfg.db_path)
        cls.addClassCleanup(cls.store.close)
        cls._seed()
        cls.server = web.create_server(cls.cfg, cls.store, "127.0.0.1", 0)
        cls._thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls._thread.start()
        cls.addClassCleanup(cls._stop_server)
        cls.base_url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def _seed(cls) -> None:
        cls.store.start_run(RUN_ID, "SERIAL-A", "2025-03-16T10:15:00Z")
        cls.store.finish_run(
            RUN_ID,
            "completed",
            added=2,
            modified=1,
            deleted=1,
            bytes_copied=1234,
            bytes_total=4096,
            duration_ms=2300,
            git_commit="abc123",
        )
        cls.store.add_run_items(
            RUN_ID, "added",
            [{"relpath": "docs/readme.txt"}, {"relpath": "photos/海边 照片.jpg"}],
        )
        cls.store.add_run_items(RUN_ID, "modified", [{"relpath": "notes/日记.md"}])
        cls.store.add_run_items(RUN_ID, "deleted", [{"relpath": "old/tmp.txt"}])
        # 设备序列号与错误文本同样来自不可信来源，用于验证 HTML 转义。
        cls.store.start_run(FAILED_RUN_ID, "<script>alert(1)</script>", "2025-03-16T11:15:00Z")
        cls.store.finish_run(FAILED_RUN_ID, "failed", error="<b>挂载失败</b>")

    @classmethod
    def _stop_server(cls) -> None:
        cls.server.shutdown()
        cls._thread.join(timeout=10)
        cls.server.server_close()

    def _open(self, path: str, authorization=None, method: str = "GET") -> tuple:
        request = urllib.request.Request(self.base_url + path, method=method)
        if authorization is not None:
            request.add_header("Authorization", authorization)
        try:
            with OPENER.open(request, timeout=15) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, dict(exc.headers), exc.read()
            finally:
                exc.close()

    def _get(self, path: str, password=PASSWORD, method: str = "GET") -> tuple:
        authorization = None if password is None else _basic_auth(password)
        return self._open(path, authorization, method)

    def _get_text(self, path: str) -> str:
        status, headers, body = self._get(path)
        self.assertEqual(status, 200, msg=body[:400])
        self.assertIn("charset=utf-8", _header(headers, "Content-Type") or "")
        return body.decode("utf-8")


class AuthenticationTest(WebTestCase):
    def test_missing_credentials_returns_401(self) -> None:
        status, headers, body = self._get("/", password=None)
        self.assertEqual(status, 401)
        self.assertEqual(_header(headers, "WWW-Authenticate"), CHALLENGE)
        self.assertNotIn(RUN_ID.encode("utf-8"), body)
        self.assertNotIn(b"SERIAL-A", body)

    def test_wrong_password_returns_401(self) -> None:
        status, headers, _ = self._get("/", password="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(_header(headers, "WWW-Authenticate"), CHALLENGE)

    def test_malformed_authorization_returns_401(self) -> None:
        cases = (
            "Bearer abc123",
            "Basic",
            "Basic !!!not-base64!!!",
            "Basic " + base64.b64encode(PASSWORD.encode("utf-8")).decode("ascii"),
            "Basic " + base64.b64encode(b"\xff\xfe").decode("ascii"),
        )
        for value in cases:
            with self.subTest(value=value):
                status, headers, _ = self._open("/", value)
                self.assertEqual(status, 401)
                self.assertEqual(_header(headers, "WWW-Authenticate"), CHALLENGE)

    def test_username_is_ignored(self) -> None:
        for user in ("", "admin", "任意用户"):
            with self.subTest(user=user):
                status, _, _ = self._open("/", _basic_auth(PASSWORD, user))
                self.assertEqual(status, 200)

    def test_every_route_requires_auth(self) -> None:
        for path in ("/", "/files", "/run/" + RUN_ID, "/raw/a.txt", "/log/" + RUN_ID, "/nope"):
            with self.subTest(path=path):
                status, headers, _ = self._get(path, password=None)
                self.assertEqual(status, 401)
                self.assertEqual(_header(headers, "WWW-Authenticate"), CHALLENGE)


class IndexPageTest(WebTestCase):
    def test_index_lists_runs(self) -> None:
        text = self._get_text("/")
        self.assertIn(RUN_ID, text)
        self.assertIn("SERIAL-A", text)
        self.assertIn("+2 ~1 -1", text)
        self.assertIn("completed", text)
        self.assertIn("2.3 秒", text)
        self.assertIn('href="/run/' + RUN_ID + '"', text)
        self.assertIn('href="/log/' + RUN_ID + '"', text)

    def test_index_escapes_untrusted_device_serial(self) -> None:
        text = self._get_text("/")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn("<script", text)

    def test_index_has_no_external_resources(self) -> None:
        text = self._get_text("/")
        self.assertNotIn("<script", text)
        self.assertNotIn("http://", text.split("</style>")[1])
        self.assertNotIn("https://", text.split("</style>")[1])


class RunDetailTest(WebTestCase):
    def test_run_detail_escapes_error_text(self) -> None:
        text = self._get_text("/run/" + FAILED_RUN_ID)
        self.assertIn("&lt;b&gt;挂载失败&lt;/b&gt;", text)
        self.assertNotIn("<b>挂载失败</b>", text)

    def test_run_detail_lists_three_columns(self) -> None:
        text = self._get_text("/run/" + RUN_ID)
        for title in ("新增", "修改", "删除"):
            self.assertIn(title, text)
        self.assertIn("docs/readme.txt", text)
        self.assertIn("notes/日记.md", text)
        self.assertIn("old/tmp.txt", text)
        self.assertIn("abc123", text)
        self.assertIn("/raw/_deleted/" + RUN_ID + "/old/tmp.txt", text)

    def test_unknown_run_returns_404(self) -> None:
        status, _, _ = self._get("/run/" + MISSING_RUN_ID)
        self.assertEqual(status, 404)

    def test_illegal_run_id_returns_400(self) -> None:
        for suffix in ("..%2f..%2fetc%2fpasswd", "a.b", "%2e%2e", ""):
            with self.subTest(suffix=suffix):
                status, _, _ = self._get("/run/" + suffix)
                self.assertEqual(status, 400)


class FilesPageTest(WebTestCase):
    def test_files_walks_work_tree_only(self) -> None:
        text = self._get_text("/files")
        self.assertIn("a.txt", text)
        self.assertIn("docs/readme.txt", text)
        self.assertIn("nested/b.txt", text)
        self.assertIn("海边 照片.jpg", text)
        self.assertNotIn(".git/", text)
        self.assertNotIn("_deleted/", text)

    def test_files_query_filters_paths(self) -> None:
        text = self._get_text("/files?q=README")
        self.assertIn("docs/readme.txt", text)
        self.assertNotIn("nested/b.txt", text)
        self.assertNotIn("海边", text)

    def test_files_query_accepts_percent_encoded_chinese(self) -> None:
        text = self._get_text("/files?q=" + urllib.parse.quote("日记"))
        self.assertIn("notes/日记.md", text)
        self.assertNotIn("docs/readme.txt", text)

    def test_files_query_without_matches(self) -> None:
        text = self._get_text("/files?q=" + urllib.parse.quote("不存在的路径"))
        self.assertIn("没有匹配的文件", text)
        self.assertNotIn("nested/b.txt", text)

    def test_files_escapes_untrusted_names(self) -> None:
        text = self._get_text("/files")
        self.assertIn("a&amp;b.txt", text)
        self.assertNotIn("a&b.txt", text)


class RawDownloadTest(WebTestCase):
    def test_download_plain_file(self) -> None:
        status, headers, body = self._get("/raw/docs/readme.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"readme-body")
        self.assertEqual(
            _header(headers, "Content-Disposition"),
            "attachment; filename*=UTF-8''readme.txt",
        )

    def test_download_chinese_filename(self) -> None:
        relpath = "photos/海边 照片.jpg"
        status, headers, body = self._get("/raw/" + urllib.parse.quote(relpath, safe="/"))
        self.assertEqual(status, 200)
        self.assertEqual(body, "jpeg-body".encode("utf-8"))
        disposition = _header(headers, "Content-Disposition") or ""
        expected = "attachment; filename*=UTF-8''" + urllib.parse.quote("海边 照片.jpg", safe="")
        self.assertEqual(disposition, expected)
        self.assertNotIn("海边", disposition)

    def test_download_nested_file(self) -> None:
        status, _, body = self._get("/raw/nested/b.txt")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"bee-body")

    def test_download_missing_file_returns_404(self) -> None:
        for path in ("/raw/docs/missing.txt", "/raw/a.txt/extra"):
            with self.subTest(path=path):
                status, _, _ = self._get(path)
                self.assertEqual(status, 404)


class TraversalTest(WebTestCase):
    REJECTED = (
        "/raw/../secret.txt",
        "/raw/../../etc/passwd",
        "/raw/..%2f..%2fetc%2fpasswd",
        "/raw/%2E%2E%2F%2E%2E%2Fetc%2Fpasswd",
        "/raw/..%5C..%5Cwindows",
        "/raw/docs/..%2f..%2fsecret.txt",
        "/raw/%2Fetc%2Fpasswd",
        "/raw//etc/passwd",
        "/raw/%00",
        "/raw/",
        "/raw/.",
    )

    def test_traversal_is_forbidden(self) -> None:
        for path in self.REJECTED:
            with self.subTest(path=path):
                status, _, body = self._get(path)
                self.assertEqual(status, 403)
                self.assertNotIn(b"TOP-SECRET", body)
                self.assertNotIn(b"root:", body)

    def test_windows_style_absolute_path_never_escapes(self) -> None:
        status, _, body = self._get("/raw/C%3A%5Cwindows%5Cwin.ini")
        self.assertIn(status, (403, 404))
        self.assertNotIn(b"TOP-SECRET", body)


class LogPageTest(WebTestCase):
    def test_log_served_as_plain_text(self) -> None:
        status, headers, body = self._get("/log/" + RUN_ID)
        self.assertEqual(status, 200)
        self.assertTrue((_header(headers, "Content-Type") or "").startswith("text/plain"))
        self.assertIn("备份完成", body.decode("utf-8"))

    def test_missing_log_returns_404(self) -> None:
        status, _, _ = self._get("/log/" + MISSING_RUN_ID)
        self.assertEqual(status, 404)

    def test_illegal_run_id_returns_400(self) -> None:
        for suffix in ("..%2f..", "a%2Fb", "a.b", "%2e%2e", ""):
            with self.subTest(suffix=suffix):
                status, _, _ = self._get("/log/" + suffix)
                self.assertEqual(status, 400)


class RoutingTest(WebTestCase):
    def test_unknown_paths_return_404(self) -> None:
        for path in ("/nope", "/run", "/files/extra", "/raw", "/log", "/files/", "/index.html"):
            with self.subTest(path=path):
                status, _, _ = self._get(path)
                self.assertEqual(status, 404)

    def test_non_get_methods_return_404(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status, _, _ = self._get("/", method=method)
                self.assertEqual(status, 404)

    def test_non_get_without_credentials_returns_401(self) -> None:
        status, headers, _ = self._get("/", password=None, method="POST")
        self.assertEqual(status, 401)
        self.assertEqual(_header(headers, "WWW-Authenticate"), CHALLENGE)

    def test_head_is_not_allowed_and_has_no_body(self) -> None:
        status, _, body = self._get("/files", method="HEAD")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")


class ServerLifecycleTest(unittest.TestCase):
    def _prepare(self, password_hash: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = _make_config(Path(tmp.name), password_hash)
        store = Store(cfg.db_path)
        self.addCleanup(store.close)
        return cfg, store

    def test_create_server_binds_ephemeral_port(self) -> None:
        cfg, store = self._prepare(util.hash_password(PASSWORD, iterations=ITERATIONS))
        server = web.create_server(cfg, store, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        host, port = server.server_address[0], server.server_address[1]
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(port, 0)
        self.assertTrue(server.daemon_threads)
        self.assertTrue(server.allow_reuse_address)

    def test_serve_refuses_without_password_hash(self) -> None:
        cfg, store = self._prepare("")
        with self.assertLogs("myu.web", level="ERROR") as captured:
            code = web.serve(cfg, store)
        self.assertEqual(code, 2)
        self.assertIn("口令", captured.output[0])


class ImportContractTest(unittest.TestCase):
    """web 只能 import 标准库与更底层的同包模块，且不得 import cli。"""

    ALLOWED_SIBLINGS = {"util", "config", "index", "repo", "device", "sync"}

    def test_web_imports_are_stdlib_or_lower_layers(self) -> None:
        source = Path(web.__file__).read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    roots.add(node.module.split(".")[0])
        self.assertNotIn("cli", roots)
        stdlib = getattr(sys, "stdlib_module_names", None)
        for root in roots - self.ALLOWED_SIBLINGS - {"__future__"}:
            if stdlib is not None:
                self.assertIn(root, stdlib, msg="非标准库 import: " + root)
