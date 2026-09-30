"""Behavior checks against a synthetic sysfs and /dev tree."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bench-inventory.py"
spec = importlib.util.spec_from_file_location("bench_inventory", SCRIPT)
assert spec and spec.loader
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.sys = root / "sys"
        self.dev = root / "dev"
        self.proc = root / "proc"
        for path in (self.sys, self.dev, self.proc):
            path.mkdir()

    def scan(self, private: bool = False) -> dict:
        return bench.inventory(self.sys, self.dev, self.proc,
                               role="controller", include_private=private)

    def test_video_sysfs_presence_is_not_device_access(self) -> None:
        write(self.sys / "class/video4linux/video0/name", "amd_isp_capture\n")
        write(self.proc / "meminfo", "MemTotal:       32768 kB\n")

        result = self.scan()

        self.assertFalse(result["device_io_tested"])
        self.assertEqual(result["host"]["memory_bytes"],
                         {"status": "present", "value": 32768 * 1024})
        self.assertEqual(result["video"]["sysfs_directory_status"], "present")
        self.assertEqual(result["video"]["devices"], [{
            "node": str(self.dev / "video0"),
            "sysfs_status": "present",
            "topology": str(self.sys / "class/video4linux/video0"),
            "dev_node_status": "absent",
            "aliases": [],
            "name": {"status": "present", "value": "amd_isp_capture"},
        }])
        self.assertEqual(result["serial"]["sysfs_directory_status"], "absent")

    def test_aliases_and_disk_serials_are_private_by_default(self) -> None:
        write(self.sys / "class/tty/ttyUSB0/device", "")
        write(self.sys / "class/video4linux/video2/name", "Capture Card\n")
        write(self.sys / "block/sdb/size", "2097152\n")
        write(self.sys / "block/sdb/removable", "1\n")
        write(self.sys / "block/sdb/device/model", "Test SSD\n")
        for name in ("ttyUSB0", "video2", "sdb"):
            write(self.dev / name, "")
        paths = (
            ("serial/by-path/pci-usb-port0", "ttyUSB0"),
            ("serial/by-id/secret-serial-123", "ttyUSB0"),
            ("v4l/by-path/pci-capture", "video2"),
            ("v4l/by-id/secret-capture-456", "video2"),
            ("disk/by-path/pci-usb-storage", "sdb"),
            ("disk/by-id/secret-disk-789", "sdb"),
        )
        for alias, target in paths:
            link = self.dev / alias
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.dev / target)

        public = self.scan()
        private = self.scan(private=True)

        self.assertEqual(public["serial"]["devices"][0]["aliases"],
                         ["serial/by-path/pci-usb-port0"])
        self.assertEqual(public["video"]["devices"][0]["aliases"],
                         ["v4l/by-path/pci-capture"])
        self.assertEqual(public["disks"]["devices"][0]["capacity_bytes"],
                         2097152 * 512)
        self.assertEqual(public["disks"]["devices"][0]["aliases"],
                         ["disk/by-path/pci-usb-storage"])
        self.assertNotIn("secret", str(public))
        self.assertIn("serial/by-id/secret-serial-123",
                      private["serial"]["devices"][0]["aliases"])
        self.assertIn("disk/by-id/secret-disk-789",
                      private["disks"]["devices"][0]["aliases"])

    def test_firmware_pci_classification_and_usb_whole_devices(self) -> None:
        dmi = self.sys / "class/dmi/id"
        write(dmi / "bios_vendor", "Example Firmware Co\n")
        write(dmi / "bios_version", "1.2.3\n")
        write(dmi / "bios_date", "09/30/2026\n")
        write(dmi / "product_serial", "private-serial-never-read\n")
        write(self.sys / "bus/pci/devices/0000:00:02.0/class", "0x030000\n")
        write(self.sys / "bus/pci/devices/0000:00:02.0/vendor", "0x8086\n")
        write(self.sys / "bus/pci/devices/0000:00:02.0/device", "0x1234\n")
        write(self.sys / "bus/pci/devices/0000:00:1f.6/class", "0x020000\n")
        write(self.sys / "bus/pci/devices/0000:00:1f.6/vendor", "0x8086\n")
        write(self.sys / "bus/pci/devices/0000:00:1f.6/device", "0x5678\n")
        write(self.sys / "bus/pci/devices/0000:00:14.0/class", "0x0c0330\n")
        write(self.sys / "bus/usb/devices/1-2/idVendor", "1a2b\n")
        write(self.sys / "bus/usb/devices/1-2/idProduct", "3c4d\n")
        write(self.sys / "bus/usb/devices/1-2:1.0/bInterfaceClass", "03\n")

        result = self.scan()

        self.assertEqual([entry["address"] for entry in result["graphics_pci"]["devices"]],
                         ["0000:00:02.0"])
        self.assertEqual([entry["address"] for entry in result["network_pci"]["devices"]],
                         ["0000:00:1f.6"])
        self.assertEqual(result["network_pci"]["devices"][0]["device_id"]["value"],
                         "0x5678")
        self.assertEqual(result["host"]["bios"], {
            "vendor": {"status": "present", "value": "Example Firmware Co"},
            "version": {"status": "present", "value": "1.2.3"},
            "date": {"status": "present", "value": "09/30/2026"},
        })
        self.assertNotIn("private-serial-never-read", str(result))
        self.assertEqual([entry["sysfs_name"] for entry in result["usb"]["devices"]],
                         ["1-2"])

    def test_video_topology_correlates_with_usb_parent(self) -> None:
        usb = self.sys / "devices/pci0000:00/usb1/1-2"
        write(usb / "idVendor", "1234\n")
        write(usb / "idProduct", "5678\n")
        write(usb / "1-2:1.0/video4linux/video3/name", "HDMI Capture\n")
        (self.sys / "bus/usb/devices").mkdir(parents=True)
        (self.sys / "bus/usb/devices/1-2").symlink_to(usb)
        (self.sys / "class/video4linux").mkdir(parents=True)
        (self.sys / "class/video4linux/video3").symlink_to(
            usb / "1-2:1.0/video4linux/video3")

        result = self.scan()

        usb_topology = result["usb"]["devices"][0]["topology"]
        video_topology = result["video"]["devices"][0]["topology"]
        self.assertTrue(video_topology.startswith(usb_topology + "/"))
        self.assertEqual(result["video"]["devices"][0]["dev_node_status"], "absent")

    def test_unreadable_metadata_differs_from_absent(self) -> None:
        loop = self.sys / "class/dmi/id/sys_vendor"
        loop.parent.mkdir(parents=True)
        loop.symlink_to(loop)
        write(self.sys / "class/tty", "not a directory")

        result = self.scan()

        self.assertEqual(result["host"]["vendor"]["status"], "unreadable")
        self.assertEqual(result["host"]["product"]["status"], "absent")
        self.assertEqual(result["serial"]["sysfs_directory_status"], "unreadable")


if __name__ == "__main__":
    unittest.main()
