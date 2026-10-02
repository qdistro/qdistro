#!/usr/bin/python3 -I
"""qdistro-approvals — admin CLI for pending requests, the approval cache and the audit log.

Subcommands:
    pending [--json]           List requests waiting for an admin decision
    approve <id> [--scope S]   Allow a pending request (default scope: once)
    deny <id>                  Deny a pending request
    list                       List currently-cached approvals
    revoke <id>                Delete one cached approval by id
    revoke --all-for-uid <N>   Delete all cached approvals for a uid
    audit [--uid N] [--action X] [--limit N]
                               Show recent audit rows (newest first)
    gc                         Run cache GC immediately (delete expired rows)

Root-only. The broker trusts this tool as a root admin-control peer only
when it runs from its installed path, /usr/local/sbin/qdistro-approvals
(the path must appear in the Python process's argv; see
_peer_matches_admin_control in broker/qdistro_admin_broker.py). Run it as
`qdistro-approvals ...` (PATH lookup; under sudo use the absolute path,
secure_path lacks /usr/local/sbin) or by that absolute path, not as
`python3 cli/qdistro_approvals.py` from a source tree. The argv match
identifies the genuine tool for honest callers; it is not a security
boundary against root (root is fully trusted by the broker).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import time
from typing import Any

CACHE_DB = "/var/lib/qdistro/approvals/approvals.sqlite"
AUDIT_DB = "/var/lib/qdistro/audit/audit.sqlite"


def _require_root() -> None:
    if os.geteuid() != 0:
        print("qdistro-approvals: must be run as root", file=sys.stderr)
        sys.exit(1)


def _open(db_path: str) -> sqlite3.Connection:
    # Symlink-safe path check: lstat + S_ISREG rejects an attacker who
    # got write to the parent directory and swapped the db for a symlink
    # to /etc/shadow. (The parent dir is 0700 root in production, so the
    # attack requires that hardening to fail first; this is defense in
    # depth.) The follow-up "attacker created a regular file as their
    # own uid" requires the same parent-dir compromise; we don't ratchet
    # to a root-owned check here because it'd break headless tests, and
    # the systemd unit + bootstrap already enforce root ownership at
    # creation time.
    try:
        st = os.lstat(db_path)
    except FileNotFoundError:
        print(f"qdistro-approvals: db not found: {db_path}", file=sys.stderr)
        sys.exit(2)
    if not stat.S_ISREG(st.st_mode):
        print(f"qdistro-approvals: not a regular file: {db_path}", file=sys.stderr)
        sys.exit(3)
    # autocommit so DELETE/INSERT take effect immediately, matching the
    # broker's connection mode and avoiding silently-rolled-back changes
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fmt_time(ts: int | None) -> str:
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_expiry(expires_at: int | None) -> str:
    if expires_at is None:
        return "never"
    delta = expires_at - int(time.time())
    if delta <= 0:
        return f"{_fmt_time(expires_at)} (expired)"
    if delta < 3600:
        rel = f"in {delta // 60}m"
    elif delta < 86_400:
        rel = f"in {delta // 3600}h{(delta % 3600) // 60}m"
    else:
        rel = f"in {delta // 86_400}d"
    return f"{_fmt_time(expires_at)} ({rel})"


def cmd_list(_args) -> int:
    conn = _open(CACHE_DB)
    rows = conn.execute(
        """
        SELECT id, caller_uid, action, match_kind, match_value,
               decision, expires_at, created_at, approver_uid
        FROM approvals
        ORDER BY id ASC
        """
    ).fetchall()
    if not rows:
        print("(no cached approvals)")
        return 0
    print(f"{'id':>4}  {'uid':>5}  {'decision':<8}  {'match':<11}  "
          f"{'value':<40}  {'expires':<32}  approver")
    print("-" * 120)
    for r in rows:
        rid, uid, action, kind, value, decision, expires, created, approver = r
        dec_s = "allow" if decision else "deny"
        match_s = f"{kind}"
        val_s = (value[:38] + "..") if len(value) > 40 else value
        print(f"{rid:>4}  {uid:>5}  {dec_s:<8}  {match_s:<11}  "
              f"{val_s:<40}  {_fmt_expiry(expires):<32}  uid={approver}")
        print(f"{'':>4}  action={action}")
    return 0


BUS_NAME = "org.qdistro.AdminBroker1"
OBJ_PATH = "/org/qdistro/AdminBroker1"


def _broker():
    """Return a D-Bus interface for the broker, or exit with a clear error."""
    try:
        import dbus  # type: ignore[import-not-found]
    except ImportError:
        print("qdistro-approvals: dbus-python not installed", file=sys.stderr)
        sys.exit(4)
    try:
        bus = dbus.SystemBus()
        obj = bus.get_object(BUS_NAME, OBJ_PATH)
        return dbus.Interface(obj, BUS_NAME), dbus
    except Exception as e:
        print(f"qdistro-approvals: broker unreachable: {e}", file=sys.stderr)
        sys.exit(5)


def cmd_revoke(args) -> int:
    iface, dbus_mod = _broker()
    if args.all_for_uid is not None:
        try:
            n = int(iface.RevokeAllForUid(int(args.all_for_uid)))
        except dbus_mod.DBusException as e:
            print(f"qdistro-approvals: revoke failed: {e}", file=sys.stderr)
            return 1
        print(f"revoked {n} approval(s) for uid={args.all_for_uid}")
        return 0
    # argparse's mutually_exclusive_group(required=True) ensures
    # we always have either id or --all-for-uid here.
    try:
        ok = bool(iface.RevokeApproval(int(args.id)))
    except dbus_mod.DBusException as e:
        print(f"qdistro-approvals: revoke failed: {e}", file=sys.stderr)
        return 1
    if not ok:
        print(f"no cached approval with id={args.id}", file=sys.stderr)
        return 1
    print(f"revoked approval id={args.id}")
    return 0


def cmd_audit(args) -> int:
    conn = _open(AUDIT_DB)
    sql = ("SELECT ts, caller_uid, caller_pid, caller_exe, action, "
           "decision, scope, source, approver_uid FROM audit")
    where, params = [], []
    if args.uid is not None:
        where.append("caller_uid = ?")
        params.append(args.uid)
    if args.action is not None:
        where.append("action = ?")
        params.append(args.action)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("(no matching audit rows)")
        return 0
    print(f"{'when':<19}  {'uid':>5}  {'pid':>6}  {'src':<6}  "
          f"{'decision':<8}  {'scope':<11}  {'approver':<8}  action")
    print("-" * 100)
    for r in rows:
        ts, uid, pid, exe, action, decision, scope, source, approver = r
        dec_s = "allow" if decision else "deny"
        scope_s = scope or "-"
        approver_s = f"uid={approver}" if approver is not None else "-"
        print(f"{_fmt_time(ts):<19}  {uid:>5}  {pid:>6}  {source:<6}  "
              f"{dec_s:<8}  {scope_s:<11}  {approver_s:<8}  {action}")
        print(f"{'':<19}  exe={exe}")
    return 0


# Mirrors broker/qdistro_admin_broker.py _VALID_SCOPES (and the TUI/Qt app
# scope pickers). The broker re-validates every scope and refuses the ones a
# given request cannot take (delegated, one-shot, argv-less); this list only
# keeps an obvious typo from reaching the bus.
SCOPES = ("once", "1h", "24h", "forever", "forever_exe",
          "forever_argv", "forever_basename", "forever_prefix")


def _safe(value) -> str:
    """Render requester-controlled text for a root terminal.

    action, exe and details come from the (untrusted) requesting process.
    Escape every non-printable character so a crafted request cannot emit
    terminal control sequences into the admin's shell.
    """
    s = str(value)
    if s.isprintable():
        return s
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in s)


def _dbus_error_text(exc) -> str:
    name = ""
    try:
        name = exc.get_dbus_name() or ""
    except Exception:  # noqa: BLE001
        name = ""
    msg = ""
    try:
        msg = exc.get_dbus_message() or ""
    except Exception:  # noqa: BLE001
        msg = str(exc)
    short = name.rsplit(".", 1)[-1] if name else ""
    return f"{short}: {msg}" if short else (msg or str(exc))


def _pending_rows(iface) -> list[dict]:
    """GetPending, normalized to plain Python types."""
    out = []
    for r in iface.GetPending():
        row: dict[str, Any] = {}
        for k, v in dict(r).items():
            k = str(k)
            if k in ("id", "uid", "pid"):
                row[k] = int(v)
            elif k == "details":
                row[k] = {str(dk): str(dv) for dk, dv in dict(v).items()}
            elif k == "layered_pending":
                row[k] = bool(v)
            else:
                row[k] = str(v)
        out.append(row)
    out.sort(key=lambda r: r.get("id", 0))
    return out


def cmd_pending(args) -> int:
    iface, dbus_mod = _broker()
    try:
        rows = _pending_rows(iface)
    except dbus_mod.DBusException as e:
        print(f"qdistro-approvals: pending failed: {_dbus_error_text(e)}",
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("(no pending requests)")
        return 0
    print(f"{'id':>5}  {'uid':>5}  {'pid':>7}  action")
    print("-" * 72)
    for r in rows:
        print(f"{r.get('id', 0):>5}  {r.get('uid', 0):>5}  "
              f"{r.get('pid', 0):>7}  {_safe(r.get('action', ''))}")
        print(f"{'':>5}  exe={_safe(r.get('exe', ''))}")
        details = r.get("details") or {}
        for k in sorted(details):
            print(f"{'':>5}  {_safe(k)}={_safe(details[k])}")
    return 0


def _decide(args, decision: str, scope: str) -> int:
    verb = "approve" if decision == "allow" else "deny"
    iface, dbus_mod = _broker()
    rid = int(args.id)
    try:
        before = {r["id"]: r for r in _pending_rows(iface)}
    except dbus_mod.DBusException as e:
        print(f"qdistro-approvals: {verb} failed: {_dbus_error_text(e)}",
              file=sys.stderr)
        return 1
    req = before.get(rid)
    # Check first, so a typo is reported as such rather than as the
    # broker's "unknown" (exit 3, unconfirmed) below.
    if req is None:
        print(f"qdistro-approvals: no pending request with id={rid}",
              file=sys.stderr)
        return 1
    if decision == "allow" and scope != "once":
        print(f"qdistro-approvals: warning: scope {scope!r} caches this "
              f"approval beyond this one request (revoke with "
              f"`qdistro-approvals list` / `revoke`)", file=sys.stderr)
    # The broker answers atomically, under the same lock that records the
    # decision, whether THIS call applied it. The proxy from _broker() is
    # bound to the broker's unique bus name (dbus-python's default), so a
    # broker restart between the GetPending snapshot and this call fails
    # the call instead of deciding a reused id in a new instance.
    try:
        result = iface.DecideRequest(rid, decision, scope)
    except dbus_mod.DBusException as e:
        print(f"qdistro-approvals: {verb} failed: {_dbus_error_text(e)}",
              file=sys.stderr)
        return 1
    return _report_outcome(req, decision, scope, result)


def _report_outcome(req: dict, decision: str, scope: str, result) -> int:
    """Map DecideRequest's atomic result to output and exit code.

    "applied" (or "applied-uncached", with a warning: caching is
    best-effort) is the only success. "already-allow"/"already-deny" means a
    concurrent decider (Qt app, TUI, another CLI) got there first and this
    call changed nothing: exit 1 naming the decision that holds. Anything
    else (an id unknown to the broker, a broker that predates the return
    value) is fail-closed: exit 3, outcome unconfirmed.
    """
    rid = int(req["id"])
    verb = "approve" if decision == "allow" else "deny"
    result = "" if result is None else str(result)
    if result in ("applied", "applied-uncached"):
        if result == "applied-uncached":
            print(f"qdistro-approvals: warning: the decision was applied but "
                  f"the broker could not store the {scope!r} cache row; "
                  f"later identical requests will prompt again",
                  file=sys.stderr)
        done = "approved" if decision == "allow" else "denied"
        print(f"{done} request id={rid} uid={req.get('uid', 0)} "
              f"pid={req.get('pid', 0)} action={_safe(req.get('action', ''))} "
              f"(scope: {scope})")
        return 0
    if result in ("already-allow", "already-deny"):
        got = result.split("-", 1)[1]
        print(f"qdistro-approvals: request id={rid} was NOT decided by this "
              f"command: it had already been decided ({got}) by another "
              f"approver; nothing was changed", file=sys.stderr)
        return 1
    print(f"qdistro-approvals: {verb} sent for request id={rid}, but the "
          f"outcome is unconfirmed (broker answered {_safe(result or 'nothing')!r})",
          file=sys.stderr)
    return 3


def cmd_approve(args) -> int:
    return _decide(args, "allow", args.scope)


def cmd_deny(args) -> int:
    # A deny is never cached (the broker writes cache rows on allow only),
    # so it carries the narrowest scope.
    return _decide(args, "deny", "once")


def cmd_audit_gc(args) -> int:
    iface, dbus_mod = _broker()
    try:
        n = int(iface.RunAuditGc(int(args.retention_days)))
    except dbus_mod.DBusException as e:
        print(f"qdistro-approvals: audit-gc failed: {e}", file=sys.stderr)
        return 1
    print(f"audit-gc: deleted {n} row(s) older than {args.retention_days}d")
    return 0


def cmd_gc(_args) -> int:
    iface, dbus_mod = _broker()
    try:
        n = int(iface.RunCacheGc())
    except dbus_mod.DBusException as e:
        print(f"qdistro-approvals: gc failed: {e}", file=sys.stderr)
        return 1
    print(f"deleted {n} expired approval(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="qdistro-approvals",
                                 description=__doc__.split("\n")[0])
    # Cheap, side-effect-free health smoke. Handled before _require_root()
    # in main() so the version check works without privileges.
    ap.add_argument("--version", action="version",
                    version="%(prog)s (qdistro)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pending", help="List requests waiting for a decision")
    pp.add_argument("--json", action="store_true",
                    help="print the broker's GetPending rows as JSON")
    pp.set_defaults(fn=cmd_pending)

    pap = sub.add_parser("approve", help="Allow a pending request")
    pap.add_argument("id", type=int, help="request id from `pending`")
    pap.add_argument("--scope", choices=SCOPES, default="once",
                     help="how long the approval holds (default: once; "
                          "anything else is cached)")
    pap.set_defaults(fn=cmd_approve)

    pd = sub.add_parser("deny", help="Deny a pending request")
    pd.add_argument("id", type=int, help="request id from `pending`")
    pd.set_defaults(fn=cmd_deny)

    sub.add_parser("list", help="List currently-cached approvals").set_defaults(fn=cmd_list)

    pr = sub.add_parser("revoke", help="Delete cached approval(s)")
    pr_target = pr.add_mutually_exclusive_group(required=True)
    pr_target.add_argument("id", nargs="?", type=int, help="approval id from `list`")
    pr_target.add_argument("--all-for-uid", type=int,
                           help="revoke all approvals for this caller uid")
    pr.set_defaults(fn=cmd_revoke)

    pa = sub.add_parser("audit", help="Show audit log entries (newest first)")
    pa.add_argument("--uid", type=int, help="filter by caller_uid")
    pa.add_argument("--action", help="filter by exact action string")
    pa.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    pa.set_defaults(fn=cmd_audit)

    sub.add_parser("gc", help="Delete expired cache rows now").set_defaults(fn=cmd_gc)

    pag = sub.add_parser("audit-gc", help="Delete audit rows older than N days")
    pag.add_argument("--retention-days", type=int, default=90,
                     help="keep rows newer than this many days (default 90; 0 wipes all)")
    pag.set_defaults(fn=cmd_audit_gc)

    return ap


def main() -> int:
    parser = build_parser()
    # --version is a side-effect-free smoke check: handle it (the
    # action="version" path exits 0 inside parse_args) before the root
    # gate so the health probe needs no privileges.
    if "--version" in sys.argv[1:]:
        parser.parse_args()
    _require_root()
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
