"""qdistro-recall ingest module — Phase-8 §step 0 MVP skeleton.

Per doc/recall.md §"Phase-8 §step 0 MVP". Closes the
architectural seam: there is now a place where text snapshots
land, an FTS5-indexed schema, and a producer-side exclusion gate
that rejects pwd-domain pushes BEFORE they hit disk.

Out of scope here (deferred to Phase-9): screenshots + perceptual
hash, OCR, embeddings + sqlite-vec, recall-user uid + tty4
compositor, TPM seal-at-rest, PyQt viewer, exclusion YAML,
encryption-at-rest. Spec calls these out explicitly.

The schema is FTS5-on-mainline-SQLite (≥3.46 ships on Tumbleweed).
Per-day rolling DB at:

  /var/lib/qdistro/recall/<user>/<YYYY-MM>/<YYYY-MM-DD>.db

…but tests parameterise the directory so they don't touch /var.

Pwd-domain exclusion is the load-bearing layer at the WM (per
spec/17). This module's exclusion is the second-line "advisory"
gate the spec describes — push() refuses entries tagged as
pwd-domain so a misbehaving SDK or browser bridge can't smuggle
pwd surfaces into the index even if the WM gate is bypassed.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable

# Domains tagged like spec/29 secctx labels. Spec/17 §"Pwd-manager
# exclusion" calls these out as hard-coded.
PWD_DOMAINS: tuple[str, ...] = (
    "qdistro:pwd",
    "qdistro:pwd:ui",
    "qdistro:pwd:fill",
)
# App IDs / exe-basename matchers also rejected.
PWD_APP_IDS: tuple[str, ...] = (
    "qdistro.pwd",
    "qdistro.pwd-ui",
)
PWD_EXE_PREFIXES: tuple[str, ...] = (
    "/usr/bin/qdistro-pwd",
    "/usr/lib/qdistro/pwd",
    "/usr/libexec/qdistro/pwd",
    "/usr/bin/keepassxc",
    "/usr/bin/bitwarden",
)


# ---- schema --------------------------------------------------------

SCHEMA = [
    # Main row table — one entry per text snapshot push.
    """CREATE TABLE IF NOT EXISTS recall_entries (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        source TEXT NOT NULL,           -- 'sdk' | 'bridge' | 'wm'
        user TEXT NOT NULL,             -- silo name ('work-user' etc.)
        app_id TEXT,
        exe TEXT,
        secctx TEXT,
        url TEXT,
        title TEXT,
        text TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_recall_ts
        ON recall_entries (ts)""",
    # FTS5 contentless index over the text column. We keep the
    # original row in `recall_entries` and use the FTS table
    # purely for querying. external-content keeps storage from
    # doubling.
    """CREATE VIRTUAL TABLE IF NOT EXISTS recall_text USING fts5(
        text, title, url,
        content='recall_entries',
        content_rowid='id',
        tokenize='porter unicode61 remove_diacritics 2'
    )""",
    # Triggers keep FTS in sync with row inserts/updates/deletes.
    """CREATE TRIGGER IF NOT EXISTS recall_entries_ai
        AFTER INSERT ON recall_entries BEGIN
            INSERT INTO recall_text (rowid, text, title, url)
            VALUES (new.id, new.text, new.title, new.url);
        END""",
    """CREATE TRIGGER IF NOT EXISTS recall_entries_ad
        AFTER DELETE ON recall_entries BEGIN
            INSERT INTO recall_text (recall_text, rowid, text, title, url)
            VALUES ('delete', old.id, old.text, old.title, old.url);
        END""",
    """CREATE TRIGGER IF NOT EXISTS recall_entries_au
        AFTER UPDATE ON recall_entries BEGIN
            INSERT INTO recall_text (recall_text, rowid, text, title, url)
            VALUES ('delete', old.id, old.text, old.title, old.url);
            INSERT INTO recall_text (rowid, text, title, url)
            VALUES (new.id, new.text, new.title, new.url);
        END""",
]


# ---- exclusion gate ----------------------------------------------

def is_pwd_domain(secctx: str | None = None,
                  app_id: str | None = None,
                  exe: str | None = None) -> bool:
    """Return True if any of the supplied identity fields names a
    pwd-manager surface that recall must never index.

    Match shape:
    - secctx prefix (matches "qdistro:pwd" and any sub-domain).
    - app_id exact (no globs — the SDK passes a stable string).
    - exe path-prefix (handles different install layouts).
    """
    s = (secctx or "").strip()
    if s:
        for d in PWD_DOMAINS:
            if s == d or s.startswith(d + ":"):
                return True
    a = (app_id or "").strip()
    if a in PWD_APP_IDS:
        return True
    e = (exe or "").strip()
    if e:
        for prefix in PWD_EXE_PREFIXES:
            if e == prefix or e.startswith(prefix + "/") \
                    or e.startswith(prefix + "-"):
                return True
    return False


# ---- DB lifecycle -------------------------------------------------

def open_db(path: str | os.PathLike) -> sqlite3.Connection:
    """Open (or create) a recall DB and ensure the schema exists."""
    parent = os.path.dirname(os.fspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(os.fspath(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()
    return conn


def db_path_for(root: str | os.PathLike, user: str,
                ts: float | None = None) -> str:
    """Build /var/lib/qdistro/recall/<user>/<YYYY-MM>/<YYYY-MM-DD>.db
    from `root`. ts defaults to now.
    """
    t = time.gmtime(ts if ts is not None else time.time())
    ym = time.strftime("%Y-%m", t)
    d = time.strftime("%Y-%m-%d", t)
    return os.path.join(os.fspath(root), user, ym, f"{d}.db")


# ---- ingest API ---------------------------------------------------

class PwdDomainRefused(Exception):
    """Raised by push() when the entry is in the pwd-domain
    exclusion set. Callers — bridge, SDK, WM — translate to their
    own error surface (DBus error, refusal JSON, weston log line).
    """


def push_text(
        conn: sqlite3.Connection, *,
        user: str,
        text: str,
        source: str,                # 'sdk' / 'bridge' / 'wm'
        app_id: str | None = None,
        exe: str | None = None,
        secctx: str | None = None,
        url: str | None = None,
        title: str | None = None,
        ts: float | None = None,
) -> int:
    """Insert a text-snapshot row. Returns the new row id.

    Raises PwdDomainRefused before touching the DB if the entry
    is in any pwd-domain exclusion set.
    """
    if not text or not text.strip():
        raise ValueError("text must be non-empty")
    if source not in ("sdk", "bridge", "wm"):
        raise ValueError(f"unknown source: {source}")
    if is_pwd_domain(secctx=secctx, app_id=app_id, exe=exe):
        raise PwdDomainRefused(
            f"refusing pwd-domain push: secctx={secctx!r} "
            f"app_id={app_id!r} exe={exe!r}")
    cur = conn.execute(
        """INSERT INTO recall_entries
           (ts, source, user, app_id, exe, secctx, url, title, text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts if ts is not None else time.time(),
         source, user, app_id, exe, secctx, url, title, text))
    conn.commit()
    return int(cur.lastrowid)


