#!/usr/bin/env python3
"""Read-only host inventory for preparing a qdistro hardware bench."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

SERIAL_TTY = re.compile(r"tty(?:USB|ACM)[0-9]+$")
DISK_NAME = re.compile(r"(?:sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+|mmcblk[0-9]+)$")


def read_field(path: Path) -> dict:
    """Read a sysfs/proc scalar; preserve missing versus inaccessible."""
    try:
        return {"status": "present", "value": path.read_text(encoding="utf-8").strip()}
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as error:
        return {"status": "unreadable", "reason": error.strerror or type(error).__name__}


def entries(path: Path) -> tuple[str, list[Path]]:
    try:
        return "present", sorted(path.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        return "absent", []
    except OSError:
        return "unreadable", []


def node_status(path: Path) -> str:
    """Inspect metadata only; never open a device."""
    try:
        path.lstat()
        return "present"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"


def aliases(dev_root: Path, directory: str, include_private: bool) -> dict:
    """Map /dev targets to alias names, omitting serial-bearing by-id names."""
    result: dict = {"status": {}, "targets": {}}
    kinds = ("by-path", "by-id") if include_private else ("by-path",)
    for kind in kinds:
        alias_dir = dev_root / directory / kind
        status, names = entries(alias_dir)
        result["status"][kind] = status
        for link in names:
            if not link.is_symlink():
                continue
            # Resolve a symlink name only. A dangling or sandbox-hidden target
            # remains in the inventory; the device node status is separate.
            target = os.path.realpath(link)
            result["targets"].setdefault(target, []).append(f"{directory}/{kind}/{link.name}")
    return result


def item_aliases(lookup: dict, node: Path) -> list[str]:
    return lookup["targets"].get(os.path.realpath(node), [])


def class_devices(sys_root: Path, dev_root: Path, class_name: str,
                  pattern: re.Pattern[str] | None, lookup: dict) -> dict:
    status, paths = entries(sys_root / "class" / class_name)
    devices = []
    for path in paths:
        if pattern is not None and not pattern.fullmatch(path.name):
            continue
        node = dev_root / path.name
        row = {
            "node": str(node),
            "sysfs_status": node_status(path),
            "topology": str(path.resolve()),
            "dev_node_status": node_status(node),
            "aliases": item_aliases(lookup, node),
        }
        if class_name == "video4linux":
            row["name"] = read_field(path / "name")
        devices.append(row)
    return {"sysfs_directory_status": status,
            "alias_directory_status": lookup["status"], "devices": devices}


def pci_devices(sys_root: Path, class_prefix: str) -> dict:
    status, paths = entries(sys_root / "bus" / "pci" / "devices")
    devices = []
    for path in paths:
        pci_class = read_field(path / "class")
        if pci_class.get("status") != "present" or not pci_class["value"].lower().startswith(class_prefix):
            continue
        devices.append({"address": path.name, "class": pci_class,
                        "vendor_id": read_field(path / "vendor"),
                        "device_id": read_field(path / "device")})
    return {"sysfs_directory_status": status, "devices": devices}


def usb_devices(sys_root: Path) -> dict:
    status, paths = entries(sys_root / "bus" / "usb" / "devices")
    devices = []
    for path in paths:
        vendor = read_field(path / "idVendor")
        product = read_field(path / "idProduct")
        if vendor.get("status") != "present" or product.get("status") != "present":
            continue  # USB interface entries are not whole devices.
        devices.append({"sysfs_name": path.name, "topology": str(path.resolve()),
                        "vendor_id": vendor,
                        "product_id": product})
    return {"sysfs_directory_status": status, "devices": devices}


def disk_transport(path: Path) -> str:
    resolved = str(path.resolve())
    for token in ("/usb", "/nvme", "/virtio", "/ata"):
        if token in resolved:
            return token[1:]
    return "unknown"


def disks(sys_root: Path, dev_root: Path, include_private: bool) -> dict:
    status, paths = entries(sys_root / "block")
    lookup = aliases(dev_root, "disk", include_private)
    devices = []
    for path in paths:
        if not DISK_NAME.fullmatch(path.name):
            continue
        node = dev_root / path.name
        sectors = read_field(path / "size")
        try:
            capacity = int(sectors["value"]) * 512
        except (KeyError, ValueError):
            capacity = None
        devices.append({"node": str(node), "sysfs_status": node_status(path),
                        "dev_node_status": node_status(node),
                        "model": read_field(path / "device" / "model"),
                        "capacity_bytes": capacity,
                        "capacity_status": sectors["status"],
                        "removable": read_field(path / "removable"),
                        "transport_hint": disk_transport(path),
                        "topology": str(path.resolve()),
                        "aliases": item_aliases(lookup, node)})
    return {"sysfs_directory_status": status,
            "alias_directory_status": lookup["status"], "devices": devices}


def memory_bytes(proc_root: Path) -> dict:
    field = read_field(proc_root / "meminfo")
    if field["status"] != "present":
        return field
    match = re.search(r"^MemTotal:\s+([0-9]+)\s+kB$", field["value"], re.MULTILINE)
    if not match:
        return {"status": "unreadable", "reason": "MemTotal missing"}
    return {"status": "present", "value": int(match.group(1)) * 1024}


def inventory(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev"),
              proc_root: Path = Path("/proc"), *, role: str | None = None,
              include_private: bool = False) -> dict:
    dmi = sys_root / "class" / "dmi" / "id"
    return {
        "schema": "qdistro-bench-inventory-v1",
        "role": role,
        "device_io_tested": False,
        "private_aliases_included": include_private,
        "host": {
            "vendor": read_field(dmi / "sys_vendor"),
            "product": read_field(dmi / "product_name"),
            "bios": {
                "vendor": read_field(dmi / "bios_vendor"),
                "version": read_field(dmi / "bios_version"),
                "date": read_field(dmi / "bios_date"),
            },
            "kernel": os.uname().release,
            "memory_bytes": memory_bytes(proc_root),
        },
        "graphics_pci": pci_devices(sys_root, "0x03"),
        "network_pci": pci_devices(sys_root, "0x02"),
        "usb": usb_devices(sys_root),
        "video": class_devices(sys_root, dev_root, "video4linux", None,
                               aliases(dev_root, "v4l", include_private)),
        "serial": class_devices(sys_root, dev_root, "tty", SERIAL_TTY,
                                aliases(dev_root, "serial", include_private)),
        "disks": disks(sys_root, dev_root, include_private),
        "commands": {name: shutil.which(name) is not None for name in
                     ("python", "python3", "ffmpeg", "ffprobe", "v4l2-ctl")},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", help="user label, e.g. controller or Latitude")
    parser.add_argument("--include-private-ids", action="store_true",
                        help="include by-id aliases, which may contain device serial numbers")
    args = parser.parse_args()
    print(json.dumps(inventory(role=args.role,
                               include_private=args.include_private_ids),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
