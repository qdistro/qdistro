"""qdistro_app.recall — SDK helpers for spec/17 §step 0 MVP.

Two surfaces:

- ``push_text_snapshot(content, *, ...)`` — write a text entry to
  the calling user's per-day recall DB.
- ``exclude_fields(widget_ids)`` — Phase-9 stub. The actual exclusion
  is enforced at the WM (the only producer with non-bypassable
  visibility into focused widgets); the SDK signature is here only
  so apps don't ship code that breaks when the Phase-9 daemon ships.

The Phase-8 MVP writes the local SQLite DB directly. No daemon, no
DBus. The path layout matches spec/17 §step 0:

  /var/lib/qdistro/recall/<user>/<YYYY-MM>/<YYYY-MM-DD>.db

Tests override the root via ``QDISTRO_RECALL_ROOT`` so they don't
touch /var.

Pwd-domain exclusion is enforced via ``qdistro_recall_ingest.is_pwd_domain``;
SDK callers are advisory per spec/17 §"Pwd-manager exclusion".
"""
from __future__ import annotations

import getpass
import os
import sqlite3
import sys
import time

# We import the engine module via path manipulation so this module
# is importable both:
# - inside the qdistro tree (where qdistro/ is on sys.path), AND
# - on installed VMs (where the engine lives at
#   /usr/libexec/qdistro/qdistro_recall_ingest.py and the SDK lives
#   under site-packages).
def _load_engine():
    try:
        import qdistro_recall_ingest as eng  # type: ignore
        return eng
    except ImportError:
        pass
    candidates = [
        "/usr/libexec/qdistro/qdistro_recall_ingest.py",
        os.path.join(os.path.dirname(__file__),
                     "..", "..", "recall",
                     "qdistro_recall_ingest.py"),
    ]
    import importlib.util
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(
                "qdistro_recall_ingest", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["qdistro_recall_ingest"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError(
        "qdistro_recall_ingest module not found "
        "(install qdistro-recall on the VM, or put qdistro/ on sys.path)")


DEFAULT_ROOT = "/var/lib/qdistro/recall"


def _resolve_root(root: str | None) -> str:
    if root:
        return root
    env = os.environ.get("QDISTRO_RECALL_ROOT", "").strip()
    if env:
        return env
    return DEFAULT_ROOT


def push_text_snapshot(
        content: str, *,
        user: str | None = None,
        app_id: str | None = None,
        url: str | None = None,
        title: str | None = None,
        secctx: str | None = None,
        root: str | None = None,
        source: str = "sdk",
) -> int:
    """Push a text snapshot to recall. Returns the new row id.

    ``content`` must be non-empty. ``user`` defaults to the current
    ``getpass.getuser()`` (the SDK runs in the user's process).
    Pwd-domain entries (``secctx`` matches a qdistro:pwd domain or
    ``app_id`` is in PWD_APP_IDS) raise PwdDomainRefused — callers
    decide whether to swallow or surface.
    """
    eng = _load_engine()
    user = user or getpass.getuser()
    rt = _resolve_root(root)
    path = eng.db_path_for(rt, user)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = eng.open_db(path)
    try:
        rowid = eng.push_text(
            conn, user=user, text=content, source=source,
            app_id=app_id, secctx=secctx, url=url, title=title)
    finally:
        conn.close()
    return rowid


def exclude_fields(widget_ids):
    """Phase-9 stub — see module docstring. No-op.

    Exists so apps coding against the recall SDK don't break when
    the Phase-9 daemon adds the real exclusion side-channel; their
    calls become live-wired at that point.
    """
    return list(widget_ids)
