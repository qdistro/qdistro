"""Persistent approval cache for the qdistro admin broker.

Schema (see  A.1 + spec/21 for the
match_kind vocabulary; task 069 added argv-aware matching):

    approvals:
        id INTEGER PK
        caller_uid INTEGER       -- requesting user uid
        action TEXT              -- e.g. "test.action"
        match_kind TEXT          -- 'argv_exact' | 'basename' | 'prefix'
                                 -- | 'exe_only' | 'always'
        match_value TEXT         -- exe path (matched against caller_exe
                                 -- for argv_exact / exe_only; empty for
                                 -- basename / prefix / always)
        argv TEXT                -- JSON-encoded list. Stores:
                                 --   argv_exact: full argv tuple
                                 --   basename:   single basename string
                                 --   prefix:     argv prefix tuple
                                 --   exe_only / always: ''
        decision INTEGER         -- 1 allow, 0 deny
        expires_at INTEGER       -- unix epoch; NULL = never
        created_at INTEGER
        approver_uid INTEGER
        scope TEXT               -- the UI scope tag for audit display

Lookup precedence (most-specific wins):
    argv_exact > prefix > basename > exe_only > always.

Phase 2 slice A only handles allow rows (deny isn't cached — admin must
re-prompt to confirm a deny was intentional). The decision column is kept
to forward-fit future "remember deny for N hours" UX.

task(069): argv-aware matching. Pre-task(069) the cache stored
forever_exe approvals as match_kind='argv_exact' with match_value=exe
and no argv tracking — i.e. "forever-allow ANY argv under this exe",
which is a real qsu threat-model gap (an admin who approves
`/usr/bin/apt-get update` forever_exe-style inadvertently grants
forever access to `apt-get install <anything>`). Post-69 the legacy
'argv_exact' kind is migrated to 'exe_only' (semantic name match) and
the new 'argv_exact' / 'basename' / 'prefix' kinds tighten the match
on the actual argv tuple. The UI gains forever_argv / forever_basename
/ forever_prefix scope options on top of the existing forever_exe.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY,
    caller_uid   INTEGER NOT NULL,
    action       TEXT NOT NULL,
    match_kind   TEXT NOT NULL,
    match_value  TEXT NOT NULL,
    argv         TEXT NOT NULL DEFAULT '',
    decision     INTEGER NOT NULL,
    expires_at   INTEGER,
    created_at   INTEGER NOT NULL,
    approver_uid INTEGER NOT NULL,
    scope        TEXT
);
CREATE INDEX IF NOT EXISTS approvals_lookup
  ON approvals(caller_uid, action, match_kind);
"""


def _migrate(conn) -> None:
    """In-place migration. Pre-1.0; no compatibility shim required.

    - Adds the `scope` column (review-fixes pass) if missing.
    - task(069): adds the `argv` column if missing AND remaps any
      legacy 'argv_exact' rows that have no argv data to 'exe_only'.
      The legacy 'argv_exact' value semantically WAS "match by exe
      only" (match_value held the exe path; argv was never compared).
      Renaming to 'exe_only' makes the misnomer explicit and frees up
      'argv_exact' for the spec/21 actual-argv-tuple match.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    if "scope" not in cols:
        conn.execute("ALTER TABLE approvals ADD COLUMN scope TEXT")
    if "argv" not in cols:
        conn.execute("ALTER TABLE approvals ADD COLUMN argv TEXT NOT NULL DEFAULT ''")
        # Legacy 'argv_exact' rows had argv=='' (no argv tracking) and
        # semantic was actually "match by exe only" — flip the kind.
        conn.execute(
            "UPDATE approvals SET match_kind='exe_only' "
            "WHERE match_kind='argv_exact' AND argv=''"
        )


# scope-string -> (match_kind, ttl_seconds_or_None_or_dont_persist)
# 'once' is the special case: returns None to signal "do not persist".
#
# task(069): new vocabulary. Old `forever_exe` semantic preserved as
# `exe_only` match_kind. New scopes (forever_argv / forever_basename /
# forever_prefix) tighten the gate by fixing argv. 1h / 24h use the
# new argv-tracked `argv_exact` when argv is present; non-argv callers
# keep the UI's longstanding timed exe-only behavior.
_SCOPE_MAP: dict[str, tuple[str, int | None] | None] = {
    "once":             None,
    "1h":               ("argv_exact", 3600),
    "24h":              ("argv_exact", 86_400),
    "forever":          ("always",     None),
    "forever_exe":      ("exe_only",   None),
    "forever_argv":     ("argv_exact", None),
    "forever_basename": ("basename",   None),
    "forever_prefix":   ("prefix",     None),
}


def scope_to_row(scope: str) -> tuple[str, int | None] | None:
    """Map a UI scope string to (match_kind, ttl) or None for 'don't persist'."""
    return _SCOPE_MAP.get(scope)


