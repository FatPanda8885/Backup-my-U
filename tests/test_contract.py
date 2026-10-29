from __future__ import annotations

# 契约一致性测试：docs/DESIGN.md 声明的接口必须与 myu 包的真实代码一致。
# 期望值一律在运行时从 DESIGN.md 原文解析，本文件不硬编码任何字段名 / 键名 / 路由。
# 解析不出来就 self.fail（绝不静默返回空集合导致假绿）。

import argparse
import base64
import dataclasses
import difflib
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from myu import cli, config, device, index, repo, report, restore, sync, util, web

DESIGN_PATH = Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md"

# §7 各小节 -> 该节 python 代码块所描述的模块
SECTION_MODULES = (
    ("7.1", util),
    ("7.2", config),
    ("7.3", index),
    ("7.4", repo),
    ("7.5", device),
    ("7.6", sync),
    ("7.7", report),
    ("7.8", restore),
    ("7.9", web),
    ("7.10", cli),
)

# §7.5 / §7.6 / §7.8 声明的数据类：(节号, 类名, 模块)
SECTION_DATACLASSES = (
    ("7.5", "DeviceInfo", device),
    ("7.6", "FileState", sync),
    ("7.6", "Plan", sync),
    ("7.6", "MirrorStats", sync),
    ("7.8", "RestoreStats", restore),
)

_FENCE_RE = re.compile(r"^\s*```(.*)$")
_CLASS_RE = re.compile(r"^(\s*)class[ \t]+([A-Za-z_]\w*)")
_DEF_RE = re.compile(r"^(\s*)def[ \t]+([A-Za-z_]\w*)[ \t]*\(")
_FIELD_RE = re.compile(r"^(\s+)([A-Za-z_]\w*)[ \t]*:(?!:)")
_INI_SECTION_RE = re.compile(r"^\[[ \t]*([^\]]*?)[ \t]*\]$")
_INI_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*[=:]")
_SUBCMD_RE = re.compile(r"^myu[ \t]+([a-z][a-z0-9-]*)(?![A-Za-z0-9-])")

_MISSING = object()
_CACHE: dict = {}


def _design_text() -> str:
    """整份 DESIGN.md 原文；只读一次。"""
    if "text" not in _CACHE:
        if not DESIGN_PATH.is_file():
            raise AssertionError(f"契约文件不存在: {DESIGN_PATH}")
        _CACHE["text"] = DESIGN_PATH.read_text(encoding="utf-8")
    return _CACHE["text"]


