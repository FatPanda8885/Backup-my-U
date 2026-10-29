
# Backup-my-U

插上 U 盘，自动把这一周的**新增**和**修改**归档进一个 git 仓库，随时能查、能下载、能恢复。

为「每周回一次家」的场景设计：回家把 U 盘插到家里云服务器上，剩下的它自己做完。

```
U 盘插入
   │
   ├─ udev 规则命中设备序列号 ─┐
   └─ 兜底定时器每分钟轮询 ────┤
                               ▼
                        文件锁（单实例）
                               ▼
                    只读挂载 -o ro,nosuid,nodev
                               ▼
                  扫描 U 盘（按 exclude 规则过滤）
                               ▼
              差异判定：size → mtime → 逐字节复核
                               ▼
        镜像进 git 工作区（被删的移进 _deleted/<run_id>/）
                               ▼
              git add -A → commit → tag run-<run_id>
                               ▼
              SQLite 记录 + Markdown 摘要报告
                               ▼
                卸载 U 盘（仅当是自己挂的）
                               ▼
        网页 /run/<run_id> 可看这一周到底变了什么
```

## 为什么用 git 做存储引擎

备份产物就是一个**普通文件夹 + .git**，不装本程序也能直接用文件管理器翻、用 `git log` 查。

- **去重免费**：git 是内容寻址存储，同一份内容只存一个 blob。
- **判据免费**：git 的 index 本来就缓 size + mtime，mtime 变了才重算内容哈希，内容没变就不产生新对象 —— 这正是我们要的「大小+时间主判据、内容复核」。
- **历史与恢复免费**：`git log` / `git diff` / `git checkout` 现成可用。
- **删除不丢数据**：U 盘上删掉的文件移进 `_deleted/<run_id>/` 后提交，工作区里仍看得见，历史里也留得住。

## 快速开始

### 服务器上（Linux + systemd）

```sh
git clone <本仓库> /tmp/backup-my-u && cd /tmp/backup-my-u
sudo sh deploy/install.sh
```

安装脚本会问你要备份到哪、U 盘序列号是多少，然后装好 systemd 服务、udev 规则与轮询定时器。

三步收尾：

```sh
# 1. 插上 U 盘，确认它被认出来（打印所有块设备的序列号）
myu device-id

# 2. 给网页设一个口令（不设口令，网页服务拒绝启动）
myu set-password

# 3. 先空跑一次，只看它「打算备份什么」，不写任何东西
myu scan
```

空跑结果符合预期后，插上 U 盘即可，或手动跑一次：

```sh
myu backup
sudo journalctl -u myu-backup -n 50 --no-pager
```

## 日常命令

| 命令 | 作用 |
|---|---|
| `myu scan` | 只比对不写盘，告诉你这一周会备份哪些文件 |
| `myu backup` | 真跑一次（服务调的就是它） |
| `myu status` | 最近一次运行的摘要 |
| `myu runs` | 历史运行列表 |
| `myu show <run_id>` | 某次运行的变更明细 |
| `myu restore <run_id> <目录>` | 把某次备份恢复到指定目录 |
| `myu restore <run_id> <目录> --deleted` | 额外把该次运行删掉的文件捞回来 |
| `myu web` | 启动只读网页 |
| `myu device-id` | 列出块设备与序列号 |
| `myu set-password` | 设置网页访问口令 |
| `myu gc` | 手动整理仓库（平时自动调度） |

退出码：`0` 成功、`1` 运行失败、`2` 用法错误、`3` 没找到目标 U 盘、`75` 已有实例在跑。

## 恢复

```sh
# 把 20250316T101500Z 那次备份整体恢复到 /tmp/restore
myu restore 20250316T101500Z /tmp/restore

# 只恢复其中照片目录
myu restore 20250316T101500Z /tmp/restore --path 照片/

# 覆盖目标里已存在的同名文件；并保留原始修改时间
myu restore 20250316T101500Z /tmp/restore --overwrite

# 把这一次运行里被删掉的文件也一并捞回来
myu restore 20250316T101500Z /tmp/restore --deleted
```

