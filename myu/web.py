from __future__ import annotations

import base64
import html
import logging
import os
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import Config
from .index import Store
from .util import MyuError, human_bytes, normalize_relpath, verify_password

_LOG = logging.getLogger("myu.web")

AUTH_CHALLENGE = 'Basic realm="Backup-my-U"'
AUTH_SCHEME = "Basic"
RUN_ID_CHARS = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-")
SKIP_DIRS = (".git", "_deleted")
KIND_TITLES = (("added", "新增"), ("modified", "修改"), ("deleted", "删除"))
RUN_LIST_LIMIT = 50
CHUNK_SIZE = 256 * 1024

_STYLE = """
body { font-family: system-ui, "Segoe UI", sans-serif; margin: 0; padding: 1.5rem 2rem;
       background: #f6f7f9; color: #1f2328; line-height: 1.5; }
h1 { font-size: 1.35rem; margin: 0 0 1rem; }
h2 { font-size: 1.05rem; margin: 0 0 .6rem; }
a { color: #0b5cad; text-decoration: none; }
a:hover { text-decoration: underline; }
nav { margin-bottom: 1.2rem; font-size: .95rem; }
nav a { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: .92rem; }
table.kv { width: auto; min-width: 26rem; margin-bottom: 1rem; }
th, td { border: 1px solid #d8dde3; padding: .35rem .55rem; text-align: left; vertical-align: top; }
th { background: #eef1f5; font-weight: 600; }
form.filter { margin: 0 0 1rem; }
input[type="search"] { padding: .35rem .5rem; border: 1px solid #c4ccd4; border-radius: 4px;
                       min-width: 18rem; }
button { padding: .35rem .8rem; border: 1px solid #c4ccd4; border-radius: 4px; background: #fff; }
.mono { font-family: ui-monospace, Consolas, monospace; }
.muted { color: #5c6670; }
.cols { display: flex; gap: 1.2rem; flex-wrap: wrap; align-items: flex-start; }
.cols section { flex: 1 1 18rem; min-width: 16rem; background: #fff; padding: .6rem .8rem;
                border: 1px solid #d8dde3; border-radius: 6px; }
"""

_NAV = '<nav><a href="/">运行历史</a><a href="/files">备份树</a></nav>'


def _valid_run_id(run_id: str) -> bool:
    """run_id 只允许 [0-9A-Za-z_-]+，杜绝路径注入。"""
    return bool(run_id) and all(char in RUN_ID_CHARS for char in run_id)


def _raw_href(relpath: str) -> str:
    return "/raw/" + quote(relpath, safe="/")


def _run_href(run_id: str) -> str:
    return "/run/" + quote(run_id, safe="")


def _log_href(run_id: str) -> str:
    return "/log/" + quote(run_id, safe="")


def _duration_text(duration_ms: object) -> str:
    if duration_ms is None:
        return "-"
    seconds = int(duration_ms) / 1000.0
    if seconds < 60.0:
        return f"{seconds:.1f} 秒"
    return f"{int(seconds // 60)} 分 {seconds % 60:.0f} 秒"


def _bytes_text(value: object) -> str:
    return human_bytes(int(value or 0))


def _int_text(value: object) -> str:
    return str(int(value or 0))


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n<body>\n"
        f"{_NAV}\n<h1>{html.escape(title)}</h1>\n{body}\n"
        "</body>\n</html>\n"
    )


def _index_page(store) -> str:
    runs = store.list_runs(limit=RUN_LIST_LIMIT)
    if not runs:
        return _page("运行历史", '<p class="muted">还没有任何运行记录。</p>')
    head = (
        "<thead><tr><th>时间</th><th>设备</th><th>变更</th><th>字节</th>"
        "<th>耗时</th><th>状态</th><th>日志</th></tr></thead>"
    )
    rows = "".join(_index_row(run) for run in runs)
    return _page("运行历史", f"<table>{head}<tbody>{rows}</tbody></table>")


