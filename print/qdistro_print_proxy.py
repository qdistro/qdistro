#!/usr/bin/env python3
"""qdistro-print-proxy — host-side IPP proxy for spec/20 CUPS-in-VM.

Phase-9 §step 1 (gate + spawn-on-demand). Listens on a localhost
AF_UNIX endpoint at /run/qdistro-print/ipp.sock; forwards every
accepted connection to a backend (initially AF_VSOCK to a per-host
CUPS VM, or AF_UNIX to a host-local cupsd for development).

The proxy intentionally has narrow responsibilities:

- Per-connection forward (one socketpair per incoming stream).
- Peer-cred capture (uid/pid + best-effort /proc/<pid>/exe) for
  audit + the broker gate.
- Broker gate via org.qdistro.AdminBroker1.CheckPermission — REQUIRED
  by default (fail closed). Set QDISTRO_PRINT_GATE_REQUIRED=0 only for
  development to run ungated. An absent broker rule ("unknown") is
  treated as DENY unless QDISTRO_PRINT_GATE_ALLOW_UNKNOWN=1 is set for
  rule bring-up.
- Spawn-on-demand for the vsock backend: if the backend connect
  fails with ECONNREFUSED / EHOSTUNREACH and QDISTRO_PRINT_VM_SPAWN
  is set, the proxy invokes the configured spawn helper (default
  /usr/local/bin/spawn-print-vm.sh) and retries the connect once
  with a short backoff.

Apps and Qt/Gtk print dialogs see this socket as a normal CUPS daemon
once `CUPS_SERVER=/run/qdistro-print/ipp.sock` is exported to their
environment (or the global cups client.conf points at it).

Backend selection (env vars):

    QDISTRO_PRINT_BACKEND        = vsock | unix | tcp                 (default: vsock)
    QDISTRO_PRINT_VSOCK_CID      = remote VM CID                      (default: 3)
    QDISTRO_PRINT_VSOCK_PORT     = remote IPP port                    (default: 631)
    QDISTRO_PRINT_UNIX_PATH      = backend AF_UNIX path               (default: /run/cups/cups.sock)
    QDISTRO_PRINT_TCP_HOST       = backend TCP host                   (default: 127.0.0.1)
    QDISTRO_PRINT_TCP_PORT       = backend TCP port                   (default: 631)

Gate / spawn (env vars):

    QDISTRO_PRINT_GATE_REQUIRED  = 0 | 1                              (default: 1)
    QDISTRO_PRINT_GATE_ALLOW_UNKNOWN = 0 | 1  (dev: allow "unknown")  (default: 0)
    QDISTRO_PRINT_GATE_ACTION    = broker action name                 (default: print.access)
    QDISTRO_PRINT_VM_SPAWN       = path to spawn helper, or empty     (default: empty / disabled)
    QDISTRO_PRINT_SPAWN_BACKOFF_S = float seconds to wait after spawn (default: 1.5)

Frontend listener path is fixed at /run/qdistro-print/ipp.sock with
mode 0660 owned by root:lp (so the lp group reaches it like a normal
cupsd socket).

Phase-9 §step 2 (this task) adds:
    - Per-connection audit row via PrintAuditLog (sqlite).
    - The actual qdistro-print libvirt domain template + image
      builder (print-vm/).
    - USB hot-plug helpers (qdistro-print-{attach,detach}-usb)
      gated through polkit (org.qdistro.print.{attach,detach}-usb).
    - polkit policy library (org.qdistro.print.policy).

Phase-9 deferred (per spec/20):
    - Job-size + page-count caps.
    - Admin panel "Printing" page (history view + USB attach UI).
    - Browser-style job-cancel surface (admin runs
      `qdistro-print-job-control cancel <jobid>` via guest-exec for now).
"""
from __future__ import annotations

import errno
import os
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

LISTEN_PATH = os.environ.get(
    "QDISTRO_PRINT_LISTEN", "/run/qdistro-print/ipp.sock")

