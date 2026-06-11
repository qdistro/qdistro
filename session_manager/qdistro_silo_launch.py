"""qdistro-silo-launch — thin CLI over the session manager's D-Bus surface
(fableplan2 task 04).

    qdistro-silo-launch <silo>            # StartSilo (the real launch path)
    qdistro-silo-launch --stop <silo>     # StopSilo
    qdistro-silo-launch --status [--json] # delegate to qdistro-template-status

This wraps the EXISTING session-manager lifecycle (Start/Stop go through the
state machine and its audit) — it is NOT a second launch path that bypasses
them. Starting a tier2-template silo runs its launcher unit, which execs
spawn-tier2 as admin; the container is named ``qdistro-silo-<silo>`` so a
test harness can ``podman exec`` into a running templated silo. The
LAUNCH_TOKEN-on-stdout contract is preserved by spawn-tier2 itself (the
launcher unit's ExecStart), not re-implemented here.

The supervised-detach semantics (return while the container keeps running)
are provided by the launcher unit being a foreground systemd service: once
``StartSilo`` returns, the silo is Active and the container is up. For
dev/CI without a session manager, set ``TIER2_DETACH=1`` and invoke
spawn-tier2 directly.
"""
from __future__ import annotations

import argparse
import sys

BUS_NAME = "org.qdistro.SessionManager1"
OBJ_PATH = "/org/qdistro/SessionManager1"
IFACE = "org.qdistro.SessionManager1"


def _session_manager():
    import dbus  # imported lazily so --help works without dbus
    bus = dbus.SystemBus()
    obj = bus.get_object(BUS_NAME, OBJ_PATH)
    return dbus.Interface(obj, IFACE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-silo-launch")
    parser.add_argument("silo", nargs="?", default=None)
    parser.add_argument("--stop", action="store_true",
                        help="stop the silo instead of starting it")
    parser.add_argument("--grace", type=int, default=5,
                        help="stop grace period in seconds (default 5; "
                             "matches the daemon's DEFAULT_STOP_GRACE_S). NOTE: "
                             "honoured only for tier-3 user silos (the SIGTERM "
                             "kill window). A tier-2 templated silo is torn down "
                             "by its unit's fixed ExecStop (podman stop -t 10), "
                             "so --grace does not change its stop timeout.")
    parser.add_argument("--status", action="store_true",
                        help="print template/silo status and exit")
    parser.add_argument("--json", action="store_true",
                        help="with --status, emit JSON")
    args = parser.parse_args(argv)

    if args.status:
        import qdistro_template_status as st
        return st.main(["--json"] if args.json else [])

    if not args.silo:
        parser.error("a silo name is required (or use --status)")

    try:
        mgr = _session_manager()
        if args.stop:
            # StopSilo's D-Bus signature is "si" (name + grace_s); the grace
            # int is required or dbus-python raises "More items found in D-Bus
            # signature than in Python arguments" and the stop silently fails.
            mgr.StopSilo(args.silo, args.grace)
            print(f"stopped {args.silo}")
        else:
            mgr.StartSilo(args.silo)
            print(f"started {args.silo}")
    except Exception as exc:  # noqa: BLE001 — surface the D-Bus error plainly
        print(f"[silo-launch] FATAL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
