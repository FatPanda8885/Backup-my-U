from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myu import device, util
from myu.config import Config
from myu.device import DeviceInfo, matches_target, parse_lsblk, read_marker

LSBLK_FULL = """
{
   "blockdevices": [
      {"name": "sda", "maj:min": "8:0", "rm": false, "size": 512110190592, "ro": false,
       "type": "disk", "mountpoint": null, "vendor": "ATA     ", "model": "Samsung SSD 860 ",
       "serial": "S3Z1NB0K123456A", "tran": "sata",
       "children": [
          {"name": "sda1", "maj:min": "8:1", "rm": false, "size": 536870912, "ro": false,
           "type": "part", "mountpoint": "/boot/efi", "fstype": "vfat", "fsver": "FAT32",
           "label": null, "uuid": "1A2B-3C4D", "partuuid": "9a0c7e01-0001", "partlabel": null,
           "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "serial": null, "tran": null},
          {"name": "sda2", "maj:min": "8:2", "rm": false, "size": 511573319680, "ro": false,
           "type": "part", "mountpoint": "/", "fstype": "ext4", "fsver": "1.0",
           "label": "", "uuid": "6f7e2b1c-1111-2222-3333-444455556666",
           "partuuid": "9a0c7e01-0002", "partlabel": null,
           "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4", "serial": null, "tran": null},
          {"name": "sda3", "maj:min": "8:3", "rm": false, "size": 8589934592, "ro": false,
           "type": "part", "mountpoint": null, "fstype": "LVM2_member", "fsver": "LVM2 001",
           "label": null, "uuid": "Qk3pQ2-uW0Yt-8x3Q-Fh0v-T0Pq-2v1z-9kQ0aB",
           "partuuid": "9a0c7e01-0003", "partlabel": null,
           "parttype": "e6d6d379-f507-44c2-a23c-238f2a3df928", "serial": null, "tran": null,
           "children": [
              {"name": "vg0-root", "maj:min": "253:0", "rm": false, "size": 4294967296,
               "ro": false, "type": "lvm", "mountpoint": "/var", "fstype": "ext4",
               "fsver": "1.0", "label": "root", "uuid": "deadbeef-0000-1111-2222-333344445555",
               "serial": null}
           ]}
       ]},
      {"name": "sdb", "maj:min": "8:16", "rm": true, "size": 31004295168, "ro": false,
       "type": "disk", "mountpoint": null, "vendor": "SanDisk ", "model": "Ultra USB 3.0",
       "serial": "0c5d3a1f9e8b7d2a", "tran": "usb",
       "children": [
          {"name": "sdb1", "maj:min": "8:17", "rm": true, "size": 31004229632, "ro": false,
           "type": "part", "mountpoint": null, "fstype": "exfat", "fsver": "1.0",
           "label": "备份盘", "uuid": "8C4E-1F2A", "partuuid": "1b2c4d-0001", "partlabel": null,
           "parttype": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", "serial": null, "tran": null},
          {"name": "sdb2", "maj:min": "8:18", "rm": true, "size": 61440, "ro": false,
           "type": "part", "mountpoint": null, "fstype": null, "fsver": null,
           "label": null, "uuid": null, "partuuid": "1b2c4d-0002", "partlabel": null,
           "parttype": null, "serial": null, "tran": null}
       ]},
      {"name": "sr0", "maj:min": "11:0", "rm": true, "size": 1073741312, "ro": false,
       "type": "rom", "mountpoint": null, "vendor": "QEMU    ", "model": "QEMU DVD-ROM",
       "serial": null, "tran": null, "fstype": null}
   ]
}
"""

LSBLK_NO_SERIAL = """
{
   "blockdevices": [
      {"name": "vda", "maj:min": "254:0", "rm": false, "size": 42949672960, "ro": false,
       "type": "disk", "mountpoint": null, "serial": null, "tran": null,
       "children": [
          {"name": "vda1", "maj:min": "254:1", "rm": false, "size": 42948624384,
           "ro": false, "type": "part", "mountpoint": "/", "fstype": "ext4",
           "label": null, "uuid": null, "serial": null}
       ]}
   ]
}
"""

LSBLK_STRING_SIZE = """
{
   "blockdevices": [
      {"name": "sdc", "size": 1024, "type": "disk", "serial": "abc", "fstype": null,
       "children": [
          {"name": "sdc1", "size": "4096", "fstype": "ntfs", "label": "WIN",
           "uuid": "0A1B2C3D", "mountpoint": ""}
       ]}
   ]
}
"""


