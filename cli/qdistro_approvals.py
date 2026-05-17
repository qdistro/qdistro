#!/usr/bin/env python3
"""qdistro-approvals — admin CLI for the approval cache + audit log.

Subcommands:
    list                       List currently-cached approvals
    revoke <id>                Delete one cached approval by id
    revoke --all-for-uid <N>   Delete all cached approvals for a uid
    audit [--uid N] [--action X] [--limit N]
                               Show recent audit rows (newest first)
    gc                         Run cache GC immediately (delete expired rows)

Root-only (writes to /var/lib/qdistro/...).
"""
from __future__ import annotations

import argparse
import stat
import os
import sqlite3
import sys
import time

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


BUS_NAME = "com.qdistro.AdminBroker1"
OBJ_PATH = "/com/qdistro/AdminBroker1"


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
    sub = ap.add_subparsers(dest="cmd", required=True)

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
    _require_root()
    args = build_parser().parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