def _normalize_ddl(text: str) -> list:
    """按行折叠空白、丢弃空行：只比对 SQL 的有意义字符，不比对缩进宽度。"""
    lines = []
    for line in text.splitlines():
        collapsed = re.sub(r"[ \t]+", " ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return lines


class _ContractCase(unittest.TestCase):
    """DESIGN.md 的解析工具；任何抽取为空都在这里 self.fail，而不是返回空集合。"""

    # ------------------------------------------------------------ 文档解析

    def section(self, number: str) -> str:
        """按节标题取正文；number 形如 '5'、'6'、'7.2'、'7.10'。"""
        text = _design_text()
        pattern = re.compile(r"^#{2,4}[ \t]*" + re.escape(number) + r"(?!\d)", re.MULTILINE)
        found = pattern.search(text)
        if found is None:
            self.fail(
                f"在 {DESIGN_PATH} 里找不到 §{number} 的标题"
                f"（期待形如 '## {number}. 标题' 或 '### {number} 标题' 的一行）"
            )
        line_end = text.find("\n", found.end())
        body = "" if line_end < 0 else text[line_end + 1:]
        following = re.search(r"^#{1,6}[ \t]", body, re.MULTILINE)
        if following is not None:
            body = body[:following.start()]
        if not body.strip():
            self.fail(f"§{number} 标题后没有任何正文，契约内容缺失")
        return body

    def blocks(self, number: str) -> list:
        """该节所有围栏代码块：[(语言标记, [行...]), ...]。"""
        blocks = []
        current = None
        for line in self.section(number).splitlines():
            fence = _FENCE_RE.match(line)
            if fence:
                if current is None:
                    current = (fence.group(1).strip().lower(), [])
                else:
                    blocks.append((current[0], current[1]))
                    current = None
                continue
            if current is not None:
                current[1].append(line)
        if current is not None:
            self.fail(f"§{number} 里有一个没有闭合的 ``` 代码块，无法解析")
        if not blocks:
            self.fail(f"§{number} 里没有解析到任何 ``` 代码块（契约文本可能改了排版）")
        return blocks

    def block(self, number: str, *langs) -> list:
        """该节里语言标记命中 langs 的那唯一一个代码块的行列表。"""
        blocks = self.blocks(number)
        picked = [lines for lang, lines in blocks if lang in langs]
        wanted = "或".join(f"```{lang}" if lang else "无语言标记的 ```" for lang in langs)
        if len(picked) != 1:
            seen = "、".join((lang or "无标记") for lang, _ in blocks)
            self.fail(
                f"§{number}: 期待恰好一个 {wanted} 代码块，实际读到 {len(picked)} 个；"
                f"该节共 {len(blocks)} 个代码块，语言标记依次为: {seen}"
            )
        if not any(line.strip() for line in picked[0]):
            self.fail(f"§{number}: {wanted} 代码块是空的，没有可比对的内容")
        return picked[0]

    def class_fields(self, number: str, class_name: str) -> list:
        """契约里某个 class 的字段名，按出现顺序。"""
        lines = self.block(number, "python")
        start = None
        for position, line in enumerate(lines):
            matched = _CLASS_RE.match(line)
            if matched is not None and matched.group(2) == class_name:
                if start is not None:
                    self.fail(f"§{number}: `class {class_name}` 在该节代码块里出现了不止一次")
                start = position
        if start is None:
            declared = [
                matched.group(2)
                for matched in (_CLASS_RE.match(line) for line in lines)
                if matched is not None
            ]
            self.fail(
                f"§{number}: python 代码块里找不到 `class {class_name}`；"
                f"该块声明的类只有: {declared or '无'}"
            )
        base_indent = len(_CLASS_RE.match(lines[start]).group(1))
        fields = []
        for line in lines[start + 1:]:
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= base_indent:
                break
            matched = _FIELD_RE.match(line)
            if matched is not None:
                fields.append(matched.group(2))
        if not fields:
            self.fail(
                f"§{number}: `class {class_name}` 的类体里没有解析出任何字段"
                f"（期待形如 '    name: type = 默认值'）；契约文本可能变了"
            )
        return fields

    def contract_members(self, number: str) -> list:
        """代码块里出现的 ('class' | 'def', 名字, 所属类名或 None)，按出现顺序。"""
        lines = self.block(number, "python")
        members = []
        owner = None
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            klass = _CLASS_RE.match(line)
            if klass is not None:
                owner = (klass.group(2), indent)
                members.append(("class", klass.group(2), None))
                continue
            func = _DEF_RE.match(line)
            if func is not None:
                enclosing = owner[0] if owner is not None and indent > owner[1] else None
                members.append(("def", func.group(2), enclosing))
                continue
            if owner is not None and indent <= owner[1]:
                owner = None
        if not members:
            self.fail(
                f"§{number}: python 代码块里没有解析出任何 `class 名字` 或 `def 名字(`；"
                f"契约文本可能变了"
            )
        return members

    # ------------------------------------------------------------ 代码侧读取

    def module_class_fields(self, module, class_name: str, number: str) -> list:
        klass = getattr(module, class_name, None)
        if klass is None:
            self.fail(f"§{number}: 契约声明了 `class {class_name}`，但 {module.__name__} 里没有这个类")
        if not dataclasses.is_dataclass(klass):
            self.fail(
                f"§{number}: 契约把 {module.__name__}.{class_name} 写成数据类，"
                f"但代码里它不是 dataclass，无法比对字段"
            )
        return [field.name for field in dataclasses.fields(klass)]

    def ini_sections(self) -> dict:
        """§5 ini 代码块：{节名小写: {键名小写, ...}}。"""
        sections: dict = {}
        current = None
        for line in self.block("5", "ini"):
            if not line.strip():
                continue
            if line[0] in " \t":
                continue  # exclude 的多行 glob 续行，不是键
            stripped = line.strip()
            if stripped[0] in ";#":
                continue
            header = _INI_SECTION_RE.match(stripped)
            if header is not None:
                current = header.group(1).strip().lower()
                sections.setdefault(current, set())
                continue
            key = _INI_KEY_RE.match(stripped)
            if key is not None and current is not None:
                sections[current].add(key.group(1).lower())
        if not sections:
            self.fail("§5: ini 代码块里没有解析出任何 `[节名]`，契约模板可能变了")
        if not any(sections.values()):
            self.fail(f"§5: 解析出的节 {sorted(sections)} 下一个键都没有，契约模板可能变了")
        return sections

    def assert_same_fields(self, number: str, label: str, contract: list, code: list) -> None:
        if contract == code:
            return
        details = []
        missing = [name for name in contract if name not in code]
        extra = [name for name in code if name not in contract]
        if missing:
            details.append(f"  契约有、代码没有: {missing}")
        if extra:
            details.append(f"  代码有、契约没有: {extra}")
        if not missing and not extra:
            for position, (left, right) in enumerate(zip(contract, code)):
                if left != right:
                    details.append(
                        f"  字段集合相同但顺序不同：第 {position + 1} 个字段契约是 {left}，代码是 {right}"
                    )
                    break
        details.append(f"  契约（§{number}）: {contract}")
        details.append(f"  代码（{label}）: {code}")
        self.fail(f"§{number} {label} 的字段与契约不一致:\n" + "\n".join(details))


class ConfigContractTests(_ContractCase):
    """§7.2 Config 数据类字段 + §5 配置文件键。"""

    def test_7_2_config_fields_match_dataclass(self) -> None:
        contract = self.class_fields("7.2", "Config")
        code = self.module_class_fields(config, "Config", "7.2")
        self.assert_same_fields("7.2", "myu.config.Config", contract, code)

    def test_5_ini_keys_match_known_options(self) -> None:
        contract = self.ini_sections()
        code = {name: set(options) for name, options in config._KNOWN_OPTIONS.items()}
        problems = []
        for name in sorted(set(contract) | set(code)):
            if name not in code:
                problems.append(
                    f"  §5 有 [{name}] 节（键 {sorted(contract[name])}），"
                    f"但 myu.config._KNOWN_OPTIONS 里没有该节：这些键在真实配置里会被当未知键丢弃"
                )
                continue
            if name not in contract:
                problems.append(
                    f"  myu.config._KNOWN_OPTIONS 有 [{name}] 节（键 {sorted(code[name])}），"
                    f"但 §5 的配置模板里没有该节"
                )
                continue
            missing = sorted(contract[name] - code[name])
            extra = sorted(code[name] - contract[name])
            if missing:
                problems.append(
                    f"  [{name}] 契约声明但代码不认（写进配置会被当未知键丢弃）: {missing}"
                )
            if extra:
                problems.append(f"  [{name}] 代码接受但 §5 模板没有: {extra}")
        if problems:
            self.fail("§5 配置键与 myu.config._KNOWN_OPTIONS 不一致:\n" + "\n".join(problems))


class DataclassContractTests(_ContractCase):
    """§7.5 / §7.6 / §7.8 数据类字段（名字与顺序都要一致）。"""

    def test_data_class_fields_match_code(self) -> None:
        for number, class_name, module in SECTION_DATACLASSES:
            with self.subTest(section=number, dataclass=class_name):
                contract = self.class_fields(number, class_name)
                code = self.module_class_fields(module, class_name, number)
                self.assert_same_fields(number, f"{module.__name__}.{class_name}", contract, code)


class ModuleMemberContractTests(_ContractCase):
    """§7.1-§7.10 声明的 class/def 必须真的存在（代码多于契约不算漂移）。"""

    def test_7_section_members_exist(self) -> None:
        problems = []
        for number, module in SECTION_MODULES:
            for kind, name, enclosing in self.contract_members(number):
                if kind == "class":
                    value = getattr(module, name, _MISSING)
                    if value is _MISSING:
                        problems.append(
                            f"§{number}: 契约声明了 class {name}，但 {module.__name__} 里没有该类"
                        )
                    elif not isinstance(value, type):
                        problems.append(
                            f"§{number}: 契约声明了 class {name}，"
                            f"但 {module.__name__}.{name} 不是类（实际是 {type(value).__name__}）"
                        )
                    continue
                if enclosing is None:
                    if not hasattr(module, name):
                        problems.append(
                            f"§{number}: 契约声明了 {module.__name__}.{name}()，但模块里没有这个函数"
                        )
                    continue
                holder = getattr(module, enclosing, None)
                if holder is None:
                    problems.append(
                        f"§{number}: 契约声明了 {name}()，它属于 class {enclosing}，"
                        f"但 {module.__name__} 里没有 {enclosing}"
                    )
                elif not hasattr(holder, name):
                    problems.append(
                        f"§{number}: 契约声明了 {module.__name__}.{enclosing}.{name}()，"
                        f"但 {enclosing} 类上没有该方法"
                    )
        if problems:
            self.fail(
                "§7 模块成员少于契约（漂移）:\n" + "\n".join(problems)
            )


class CliContractTests(_ContractCase):
    """§7.10 CLI 子命令集合。"""

    def cli_subcommands(self) -> set:
        lines = self.block("7.10", "text", "")
        commands = set()
        for line in lines:
            matched = _SUBCMD_RE.match(line)
            if matched is not None:
                commands.add(matched.group(1))
        if not commands:
            self.fail("§7.10: 纯文本代码块里没有解析出任何 `myu <子命令>` 形式的行")
        return commands

    def test_7_10_subcommand_set_matches_parser(self) -> None:
        contract = self.cli_subcommands()
        action = None
        for candidate in cli.build_parser()._actions:
            if isinstance(candidate, argparse._SubParsersAction):
                action = candidate
                break
        if action is None:
            self.fail("cli.build_parser() 里找不到 argparse 子命令容器（_SubParsersAction）")
        code = set(action.choices)
        missing = sorted(contract - code)
        extra = sorted(code - contract)
        if missing or extra:
            self.fail(
                "§7.10 子命令集合与 cli 的 argparse 不一致:\n"
                f"  契约有、代码没有: {missing}\n"
                f"  代码有、契约没有: {extra}\n"
                f"  契约: {sorted(contract)}\n"
                f"  代码: {sorted(code)}"
            )


class SchemaContractTests(_ContractCase):
    """§6 DDL 与 myu.index.SCHEMA_SQL 的空白归一化比对。"""

    def test_6_ddl_matches_schema_sql(self) -> None:
        contract = _normalize_ddl("\n".join(self.block("6", "sql")))
        code = _normalize_ddl(index.SCHEMA_SQL)
        if contract == code:
            return
        diff = list(
            difflib.unified_diff(
                contract,
                code,
                fromfile="docs/DESIGN.md §6",
                tofile="myu.index.SCHEMA_SQL",
                lineterm="",
                n=2,
            )
        )
        self.fail(
            "§6 DDL 与 myu.index.SCHEMA_SQL 不一致（已按行折叠空白后比对）:\n" + "\n".join(diff)
        )


class WebRouteContractTests(_ContractCase):
    """§7.9 HTTP 契约：把表格里的每条路径打到真实 server 上，断言路由确实被处理。

    选型说明：web.py 里没有可反射的路由常量表（路由是 _dispatch 里的字符串分支），
    因此这里起一个真实 server（127.0.0.1:0）逐条发请求，比断言「字符串出现在源码里」更有牙。
    """

    RUN_ID = "20250316T101500Z"
    PASSWORD = "contract-pass"
    RAW_FILE = "hello.txt"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        root = Path(cls._tmp.name)
        cls.cfg = config.Config(
            repo_root=root / "repo",
            state_dir=root / "state",
            log_dir=root / "logs",
            web_password_hash=util.hash_password(cls.PASSWORD, iterations=1000),
        )
        for directory in (cls.cfg.repo_root, cls.cfg.state_dir, cls.cfg.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        (cls.cfg.work_tree / cls.RAW_FILE).write_text("hello contract\n", encoding="utf-8")
        (cls.cfg.log_dir / f"{cls.RUN_ID}.log").write_text(
            "2025-03-16 10:15:00 INFO    契约测试日志\n", encoding="utf-8"
        )
        cls.store = index.Store(cls.cfg.db_path)
        cls.addClassCleanup(cls.store.close)
        cls.store.start_run(cls.RUN_ID, "CONTRACT-SERIAL", "2025-03-16T10:15:00Z")
        cls.store.finish_run(
            cls.RUN_ID,
            "completed",
            added=1,
            bytes_copied=15,
            bytes_total=15,
            duration_ms=1500,
            git_commit="c0ffee",
        )
        cls.store.add_run_items(cls.RUN_ID, "added", [{"relpath": cls.RAW_FILE}])
        cls.server = web.create_server(cls.cfg, cls.store, "127.0.0.1", 0)
        cls._thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls._thread.start()
        cls.addClassCleanup(cls._stop_server)
        cls.base_url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def _stop_server(cls) -> None:
        cls.server.shutdown()
        cls._thread.join(timeout=15)
        cls.server.server_close()

    def http_table_paths(self) -> list:
        """§7.9 HTTP 契约表格里的路径列（原样，含 <占位符>）。"""
        paths = []
        for line in self.section("7.9").splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 3:
                continue
            path = cells[1].strip("`").strip()
            if path.startswith("/"):
                paths.append(path)
        if not paths:
            self.fail("§7.9: 没有从 HTTP 契约表格里解析出任何以 / 开头的路径")
        return paths

    def concrete_path(self, path: str) -> str:
        if "<" not in path:
            return path
        head = path.split("<", 1)[0]
        if head == "/run/":
            return head + self.RUN_ID
        if head == "/log/":
            return head + self.RUN_ID
        if head == "/raw/":
            return head + self.RAW_FILE
        self.fail(
            f"§7.9 表格里的路径 {path} 带测试不认识的占位符；"
            f"请在 tests/test_contract.py 的 concrete_path 里补上它的合法取值"
        )
        return path

    def get(self, path: str) -> int:
        token = base64.b64encode(f"myu:{self.PASSWORD}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(self.base_url + path)
        request.add_header("Authorization", "Basic " + token)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=30) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as exc:
            try:
                return exc.code
            finally:
                exc.close()

    def test_7_9_routes_are_handled(self) -> None:
        for path in self.http_table_paths():
            with self.subTest(route=path):
                concrete = self.concrete_path(path)
                status = self.get(concrete)
                self.assertEqual(
                    200,
                    status,
                    f"§7.9 声明了路由 GET {path}，但真实 server 对 GET {concrete} 返回 {status}"
                    f"（404 表示该路由未实现；其它非 200 表示该路由崩了或参数语义不符）",
                )


if __name__ == "__main__":
    unittest.main()