BACKEND = os.environ.get("QDISTRO_PRINT_BACKEND", "vsock").lower()
VSOCK_CID = int(os.environ.get("QDISTRO_PRINT_VSOCK_CID", "3"))
VSOCK_PORT = int(os.environ.get("QDISTRO_PRINT_VSOCK_PORT", "631"))
UNIX_PATH = os.environ.get("QDISTRO_PRINT_UNIX_PATH", "/run/cups/cups.sock")
TCP_HOST = os.environ.get("QDISTRO_PRINT_TCP_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("QDISTRO_PRINT_TCP_PORT", "631"))

CONNECT_TIMEOUT_S = 5.0
BUFSIZE = 64 * 1024

# Per-connection audit (Phase-9 §step 2). Lazy-initialised on first
# event to keep the import path light; tests can pin the DB path via
# QDISTRO_PRINT_AUDIT_DB.
_AUDIT_LOG = None  # type: ignore[var-annotated]


def _audit() -> "PrintAuditLog | None":  # noqa: F821 — forward ref
    global _AUDIT_LOG
    if _AUDIT_LOG is None:
        try:
            from qdistro_print_audit import (  # type: ignore[import-not-found]
                PrintAuditLog as _PA,
            )
            _AUDIT_LOG = _PA()
        except Exception as e:  # noqa: BLE001
            print(f"[qdistro-print-proxy] audit init failed: {e!r}",
                  file=sys.stderr, flush=True)
            _AUDIT_LOG = False  # type: ignore[assignment]
    return _AUDIT_LOG if _AUDIT_LOG else None

# Fail-closed by default. The broker gate is REQUIRED unless an operator
# explicitly disables it for development by setting
# QDISTRO_PRINT_GATE_REQUIRED=0. Historically this defaulted to "0"
# (allow-on-disabled), which meant any member of the socket's owning
# group could push arbitrary IPP traffic at the print backend with no
# broker decision whenever the systemd unit forgot to set the env. The
# default is now "1" so production is gated even if the unit is missing
# the variable; dev/bring-up opts out with an explicit "0".
GATE_REQUIRED = os.environ.get("QDISTRO_PRINT_GATE_REQUIRED", "1") != "0"
GATE_ACTION = os.environ.get("QDISTRO_PRINT_GATE_ACTION", "print.access")
# When the broker has no rule for this caller it returns "unknown". By
# default we treat that as DENY (fail closed) — an absent rule must not
# silently grant printing. A developer can set
# QDISTRO_PRINT_GATE_ALLOW_UNKNOWN=1 to keep the proxy available during
# rule authoring; that opt-out is logged on every unknown verdict.
GATE_ALLOW_UNKNOWN = os.environ.get(
    "QDISTRO_PRINT_GATE_ALLOW_UNKNOWN", "0") != "0"
VM_SPAWN = os.environ.get("QDISTRO_PRINT_VM_SPAWN", "")
SPAWN_BACKOFF_S = float(os.environ.get("QDISTRO_PRINT_SPAWN_BACKOFF_S", "1.5"))

BROKER_BUS = "org.qdistro.AdminBroker1"
BROKER_OBJ = "/org/qdistro/AdminBroker1"


def _read_proc_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _broker_check_permission(uid: int, pid: int, exe: str) -> str:
    """Call broker.CheckPermission with the peer details. Returns
    "allow" / "deny" / "unknown" / "error". Errors map to "error" so
    the caller can decide whether to fail open or closed."""
    try:
        import dbus  # imported lazily so the proxy starts on hosts
                     # without dbus-python (the gate is opt-in).
    except ImportError:
        return "error"
    try:
        bus = dbus.SystemBus()
        obj = bus.get_object(BROKER_BUS, BROKER_OBJ)
        ifc = dbus.Interface(obj, BROKER_BUS)
        details = {
            "peer_uid": dbus.String(str(uid)),
            "peer_pid": dbus.String(str(pid)),
            "peer_exe": dbus.String(exe or ""),
        }
        return str(ifc.CheckPermission(GATE_ACTION, details))
    except Exception as e:  # noqa: BLE001 — broker can be down
        sys.stderr.write(
            f"[qdistro-print-proxy] broker CheckPermission failed: {e}\n")
        sys.stderr.flush()
        return "error"


def _spawn_print_vm() -> bool:
    """Run the spawn helper. Returns True on rc=0, False otherwise.
    The helper is expected to start the qdistro-print VM and return
    once the vsock backend is ready (or quickly if it can't)."""
    if not VM_SPAWN:
        return False
    try:
        argv = shlex.split(VM_SPAWN)
        rc = subprocess.run(argv, timeout=30).returncode
        return rc == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"[qdistro-print-proxy] spawn failed: {e}\n")
        sys.stderr.flush()
        return False


