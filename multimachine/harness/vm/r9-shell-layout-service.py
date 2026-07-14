#!/usr/bin/env python3
"""One-shot live gate for controller → authenticated qdshell → qdwin apply.

This is orchestration apparatus, not a second output client.  The real mailbox
and D-Bus facade publish one already-verified slot delta, authenticate qdshell's
busctl child against the supplied live shell pid, and wait for qdshell's tagged
compositor acknowledgement.
"""
from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from multimachine.display_shell_mailbox import DisplayShellMailbox
from multimachine.display_shell_service import register_authenticated_service
from multimachine.remote_display_slot import ActionKind, SlotAction


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell-pid", type=int, required=True)
    ap.add_argument("--grant", type=Path, required=True)
    ap.add_argument("--action", choices=("enable", "disable"), required=True)
    ap.add_argument("--status", type=Path, required=True)
    args = ap.parse_args()

    grant = json.loads(args.grant.read_text(encoding="utf-8"))
    DBusGMainLoop(set_as_default=True)
    mailbox = DisplayShellMailbox(request_timeout=15)
    registration = register_authenticated_service(
        mailbox, shell_pid=args.shell_pid)
    assert registration
    loop = GLib.MainLoop()
    result: dict = {}

    def execute() -> None:
        kind = (ActionKind.PRIMARY_ENABLE_OUTPUT
                if args.action == "enable"
                else ActionKind.PRIMARY_DISABLE_OUTPUT)
        try:
            mailbox.perform(SlotAction(kind), grant)
            result.update(ok=True, action=args.action,
                          generation=grant["generation"])
        except Exception as exc:
            result.update(ok=False, action=args.action,
                          generation=grant.get("generation"),
                          error=f"{type(exc).__name__}:{exc}")
        GLib.idle_add(loop.quit)

    worker = threading.Thread(target=execute, name="r9-shell-layout", daemon=True)
    worker.start()
    loop.run()
    worker.join(timeout=2)
    args.status.write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
