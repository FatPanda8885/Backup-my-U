#!/bin/sh
# Backup-my-U 安装脚本 —— Linux + systemd + udev
#
# 用法：
#   sudo sh deploy/install.sh
#
# 非交互覆盖（设置任一环境变量即跳过对应询问）：
#   MYU_SERIAL=...       目标 U 盘的 ID_SERIAL；设为空串表示跳过序列号匹配
#   MYU_BACKUP_ROOT=...  git 仓库工作区路径，默认 /srv/myu-backup/repo
#   MYU_STATE_DIR=...    SQLite 与运行报告目录，默认 /srv/myu-backup/state
#   MYU_LOG_DIR=...      运行日志目录，默认 <状态目录的父目录>/logs
#
# 本脚本只做部署：不启用网页服务，也不执行真实备份（那会写数据）。
# 可重复执行：已存在的 /etc/myu/config.ini 不会被覆盖。
set -eu

LIB_ROOT=/usr/local/lib/myu
PKG_DIR=$LIB_ROOT/myu
LAUNCHER=/usr/local/bin/myu
CONFIG_DIR=/etc/myu
CONFIG_FILE=$CONFIG_DIR/config.ini
UNIT_DIR=/etc/systemd/system
UDEV_DIR=/etc/udev/rules.d
UDEV_RULE=$UDEV_DIR/99-myu-usb.rules
UNIT_NAMES="myu-backup.service myu-poll.service myu-poll.timer"

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$SCRIPT_DIR
SRC_PKG=$SCRIPT_DIR/../myu

info() { printf '%s\n' "$*"; }
warn() { printf '警告: %s\n' "$*" >&2; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

# 从控制终端读一行到 ANSWER；无终端或 EOF 时 ANSWER 为空串
read_answer() {
    ANSWER=""
    if [ -r /dev/tty ]; then
        read -r ANSWER < /dev/tty 2>/dev/null || ANSWER=""
    else
        read -r ANSWER || ANSWER=""
    fi
}

# ask <提示语> <默认值>：空输入取默认值，结果放入 ANSWER
ask() {
    printf '%s [%s]: ' "$1" "$2"
    read_answer
    if [ -z "$ANSWER" ]; then
        ANSWER=$2
    fi
}

info ""
info "== 步骤 1/7：环境检查 =="

if [ "$(id -u)" -ne 0 ]; then
    die "需要 root 权限。请用 sudo 重新执行：sudo sh deploy/install.sh"
fi
info "  权限：root"

if ! command -v python3 >/dev/null 2>&1; then
    die "未找到 python3。请先安装 Python 3.9 或更高版本（Debian/Ubuntu：apt install python3）。"
fi
PY_FULL=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info[:3] >= (3, 9, 0) else 0)')
if [ "$PY_OK" != "1" ]; then
    die "Python 版本过低：当前 $PY_FULL，需要 3.9 或更高版本。"
fi
info "  python3：$PY_FULL"

if ! command -v git >/dev/null 2>&1; then
    die "未找到 git。请先安装 git 2.25 或更高版本（Debian/Ubuntu：apt install git）。"
fi
GIT_SHORT=$(git --version | awk '{ print $3 }' | awk -F. '{ print $1 "." $2 }')
GIT_OK=$(printf '%s\n' "$GIT_SHORT" | awk -F. '{ if ($1+0 > 2 || ($1+0 == 2 && $2+0 >= 25)) print 1; else print 0 }')
if [ "$GIT_OK" != "1" ]; then
    die "git 版本过低：当前 $(git --version)，需要 2.25 或更高版本。"
fi
info "  git：$(git --version)"

if ! command -v systemctl >/dev/null 2>&1; then
    die "未找到 systemctl。本系统不是 systemd 系统，无法安装服务单元。"
fi
if [ ! -d /run/systemd/system ]; then
    die "systemd 没有作为 init 运行（缺少 /run/systemd/system）。请在 PVE-LXC 容器内或 systemd 宿主机上执行。"
fi
info "  systemd：可用"

if [ ! -d "$SRC_PKG" ]; then
    die "找不到 Python 包目录 $SRC_PKG。请在仓库根目录执行：sudo sh deploy/install.sh"
fi
PY_FILES=$(find "$SRC_PKG" -maxdepth 1 -type f -name '*.py' | wc -l | tr -d ' ')
if [ "$PY_FILES" -eq 0 ]; then
    die "目录 $SRC_PKG 下没有 .py 文件，源码不完整。"
fi
info "  源码包：$SRC_PKG（$PY_FILES 个 .py 文件）"

info ""
info "== 步骤 2/7：安装参数 =="

SERIAL=""
if [ -n "${MYU_SERIAL+x}" ]; then
    SERIAL=$MYU_SERIAL
else
    info "  请插入目标 U 盘，然后另开终端运行 myu device-id（或 lsblk -o NAME,SERIAL）取得 ID_SERIAL。"
    info "  直接回车 = 跳过序列号，udev 规则退化为「任意带文件系统的 USB 移动块设备」（有误触发风险）。"
    printf 'U 盘序列号 ID_SERIAL: '
    read_answer
    SERIAL=$ANSWER
