
# Backup-my-U 设计契约

> 本文件是实现者共用的接口契约。**代码不得偏离**此处定义的模块边界、函数签名、SQLite 结构与配置键。
> 需要变更契约时，先改本文件，评审通过后再改代码。

## 1. 目标

每周把特定 U 盘插入家里云服务器，系统自动识别、比对与上次备份的差异，把**新增**与**修改**的文件归档进一个 git 仓库，可通过带口令的只读网页查看历史与下载文件，也可用一条命令恢复到指定目录。

非目标（v1 不做）：双向同步、Windows 端代理、归档加密、跨设备去重、实时 inotify 监控、云上传。

## 2. 运行环境与硬性约束

| 项目 | 约束 |
|---|---|
| 目标平台 | Linux（Debian/Ubuntu/PVE-LXC），systemd + udev |
| Python | 3.9 及以上，**仅标准库**，禁止任何第三方依赖 |
| git | 2.25 及以上（备份产物即 git 仓库提交） |
| 开发平台 | Windows，仅用于写代码与跑测试（测试用临时目录模拟 U 盘） |
| 文件编码 | UTF-8 无 BOM；.sh/.service/.timer/.rules 必须 **LF** 换行 |
| 每个 .py | 首个语句必须是 `from __future__ import annotations`（允许模块 docstring 在前） |

## 3. 设计不变量（违反即不通过验收）

1. **绝不写 U 盘**：只以 `-o ro,nosuid,nodev` 挂载，所有写操作都发生在仓库工作区。
2. **数据只增不删**：U 盘上删除的文件移入 `_deleted/<run_id>/<relpath>` 后提交，工作区仍可见；git 历史永不 rewrite，禁止任何会 prune 可达对象的操作。
3. **单实例**：一次运行全程持有 FileLock，冲突立即失败退出（exit code 75）。
4. **原子落盘**：复制文件先写同目录 `<name>.myu-tmp` 再 `os.replace`。
5. **失败不污染历史**：mirror 阶段抛异常即不提交，该次运行记为 failed。
6. **路径规范化**：仓库内路径统一 `/` 分隔、相对、无前导 `/`、禁止 `..` 段。
7. **只读网页**：Web 层不得有任何写操作。

## 4. 运行时目录布局

```
/srv/myu-backup/                  BACKUP_ROOT（可配置）
├── repo/                        git 工作区，= Config.repo_root
│   ├── .git/
│   ├── <U盘文件树>
│   └── _deleted/<run_id>/<原路径>
├── state/                       Config.state_dir
│   ├── myu.db                   SQLite
│   └── reports/<run_id>.md      运行摘要
└── logs/<run_id>.log            运行日志
/etc/myu/config.ini              配置（环境变量 MYU_CONFIG 可覆盖）
/run/myu/mnt/<dev>/              程序自行只读挂载点
```

## 5. 配置文件（INI，configparser 读取）

未知键忽略并告警；缺失键取默认值。模板由 `config.write_template()` 生成。

```ini
[general]
repo_root = /srv/myu-backup/repo
state_dir = /srv/myu-backup/state
log_dir   = /srv/myu-backup/logs
; 每行一个 glob，可缩进续写
exclude = $RECYCLE.BIN/**
    System Volume Information/**
    .Trashes/**
    .Spotlight-V100/**
    .fseventsd/**
    .Trash-*/**
    *.tmp
    pagefile.sys
; 轮询兜底的最小间隔（秒）：任何一次运行结束后的该时长内，带 --poll 的调用直接跳过
poll_guard_seconds = 3600

[device]
; U 盘的 ID_SERIAL；留空则只靠标记文件识别
serial      =
; 序列号缺失时的兜底识别：盘内该文件存在（且内容匹配 marker_value）即认
marker_file = .myu-id
marker_value =

[git]
big_file_threshold = 512m
gc_after_runs      = 20

[web]
bind          = 0.0.0.0
port          = 8787
; 由 myu set-password 写入：pbkdf2_sha256$迭代数$盐b64$哈希b64
password_hash =
```