def _index_row(run: dict) -> str:
    run_id = str(run.get("run_id") or "")
    time_cell = (
        f'<a href="{_run_href(run_id)}">{html.escape(str(run.get("started_at") or ""))}</a>'
        f'<br><span class="muted mono">{html.escape(run_id)}</span>'
    )
    counts = (
        f"+{_int_text(run.get('added'))} ~{_int_text(run.get('modified'))}"
        f" -{_int_text(run.get('deleted'))}"
    )
    return (
        f"<tr><td>{time_cell}</td>"
        f'<td class="mono">{html.escape(str(run.get("device_serial") or ""))}</td>'
        f'<td class="mono">{counts}</td>'
        f"<td>{html.escape(_bytes_text(run.get('bytes_copied')))}</td>"
        f"<td>{html.escape(_duration_text(run.get('duration_ms')))}</td>"
        f'<td>{html.escape(str(run.get("status") or ""))}</td>'
        f'<td><a href="{_log_href(run_id)}">日志</a></td></tr>\n'
    )


def _item_row(run_id: str, kind: str, item: dict) -> str:
    relpath = str(item.get("relpath") or "")
    source = relpath if kind != "deleted" else f"_deleted/{run_id}/{relpath}"
    size = item.get("size")
    size_text = "-" if size is None else human_bytes(int(size))
    return (
        f'<tr><td class="mono"><a href="{_raw_href(source)}">{html.escape(relpath)}</a></td>'
        f'<td class="muted">{html.escape(size_text)}</td></tr>\n'
    )


def _run_page(store, run: dict) -> str:
    run_id = str(run.get("run_id") or "")
    facts = [
        ("运行标识", f'<span class="mono">{html.escape(run_id)}</span>'),
        ("设备", html.escape(str(run.get("device_serial") or ""))),
        ("开始", html.escape(str(run.get("started_at") or ""))),
        ("结束", html.escape(str(run.get("finished_at") or "-"))),
        ("状态", html.escape(str(run.get("status") or ""))),
        ("字节", html.escape(_bytes_text(run.get("bytes_copied")))),
        ("耗时", html.escape(_duration_text(run.get("duration_ms")))),
        ("提交", html.escape(str(run.get("git_commit") or "-"))),
    ]
    if run.get("error"):
        facts.append(("错误", html.escape(str(run.get("error")))))
    overview = '<table class="kv">' + "".join(
        f"<tr><th>{title}</th><td>{value}</td></tr>" for title, value in facts
    ) + "</table>"
    sections = []
    for kind, title in KIND_TITLES:
        items = store.list_run_items(run_id, kind)
        if items:
            listing = "<table><tbody>" + "".join(
                _item_row(run_id, kind, item) for item in items
            ) + "</tbody></table>"
        else:
            listing = '<p class="muted">无</p>'
        sections.append(
            f'<section><h2>{title} <span class="muted">{len(items)}</span></h2>{listing}</section>'
        )
    body = (
        overview
        + f'<p>日志：<a class="mono" href="{_log_href(run_id)}">'
        + f"{html.escape(run_id)}.log</a></p>"
        + '<div class="cols">'
        + "".join(sections)
        + "</div>"
    )
    return _page(f"运行 {run_id}", body)


def _files_page(files: list, query: str, total: int) -> str:
    form = (
        '<form class="filter" method="get" action="/files">'
        f'<input type="search" name="q" value="{html.escape(query)}" '
        'placeholder="按路径过滤"> <button type="submit">过滤</button></form>'
    )
    summary = f'<p class="muted">匹配 {len(files)} 个文件，工作区共 {total} 个文件。</p>'
    if not files:
        return _page("备份树", form + summary + '<p class="muted">没有匹配的文件。</p>')
    rows = "".join(
        f'<tr><td class="mono"><a href="{_raw_href(relpath)}">{html.escape(relpath)}</a></td>'
        f'<td class="muted">{html.escape(human_bytes(int(size)))}</td></tr>\n'
        for relpath, size in files
    )
    table = f"<table><thead><tr><th>路径</th><th>大小</th></tr></thead><tbody>{rows}</tbody></table>"
    return _page("备份树", form + summary + table)