def search(
        conn: sqlite3.Connection, query: str,
        *, user: str | None = None, limit: int = 50,
) -> list[dict]:
    """FTS5 query. Returns newest-first list of hit dicts.

    Caller-supplied `query` is passed straight to FTS5's MATCH
    operator — callers are expected to escape user input via the
    normal FTS5 quoting rules.
    """
    sql = (
        "SELECT e.id, e.ts, e.source, e.user, e.app_id, e.exe, "
        "e.secctx, e.url, e.title, e.text "
        "FROM recall_text t "
        "JOIN recall_entries e ON e.id = t.rowid "
        "WHERE recall_text MATCH ? "
    )
    args: list = [query]
    if user is not None:
        sql += "AND e.user = ? "
        args.append(user)
    sql += "ORDER BY e.ts DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    cols = ("id", "ts", "source", "user", "app_id", "exe",
            "secctx", "url", "title", "text")
    return [dict(zip(cols, r, strict=True)) for r in rows]


def list_recent(
        conn: sqlite3.Connection,
        *, user: str | None = None, limit: int = 50,
) -> list[dict]:
    """Newest-first list of entries (no FTS). Useful for the
    timeline view + for tests that just want "is anything in here".
    """
    sql = ("SELECT id, ts, source, user, app_id, exe, secctx, "
           "url, title, text FROM recall_entries ")
    args: list = []
    if user is not None:
        sql += "WHERE user = ? "
        args.append(user)
    sql += "ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    cols = ("id", "ts", "source", "user", "app_id", "exe",
            "secctx", "url", "title", "text")
    return [dict(zip(cols, r, strict=True)) for r in rows]


def purge_older_than(conn: sqlite3.Connection,
                     cutoff_ts: float) -> int:
    """Delete entries with ts < cutoff_ts. Returns count deleted.

    Used by the daily TTL reaper (Phase-9 will wire this; in MVP
    it's exposed for tests + ad-hoc admin runs).
    """
    cur = conn.execute(
        "DELETE FROM recall_entries WHERE ts < ?", (cutoff_ts,))
    conn.commit()
    return cur.rowcount or 0


def push_many_text(
        conn: sqlite3.Connection,
        rows: Iterable[dict],
) -> tuple[int, int]:
    """Batch-push; returns (inserted, refused).

    Each row dict must include 'user', 'text', 'source'. Optional
    fields match push_text(). PwdDomainRefused does NOT abort the
    batch — it increments the refused counter and the next row
    proceeds.
    """
    inserted = 0
    refused = 0
    for r in rows:
        try:
            push_text(conn, **r)
            inserted += 1
        except PwdDomainRefused:
            refused += 1
    return inserted, refused
