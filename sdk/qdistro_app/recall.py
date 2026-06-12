"""qdistro_app.recall — dormant SDK helpers for post-v1 Recall.

Recall capture is cut from qdistro v1. This module remains importable so
applications that probe for the SDK do not fail at import time, but capture
entry points fail closed until the viewer/query grant model returns.

Two surfaces:

- ``push_text_snapshot(content, *, ...)`` — write a text entry to
  the calling user's per-day recall DB. Disabled for v1.
- ``exclude_fields(widget_ids)`` — Phase-9 stub. The actual exclusion
  is enforced at the WM (the only producer with non-bypassable
  visibility into focused widgets); the SDK signature is here only
  so apps don't ship code that breaks when the Phase-9 daemon ships.

The Phase-8 MVP writes the local SQLite DB directly. No daemon, no
DBus. The path layout matches spec/17 §step 0:

  /var/lib/qdistro/recall/<user>/<YYYY-MM>/<YYYY-MM-DD>.db

Tests override the root via ``QDISTRO_RECALL_ROOT`` so they don't
touch /var.

Pwd-domain exclusion remains in the dormant engine, but no v1 caller should
reach it through this SDK.
"""
from __future__ import annotations

import os
import sys


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


class RecallDisabled(RuntimeError):
    """Raised when v1 code attempts to capture Recall data."""


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
    """Push a text snapshot to recall. Disabled for v1.

    The dormant implementation is intentionally not reachable from v1
    application code: Recall capture and viewing return post-v1 only after
    the separate viewer/query grant model ships.
    """
    raise RecallDisabled("Recall capture is disabled in qdistro v1")


def exclude_fields(widget_ids):
    """Phase-9 stub — see module docstring. No-op.

    Exists so apps coding against the recall SDK don't break when
    the Phase-9 daemon adds the real exclusion side-channel; their
    calls become live-wired at that point.
    """
    return list(widget_ids)