def make_config(root: Path, **overrides) -> Config:
    params = dict(
        repo_root=root / "repo",
        state_dir=root / "state",
        log_dir=root / "logs",
    )
    params.update(overrides)
    return Config(**params)


def make_device(**overrides) -> DeviceInfo:
    params = dict(
        name="sdb1",
        serial="",
        label="",
        fs_type="exfat",
        uuid="",
        mountpoint=None,
        size=1024,
    )
    params.update(overrides)
    return DeviceInfo(**params)


class ParseLsblkTest(unittest.TestCase):
    def test_parses_realistic_output(self) -> None:
        devices = parse_lsblk(LSBLK_FULL)

        self.assertEqual([item.name for item in devices], ["sda1", "sda2", "sda3", "vg0-root", "sdb1"])
        for item in devices:
            self.assertTrue(item.fs_type)
            self.assertIsInstance(item, DeviceInfo)

        efi = devices[0]
        self.assertEqual(efi.fs_type, "vfat")
        self.assertEqual(efi.serial, "S3Z1NB0K123456A")
        self.assertEqual(efi.uuid, "1A2B-3C4D")
        self.assertEqual(efi.label, "")
        self.assertEqual(efi.mountpoint, Path("/boot/efi"))
        self.assertEqual(efi.size, 536870912)

        self.assertEqual(devices[1].mountpoint, Path("/"))
        self.assertEqual(devices[1].fs_type, "ext4")

        lvm = devices[3]
        self.assertEqual(lvm.serial, "S3Z1NB0K123456A")
        self.assertEqual(lvm.label, "root")
        self.assertEqual(lvm.mountpoint, Path("/var"))

        stick = devices[4]
        self.assertEqual(stick.serial, "0c5d3a1f9e8b7d2a")
        self.assertEqual(stick.fs_type, "exfat")
        self.assertEqual(stick.label, "备份盘")
        self.assertIsNone(stick.mountpoint)
        self.assertEqual(stick.size, 31004229632)

        names = [item.name for item in devices]
        self.assertNotIn("sdb2", names)
        self.assertNotIn("sr0", names)

    def test_empty_serial_stays_empty(self) -> None:
        devices = parse_lsblk(LSBLK_NO_SERIAL)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "vda1")
        self.assertEqual(devices[0].serial, "")
        self.assertEqual(devices[0].label, "")
        self.assertEqual(devices[0].uuid, "")
        self.assertEqual(devices[0].mountpoint, Path("/"))
        self.assertEqual(devices[0].size, 42948624384)

    def test_string_size_and_empty_mountpoint(self) -> None:
        devices = parse_lsblk(LSBLK_STRING_SIZE)

        self.assertEqual([item.name for item in devices], ["sdc1"])
        self.assertEqual(devices[0].size, 4096)
        self.assertEqual(devices[0].label, "WIN")
        self.assertIsNone(devices[0].mountpoint)

    def test_nodes_without_fstype_are_dropped(self) -> None:
        text = "{\"blockdevices\": [{\"name\": \"sdz\", \"size\": 10, \"fstype\": null}]}"

        self.assertEqual(parse_lsblk(text), [])

    def test_missing_children_is_fine(self) -> None:
        text = '{"blockdevices": [{"name": "sdd1", "fstype": "ext4", "children": null}]}'

        devices = parse_lsblk(text)

        self.assertEqual([item.name for item in devices], ["sdd1"])

    def test_children_with_junk_are_ignored(self) -> None:
        text = '{"blockdevices": [{"name": "sdd1", "fstype": "ext4", "children": [null, "x", 3]}]}'

        self.assertEqual([item.name for item in parse_lsblk(text)], ["sdd1"])

    def test_empty_blockdevices(self) -> None:
        self.assertEqual(parse_lsblk('{"blockdevices": []}'), [])
        self.assertEqual(parse_lsblk("{}"), [])

    def test_invalid_inputs_raise(self) -> None:
        for text in ("", "   ", "not json", "[]", '{"blockdevices": "nope"}'):
            with self.subTest(text=text):
                with self.assertRaises(util.MyuError):
                    parse_lsblk(text)

    def test_non_string_input_raises(self) -> None:
        with self.assertRaises(util.MyuError):
            parse_lsblk(b'{"blockdevices": []}')


