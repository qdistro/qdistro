#!/usr/bin/env python3
"""Listen for a single qdistro AdminBroker1 D-Bus signal and report its payload.

A real in-process subscriber (dbus-python + DBusGMainLoop) — the same path the
product's own subscribers use — instead of `dbus-monitor`. dbus-monitor uses
the bus's BecomeMonitor eavesdrop privilege; a plain subscriber exercises the
ordinary `<allow receive_sender=...>` policy that production qdshell relies on,
so a pass here proves the wire-level contract the way scenario 22 intends.

Usage:
    listen-broker-signal.py MEMBER [--ready FILE] [--out FILE] [--timeout SEC]

MEMBER is the signal name, e.g. ApprovalRevoked or RequestDecided.

Writes one JSON object per captured signal to --out (default stdout):
    {"member": "ApprovalRevoked", "args": [2000, "test.action", "/usr/bin/python3.13"]}
Touches --ready AFTER the match rule is installed, so the caller can wait for
readiness before triggering the emitter (closing the subscribe/emit race).
Exits 0 once at least one signal is seen, 2 on timeout with none seen.
"""
import argparse
import json
import os
import sys

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

IFACE = "org.qdistro.AdminBroker1"
PATH = "/org/qdistro/AdminBroker1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("member")
    ap.add_argument("--ready", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    out = open(args.out, "w") if args.out else sys.stdout
    seen = {"n": 0}

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = GLib.MainLoop()

    def on_signal(*a):
        # dbus types -> plain JSON; ints/strings survive json.dumps via str/int.
        payload = []
        for v in a:
            try:
                payload.append(int(v))
            except (TypeError, ValueError):
                payload.append(str(v))
        out.write(json.dumps({"member": args.member, "args": payload}) + "\n")
        out.flush()
        seen["n"] += 1
        loop.quit()

    bus.add_signal_receiver(
        on_signal,
        signal_name=args.member,
        dbus_interface=IFACE,
        path=PATH,
    )

    # Match rule is now installed on the bus — announce readiness.
    if args.ready:
        with open(args.ready, "w") as r:
            r.write("ready\n")

    def on_timeout():
        loop.quit()
        return False

    GLib.timeout_add(int(args.timeout * 1000), on_timeout)
    loop.run()

    if args.out:
        out.close()
    if seen["n"] == 0:
        sys.stderr.write(f"no {args.member} signal within {args.timeout}s\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