def _walk_files(root: Path, prefix: str = "") -> Iterator[tuple]:
    """遍历工作区普通文件（相对路径, 字节数），不进入 .git 与 _deleted。"""
    directory = Path(root)
    try:
        with os.scandir(directory) as scanned:
            children = sorted(scanned, key=lambda item: item.name.casefold())
    except OSError as exc:
        _LOG.warning("无法读取目录 %s: %s", directory, exc)
        return
    for child in children:
        relpath = f"{prefix}{child.name}"
        try:
            if child.is_dir(follow_symlinks=False):
                if child.name in SKIP_DIRS:
                    continue
                yield from _walk_files(Path(child.path), f"{relpath}/")
                continue
            if not child.is_file(follow_symlinks=False):
                continue
            size = child.stat(follow_symlinks=False).st_size
        except OSError as exc:
            _LOG.warning("无法读取目录项 %s: %s", child.path, exc)
            continue
        yield relpath, size


def _safe_member(root: Path, relpath: str) -> Path:
    """把请求里的相对路径解析成工作区内的绝对路径；越界一律抛 MyuError。"""
    if "\x00" in relpath:
        raise MyuError(f"路径包含非法字符: {relpath!r}")
    if relpath.startswith(("/", "\\")):
        raise MyuError(f"不接受绝对路径: {relpath!r}")
    normalized = normalize_relpath(relpath)
    if not normalized:
        raise MyuError("路径为空")
    base = Path(root).resolve()
    candidate = (base / normalized).resolve()
    if candidate != base and base not in candidate.parents:
        raise MyuError(f"路径越界: {relpath!r}")
    return candidate