不写 `--no-mtime` 时，文件修改时间会从数据库回填（git 本身不记录 mtime）。

### 关于删除的文件

U 盘上删掉的东西**不会消失**，它被移到 `_deleted/<run_id>/` 里提交进仓库。找回它有两条路：

| 想要什么 | 怎么做 |
|---|---|
| 拿到删除发生时的那一整周状态（**不含**被删的文件） | `myu restore <删除当周的 run_id> <目录>` |
| 只要把当周删掉的那几个文件捞回来 | `myu restore <删除当周的 run_id> <目录> --deleted` |
| 拿回删除**之前**的完整状态 | `myu restore <上一周的 run_id> <目录>` |
| 只下载其中某一个 | 网页 `/run/<run_id>` 的删除栏，点文件名即可下载 |

为什么这样设计？因为 `restore` 默认还原的是「U 盘当时的样子」—— 那一周你本来就把文件删了。
把墓碑一起塞回目标目录，会让恢复结果和 U 盘状态对不上，反而难以判断备份是否正确。

## 网页

```sh
myu web            # 默认 0.0.0.0:8787
myu web --port 9000 --bind 127.0.0.1
```

浏览器打开后弹出口令框。页面全部只读：

| 路径 | 内容 |
|---|---|
| `/` | 运行历史：时间、设备、+新增 ~修改 -删除、耗时、状态 |
| `/run/<run_id>` | 那次到底变了什么，三栏列出 |
| `/files` | 当前备份树，`?q=` 搜索 |
| `/raw/<路径>` | 下载单个文件 |
| `/log/<run_id>` | 运行日志 |

只读意味着它永远不可能删你东西。

## 备份长什么样

```
/srv/myu-backup/
├── repo/                        git 工作区，直接可浏览
│   ├── .git/
│   ├── <U盘文件树>
│   └── _deleted/<run_id>/<原路径>
├── state/
│   ├── myu.db                   设备、运行、文件状态
│   └── reports/<run_id>.md      每次运行的中文摘要
└── logs/<run_id>.log
```

## 配置

`/etc/myu/config.ini`（环境变量 `MYU_CONFIG` 可指向别处）。完整键说明见 [docs/DESIGN.md](docs/DESIGN.md) 第 5 节。最常改的几个：

```ini
[general]
repo_root = /srv/myu-backup/repo
exclude = $RECYCLE.BIN/**
    System Volume Information/**
    *.tmp

[device]
serial = 你的U盘序列号

[web]
port = 8787
password_hash = （用 myu set-password 生成，别手写）
```

## 安全边界

- **绝不写 U 盘**：只以 `-o ro,nosuid,nodev` 挂载。
- **只增不删**：删除的文件进 `_deleted/` 保留在工作区，git 历史永不 rewrite。
- **单实例**：一次运行全程持有文件锁，重复的 udev 事件会被挡掉。
- **原子落盘**：先写 `*.myu-tmp` 再 rename，断电不会留下半个文件。
- **失败不污染历史**：镜像阶段出错就不提交。
- **不加密**：靠文件权限（0700）与内网隔离保护，磁盘被物理拿走则无防护。

## 已知边界

- git 不记录 mtime，恢复时从数据库回填；空目录不被跟踪。
- 单个超大文件（如几十 GB 的镜像）会显著拖慢提交，`core.bigFileThreshold` 默认 512M 之后走流式处理。
- 删掉的旧版本仍占空间（这正是「只增不删」的代价），需要时手动 `myu gc`。
- 只在 Linux + systemd 上验证过；Windows 仅用于开发与跑测试。

## 开发

```sh
python -m unittest discover -s tests -v
```

零第三方依赖，只用标准库，Python 3.9+。接口契约见 [docs/DESIGN.md](docs/DESIGN.md)，部署细节见 [docs/DEPLOY.md](docs/DEPLOY.md)。