exclude 匹配语义（由 `util.path_matches` 实现）：

- `*` 不跨 `/`，`**` 跨任意层级，`?` 匹配单字符。
- 模式**不以** `/` 开头时，自动允许匹配任意层级前缀（等价于前置一个任意目录前缀）。
- 例：`*.tmp` 命中 `a/b/x.tmp`；`System Volume Information/**` 命中 `System Volume Information/desktop.ini`。

## 6. SQLite 结构（必须逐字使用，DDL 放在 index.Store.migrate）

```sql
CREATE TABLE IF NOT EXISTS meta (
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
```

- runs.status 取值：running / completed / failed / dryrun。
- run_items.kind 取值：added / modified / deleted。
- meta 固定键：`schema_version = 1`、`last_gc_commit`。
- 所有时间戳为 UTC ISO-8601 秒精度，形如 `2025-03-16T10:15:00Z`。
- blob_sha 是 git blob 的哈希（由 `git ls-tree -r -l` 得到），不是自算的 sha256。
- 连接须开 `PRAGMA journal_mode=WAL`、`PRAGMA synchronous=NORMAL`、`PRAGMA foreign_keys=ON`。
## 7. 模块接口契约

只允许 import 标准库与**更底层**的同包模块。依赖方向单向，禁止反向 import：

```
util  <-  config  <-  index  <-  repo  <-  device  <-  sync  <-  report  <-  restore  <-  web  <-  cli
```

即：cli 可 import 全部；web/restore/report 可 import util, config, index, repo, sync, device；sync 可 import util, config, device；device 可 import util, config；repo 可 import util, config；index 可 import util；config 可 import util；**util 不得 import 任何同包模块**。

### 7.1 myu/util.py

```python
class MyuError(Exception): ...

class FileLock:                # 上下文管理器；POSIX 用 fcntl.flock，Windows 用 msvcrt.locking
    def __init__(self, path: Path, timeout: float = 0.0) -> None: ...
    def __enter__(self) -> FileLock: ...   # 抢不到抛 MyuError("另一个备份进程正在运行")
    def __exit__(self, exc_type, exc, tb) -> None: ...

def parse_size(text: str) -> int                  # "512m" -> 536870912，非法抛 MyuError
def human_bytes(n: int) -> str                    # 1500000 -> "1.43 MB"（1024 进制，两位小数）
def utc_now_iso() -> str                          # "2025-03-16T10:15:00Z"
def new_run_id(now=None) -> str                   # "20250316T101500Z"
def setup_logger(log_dir: Path, run_id=None, verbose: bool = False) -> logging.Logger
def path_matches(relpath: str, patterns) -> bool
def normalize_relpath(path: str) -> str           # 反斜杠转 /，去前导 /，拒绝 ..，非法抛 MyuError
def iter_files(root: Path, excludes) -> Iterator
def atomic_replace(src: Path, dst: Path) -> None  # 同目录 rename，失败时清理临时文件
def copy_file_atomic(src: Path, dst: Path) -> int # 写 .myu-tmp -> flush -> fsync -> atomic_replace，返回字节数
def hash_password(password: str, iterations: int = 200000) -> str
def verify_password(stored: str, password: str) -> bool
```

日志格式 `%(asctime)s %(levelname)-7s %(message)s`，同时输出到 stderr 与 `log_dir/<run_id>.log`（run_id 为 None 时只输出 stderr）。

### 7.2 myu/config.py