class WebHandler(BaseHTTPRequestHandler):
    """只读网页：五条路由全部要求 Basic 认证，非 GET 一律 404。"""

    protocol_version = "HTTP/1.0"
    _responded = False

    def version_string(self) -> str:
        return "Backup-my-U"

    def do_GET(self) -> None:
        self._handle()

    # 只允许 GET：其余方法走同一入口，认证通过后直接 404。
    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    def log_message(self, format: str, *args) -> None:
        """访问日志交给 logging，不写 stderr 打扰 systemd。"""
        _LOG.debug("web %s %s", self.client_address[0], format % args)

    @property
    def _cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def _open_store(self) -> Store:
        """每条请求各开一条连接：sqlite3 连接不允许跨线程复用。"""
        return Store(self.server.db_path)  # type: ignore[attr-defined]

    def _handle(self) -> None:
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError):
            _LOG.debug("客户端提前断开: %s %s", self.command, self.path)
        except Exception:
            _LOG.exception("处理请求失败: %s %s", self.command, self.path)
            if not self._responded:
                self._fail(500, "服务器内部错误。")

    def _dispatch(self) -> None:
        if not self._authorized():
            _LOG.warning("网页认证失败: %s %s", self.client_address[0], self.path)
            self._fail(401, "需要认证：请输入网页口令。", (("WWW-Authenticate", AUTH_CHALLENGE),))
            return
        if self.command != "GET":
            self._fail(404, "只允许 GET 请求。")
            return
        parsed = urlsplit(self.path)
        route = parsed.path
        if route == "/":
            with self._open_store() as store:
                page = _index_page(store)
            self._send_html(200, page)
        elif route.startswith("/run/"):
            self._show_run(route[len("/run/"):])
        elif route == "/files":
            self._show_files(parse_qs(parsed.query, keep_blank_values=True))
        elif route.startswith("/raw/"):
            self._send_raw(route[len("/raw/"):])
        elif route.startswith("/log/"):
            self._send_log(route[len("/log/"):])
        else:
            self._fail(404, "页面不存在。")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization")
        if not header:
            return False
        parts = header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != AUTH_SCHEME.lower():
            return False
        try:
            decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, separator, password = decoded.partition(":")
        if not separator:
            return False
        return verify_password(self._cfg.web_password_hash, password)

    def _show_run(self, remainder: str) -> None:
        run_id = unquote(remainder)
        if not _valid_run_id(run_id):
            self._fail(400, "运行标识非法。")
            return
        with self._open_store() as store:
            run = store.get_run(run_id)
            page = None if run is None else _run_page(store, run)
        if page is None:
            self._fail(404, "运行记录不存在。")
            return
        self._send_html(200, page)

    def _show_files(self, query: dict) -> None:
        values = query.get("q") or [""]
        text = values[0] if values else ""
        files = list(_walk_files(self._cfg.work_tree))
        total = len(files)
        if text:
            needle = text.casefold()
            files = [item for item in files if needle in item[0].casefold()]
        self._send_html(200, _files_page(files, text, total))

    def _send_raw(self, remainder: str) -> None:
        relpath = unquote(remainder)
        try:
            target = _safe_member(self._cfg.work_tree, relpath)
        except MyuError as exc:
            _LOG.warning("拒绝下载 %r: %s", relpath, exc)
            self._fail(403, "拒绝访问：路径不在备份工作区内。")
            return
        if not target.is_file():
            self._fail(404, "文件不存在。")
            return
        try:
            size = target.stat().st_size
        except OSError as exc:
            _LOG.warning("无法读取文件 %s: %s", target, exc)
            self._fail(404, "文件不存在。")
            return
        disposition = "attachment; filename*=UTF-8''" + quote(target.name, safe="")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self._responded = True
        if self.command == "HEAD":
            return
        try:
            with open(target, "rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError as exc:
            _LOG.warning("传输文件中断 %s: %s", target, exc)

    def _send_log(self, remainder: str) -> None:
        run_id = unquote(remainder)
        if not _valid_run_id(run_id):
            self._fail(400, "运行标识非法。")
            return
        path = Path(self._cfg.log_dir) / f"{run_id}.log"
        try:
            if not path.is_file():
                self._fail(404, "日志不存在。")
                return
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _LOG.warning("无法读取日志 %s: %s", path, exc)
            self._fail(404, "日志不存在。")
            return
        self._send_bytes(200, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _fail(self, code: int, detail: str, headers: tuple = ()) -> None:
        self._send_html(code, _page(str(code), f'<p class="muted">{html.escape(detail)}</p>'),
                        headers)

    def _send_html(self, code: int, text: str, headers: tuple = ()) -> None:
        self._send_bytes(code, text.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _send_bytes(self, code: int, payload: bytes, content_type: str,
                    headers: tuple = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self._responded = True
        if self.command != "HEAD":
            self.wfile.write(payload)


class WebServer(ThreadingHTTPServer):
    """只读网页服务：daemon 线程 + 端口复用。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple, cfg: Config, store) -> None:
        self.cfg = cfg
        self.store = store
        # sqlite3 连接绑定创建它的线程，而 ThreadingHTTPServer 每连接一个新线程，
        # 因此只沿用传入 Store 的库文件路径，连接在每次请求里按需新开、用完即关。
        self.db_path = Path(store.db_path)
        super().__init__(address, WebHandler)


def create_server(cfg: Config, store, host: str, port: int) -> ThreadingHTTPServer:
    """port=0 时由系统分配端口，便于测试。"""
    return WebServer((host, int(port)), cfg, store)


def serve(cfg: Config, store) -> int:
    """读 cfg.web_bind/web_port 起服务；未设口令时拒绝启动并返回 2。"""
    if not cfg.web_password_hash:
        _LOG.error("未设置网页口令（web.password_hash 为空），拒绝启动网页服务；"
                   "请先执行 myu set-password")
        return 2
    server = create_server(cfg, store, cfg.web_bind, cfg.web_port)
    host, port = server.server_address[0], server.server_address[1]
    _LOG.info("网页服务已启动: http://%s:%d/", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOG.info("收到中断信号，网页服务停止")
    finally:
        server.server_close()
    return 0
