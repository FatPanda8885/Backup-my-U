# Backup-my-U 部署与排错手册

本文面向在一台常开 Linux 机器（Debian / Ubuntu / PVE-LXC）上部署 Backup-my-U 的运维人员。
接口契约见 `docs/DESIGN.md`，本文只描述部署件（`deploy/` 目录）与运维动作。

## 1. 部署形态

| 路径 | 内容 |
| --- | --- |
| `/usr/local/lib/myu/myu/*.py` | Python 包本体；`PYTHONPATH` 根为 `/usr/local/lib/myu` |
| `/usr/local/bin/myu` | 启动器（sh 包装）：`PYTHONPATH=/usr/local/lib/myu exec python3 -m myu "$@"` |
| `/etc/myu/config.ini` | 配置，权限 0600、属主 root |
| `/etc/systemd/system/myu-backup.service` | 备份服务，**普通 oneshot**，由 udev 触发 |
| `/etc/systemd/system/myu-poll.service` | 轮询兜底 oneshot，由定时器触发 |
| `/etc/systemd/system/myu-poll.timer` | 每分钟触发一次 `myu-poll.service` |
| `/etc/udev/rules.d/99-myu-usb.rules` | U 盘插入触发规则 |
| `/run/myu/mnt/<dev>/` | 程序自建的只读挂载点（由单元里的 `RuntimeDirectory=myu` 提供） |
| `/srv/myu-backup/repo` | git 工作区，备份产物在这里提交 |
| `/srv/myu-backup/state` | `myu.db` 与 `reports/<run_id>.md` |
| `/srv/myu-backup/logs` | `<run_id>.log` 运行日志 |

两条触发链最终都执行 `/usr/local/bin/myu backup`，轮询那条多带一个 `--poll`：

1. U 盘插入 -> udev -> `ENV{SYSTEMD_WANTS}="myu-backup.service"` -> `myu-backup.service`（带 3 秒延迟，`myu backup`）
2. 每整分钟 -> `myu-poll.timer` -> `myu-poll.service`（兜底：udev 没触发、事件丢失、跳过了序列号，`myu backup --poll`）

轮询链受节流保护：`config.ini` 的 `[general] poll_guard_seconds`（默认 3600 秒）内如果已经有过一次运行，
`--poll` 直接退出 0，只付一次 `stat` 的代价，不会每分钟全盘扫描；udev 那条链不带 `--poll`，永远立即执行。

`myu-backup.service` 是普通单元而不是 `%i` 模板单元：udev 只负责“有块设备插上了”，
具体是哪一块盘由程序调用 `device.find_target` 按 `config.ini` 的 `serial` 或标记文件判断。
两条链都没有 U 盘时以退出码 3 结束，这是**正常情况**（单元里已配置 `SuccessExitStatus=3 75`）。

服务以 root 运行：`mount` / `umount` 设备与读取设备序列号都要求特权，不要改成普通用户。

## 2. 前置条件

- Linux，systemd 与 udev 都在运行（`/run/systemd/system` 存在）。
- Python **3.9 及以上**，只用标准库，不需要 pip 安装任何东西。
- git **2.25 及以上**。
- root 权限（安装、udev 触发、mount）。
- U 盘文件系统被内核支持：FAT32/exFAT 通常开箱可用；NTFS 需要内核 `ntfs3` 或用户态 `ntfs-3g`。
- 需要网页时放行端口（默认 8787）。
- LXC 容器部署必须先把 USB 设备透传进容器，容器内看不到 `/dev/sd*` 时本文所有排查都无从谈起。

## 3. 安装步骤

```sh
git clone <仓库地址> Backup-my-U
cd Backup-my-U
sudo sh deploy/install.sh
```

安装脚本按 7 个步骤执行，并逐项打印结果：

1. 环境检查：root、python3 >= 3.9、git >= 2.25、systemd/udev 可用、源码包存在。
2. 询问安装参数并回显所有将写入的路径。
3. 创建数据目录、复制 `myu/*.py` 到 `/usr/local/lib/myu/myu/`、生成启动器。
4. 生成 `/etc/myu/config.ini`（**已存在则不覆盖**）并设权限 0600、属主 root；随后调用
   `/usr/local/bin/myu init --repo-root ... --state-dir ...` 完成仓库初始化（DESIGN 7.10，幂等）。
