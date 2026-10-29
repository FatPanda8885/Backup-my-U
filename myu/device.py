from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from myu.config import Config
from myu.util import MyuError

MOUNT_BASE = Path("/run/myu/mnt")
MOUNT_OPTIONS = "ro,nosuid,nodev"
MOUNT_TABLE = Path("/proc/self/mounts")


@dataclass(frozen=True)
class DeviceInfo:
    """lsblk 里有 FSTYPE 的那一层节点；可挂载的最小单位。"""

    name: str
    serial: str
    label: str
    fs_type: str
    uuid: str
    mountpoint: object
    size: int


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _integer(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _mountpoint(value: object) -> Path | None:
    text = _text(value)
    return Path(text) if text else None


def _collect(node: object, inherited_serial: str, into: list) -> None:
    if not isinstance(node, dict):
        return
    serial = _text(node.get("serial")) or inherited_serial
    fs_type = _text(node.get("fstype"))
    if fs_type:
        into.append(
            DeviceInfo(
                name=_text(node.get("name")),
                serial=serial,
                label=_text(node.get("label")),
                fs_type=fs_type,
                uuid=_text(node.get("uuid")),
                mountpoint=_mountpoint(node.get("mountpoint")),
                size=_integer(node.get("size")),
            )
        )
    children = node.get("children")
    if not isinstance(children, list):
        return
    for child in children:
        _collect(child, serial, into)


def parse_lsblk(text: str) -> list:
    """纯函数：解析 lsblk -J -b 输出，只保留带 FSTYPE 的节点，序列号向父节点继承。"""
    if not isinstance(text, str):
        raise MyuError(f"lsblk 输出必须是字符串: {text!r}")
    if not text.strip():
        raise MyuError("lsblk 输出为空")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise MyuError(f"无法解析 lsblk 输出: {exc}") from exc
    if not isinstance(payload, dict):
        raise MyuError("无法解析 lsblk 输出: 顶层不是对象")
    devices = payload.get("blockdevices")
    if devices is None:
        return []
    if not isinstance(devices, list):
        raise MyuError("无法解析 lsblk 输出: blockdevices 不是数组")
    result: list = []
    for node in devices:
        _collect(node, "", result)
    return result


def _run_command(args: list, action: str) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise MyuError(f"{action}失败：系统里找不到命令 {args[0]}") from exc
    except OSError as exc:
        raise MyuError(f"{action}失败：无法执行 {args[0]}: {exc}") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        summary = stderr.splitlines()[-1] if stderr else f"退出码 {completed.returncode}"
        raise MyuError(f"{action}失败：{summary}")
    return completed.stdout or ""


def list_block_devices() -> list:
    """调 lsblk 列出全部可挂载块设备；命令缺失或失败抛 MyuError。"""
    return parse_lsblk(_run_command(["lsblk", "-J", "-b"], "读取块设备列表"))


def read_marker(mountpoint: Path, marker_file: str) -> str | None:
    """读取盘内标记文件内容；文件不存在或读不了返回 None。"""
    name = _text(marker_file)
    if not name:
        return None
    path = Path(mountpoint) / name
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def matches_target(dev: DeviceInfo, mountpoint: Path, cfg: Config) -> bool:
    """序列号相等即认；否则看标记文件（marker_value 为空时存在即认）。"""
    wanted = _text(cfg.device_serial)
    serial = _text(dev.serial)
    if wanted and serial and serial == wanted:
        return True
    content = read_marker(Path(mountpoint), cfg.marker_file)
    if content is None:
        return False
    expected = _text(cfg.marker_value)
    if not expected:
        return True
    return content == expected


def _unescape_field(field: str) -> str:
    return field.replace("\\134", "\\").replace("\\040", " ").replace("\\011", "\t")


def _read_mount_table() -> list:
    try:
        text = MOUNT_TABLE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        entries.append((_unescape_field(parts[0]), _unescape_field(parts[1])))
    return entries


def _same_device(source: str, device: str) -> bool:
    if os.path.abspath(source) == os.path.abspath(device):
        return True
    try:
        return os.path.realpath(source) == os.path.realpath(device)
    except OSError:
        return False


def _find_mount(device: str) -> Path | None:
    for source, mountpoint in _read_mount_table():
        if _same_device(source, device):
            return Path(mountpoint)
    return None


def _is_mounted(mountpoint: Path) -> bool:
    candidate = os.path.abspath(str(mountpoint))
    for _, target in _read_mount_table():
        if os.path.abspath(target) == candidate:
            return True
    return False


def mount_readonly(devnode: str, target: Path) -> Path:
    """只读挂载 devnode 到 target；已挂载则返回现有挂载点。"""
    device = str(devnode)
    existing = _find_mount(device)
    if existing is not None:
        return existing
    mountpoint = Path(target)
    try:
        mountpoint.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MyuError(f"无法创建挂载点 {mountpoint}: {exc}") from exc
    _run_command(["mount", "-o", MOUNT_OPTIONS, device, str(mountpoint)], f"挂载 {device}")
    return mountpoint


def unmount(target: Path) -> None:
    """卸载 target；本来就没挂载时直接返回。"""
    mountpoint = Path(target)
    if not _is_mounted(mountpoint):
        return
    _run_command(["umount", str(mountpoint)], f"卸载 {mountpoint}")


def _devnode(name: str) -> str:
    text = _text(name)
    if text.startswith("/"):
        return text
    return str(Path("/dev") / text)


def _find_device(devname: str) -> DeviceInfo:
    wanted = _text(devname)
    for candidate in list_block_devices():
        if candidate.name == wanted or _devnode(candidate.name) == wanted:
            return candidate
    raise MyuError(f"未找到块设备: {devname}")


def _ensure_mounted(dev: DeviceInfo) -> tuple:
    """返回 (挂载点, 是否本次挂载)；已挂载设备不重复挂。"""
    node = _devnode(dev.name)
    existing = _find_mount(node)
    if existing is not None:
        return existing, False
    if dev.mountpoint is not None:
        mountpoint = Path(str(dev.mountpoint))
        if _is_mounted(mountpoint):
            return mountpoint, False
    return mount_readonly(node, MOUNT_BASE / _text(dev.name)), True


def acquire_mount(cfg: Config, devname: str) -> tuple:
    """按名字（如 sdb1）找到并只读挂载；cfg.device_serial 非空时校验目标身份。"""
    candidate = _find_device(devname)
    mountpoint, we_mounted = _ensure_mounted(candidate)
    if _text(cfg.device_serial) and not matches_target(candidate, mountpoint, cfg):
        if we_mounted:
            unmount(mountpoint)
        raise MyuError(f"设备 {candidate.name} 不匹配配置里的序列号或标记文件")
    return candidate, mountpoint, we_mounted


def find_target(cfg: Config) -> tuple | None:
    """按序列号优先、标记文件兜底找出目标盘；返回 (DeviceInfo, 挂载点, 是否本次挂载)。"""
    devices = list_block_devices()
    wanted = _text(cfg.device_serial)
    if wanted:
        for candidate in devices:
            if _text(candidate.serial) != wanted:
                continue
            mountpoint, we_mounted = _ensure_mounted(candidate)
            return candidate, mountpoint, we_mounted
    for candidate in devices:
        try:
            mountpoint, we_mounted = _ensure_mounted(candidate)
        except MyuError:
            continue
        if matches_target(candidate, mountpoint, cfg):
            return candidate, mountpoint, we_mounted
        if we_mounted:
            try:
                unmount(mountpoint)
            except MyuError:
                continue
    return None