def _open_backend() -> socket.socket:
    """Open a fresh connection to the configured backend. Caller closes."""
    if BACKEND == "vsock":
        # AF_VSOCK is socket.AF_VSOCK on Python ≥3.7; falls back to
        # numeric 40 on older / odd builds.
        af_vsock = getattr(socket, "AF_VSOCK", 40)
        s = socket.socket(af_vsock, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT_S)
        s.connect((VSOCK_CID, VSOCK_PORT))
        s.settimeout(None)
        return s
    if BACKEND == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT_S)
        s.connect(UNIX_PATH)
        s.settimeout(None)
        return s
    if BACKEND == "tcp":
        s = socket.create_connection((TCP_HOST, TCP_PORT),
                                     timeout=CONNECT_TIMEOUT_S)
        s.settimeout(None)
        return s
    raise ValueError(f"unknown QDISTRO_PRINT_BACKEND: {BACKEND!r}")


def _peer_cred(client: socket.socket) -> tuple[int, int, int]:
    """SO_PEERCRED on an accepted AF_UNIX socket → (pid, uid, gid)."""
    try:
        cred = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return struct.unpack("3i", cred)
    except OSError:
        return (-1, -1, -1)


def _pump(a: socket.socket, b: socket.socket) -> None:
    """Bidirectional byte copy between a and b until EOF / error."""
    a.setblocking(False)
    b.setblocking(False)
    open_ = {a.fileno(): a, b.fileno(): b}
    peer_of = {a.fileno(): b, b.fileno(): a}
    try:
        while open_:
            ready, _, _ = select.select(list(open_.keys()), [], [], 30.0)
            if not ready:
                continue
            for fd in ready:
                src = open_.get(fd)
                dst = peer_of.get(fd)
                if src is None or dst is None:
                    continue
                try:
                    buf = src.recv(BUFSIZE)
                except OSError as e:
                    if e.errno == errno.EAGAIN:
                        continue
                    buf = b""
                if not buf:
                    open_.pop(fd, None)
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                view = memoryview(buf)
                while view:
                    try:
                        sent = dst.send(view)
                    except BlockingIOError:
                        # drop the rest of this slice so we don't busy-spin;
                        # next select cycle picks it up.
                        sent = 0
                        break
                    except OSError:
                        sent = 0
                        open_.pop(dst.fileno(), None)
                        break
                    view = view[sent:]
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def _gate(uid: int, pid: int) -> tuple[bool, str]:
    """Decide whether this connection should proceed. Returns
    (allowed, reason). When the gate is explicitly disabled for dev,
    allowed=True with reason "gate-disabled". When on (the production
    default), queries the broker and:
      - "allow"   → allow
      - "deny"    → deny
      - "unknown" → DENY by default (rule absent → fail closed). Only
        allowed when QDISTRO_PRINT_GATE_ALLOW_UNKNOWN=1 is set for
        bring-up, in which case the dev opt-out is logged.
      - "error"   → deny with reason "gate-error" (fail closed when
        the broker is unreachable AND gate is required).
    """
    if not GATE_REQUIRED:
        # Explicit dev opt-out — log so it is visible in the journal that
        # the proxy is running ungated.
        sys.stderr.write(
            f"[qdistro-print-proxy] WARNING gate disabled "
            f"(QDISTRO_PRINT_GATE_REQUIRED=0) — allowing pid={pid} "
            f"uid={uid} without a broker decision\n")
        sys.stderr.flush()
        return True, "gate-disabled"
    exe = _read_proc_exe(pid) if pid > 0 else ""
    verdict = _broker_check_permission(uid, pid, exe)
    if verdict == "allow":
        return True, "gate-allow"
    if verdict == "deny":
        return False, "gate-deny"
    if verdict == "unknown":
        if GATE_ALLOW_UNKNOWN:
            sys.stderr.write(
                f"[qdistro-print-proxy] WARNING gate verdict=unknown "
                f"allowed by dev opt-out "
                f"(QDISTRO_PRINT_GATE_ALLOW_UNKNOWN=1) pid={pid} "
                f"uid={uid}\n")
            sys.stderr.flush()
            return True, "gate-unknown-dev"
        return False, "gate-unknown"
    return False, "gate-error"