```python
@dataclass(frozen=True)
class Config:
    repo_root: Path
    state_dir: Path
    log_dir: Path
    excludes: tuple = ()
    device_serial: str = ""
    marker_file: str = ".myu-id"
    marker_value: str = ""
    big_file_threshold: str = "512m"
    gc_after_runs: int = 20
    poll_guard_seconds: int = 3600
    web_bind: str = "0.0.0.0"
    web_port: int = 8787
    web_password_hash: str = ""
    config_path: object = None

    @property
    def db_path(self) -> Path      # state_dir / "myu.db"
    @property
    def reports_dir(self) -> Path  # state_dir / "reports"
    @property
    def work_tree(self) -> Path    # repo_root
    @property
    def content_root(self) -> Path # repo_root

def default_config_path() -> Path            # 环境变量 MYU_CONFIG 或 /etc/myu/config.ini
def load_config(path=None) -> Config         # 文件不存在抛 MyuError
def write_template(path: Path, repo_root=None, state_dir=None) -> None
def save_config(cfg: Config) -> None         # 仅用于回写 password_hash，须保留其余行与注释
```

校验：repo_root / state_dir / log_dir 必须为绝对路径，否则抛 MyuError；web_port 范围 1-65535。

### 7.3 myu/index.py

```python
class Store:
    def __init__(self, db_path) -> None
    def close(self) -> None
    def __enter__(self) / __exit__(...)

    def upsert_device(self, serial, label=None, marker=None, fs_type=None) -> None
    def get_device(self, serial) -> dict | None
    def list_devices(self) -> list

    def start_run(self, run_id, device_serial, started_at) -> int
    def finish_run(self, run_id, status, added=0, modified=0, deleted=0,
                   bytes_copied=0, bytes_total=0, duration_ms=None,
                   git_commit=None, error=None) -> None
    def get_run(self, run_id) -> dict | None
    def list_runs(self, limit=50, device_serial=None) -> list
    def latest_run(self, device_serial=None) -> dict | None
    def last_completed_run_id(self, device_serial) -> str | None

    def list_entries(self, device_serial) -> list                 # relpath/size/mtime_ns/blob_sha
    def replace_entries(self, device_serial, run_id, entries) -> None

    def add_run_items(self, run_id, kind, items) -> None
    def list_run_items(self, run_id, kind=None) -> list
    def count_run_items(self, run_id) -> dict                     # {"added": n, ...}

    def get_meta(self, key) -> str | None
    def set_meta(self, key, value) -> None
```

### 7.4 myu/repo.py

所有 git 调用统一走 GitRepo.run，环境变量追加 `GIT_TERMINAL_PROMPT=0`、`LC_ALL=C`，并固定带 `-c core.quotepath=false`（否则中文路径被转义）。

唯一例外是 `archive_to`：它必须走 `popen` 边读边写，**禁止把 tar 流读进内存**（恢复上百 GB 备份会 OOM）。

```python
class GitRepo:
    def __init__(self, work_tree: Path) -> None
    @property
    def work_tree(self) -> Path
    def exists(self) -> bool                       # work_tree/.git 是否存在
    def init(self, big_file_threshold="512m") -> None
        # git init -q，并设 local config：
        #   user.name=Backup-my-U  user.email=myu@localhost
        #   core.autocrlf=false  core.quotepath=false
        #   core.bigFileThreshold=<big_file_threshold>
        #   gc.auto=0             gc 由我们显式调度
    def run(self, *args, check=True, stdin=None) -> subprocess.CompletedProcess
    def popen(self, *args) -> subprocess.Popen   # 流式出口，stdout=PIPE；仅 archive_to 使用，调用方负责 wait()
    def add_all(self) -> None                      # git add -A
    def has_staged_changes(self) -> bool
    def commit(self, message) -> str | None        # 无变化返回 None
    def tag(self, name) -> None                    # git tag -f
    def head(self) -> str | None                   # 空仓库返回 None
    def rev_parse(self, rev) -> str | None
    def diff_name_status(self, old, new) -> list   # [("A"|"M"|"D", relpath)]
        # git diff --no-renames --name-status；old 为 None 时全部记为 A
    def tree_entries(self, rev) -> dict            # relpath -> (blob_sha, size)，一次 ls-tree -r -l
    def tracked_files(self, rev="HEAD") -> list
    def archive_to(self, rev, dest: Path, subpath=None) -> tuple   # (文件数, 字节数)
        # git archive --format=tar <rev> [-- <subpath>]，用 tarfile 的 r| 流式解包
        # 不用 <rev>:<subpath> 树对象写法：subpath 指向单个文件时 git 会报 not a tree object
        # 不依赖外部 tar 命令；不得把整条流读进内存
        # subpath 语义等同 `git archive <rev>:<subpath>`：剥掉前缀，sub/a.txt 落到 dest/a.txt
        # subpath 指向单个文件时落到 dest/<basename>
    def show_bytes(self, rev, relpath) -> bytes
    def log(self, limit=50) -> list                # [{"commit","date","subject"}]
    def rev_count(self) -> int
    def gc(self) -> None                           # git gc --quiet，不加 --prune=now
    def fsck(self) -> bool
```