5. 安装三个单元与 udev 规则（替换 `__SERIAL__`），`systemctl daemon-reload`、`udevadm control --reload-rules`。
6. `systemctl enable` 并启动 `myu-poll.timer`。
7. 打印后续步骤。

安装脚本**不会**启用网页服务，也**不会**跑一次真实备份（那会写数据）。

### 3.1 交互项与环境变量

| 交互项 | 环境变量 | 默认值 |
| --- | --- | --- |
| U 盘序列号（`ID_SERIAL`） | `MYU_SERIAL` | 无；直接回车 = 跳过序列号匹配 |
| git 工作区路径 | `MYU_BACKUP_ROOT` | `/srv/myu-backup/repo` |
| 状态目录 | `MYU_STATE_DIR` | `/srv/myu-backup/state` |
| 日志目录 | `MYU_LOG_DIR` | `<状态目录的父目录>/logs` |

节流键 `poll_guard_seconds` 没有交互项也没有环境变量，直接写在 `/etc/myu/config.ini` 的 `[general]` 段
（安装脚本生成的模板里已带 `poll_guard_seconds = 3600`）。调小 = 轮询更积极，调大 = 更省 CPU 与 I/O。

设置某个环境变量即跳过对应的询问。完全非交互安装示例：

```sh
sudo env MYU_SERIAL='SanDisk_Ultra_4C530001230523116362-0:0' \
     MYU_BACKUP_ROOT=/srv/myu-backup/repo \
     MYU_STATE_DIR=/srv/myu-backup/state \
     sh deploy/install.sh
```

显式跳过序列号（udev 规则退化为“任意 USB 移动块设备触发”）：

```sh
sudo env MYU_SERIAL= sh deploy/install.sh
```

脚本可重复执行：单元与规则会被覆盖重写，`/etc/myu/config.ini` 不会被覆盖（只提示）。

### 3.2 安装完成后先做只读验证

```sh
myu device-id        # 插上目标 U 盘后：列出块设备与 ID_SERIAL
myu scan             # 只比对不写盘（dry-run），确认识别与差异计划
myu status           # 查看配置与最近运行
```

确认无误后再让它自动跑：插上 U 盘（或等下一分钟的轮询）。

## 4. 取得 U 盘序列号

三种办法，任选其一：

```sh
myu device-id                                    # 无参数：列出全部块设备及 ID_SERIAL
lsblk -o NAME,SERIAL,LABEL,FSTYPE,SIZE,TRAN      # 系统视角
udevadm info --query=property --name=/dev/sdb1 | grep -E 'ID_SERIAL|ID_FS_TYPE|ID_BUS|ID_MODEL'
```

要点：

- 本部署统一使用 udev 的 `ID_SERIAL`，它通常形如 `厂商_型号_序列号-0:0`（空格变下划线、末尾带 `-0:0`），
  与 `lsblk` 的 `SERIAL` 列**不是同一个值**。把 `lsblk` 的值填进配置会导致永远识别不到盘。
- 序列号会落到两处：`/etc/myu/config.ini` 的 `[device] serial`（程序据此识别设备）与
  `/etc/udev/rules.d/99-myu-usb.rules`（udev 据此触发）。`install.sh` 一次写两处。
- 序列号只允许字母、数字与 `. _ : -`；含其它字符时脚本会拒绝（可改用下面的标记文件方式）。
- 已经装过、只想换序列号：重跑 `sudo env MYU_SERIAL='…' sh deploy/install.sh`。
  配置文件不会被覆盖（脚本会提示），此时手工修改 `serial` 行即可，并确认 udev 规则里的序列号也换成了新值。

标记文件兜底（无序列号时）：把 `serial` 留空，程序改为认“盘内存在 `.myu-id`”的盘
（`marker_value` 非空时还要求文件内容匹配）。这种模式下 udev 规则只能退化为
“任意带文件系统的 USB 移动块设备触发”，程序会再做一次过滤，但会多出一次进程启动与日志。

## 5. 设置网页口令并启动网页服务

```sh
sudo myu set-password                      # 交互输入两遍口令，写回 config.ini 的 password_hash
sudo myu web --bind 0.0.0.0 --port 8787    # 前台启动只读网页
```