fi
if [ -n "$SERIAL" ]; then
    case $SERIAL in
        *[!A-Za-z0-9._:-]*)
            die "序列号含有不支持的字符（只允许字母、数字与 . _ : -）：$SERIAL"
            ;;
    esac
fi

if [ -z "${MYU_BACKUP_ROOT:-}" ]; then
    ask "备份仓库（git 工作区）路径" "/srv/myu-backup/repo"
    REPO_ROOT=$ANSWER
else
    REPO_ROOT=$MYU_BACKUP_ROOT
fi

if [ -z "${MYU_STATE_DIR:-}" ]; then
    ask "状态目录（SQLite 与运行报告）路径" "/srv/myu-backup/state"
    STATE_DIR=$ANSWER
else
    STATE_DIR=$MYU_STATE_DIR
fi

if [ -z "${MYU_LOG_DIR:-}" ]; then
    LOG_DIR=$(dirname "$STATE_DIR")/logs
else
    LOG_DIR=$MYU_LOG_DIR
fi

for p in "$REPO_ROOT" "$STATE_DIR" "$LOG_DIR"; do
    case $p in
        /*) ;;
        *) die "路径必须是绝对路径：$p" ;;
    esac
done
if [ "$REPO_ROOT" = "$STATE_DIR" ]; then
    die "仓库路径与状态目录不能相同：$REPO_ROOT"
fi

if [ -n "$SERIAL" ]; then
    SERIAL_DISPLAY=$SERIAL
else
    SERIAL_DISPLAY="<跳过，匹配任意带文件系统的 USB 移动块设备>"
fi

info ""
info "  将写入以下位置："
info "    U 盘序列号   ：$SERIAL_DISPLAY"
info "    git 仓库     ：$REPO_ROOT"
info "    状态目录     ：$STATE_DIR"
info "    日志目录     ：$LOG_DIR"
info "    Python 包    ：$PKG_DIR/*.py"
info "    启动器       ：$LAUNCHER"
info "    配置文件     ：$CONFIG_FILE（已存在则不覆盖）"
info "    服务单元     ：$UNIT_DIR/$UNIT_NAMES"
info "    udev 规则    ：$UDEV_RULE"

info ""
info "== 步骤 3/7：安装 Python 包与启动器 =="

mkdir -p "$REPO_ROOT" "$STATE_DIR" "$LOG_DIR"
info "  已确保数据目录存在：$REPO_ROOT 、 $STATE_DIR 、 $LOG_DIR"

mkdir -p "$PKG_DIR"
rm -f "$PKG_DIR"/*.py
for src in "$SRC_PKG"/*.py; do
    cp "$src" "$PKG_DIR/"
done
chmod 0644 "$PKG_DIR"/*.py
info "  已复制 $PY_FILES 个 .py 文件到 $PKG_DIR"

mkdir -p "$(dirname "$LAUNCHER")"
cat > "$LAUNCHER" <<'LAUNCHER_EOF'
#!/bin/sh
# Backup-my-U 启动器：由 deploy/install.sh 生成，重新安装会覆盖本文件
PYTHONPATH=/usr/local/lib/myu exec python3 -m myu "$@"
LAUNCHER_EOF
chmod 755 "$LAUNCHER"
chown root:root "$LAUNCHER"
info "  已生成启动器：$LAUNCHER"

if PYTHONPATH=$LIB_ROOT python3 -c 'import myu' >/dev/null 2>&1; then
    info "  自检：已安装的 myu 包可以导入"
else
    warn "已安装的 myu 包无法导入（$PKG_DIR），请检查源码是否完整"
fi

info ""
info "== 步骤 4/7：配置文件与仓库初始化 =="

mkdir -p "$CONFIG_DIR"
if [ -e "$CONFIG_FILE" ]; then
    info "  配置文件已存在，保持原样（不覆盖）：$CONFIG_FILE"
    if [ -n "$SERIAL" ]; then
        info "  注意：序列号没有写入已存在的配置，如需生效请手工修改该文件的 [device] 段 serial 行。"
    fi
else
    {
        printf '%s\n' '[general]'
        printf '%s\n' '; repo_root 是 git 工作区，state_dir 存 SQLite 与运行报告，log_dir 存运行日志'
        printf 'repo_root = %s\n' "$REPO_ROOT"
        printf 'state_dir = %s\n' "$STATE_DIR"
        printf 'log_dir   = %s\n' "$LOG_DIR"
        cat <<'EXCLUDE_EOF'
; 每行一个 glob，可缩进续写；匹配语义见 docs/DESIGN.md 第 5 节
exclude = $RECYCLE.BIN/**
    System Volume Information/**
    .Trashes/**
    .Spotlight-V100/**
    .fseventsd/**
    .Trash-*/**
    *.tmp
    pagefile.sys