git 子进程失败统一抛 `MyuError("git ... 失败: stderr 摘要")`。

### 7.5 myu/device.py

```python
@dataclass(frozen=True)
class DeviceInfo:
    name: str          # "sdb1"
    serial: str
    label: str
    fs_type: str
    uuid: str
    mountpoint: object
    size: int

def parse_lsblk(text: str) -> list      # 纯函数，解析 lsblk -J -b 输出，只保留有 FSTYPE 的节点
def list_block_devices() -> list        # 调 lsblk，命令缺失抛 MyuError
def read_marker(mountpoint: Path, marker_file: str) -> str | None
def matches_target(dev, mountpoint, cfg: Config) -> bool
    # 序列号非空且相等 -> True
    # 否则标记文件匹配（存在，且 marker_value 为空或内容相等）-> True
def mount_readonly(devnode: str, target: Path) -> Path   # 已挂载则返回现有挂载点
def unmount(target: Path) -> None
def acquire_mount(cfg, devname) -> tuple  # (DeviceInfo, Path, we_mounted: bool)
def find_target(cfg) -> tuple | None
```

### 7.6 myu/sync.py

```python
@dataclass(frozen=True)
class FileState:
    relpath: str
    size: int
    mtime_ns: int
    path: Path

@dataclass
class Plan:
    added: list
    modified: list
    unchanged: list
    deleted: list          # relpath 列表
    def changed_bytes(self) -> int
    def total_bytes(self) -> int

@dataclass
class MirrorStats:
    copied: int
    deleted_moved: int
    bytes_copied: int
    errors: list

def scan(root: Path, excludes) -> dict                # relpath -> FileState，跳过符号链接
    # root 不存在必须抛 MyuError：静默当成空盘会把整个工作区判成 deleted
def scan_worktree(worktree: Path, excludes) -> dict   # 同上，强制排除 .git 与 _deleted
    # 只裁剪**顶层**的 .git 与 _deleted，且是整棵不进子树（不是扫完再过滤）
    # 工作区本身不存在时返回 {}（仓库尚未初始化）
def plan(usb, worktree, worktree_root: Path) -> Plan
def mirror(plan, worktree: Path, run_id, progress=None) -> MirrorStats
    # progress(done, total, relpath)：每处理完一个条目回调一次，total = added+modified+deleted
```

差异判据，**严格按此顺序**：

1. 工作区无该路径 -> added
2. size 不同 -> modified
3. size 相同且 mtime_ns 相同 -> unchanged
4. size 相同且 mtime_ns 不同 -> 用 filecmp 逐字节比对（shallow=False）：相同即 unchanged（只更新记录、不复制），不同才 modified

删除：工作区存在、U 盘不存在、且未被 exclude 命中 -> deleted。

mirror 行为：added 建父目录后 `copy_file_atomic`；modified 走同一原子覆盖路径；deleted 移动到 `worktree/_deleted/<run_id>/<relpath>`（同盘 rename，跨盘回退为 copy+unlink）。单个文件失败不中断，记入 errors，最后统一抛 MyuError 携带错误摘要。

### 7.7 myu/report.py