- `password_hash` 为空时 `myu web` 会拒绝启动并返回 2，所以先设口令。
- 网页是只读的，认证方式为 HTTP Basic（`realm="Backup-my-U"`）。
- 配置模板里 `bind = 0.0.0.0`：机器直接暴露在公网时请改成 `127.0.0.1` 并走反向代理，
  或至少在防火墙上限定来源网段。
- `install.sh` 不安装也不启用网页服务。需要常驻时自己建一个单元（安装脚本不会覆盖它）：

```ini
# /etc/systemd/system/myu-web.service
[Unit]
Description=Backup-my-U 只读网页
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/myu web
Restart=on-failure
RuntimeDirectory=myu
ProtectSystem=full
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now myu-web.service
journalctl -u myu-web.service -f
```

## 6. 验证插拔链路

按下面四步确认“插上 U 盘 -> 自动备份”整条链路是通的。

1）udev 是否识别到设备：

```sh
sudo udevadm monitor --udev --property --subsystem-match=block
# 另开一个终端插拔 U 盘；应出现 add 事件，并带 ID_SERIAL、ID_FS_TYPE、ID_BUS
```

2）是否触发了服务：

```sh
systemctl status myu-backup.service
journalctl -u myu-backup.service -n 50 --no-pager
journalctl -u myu-backup.service -f          # 一边插盘一边看
```

3）定时器是否在跑：

```sh
systemctl list-timers myu-poll.timer
journalctl -u myu-poll.service -n 50 --no-pager
```

4）只读验证识别与差异计划：

```sh
myu scan
myu runs --limit 5
```

手动触发一次真实备份（会写数据）：

```sh
sudo systemctl start myu-backup.service
journalctl -u myu-backup.service -f
myu runs --limit 5
myu show <run_id>
```

判断技巧：udev 不触发但 `systemctl start myu-backup.service` 能成功 ⇒ 问题在 udev 规则或事件；
两者都报“未找到目标设备”（退出码 3）⇒ 问题在设备识别（`serial` / 标记文件）。

## 7. 常见故障与排查

退出码对照（程序约定）：`0` 成功、`1` 运行失败、`2` 用法错误、`3` 未找到目标设备、`75` 已有实例在跑。
其中 `3` 与 `75` 在单元里被记为成功，不会让 systemd 标红。

### 7.1 插拔 U 盘后完全没有反应（服务没被启动）

```sh
udevadm info --query=property --name=/dev/sdb1 | grep -E 'ID_SERIAL|ID_FS_TYPE|ID_BUS'
grep -n 'ACTION' /etc/udev/rules.d/99-myu-usb.rules
sudo udevadm control --reload-rules
sudo udevadm test --action=add /sys/class/block/sdb1 2>&1 | tail -n 30
```

常见原因：

- `ID_SERIAL` 与实际不一致（漏了 `-0:0` 或用了 `lsblk` 的 SERIAL）⇒ 改规则文件后重新 `udevadm control --reload-rules`。
- `ENV{ID_FS_TYPE}` 为空：盘上没有分区，或文件系统内核不认识 ⇒ `lsblk -f` 确认。
- 安装时跳过了序列号：看规则文件里生效的是哪一条（未注释的 `ACTION=="add"` 应当只有一条）。
- 容器里没有 udev，或事件由宿主的 udev 处理（容器内需自己跑 udev 并能看到设备）。
- 抄了旧教程用 `RUN+=`：udev 会在事件处理结束后杀掉子进程，本仓库用 `SYSTEMD_WANTS`，不要改回去。

### 7.2 未识别设备（退出码 3）

```sh
myu device-id                                  # 盘在不在列表里、ID_SERIAL 是什么
grep -A3 '\[device\]' /etc/myu/config.ini     # serial 是否为空、是否写错
journalctl -u myu-poll.service -n 30 --no-pager
```

- `serial` 为空且盘上没有 `.myu-id` ⇒ 永远不会被认作目标盘（这是兜底设计的正常行为）。
- `serial` 填了 `lsblk` 的 SERIAL 而不是 udev 的 `ID_SERIAL` ⇒ 永远识别不到。
- 盘刚插上就触发、文件系统还没就绪：备份服务已有 `ExecStartPre=-/bin/sh -c 'sleep 3'`；
  仍失败可等轮询兜底（受 `poll_guard_seconds` 节流，最多要等一小时），或直接
  `sudo systemctl start myu-backup.service`（udev 单元不带 `--poll`，不受节流影响）。
