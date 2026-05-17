#!/usr/bin/env python3
"""qdistro-polkit-prompt — minimal admin-credential prompt subprocess.

Spawned by qdistro-polkit-agent on BeginAuthentication when the
selected method is ``pam`` (or ``fprint`` once the fprintd path
also drives the prompt for messaging). Reads the admin's password
on the GUI; prints it to stdout on submit; rc=1 on cancel.

Why a subprocess, not inline:
  - The polkit agent runs under a GLib mainloop (dbus-python).
    Spinning up a Qt event loop in the same process risks
    cross-loop deadlocks; a short-lived modal subprocess keeps
    each context single-loop.
  - The agent is a long-lived daemon. A leaked Qt window from
    a misbehaving prompt would survive across requests; spawning
    fresh per request is naturally bounded.
  - Tests can run the agent fully headless via
    ``QDISTRO_POLKIT_NONINTERACTIVE`` without ever invoking us.

Args (CLI):
  --mode=pam|fprint   (currently only ``pam`` reads from the user;
                       ``fprint`` is reserved for a future revision
                       that surfaces a "Touch the sensor…" panel
                       while the agent's fprintd thread runs).
  --action=<id>       polkit action id, rendered in the dialog.
  --message=<text>    polkit message, rendered in the dialog.

Stdin: unused (kept open in case the agent later wants to push
extra context).

Stdout: on submit, the entered password followed by a newline.
Stdout: on cancel or close, empty + rc=1.

Spec: spec/13 §"admin polkit AuthenticationAgent — interaction
contract". This file is the implementation of that contract.
"""
from __future__ import annotations

import argparse
import sys


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="qdistro-polkit-prompt",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="pam",
                   choices=("pam", "fprint"),
                   help="prompt mode (default: pam)")
    p.add_argument("--action", default="",
                   help="polkit action id to show in the dialog")
    p.add_argument("--message", default="Authentication required",
                   help="polkit message to show in the dialog")
    return p.parse_args()


def _qt_prompt(action: str, message: str) -> str | None:
    """Open a small modal Qt dialog and return the entered password,
    or None on cancel / Escape / window-close."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                                 QFormLayout, QLabel, QLineEdit, QVBoxLayout)
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    dlg = QDialog()
    dlg.setWindowTitle("Authentication Required")
    dlg.setModal(True)
    dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    root = QVBoxLayout(dlg)

    if message:
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet("font-weight: bold;")
        root.addWidget(msg_label)
    if action:
        sub = QLabel(f"Action: {action}")
        sub.setStyleSheet("color: #666;")
        sub.setWordWrap(True)
        root.addWidget(sub)

    form = QFormLayout()
    pw_field = QLineEdit()
    pw_field.setEchoMode(QLineEdit.EchoMode.Password)
    pw_field.setPlaceholderText("Admin password")
    form.addRow("Password:", pw_field)
    root.addLayout(form)

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    root.addWidget(buttons)

    pw_field.setFocus()
    rc = dlg.exec()
    if rc != QDialog.DialogCode.Accepted:
        return None
    return pw_field.text()


def main() -> int:
    args = _parse()
    if args.mode == "pam":
        pw = _qt_prompt(args.action, args.message)
        if pw is None:
            return 1
        sys.stdout.write(pw + "\n")
        sys.stdout.flush()
        return 0
    # mode=fprint: surface a "Touch the sensor…" panel and idle.
    # The agent waits for fprintd's VerifyStatus signal directly, so
    # this prompt is informational. We exit on user cancel.
    pw = _qt_prompt(args.action,
                    args.message + "\n\nTouch the fingerprint sensor…")
    return 0 if pw is not None else 1


if __name__ == "__main__":
    sys.exit(main())