```python
def build_markdown(run: dict, items: dict, stats=None) -> str
    # run 直接传 store.get_run() 的 dict；但 §8 第 12 步发生在 finish_run 之前，
    # 那时 duration_ms 还是 None。cli 必须自行算好耗时塞进该 dict，否则报告里渲染成「—」
    # items 形如 {"added": [...], "modified": [...], "deleted": [...]}
    # 章节顺序：标题 / 概览表 / 新增 / 修改 / 删除 / 异常
def write_report(reports_dir: Path, run_id, text) -> Path
```

### 7.8 myu/restore.py

```python
@dataclass
class RestoreStats:
    files: int
    bytes: int
    skipped: int

def restore(cfg, store, run_id, dest: Path, subpath=None, preserve_mtime=True,
            overwrite=False, dry_run=False, include_deleted=False) -> RestoreStats
```

run_id 既可以是 run_id 也可以是 git tag（run-<run_id>）。目标文件已存在且 overwrite=False 时逐文件跳过并计数，不抛异常。preserve_mtime 时从 store.list_entries 回填 mtime_ns。

默认情况下顶层 `_deleted/` 下的内容**不**落到 dest：restore 还原的是「U 盘当时的样子」。

`include_deleted=True` 时额外把**本次运行**产生的墓碑（即树里 `_deleted/<本次 run_id>/` 前缀下的条目）按原相对路径还原到 dest。
只认自己那一份：`_deleted/<其它 run_id>/` 一律忽略，否则一次 restore 会把历史上所有删除都翻出来。
`subpath` 过滤作用在**原始相对路径**上；overwrite / preserve_mtime / 统计口径与普通文件完全一致。

同一路径既有活文件又有墓碑时（同一次运行里删了又建回来），**活文件优先**，墓碑条目不落地。

### 7.9 myu/web.py

```python
def create_server(cfg: Config, store, host: str, port: int) -> ThreadingHTTPServer
    # port=0 时由系统分配，便于测试
def serve(cfg: Config, store) -> int
    # cfg.web_password_hash 为空时拒绝启动：打错误日志并返回 2
```

并发约定：ThreadingHTTPServer 每连接一个线程，而 sqlite3 连接的默认 `check_same_thread=True`。
因此 Web 层**不得**直接拿传入的 store 对象查库，只能取它的 `db_path`，每请求新开一条短连接（with 退出即关）。
`create_server` / `serve` 的 store 参数仅用于定位数据库。

HTTP 契约（路径与行为必须逐字一致）：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | / | 运行历史表：时间、设备、+N ~M -K、字节、耗时、状态 |
| GET | /run/<run_id> | 该次运行详情，三栏列出新增/修改/删除 |
| GET | /files | 当前备份树，支持 ?q= 子串过滤 |
| GET | /raw/<relpath> | 从工作区下载文件，带 Content-Disposition: attachment |
| GET | /log/<run_id> | 该次运行日志纯文本 |
| 其它 | - | 404 |

- 统一 `Authorization: Basic`；未通过返回 401 并带 `WWW-Authenticate: Basic realm="Backup-my-U"`。
- 口令比较必须用 `util.verify_password`（内部 hmac.compare_digest），禁止裸 ==。
- /raw/ 必须防目录穿越：normalize_relpath 后确认最终解析路径仍在 work_tree 内。
- HTML 输出必须 html.escape，文件名来自 U 盘属不可信输入。

### 7.10 myu/cli.py 与 myu/__main__.py

```python
def main(argv=None) -> int
```