- 已经插着没拔不会再产生 add 事件：可临时 `sudo udevadm trigger --subsystem-match=block --action=add`
  （注意这会立刻尝试一次真实备份），或直接 `sudo systemctl start myu-backup.service`。

### 7.3 挂载失败（退出码 1，日志里有 mount 报错）

```sh
journalctl -u myu-backup.service -n 80 --no-pager | grep -i mount
lsblk -f
findmnt -R /run/myu
```

- 文件系统不支持：`mount: unknown filesystem type 'exfat'` ⇒ 装内核模块或 `exfat-fuse`。
- 设备已被桌面 / udisks2 自动挂载：程序检测到已挂载会复用现有挂载点；若挂载点被以读写方式占用，
  先 `udisksctl unmount -b /dev/sdb1`（或 `umount /dev/sdb1`）。
- 残留挂载点：`sudo umount /run/myu/mnt/sdb1`（必要时加 `-l`），确认没有进程占用：`lsof +D /run/myu/mnt`。
- LXC 容器里根本没有 `/dev/sdb1` ⇒ 设备没透传进容器。

### 7.4 权限不足

本服务必须以 root 运行，不要给单元加 `User=`。排查时同样用 `sudo`：

```sh
ls -l /etc/myu/config.ini                      # 应为 -rw------- root root
stat -c '%U:%G %a' /srv/myu-backup/repo /srv/myu-backup/state /srv/myu-backup/logs
sudo systemctl show -p User -p ExecStart myu-backup.service
```

- `Permission denied: '/srv/myu-backup/state/myu.db'` ⇒ 目录属主不是 root 或权限不足，
  `chown -R root:root /srv/myu-backup` 修正（install.sh 用 `mkdir -p` 创建目录，权限取决于 umask）。
- `ProtectSystem=full` 让服务内的 `/usr`、`/etc` 只读：程序不写这两处，若你自行扩展了功能要写 `/etc`，
  需要相应调整单元（但仍必须保留 `/dev`、`/run` 与挂载能力，不要加 `ProtectHome`、`PrivateDevices`）。
- 手工在 shell 里跑 `myu backup` 与 systemd 里的运行环境不同（`RuntimeDirectory`、umask），
  优先用 `systemctl start myu-backup.service` 复现问题。

### 7.5 定时器每分钟都失败（systemd 标红）

先分清“真失败”和“正常退出”：退出码 3（没有 U 盘）与 75（已有实例在跑）已列入
`SuccessExitStatus=3 75`，不会标红；只有 1 和 2 才是真失败。

轮询本身是**节流**的：`myu backup --poll` 在 `[general] poll_guard_seconds`（默认 3600 秒）内若已有过一次
运行就直接退出 0，不做扫描。因此定时器虽然每分钟触发，正常情况下**一小时内最多一次真实扫描**。
如果 `journalctl` 里每分钟都看到扫描或失败，说明节流标记没被更新——检查 `state_dir/last-run.marker` 的
mtime 与 `state_dir` 是否可写。想立刻跑一次：`sudo systemctl start myu-backup.service`（不带 `--poll`，不受节流）。

```sh
systemctl list-timers myu-poll.timer
journalctl -u myu-poll.service --since '-10 min' --no-pager | tail -n 60
sudo systemctl status myu-poll.service
```

常见原因：`config.ini` 里写了相对路径、仓库目录不可写、git 未初始化或被改坏、
磁盘写满（`df -h /srv`）、U 盘文件系统损坏。

临时止血与恢复：

```sh
sudo systemctl stop myu-poll.timer                               # 暂停轮询
sudo systemctl start myu-poll.timer                              # 修好后恢复
sudo systemctl reset-failed myu-poll.service myu-backup.service  # 清掉红色状态
```

### 7.6 网页 401 或打不开

```sh
grep -n 'password_hash' /etc/myu/config.ini   # 为空表示没设口令，服务会拒绝启动（返回 2）
curl -sS -u user:pass -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/
ss -ltnp | grep 8787
sudo systemctl status myu-web.service         # 如果你自建了这个单元
```

- 反复弹认证框且 401：口令不对（`realm="Backup-my-U"`），重设：`sudo myu set-password`。
- 连接被拒绝：进程没起，先看 `password_hash` 是否为空，再看端口是否被占用。

### 7.7 一次插入触发了多次备份