# Match-kind precedence (most specific first). Index = priority.
_PRIORITY = {
    "argv_exact": 0,
    "prefix":     1,
    "basename":   2,
    "exe_only":   3,
    "always":     4,
}

# Match-kinds that match argv-blind (ignore the request's argv tuple):
# 'always' (scope forever) keys on (uid, action) alone, 'exe_only' (scope
# forever_exe) on (uid, action, exe). Two classes of caller must NOT be
# auto-satisfied (allow OR deny) by these blanket rows, for the same
# fail-closed reason — they fall through to an argv-pinned row
# (argv_exact / basename / prefix), a rule, a hook, or the admin prompt
# (operationally default-deny):
#   - DELEGATED requests (RequestPermissionAs): the broker never
#     authenticated the claimed peer, so it can't let one process inherit
#     another's blanket trust. Mirrors the broker's
#     _DELEGATED_FORBIDDEN_SCOPES (forever / forever_exe).
#   - authenticated SANDBOXED (tier-2) callers: the broker is the sole
#     gateway between a compromised tier-2 workload and host-side
#     sensitive ops, so a blanket grant minted for a *different exe /
#     argv / isolation tier* at the same uid must not auto-decide it.
_ARGV_BLIND_KINDS = frozenset(("exe_only", "always"))
# Back-compat alias kept for any external reference; the lookup uses
# _ARGV_BLIND_KINDS directly so the two skip reasons can never drift apart.
_DELEGATED_FORBIDDEN_KINDS = _ARGV_BLIND_KINDS


def _argv_basename(argv: list | tuple | None) -> str:
    """Return the program basename for matching purposes. argv[0] is
    the program path — we strip directories with os.path.basename and
    fall back to '' on empty argv."""
    if not argv:
        return ""
    head = str(argv[0])
    return os.path.basename(head)


def _decode_pattern(text: str) -> list:
    """Decode a stored JSON argv pattern. Empty string → []."""
    if not text:
        return []
    try:
        v = json.loads(text)
        return list(v) if isinstance(v, (list, tuple)) else []
    except (ValueError, TypeError):
        return []


def _match_row_argv(row_kind: str, row_value: str, row_argv: str,
                    exe: str, argv: list | tuple | None) -> bool:
    """Return True if the stored row matches the request's exe + argv.

    Used in Python-side post-filtering after the SQL UNION returns
    candidate rows. Keeps the SQL simple (sqlite has no list/JSON ops
    in stock builds) at the cost of a bounded Python iteration.
    """
    if row_kind == "always":
        return True
    if row_kind == "exe_only":
        return row_value == exe
    if row_kind in ("argv_exact", "basename", "prefix") and not argv:
        return False
    if row_kind == "argv_exact":
        if row_value != exe:
            return False
        return list(argv) == _decode_pattern(row_argv)
    if row_kind == "basename":
        return row_argv == _argv_basename(argv)
    if row_kind == "prefix":
        prefix = _decode_pattern(row_argv)
        if not prefix:
            return False
        actual = list(argv)
        return actual[: len(prefix)] == prefix
    return False