class ListBlockDevicesTest(unittest.TestCase):
    def test_command_missing_raises(self) -> None:
        with mock.patch("myu.device.subprocess.run", side_effect=FileNotFoundError("no lsblk")):
            with self.assertRaises(util.MyuError) as ctx:
                device.list_block_devices()

        self.assertIn("lsblk", str(ctx.exception))

    def test_parses_command_output(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout=LSBLK_FULL, stderr="")
        with mock.patch("myu.device.subprocess.run", return_value=completed) as runner:
            devices = device.list_block_devices()

        self.assertEqual(runner.call_args.args[0], ["lsblk", "-J", "-b"])
        self.assertEqual(len(devices), 5)
        self.assertEqual(devices[-1].name, "sdb1")

    def test_nonzero_exit_raises(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="lsblk: failed to access sysfs: No such file or directory\n"
        )
        with mock.patch("myu.device.subprocess.run", return_value=completed):
            with self.assertRaises(util.MyuError) as ctx:
                device.list_block_devices()

        self.assertIn("failed to access sysfs", str(ctx.exception))


class ReadMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(read_marker(self.root, ".myu-id"))

    def test_content_is_stripped(self) -> None:
        (self.root / ".myu-id").write_text("  stick-01 \n", encoding="utf-8")

        self.assertEqual(read_marker(self.root, ".myu-id"), "stick-01")

    def test_empty_file_returns_empty_string(self) -> None:
        (self.root / ".myu-id").write_text("", encoding="utf-8")

        self.assertEqual(read_marker(self.root, ".myu-id"), "")

    def test_empty_marker_name_returns_none(self) -> None:
        self.assertIsNone(read_marker(self.root, "  "))


class MatchesTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.stick = self.root / "stick"
        self.stick.mkdir()

    def write_marker(self, text: str) -> None:
        (self.stick / ".myu-id").write_text(text, encoding="utf-8")

    def test_serial_match_needs_no_marker(self) -> None:
        cfg = make_config(self.root, device_serial="0c5d3a1f9e8b7d2a")
        dev = make_device(serial="0c5d3a1f9e8b7d2a")

        self.assertTrue(matches_target(dev, self.stick, cfg))

    def test_serial_is_stripped(self) -> None:
        cfg = make_config(self.root, device_serial="  stick-01 ")
        dev = make_device(serial="stick-01")

        self.assertTrue(matches_target(dev, self.stick, cfg))

    def test_serial_mismatch_without_marker_is_false(self) -> None:
        cfg = make_config(self.root, device_serial="other")
        dev = make_device(serial="stick-01")

        self.assertFalse(matches_target(dev, self.stick, cfg))

    def test_marker_presence_is_enough_when_value_empty(self) -> None:
        cfg = make_config(self.root, device_serial="other")
        self.write_marker("anything")

        self.assertTrue(matches_target(make_device(), self.stick, cfg))

    def test_marker_value_must_match(self) -> None:
        cfg = make_config(self.root, device_serial="", marker_value="stick-01")
        self.write_marker("stick-01")
        self.assertTrue(matches_target(make_device(), self.stick, cfg))

        self.write_marker("another stick")
        self.assertFalse(matches_target(make_device(), self.stick, cfg))

    def test_empty_serial_falls_back_to_marker(self) -> None:
        cfg = make_config(self.root, device_serial="", marker_value="")
        self.write_marker("x")

        self.assertTrue(matches_target(make_device(serial=""), self.stick, cfg))

    def test_missing_marker_never_matches(self) -> None:
        cfg = make_config(self.root, device_serial="", marker_value="")

        self.assertFalse(matches_target(make_device(serial=""), self.stick, cfg))