def _open_backend_with_spawn() -> socket.socket:
    """_open_backend with a spawn-and-retry on the vsock path. If the
    backend can't be reached and a spawn helper is configured, run it
    once, wait SPAWN_BACKOFF_S, and retry."""
    try:
        return _open_backend()
    except OSError as e:
        if BACKEND != "vsock" or not VM_SPAWN:
            raise
        if e.errno not in (errno.ECONNREFUSED, errno.EHOSTUNREACH,
                           errno.ENETUNREACH, errno.ETIMEDOUT,
                           errno.EADDRNOTAVAIL):
            raise
        print(f"[qdistro-print-proxy] vsock backend unreachable ({e}); "
              f"invoking spawn helper {VM_SPAWN!r}", flush=True)
        if not _spawn_print_vm():
            raise
        time.sleep(SPAWN_BACKOFF_S)
        return _open_backend()


def _audit_record(op: str, *, decision: str, reason: str,
                  uid: int, pid: int) -> None:
    a = _audit()
    if a is None:
        return
    try:
        a.record(op, decision=decision, reason=reason,
                 caller_uid=uid, caller_pid=pid,
                 caller_exe=_read_proc_exe(pid) if pid > 0 else "",
                 backend=BACKEND)
    except Exception as e:  # noqa: BLE001
        print(f"[qdistro-print-proxy] audit record failed: {e!r}",
              file=sys.stderr, flush=True)


def _serve_one(client: socket.socket, addr) -> None:
    pid, uid, gid = _peer_cred(client)
    print(f"[qdistro-print-proxy] accept pid={pid} uid={uid}", flush=True)
    allowed, reason = _gate(uid, pid)
    if not allowed:
        print(f"[qdistro-print-proxy] gate refused pid={pid} uid={uid} "
              f"reason={reason}", file=sys.stderr, flush=True)
        _audit_record("connect", decision="deny", reason=reason,
                      uid=uid, pid=pid)
        try:
            client.close()
        except OSError:
            pass
        return
    if reason != "gate-disabled":
        print(f"[qdistro-print-proxy] gate {reason} pid={pid} uid={uid}",
              flush=True)
    try:
        backend = _open_backend_with_spawn()
    except (OSError, ValueError) as e:
        print(f"[qdistro-print-proxy] backend connect failed: {e}",
              file=sys.stderr, flush=True)
        _audit_record("connect", decision="error",
                      reason=f"backend:{e}", uid=uid, pid=pid)
        try:
            client.close()
        except OSError:
            pass
        return
    _audit_record("connect", decision="allow", reason=reason,
                  uid=uid, pid=pid)
    _pump(client, backend)
    _audit_record("close", decision="allow", reason="connection-end",
                  uid=uid, pid=pid)
    print(f"[qdistro-print-proxy] close  pid={pid} uid={uid}", flush=True)


def main() -> int:
    listen_dir = os.path.dirname(LISTEN_PATH)
    os.makedirs(listen_dir, mode=0o755, exist_ok=True)
    if os.path.exists(LISTEN_PATH):
        os.unlink(LISTEN_PATH)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(LISTEN_PATH)
    os.chmod(LISTEN_PATH, 0o660)
    sock.listen(32)
    print(f"[qdistro-print-proxy] listening on {LISTEN_PATH} → "
          f"backend={BACKEND}", flush=True)

    def _term(signum, frame):
        print(f"[qdistro-print-proxy] caught signal {signum}, shutting down",
              flush=True)
        try:
            sock.close()
        except OSError:
            pass
        try:
            os.unlink(LISTEN_PATH)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    while True:
        try:
            client, addr = sock.accept()
        except OSError as e:
            if e.errno in (errno.EINTR, errno.EAGAIN):
                continue
            raise
        t = threading.Thread(target=_serve_one, args=(client, addr),
                             daemon=True)
        t.start()


if __name__ == "__main__":
    sys.exit(main())