class ApprovalCache:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # Create with restrictive umask so the file is 0600 from the
        # very first byte (was: brief 0644 window between connect+chmod)
        old_umask = os.umask(0o077)
        try:
            self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        finally:
            os.umask(old_umask)
        self._conn.executescript(SCHEMA)
        _migrate(self._conn)
        # WAL for concurrent readers (CLI) alongside broker writer;
        # busy_timeout to retry on lock instead of raising immediately.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        os.chmod(db_path, 0o600)

    def lookup(self, caller_uid: int, action: str, exe: str,
               argv: list | tuple | None = None,
               delegated: bool = False,
               sandboxed: bool = False) -> bool | None:
        """Return True/False if an unexpired matching allow/deny exists; None for prompt."""
        hit = self.lookup_detail(caller_uid, action, exe, argv, delegated,
                                 sandboxed)
        return None if hit is None else bool(hit["decision"])

    def lookup_detail(self, caller_uid: int, action: str, exe: str,
                      argv: list | tuple | None = None,
                      delegated: bool = False,
                      sandboxed: bool = False) -> dict | None:
        """Like lookup() but returns the full matching row (or None)
        for richer logging. Selects the most-specific match.

        argv-aware: argv_exact / prefix / basename require a matching
        argv list; exe_only / always ignore argv. argv defaults to None
        for backward compatibility (rules-engine cache-store path on
        rule decisions doesn't always have argv) — passing None makes
        the argv-tracked kinds skip.

        delegated: when True, argv-blind rows (exe_only / always) are
        skipped so a delegated request cannot be satisfied program-blind
        by a pre-existing blanket grant — the decision-time mirror of
        the broker's _DELEGATED_FORBIDDEN_SCOPES store-path guard.
        Defaults to False to preserve existing (non-delegated) callers.

        sandboxed: when True, the same argv-blind rows are skipped so an
        authenticated sandboxed (tier-2) caller cannot inherit a
        uid-wide 'forever'/'forever_exe' grant minted for a different
        exe/argv/tier — closing the cache's tier-blindness on the
        non-delegated RequestPermission / CheckPermission path. The
        broker must derive this from the *verified* launch-record
        sandbox_engine, never the forgeable claimed selector, or the
        guard is bypassable in lineage shadow mode. Defaults to False.
        """
        now = int(time.time())
        skip_blind = delegated or sandboxed
        cur = self._conn.execute(
            """
            SELECT id, match_kind, match_value, argv, decision, expires_at,
                   created_at, approver_uid, scope
            FROM approvals
            WHERE caller_uid = ? AND action = ?
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (caller_uid, action, now),
        )
        best = None
        best_priority = len(_PRIORITY) + 1
        for row in cur.fetchall():
            r_id, r_kind, r_value, r_argv, r_dec, r_exp, r_cre, r_app, r_scope = row
            if skip_blind and r_kind in _ARGV_BLIND_KINDS:
                continue
            if not _match_row_argv(r_kind, r_value, r_argv, exe, argv):
                continue
            p = _PRIORITY.get(r_kind, len(_PRIORITY))
            if p < best_priority or (p == best_priority and (best is None or r_id > best["id"])):
                best = {
                    "id": r_id, "match_kind": r_kind, "match_value": r_value,
                    "argv": r_argv, "decision": r_dec,
                    "expires_at": r_exp, "created_at": r_cre,
                    "approver_uid": r_app, "scope": r_scope,
                }
                best_priority = p
        return best

    def store(self, caller_uid: int, action: str, exe: str,
              scope: str, decision: bool, approver_uid: int,
              argv: list | tuple | None = None) -> bool:
        """Persist if scope warrants. Returns True if a row was written.

        argv is required for forever_argv / forever_basename /
        forever_prefix. Passing argv=None or an empty list for those
        scopes writes no row: silently broadening an argv-pinned
        decision to exe_only would turn it into argv-blind trust.
        Timed 1h / 24h approvals keep non-argv UI compatibility by
        storing a time-limited exe_only row when argv is absent.
        """
        mapped = scope_to_row(scope)
        if mapped is None:
            return False
        match_kind, ttl = mapped
        argv_list = list(argv) if argv else []
        if match_kind in ("argv_exact", "basename", "prefix") and not argv_list:
            if scope in ("1h", "24h"):
                match_kind = "exe_only"
            else:
                return False
        if match_kind == "argv_exact":
            argv_text = json.dumps(argv_list)
            match_value = exe
        elif match_kind == "basename":
            match_kind = "basename"
            argv_text = _argv_basename(argv_list)
            match_value = ""
        elif match_kind == "prefix":
            argv_text = json.dumps(argv_list)
            match_value = ""
        elif match_kind == "exe_only":
            argv_text = ""
            match_value = exe
        else:  # always
            argv_text = ""
            match_value = ""
        now = int(time.time())
        expires_at = (now + ttl) if ttl is not None else None
        self._conn.execute(
            """
            INSERT INTO approvals
              (caller_uid, action, match_kind, match_value, argv,
               decision, expires_at, created_at, approver_uid, scope)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (caller_uid, action, match_kind, match_value, argv_text,
             1 if decision else 0, expires_at, now, approver_uid, scope),
        )
        return True

    def list_all(self) -> list[dict]:
        """Return every row, newest-first. Includes expired rows — GUI
        viewers can decide whether to show them or filter out."""
        cur = self._conn.execute(
            "SELECT id, caller_uid, action, match_kind, match_value, argv, "
            "decision, scope, approver_uid, expires_at, created_at "
            "FROM approvals ORDER BY id DESC"
        )
        return [{
            "id": r[0], "caller_uid": r[1], "action": r[2],
            "match_kind": r[3], "match_value": r[4], "argv": r[5],
            "decision": r[6], "scope": r[7], "approver_uid": r[8],
            "expires_at": r[9], "created_at": r[10],
        } for r in cur.fetchall()]

    def delete_by_id(self, approval_id: int) -> dict | None:
        """Delete one row; return its pre-delete fields, or None if absent.

        Returning the deleted row lets the caller audit the revocation
        with the same caller_uid/action/exe context the original store()
        used — without that, the audit row can only reference the opaque
        approval id.
        """
        cur = self._conn.execute(
            "SELECT id, caller_uid, action, match_kind, match_value, argv, "
            "decision, scope, approver_uid "
            "FROM approvals WHERE id = ?",
            (int(approval_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        self._conn.execute("DELETE FROM approvals WHERE id = ?", (int(approval_id),))
        return {
            "id": row[0], "caller_uid": row[1], "action": row[2],
            "match_kind": row[3], "match_value": row[4], "argv": row[5],
            "decision": row[6], "scope": row[7], "approver_uid": row[8],
        }

    def delete_by_uid(self, caller_uid: int) -> list[dict]:
        """Delete all rows for a caller uid; return pre-delete fields."""
        cur = self._conn.execute(
            "SELECT id, caller_uid, action, match_kind, match_value, argv, "
            "decision, scope, approver_uid "
            "FROM approvals WHERE caller_uid = ?",
            (int(caller_uid),),
        )
        rows = [{
            "id": r[0], "caller_uid": r[1], "action": r[2],
            "match_kind": r[3], "match_value": r[4], "argv": r[5],
            "decision": r[6], "scope": r[7], "approver_uid": r[8],
        } for r in cur.fetchall()]
        if rows:
            self._conn.execute(
                "DELETE FROM approvals WHERE caller_uid = ?",
                (int(caller_uid),),
            )
        return rows

    def gc(self) -> int:
        """Delete expired rows. Returns deleted count."""
        now = int(time.time())
        cur = self._conn.execute(
            "DELETE FROM approvals WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        return cur.rowcount or 0

    def close(self) -> None:
        self._conn.close()