@unittest.skipUnless(os.name == "posix", "mount/umount/lsblk 挂载流程仅 Linux 可跑")
class MountTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_mount_readonly_returns_existing_mountpoint(self) -> None:
        table = [("/dev/sdb1", "/media/usb")]
        with mock.patch.object(device, "_read_mount_table", return_value=table):
            with mock.patch("myu.device.subprocess.run") as runner:
                target = device.mount_readonly("/dev/sdb1", self.root / "mnt")

        self.assertEqual(target, Path("/media/usb"))
        runner.assert_not_called()

    def test_mount_readonly_passes_readonly_options(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(device, "_read_mount_table", return_value=[]):
            with mock.patch("myu.device.subprocess.run", return_value=completed) as runner:
                target = device.mount_readonly("/dev/sdb1", self.root / "mnt")

        self.assertEqual(target, self.root / "mnt")
        self.assertTrue((self.root / "mnt").is_dir())
        args = runner.call_args.args[0]
        self.assertEqual(args[:3], ["mount", "-o", "ro,nosuid,nodev"])
        self.assertEqual(args[3:], ["/dev/sdb1", str(self.root / "mnt")])

    def test_mount_readonly_raises_when_mount_fails(self) -> None:
        failed = subprocess.CompletedProcess(
            [], 32, stdout="", stderr="mount: /mnt: permission denied\n"
        )
        with mock.patch.object(device, "_read_mount_table", return_value=[]):
            with mock.patch("myu.device.subprocess.run", return_value=failed):
                with self.assertRaises(util.MyuError) as ctx:
                    device.mount_readonly("/dev/sdb1", self.root / "mnt")

        self.assertIn("permission denied", str(ctx.exception))

    def test_unmount_skips_unmounted_target(self) -> None:
        with mock.patch.object(device, "_read_mount_table", return_value=[]):
            with mock.patch("myu.device.subprocess.run") as runner:
                device.unmount(self.root / "mnt")

        runner.assert_not_called()

    def test_unmount_calls_umount(self) -> None:
        mountpoint = self.root / "mnt"
        mountpoint.mkdir()
        table = [("/dev/sdb1", str(mountpoint))]
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(device, "_read_mount_table", return_value=table):
            with mock.patch("myu.device.subprocess.run", return_value=completed) as runner:
                device.unmount(mountpoint)

        self.assertEqual(runner.call_args.args[0], ["umount", str(mountpoint)])

    def test_find_target_uses_serial_first(self) -> None:
        cfg = make_config(self.root, device_serial="stick-01")
        dev = make_device(name="sdb1", serial="stick-01")
        with mock.patch.object(device, "list_block_devices", return_value=[dev]):
            with mock.patch.object(
                device, "_ensure_mounted", return_value=(Path("/mnt/usb"), True)
            ) as ensure:
                found = device.find_target(cfg)

        self.assertEqual(found, (dev, Path("/mnt/usb"), True))
        ensure.assert_called_once_with(dev)

    def test_find_target_falls_back_to_marker(self) -> None:
        cfg = make_config(self.root, device_serial="not-there")
        dev = make_device(name="sdb1", serial="")
        stick = self.root / "stick"
        stick.mkdir()
        (stick / ".myu-id").write_text("stick-01", encoding="utf-8")
        with mock.patch.object(device, "list_block_devices", return_value=[dev]):
            with mock.patch.object(device, "_ensure_mounted", return_value=(stick, True)):
                found = device.find_target(cfg)

        self.assertEqual(found, (dev, stick, True))

    def test_find_target_unmounts_non_matching_device(self) -> None:
        cfg = make_config(self.root, device_serial="")
        dev = make_device(name="sda1", serial="S3Z1")
        empty = self.root / "empty"
        empty.mkdir()
        with mock.patch.object(device, "list_block_devices", return_value=[dev]):
            with mock.patch.object(device, "_ensure_mounted", return_value=(empty, True)):
                with mock.patch.object(device, "unmount") as unmounter:
                    found = device.find_target(cfg)

        self.assertIsNone(found)
        unmounter.assert_called_once_with(empty)

    def test_find_target_returns_none_without_devices(self) -> None:
        cfg = make_config(self.root)
        with mock.patch.object(device, "list_block_devices", return_value=[]):
            self.assertIsNone(device.find_target(cfg))

    def test_acquire_mount_returns_device_and_flag(self) -> None:
        cfg = make_config(self.root)
        dev = make_device(name="sdb1")
        with mock.patch.object(device, "list_block_devices", return_value=[dev]):
            with mock.patch.object(device, "_ensure_mounted", return_value=(Path("/mnt/usb"), True)):
                found = device.acquire_mount(cfg, "/dev/sdb1")

        self.assertEqual(found, (dev, Path("/mnt/usb"), True))

    def test_acquire_mount_unknown_device_raises(self) -> None:
        cfg = make_config(self.root)
        with mock.patch.object(device, "list_block_devices", return_value=[]):
            with self.assertRaises(util.MyuError):
                device.acquire_mount(cfg, "sdz9")


if __name__ == "__main__":
    unittest.main()