```
myu [--config PATH] [--version] <子命令>   --config 覆盖配置路径（默认见 config.default_config_path）
myu init [--repo-root P] [--state-dir P] [--log-dir P]
        幂等：建目录、repo.init、不覆盖已存在的配置；配置已存在时以文件为准，参数与文件冲突只告警
myu device-id [MOUNTPOINT]        无参数时列出全部块设备及其序列号
myu scan [--device SERIAL]        只比对不写盘（dry-run），打印将备份的文件与字节数
myu backup [--device SERIAL] [--mountpoint P] [--poll]
        --mountpoint 直接使用指定目录当作已挂载的 U 盘根（不执行 mount；排障与测试用）
        --poll 时受 poll_guard_seconds 节流
myu status
myu runs [--limit N]
myu show RUN_ID
myu restore RUN_ID DEST [--path SUB] [--no-mtime] [--overwrite] [--dry-run] [--deleted]
        --deleted：同时把该次运行删掉的文件按原路径还原出来
myu web [--port N] [--bind ADDR]
myu set-password                  交互读取口令两遍，写回 config
myu gc
```

退出码：0 成功、1 运行失败、2 用法错误、3 未找到目标设备、75 已有实例在跑。

## 8. 一次完整备份的执行顺序（cli.backup）

1. 抢 `state_dir/myu.lock` 的 FileLock
2. `device.find_target` 解析设备，找不到 -> exit 3
3. `store.last_completed_run_id` 取上次成功运行，`repo.rev_parse("run-"+prev)` 取上次 commit
4. `sync.scan` 扫描 U 盘，`sync.scan_worktree` 扫描工作区
5. `sync.plan` 得到变更计划；若三项皆空 -> **不创建 runs 行**（避免轮询定时器把历史表灌满），更新节流标记，只记一行 INFO 日志「无变化，跳过」并退出 0
6. `store.start_run` 记为 running
7. `sync.mirror` 把变更落到工作区
8. `repo.add_all` -> `repo.commit("run <run_id>: +A ~M -D")` -> `repo.tag("run-<run_id>")`
9. `repo.diff_name_status(prev, new)` 作为**权威**变更清单，过滤掉 `_deleted/` 前缀（归回 deleted 栏）
10. `repo.tree_entries(new)` 填充 entries，同样过滤 `_deleted/`
11. `store.add_run_items` 写三栏明细，`store.replace_entries` 更新当前状态
12. `report.build_markdown` + `write_report`
13. `store.finish_run(completed, ...)`；按 gc_after_runs 决定是否 `repo.gc()`
14. 更新节流标记 `state_dir/last-run.marker`（mtime 即最近一次运行结束时刻）
15. 仅当 we_mounted 为真时 umount

`--poll` 模式的额外前置检查（在步骤 1 之后立即执行）：
若 `state_dir/last-run.marker` 存在且 `now - mtime < poll_guard_seconds`，直接退出 0，不做任何扫描。
这让「盘一直插着」时轮询每分钟只付出一次 stat 的代价，而不是一次全盘扫描。

## 9. 测试要求

- 框架为标准库 unittest：`python -m unittest discover -s tests -v` 必须全绿。
- 不得依赖真实 U 盘、`/srv` 路径或 root 权限；一律用 `tempfile.TemporaryDirectory`。
- 必须能在 Windows 跑通：不得裸用 os.symlink、不得硬编码 POSIX 路径、不得依赖 mount。
- 关键必测项：
  - sync.plan 的四条判据分支，尤其是「size 相同、mtime 不同、内容相同 -> unchanged」
  - sync.mirror 删除的文件确实落到 `_deleted/<run_id>/` 且原位置消失
  - repo.commit 无变化时返回 None
  - 中文与空格文件名在 ls-tree 与 Web 下载中不被破坏
  - util.path_matches 的 `*`、`**`、无前导斜杠三种语义
  - web 未认证 401、认证通过 200、`/raw/` 穿越被拒
  - restore 支持 run_id 与 tag 两种入参，preserve_mtime 生效

## 10. 代码风格

- 每个 .py 首行 `from __future__ import annotations`，类型注解齐全。
- 不做过度抽象：不引入插件系统、不用元类、不写配置框架。
- 用户可见文案（日志、报告、HTML、CLI 帮助）用中文；标识符用英文。
- 不写「防止将来」的代码，YAGNI。