EXCLUDE_EOF
        printf '%s\n' '; 轮询兜底的最小间隔（秒）：任何一次运行结束后的该时长内，带 --poll 的调用直接跳过'
        printf '%s\n' 'poll_guard_seconds = 3600'
        printf '\n'
        printf '%s\n' '[device]'
        printf '%s\n' '; U 盘的 ID_SERIAL；留空则只靠标记文件识别'
        printf 'serial      = %s\n' "$SERIAL"
        printf '%s\n' 'marker_file = .myu-id'
        printf '%s\n' 'marker_value ='
        printf '\n'
        printf '%s\n' '[git]'
        printf '%s\n' 'big_file_threshold = 512m'
        printf '%s\n' 'gc_after_runs      = 20'
        printf '\n'
        printf '%s\n' '[web]'
        printf '%s\n' 'bind          = 0.0.0.0'
        printf '%s\n' 'port          = 8787'
        printf '%s\n' '; 由 myu set-password 写入：pbkdf2_sha256$迭代数$盐b64$哈希b64'
        printf '%s\n' 'password_hash ='
    } > "$CONFIG_FILE"
    info "  已生成配置模板：$CONFIG_FILE"
fi
chmod 0600 "$CONFIG_FILE"
chown root:root "$CONFIG_FILE"

# 仓库初始化只保留 myu init 一个事实来源（DESIGN 7.10：幂等，建目录、repo.init、
# 不覆盖已存在的配置）。此时配置模板刚写好或本来就在，因此它只会补仓库初始化。
if ! "$LAUNCHER" init --repo-root "$REPO_ROOT" --state-dir "$STATE_DIR"; then
    die "myu init 失败，请检查上面的输出"
fi
info "  已执行：$LAUNCHER init（仓库与目录已就绪）"

info ""
info "== 步骤 5/7：systemd 单元与 udev 规则 =="

mkdir -p "$UNIT_DIR"
for unit in $UNIT_NAMES; do
    if [ ! -f "$DEPLOY_DIR/$unit" ]; then
        die "缺少部署文件：$DEPLOY_DIR/$unit"
    fi
    cp "$DEPLOY_DIR/$unit" "$UNIT_DIR/$unit"
    chmod 0644 "$UNIT_DIR/$unit"
    chown root:root "$UNIT_DIR/$unit"
    info "  已安装：$UNIT_DIR/$unit"
done

if [ ! -f "$DEPLOY_DIR/99-myu-usb.rules" ]; then
    die "缺少部署文件：$DEPLOY_DIR/99-myu-usb.rules"
fi
mkdir -p "$UDEV_DIR"
if [ -n "$SERIAL" ]; then
    sed "s/\"__SERIAL__\"/\"$SERIAL\"/g" "$DEPLOY_DIR/99-myu-usb.rules" > "$UDEV_RULE.tmp"
    info "  udev 规则按序列号精确匹配：$SERIAL"
else
    sed -e '/ENV{ID_SERIAL}=="__SERIAL__"/s/^ACTION=/#ACTION=/' \
        -e '/ENV{ID_BUS}=="usb"/s/^#ACTION=/ACTION=/' \
        "$DEPLOY_DIR/99-myu-usb.rules" > "$UDEV_RULE.tmp"
    warn "未设置序列号：已启用降级规则，插拔任何 USB 移动块设备都会触发备份服务。"
fi
mv "$UDEV_RULE.tmp" "$UDEV_RULE"
chmod 0644 "$UDEV_RULE"
chown root:root "$UDEV_RULE"
info "  已安装：$UDEV_RULE"

systemctl daemon-reload
info "  已执行：systemctl daemon-reload"

if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules
    info "  已执行：udevadm control --reload-rules"
else
    warn "未找到 udevadm：已跳过 udev 规则重载，插拔 U 盘不会自动触发备份（轮询定时器仍然有效）。"
fi

info ""
info "== 步骤 6/7：启用轮询定时器 =="

systemctl enable myu-poll.timer
systemctl restart myu-poll.timer
systemctl reset-failed myu-backup.service myu-poll.service >/dev/null 2>&1 || true
info "  已启用并启动：myu-poll.timer（每分钟触发一次 myu-poll.service）"

info ""
info "== 步骤 7/7：安装完成 =="
info ""
info "后续步骤："
info "  1) 查看 U 盘序列号：  $LAUNCHER device-id"
info "  2) 只比对不写盘：    $LAUNCHER scan"
info "  3) 设置网页口令：    $LAUNCHER set-password"
info "  4) 启动只读网页：    $LAUNCHER web --bind 0.0.0.0 --port 8787"
info "     （本脚本不会自动启用网页服务；常驻方式见 docs/DEPLOY.md 第 5 节）"
info "  5) 观察插拔链路：    journalctl -u myu-backup.service -f"
info "     定时器状态：      systemctl list-timers myu-poll.timer"
info "     手动跑一次：      systemctl start myu-backup.service"
info ""
warn "本脚本没有执行任何真实备份：首次正式备份会在 U 盘插入或下一次轮询时自动开始。"
