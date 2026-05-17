#!/usr/bin/env python3
"""Headless SPICE clipboard read-side probe (task 062 / spec/10).

Closes the host-paste half of the tier-4 SPICE clipboard cross-check
that s54-tier4-spice-clipboard-live.sh marked manual. Connects to a
running libvirt-managed SPICE channel as a *protocol-level* client
(no virt-viewer / no GUI / no host wayland surface), waits for the
in-guest spice-vdagent to grab the SPICE clipboard selection, fetches
the payload, and prints it on stdout. Exits non-zero on timeout —
appropriate for `<clipboard copypaste='no'/>` runs which should
SUPPRESS the grab.

Usage:
    qdistro-spice-clipboard-probe \\
        --vm <domain> \\
        --expect <payload>           # assert payload matches
    qdistro-spice-clipboard-probe \\
        --vm <domain> --expect-silent

Reads the SPICE host:port + password via `virsh domdisplay
--include-password`. Falls back to the unsecured `virsh domdisplay`
output and reads the password separately if the include-password
flag is unsupported on this libvirt build.

Why spice-glib instead of weston-headless + virt-viewer:
- virt-viewer needs keyboard focus on its host wayland surface to
  trigger the wayland-side selection-set that vdagent forwards.
  Synthesising that focus on a weston-headless backend requires its
  own input shim.
- spice-glib's SpiceMainChannel exposes the same clipboard primitives
  (grab / request / notify) virt-viewer wires to its host wl-clipboard
  bridge — but at the protocol level, with no host display required.
- The protocol-level path is what the qdistro threat model actually
  cares about: ``<clipboard copypaste='yes'/>`` must allow the grab to
  reach a SPICE consumer; ``copypaste='no'`` must deny it. That's a
  vdagent + qemu-spice-server contract; the host wayland half is
  observational.

Spec refs:
- spec/10 §"SPICE main-channel clipboard" — the per-domain copypaste
  toggle and its threat-model rationale.
- tier4-vm/README.md §"Manual interactive clipboard
  cross-check" — the recipe this probe replaces.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse


def _virsh_domdisplay(vm: str) -> tuple[str, int, str | None]:
    """Return (host, port, password). password=None when the build
    doesn't support --include-password — caller may need to passwd-
    less-attach (libvirtd checks listen='127.0.0.1' authorisation
    independently)."""
    try:
        out = subprocess.check_output(
            ["virsh", "domdisplay", "--include-password", vm],
            stderr=subprocess.STDOUT,
        ).decode().strip()
    except subprocess.CalledProcessError as e:
        # Older libvirt without --include-password emits an "unknown
        # option" error — retry without and let SpiceSession negotiate
        # passwordless. The bats caller arranges passwords appropriately.
        if b"unknown option" in (e.output or b""):
            out = subprocess.check_output(
                ["virsh", "domdisplay", vm]
            ).decode().strip()
        else:
            raise
    # Output shape: spice://127.0.0.1:5900?password=abcdef0123456789
    # (or without ?password= when --include-password unavailable).
    parsed = urllib.parse.urlparse(out)
    if parsed.scheme != "spice":
        raise RuntimeError(f"unexpected domdisplay output: {out!r}")
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 0)
    qs = urllib.parse.parse_qs(parsed.query)
    password = (qs.get("password") or [None])[0]
    return host, port, password


def _run_probe(host: str, port: int, password: str | None,
               timeout_s: float, expect: str | None,
               expect_silent: bool) -> int:
    # Imports deferred so a host without spice-glib gives a clear SKIP
    # message rather than crashing on top-level import.
    try:
        import gi
        gi.require_version("SpiceClientGLib", "2.0")
        from gi.repository import SpiceClientGLib as Spice
        from gi.repository import GLib
    except (ValueError, ImportError) as e:
        print(f"SKIP: spice-glib bindings unavailable: {e}",
              file=sys.stderr)
        return 77

    # spice-protocol vd_agent enum values — the Python GI bindings
    # don't expose them as named symbols, so we hard-code the integers
    # straight from /usr/include/spice-1/spice/vd_agent.h. Stable
    # since 2010; the audit risk of magic numbers here is lower than
    # the maintenance risk of trying to keep a parallel binding alive.
    VD_AGENT_CLIPBOARD_SELECTION_CLIPBOARD = 0
    VD_AGENT_CLIPBOARD_UTF8_TEXT = 1

    sess = Spice.Session.new()
    sess.set_property("host", host)
    sess.set_property("port", str(port))
    if password:
        sess.set_property("password", password)
    # Enable only what we need; less code path = fewer surprises.
    sess.set_property("enable-audio", False)
    sess.set_property("enable-usbredir", False)
    sess.set_property("enable-smartcard", False)

    state = {
        "main_channel": None,
        "received": None,
        "saw_grab": False,
        "loop": GLib.MainLoop(),
    }

    def _on_clipboard_grab(channel, selection, types, ntypes,
                            _user_data=None):
        # Guest grabbed clipboard. We don't bother walking the types
        # array — vdagent under wayland always advertises UTF8_TEXT
        # for wl-copy text payloads, and the worst case (grab without
        # UTF8) is a request that returns an EMPTY notify, which our
        # selection-data handler still resolves into a payload-mismatch
        # FAIL rather than an indefinite hang.
        state["saw_grab"] = True
        if expect_silent:
            state["loop"].quit()
            return False
        if selection != VD_AGENT_CLIPBOARD_SELECTION_CLIPBOARD:
            return False
        channel.clipboard_selection_request(
            selection, VD_AGENT_CLIPBOARD_UTF8_TEXT)
        return True

    def _on_clipboard_data(channel, selection, ctype, data,
                            length, _user_data=None):
        if selection != VD_AGENT_CLIPBOARD_SELECTION_CLIPBOARD:
            return
        try:
            payload = bytes(data)[:int(length)].decode("utf-8")
        except UnicodeDecodeError:
            payload = repr(bytes(data)[:int(length)])
        state["received"] = payload
        state["loop"].quit()

    def _on_channel_new(_session, channel, _user_data=None):
        # Spice.MainChannel carries the clipboard signals. Other
        # channels (display / cursor / inputs / record / playback)
        # just connect-and-ignore.
        if isinstance(channel, Spice.MainChannel):
            state["main_channel"] = channel
            channel.connect("main-clipboard-selection-grab",
                            _on_clipboard_grab)
            channel.connect("main-clipboard-selection",
                            _on_clipboard_data)
        channel.connect_channel()

    sess.connect("channel-new", _on_channel_new)
    if not sess.connect_channel():
        print("ERROR: SpiceSession.connect() failed", file=sys.stderr)
        return 1

    def _timeout():
        state["loop"].quit()
        return False

    GLib.timeout_add(int(timeout_s * 1000), _timeout)
    state["loop"].run()
    sess.disconnect()

    if expect_silent:
        if state["saw_grab"]:
            print("FAIL: expected silent SPICE clipboard but saw "
                  "a grab from the guest", file=sys.stderr)
            return 2
        print("OK: SPICE clipboard silent (no grab observed within "
              f"{timeout_s:.1f}s)")
        return 0

    if state["received"] is None:
        print(f"FAIL: timeout — no SPICE clipboard data within "
              f"{timeout_s:.1f}s", file=sys.stderr)
        return 3
    if expect is not None and state["received"] != expect:
        print(f"FAIL: payload mismatch\n  expected: {expect!r}\n"
              f"  got:      {state['received']!r}", file=sys.stderr)
        return 4
    print(state["received"])
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="qdistro-spice-clipboard-probe",
                                 description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--vm", required=True,
                    help="libvirt domain name")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="seconds to wait for grab+data")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--expect", help="assert clipboard payload equals")
    g.add_argument("--expect-silent", action="store_true",
                   help="assert NO grab is observed within timeout")
    args = ap.parse_args(argv)

    if not re.match(r"^[A-Za-z0-9_.-]{1,63}$", args.vm):
        print(f"FAIL: refusing suspicious VM name {args.vm!r}",
              file=sys.stderr)
        return 1

    try:
        host, port, password = _virsh_domdisplay(args.vm)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        print(f"FAIL: virsh domdisplay {args.vm}: {e}", file=sys.stderr)
        return 1
    if port == 0:
        print(f"FAIL: virsh reports no SPICE port for {args.vm}",
              file=sys.stderr)
        return 1

    return _run_probe(host, port, password,
                      timeout_s=args.timeout,
                      expect=args.expect,
                      expect_silent=args.expect_silent)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