udev 对同一块盘可能发出多个 add 事件（设备 + 分区）。程序持有文件锁，第二个进程直接以 75 退出，
不会重复备份。日志里出现“另一个备份进程正在运行”或 75 退出码属于正常现象。
想减少噪音就确认规则文件里只有一条未注释的 `ACTION=="add"`。

## 8. 卸载

```sh
# 1. 停止定时器与服务（stop 会中断正在跑的备份，先确认没有进程在跑）
ps aux | grep -c '[m]yu'
sudo systemctl disable --now myu-poll.timer
sudo systemctl stop myu-poll.service myu-backup.service

# 2. 删除单元与 udev 规则并重载
sudo rm -f /etc/systemd/system/myu-backup.service \
           /etc/systemd/system/myu-poll.service \
           /etc/systemd/system/myu-poll.timer \
           /etc/systemd/system/myu-web.service
sudo rm -f /etc/udev/rules.d/99-myu-usb.rules
sudo systemctl daemon-reload
sudo udevadm control --reload-rules

# 3. 删除程序与配置（配置里有网页口令 hash）
sudo rm -rf /usr/local/lib/myu /usr/local/bin/myu /etc/myu

# 4. 数据目录默认保留，确认后再删
sudo rm -rf /srv/myu-backup
```

第 4 步默认不做：`/srv/myu-backup/repo` 是完整备份历史，删掉不可恢复。
只想“停止自动备份”的话，做第 1、2 步就够，程序和配置留着还能随时手跑 `myu backup`。

## 9. 部署决策与已知限制

`docs/DESIGN.md` 没有把下面这些点定死，部署件按“最少意外”的原则做了决定：

1. **配置由安装脚本的内置模板生成**，而不是由 `myu init` 写。原因：模板里 `repo_root` / `state_dir` / `log_dir`
   三个路径必须与用户在安装时输入的完全一致，而 `config.write_template` 没有 `log_dir` 参数，
   由程序推导可能与用户输入不一致。模板与 DESIGN 第 5 节逐键一致（含 `poll_guard_seconds`）；
   已存在的 `config.ini` 一律不覆盖。
2. **`log_dir` 默认取状态目录父目录下的 `logs`**（默认即 `/srv/myu-backup/logs`，与 DESIGN 第 4 节一致），
   可用 `MYU_LOG_DIR` 覆盖。
3. **仓库初始化交给 `myu init`**（`/usr/local/bin/myu init --repo-root ... --state-dir ...`），`install.sh` 不内联
   `git init` / `git config`，避免两处 git 配置漂移；幂等与“不覆盖已存在配置”由 `myu init` 自己保证（DESIGN 7.10）。
   调用位置在配置模板写完之后，因此 `myu init` 只会补仓库初始化，不会用程序模板覆盖安装脚本写入的路径。
4. **`SuccessExitStatus=3 75`**：3（未找到设备）是设计上的正常结果；75（已有实例在跑）表示
   “另一个进程正在做同一件事”，udev 重复事件会经常撞上它，因此同样不算失败。
5. **不加 `ProtectHome`、`PrivateDevices`、`ReadOnlyPaths=/srv`**：服务必须能读 `/dev`、写 `/run/myu`
   与 `/srv/myu-backup`，并执行 mount/umount。只加 `ProtectSystem=full`、`PrivateTmp=yes`、`NoNewPrivileges=yes`。
6. **单元里不写 `Documentation=`**：本手册只存在于仓库 `docs/DEPLOY.md`，`install.sh` 不安装文档，
   写绝对路径会指向不存在的文件。
7. **udev 规则不使用 `RUN+=`**：udev 会杀掉 `RUN+=` 起的子进程；改用 `TAG+="systemd"` +
   `ENV{SYSTEMD_WANTS}+="myu-backup.service"`。
8. **跳过序列号是降级模式**：规则退化为“任意带文件系统的 USB 移动块设备”，误触发风险写在规则文件的注释里，
   程序仍会按 `serial` / 标记文件做二次过滤。
9. **网页服务不属于部署件**：`install.sh` 不创建、不启用 `myu-web.service`，只在本手册第 5 节给出参考单元，
   由使用者自行决定是否常驻。
10. **安装脚本不执行 `udevadm trigger`**：那会在安装瞬间对已插入的盘触发一次真实备份，属于“未告知的写数据”。
    需要时可手动执行（见 7.2）。
