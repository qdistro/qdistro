"""Mount-manager plugin for QFileMan.

Adds *Mount Manager…* — a dialog listing block devices (USB sticks,
SD cards, internal partitions) with Mount / Unmount / Eject actions.
Mirrors Krusader's Mount Manager and Dolphin's *Devices* panel.

Backend: ``udisksctl`` (from the ``udisks2`` package). That binary
ships with every desktop Linux install we care about, exposes
mount/unmount over D-Bus without sudo, and outputs structured device
info via ``udisksctl dump``.

If ``udisksctl`` isn't on PATH we fall back to ``lsblk -J`` for the
listing and call ``udisksctl`` for the actions; without either,
the plugin hides itself.

:func:`parse_lsblk_json` is a pure function so the parsing can be
exercised under tests without spawning lsblk.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


def parse_lsblk_json(payload: str) -> list[dict]:
    """Flatten ``lsblk -J -o NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL`` into a list.

    Walks the nested ``children`` tree and returns one entry per leaf
    device. Each entry has ``name``, ``size``, ``fstype``, ``mountpoint``,
    ``label`` keys (any of which may be ``None``).
    """
    try:
        doc = json.loads(payload)
    except json.JSONDecodeError:
        return []
    out: list[dict] = []

    def visit(node: dict) -> None:
        children = node.get("children") or []
        if not children:
            out.append({
                "name": node.get("name"),
                "size": node.get("size"),
                "fstype": node.get("fstype"),
                "mountpoint": node.get("mountpoint")
                              or (node.get("mountpoints") or [None])[0],
                "label": node.get("label"),
            })
            return
        for child in children:
            visit(child)

    for top in doc.get("blockdevices", []):
        visit(top)
    return out


def list_block_devices() -> list[dict]:
    """Return the current block-device listing via ``lsblk``.

    Filters out devices without a filesystem (those can't be mounted)
    so the dialog only shows actionable rows. Falls back to an empty
    list if ``lsblk`` isn't installed or returns non-zero.
    """
    if not shutil.which("lsblk"):
        return []
    try:
        out = subprocess.check_output(
            ["lsblk", "-J", "-o", "NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("lsblk failed: %s", e)
        return []
    return [d for d in parse_lsblk_json(out) if d.get("fstype")]


def _udisksctl_argv(action: str, device: str) -> list[str]:
    """Build a udisksctl argv. ``action`` ∈ {mount, unmount, power-off}."""
    return ["udisksctl", action, "-b", device]


class MountManagerPlugin(MenuProvider):
    name = "mount_manager"
    description = "Mount / unmount / eject block devices"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path:
            return []
        if not shutil.which("udisksctl"):
            return []
        return [("Mount Manager…", self._show)]

    def _show(self, _path: str) -> None:
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QHBoxLayout,
            QLabel,
            QMessageBox,
            QPushButton,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
        )

        devices = list_block_devices()

        dlg = QDialog()
        dlg.setWindowTitle("Mount Manager")
        dlg.resize(700, 400)
        layout = QVBoxLayout(dlg)
        if not devices:
            layout.addWidget(QLabel("No mountable devices found.", dlg))
        else:
            tree = QTreeWidget(dlg)
            tree.setHeaderLabels(["Device", "Size", "FS", "Mountpoint", "Label"])
            tree.setRootIsDecorated(False)
            for d in devices:
                item = QTreeWidgetItem([
                    f"/dev/{d.get('name') or ''}",
                    d.get("size") or "",
                    d.get("fstype") or "",
                    d.get("mountpoint") or "",
                    d.get("label") or "",
                ])
                tree.addTopLevelItem(item)
            tree.resizeColumnToContents(0)
            layout.addWidget(tree, 1)

            buttons_row = QHBoxLayout()
            mount_btn = QPushButton("Mount", dlg)
            unmount_btn = QPushButton("Unmount", dlg)
            eject_btn = QPushButton("Eject (power off)", dlg)
            buttons_row.addWidget(mount_btn)
            buttons_row.addWidget(unmount_btn)
            buttons_row.addWidget(eject_btn)
            buttons_row.addStretch(1)
            layout.addLayout(buttons_row)

            def selected_device() -> str | None:
                item = tree.currentItem()
                if item is None:
                    return None
                return item.text(0)

            def run_action(action: str) -> None:
                dev = selected_device()
                if not dev:
                    QMessageBox.information(dlg, "Mount Manager",
                                             "Pick a device first.")
                    return
                argv = _udisksctl_argv(action, dev)
                try:
                    result = subprocess.run(
                        argv, capture_output=True, text=True, timeout=30,
                    )
                except (OSError, subprocess.TimeoutExpired) as e:
                    QMessageBox.warning(dlg, "Mount Manager", f"Failed: {e}")
                    return
                msg = (result.stdout + result.stderr).strip() or "(no output)"
                if result.returncode == 0:
                    QMessageBox.information(dlg, "Mount Manager", msg)
                else:
                    QMessageBox.warning(dlg, "Mount Manager",
                                         f"{action} failed:\n{msg}")

            mount_btn.clicked.connect(lambda: run_action("mount"))
            unmount_btn.clicked.connect(lambda: run_action("unmount"))
            eject_btn.clicked.connect(lambda: run_action("power-off"))

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        close.rejected.connect(dlg.reject)
        close.accepted.connect(dlg.accept)
        layout.addWidget(close)
        dlg.exec()
