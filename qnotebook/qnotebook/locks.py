"""Per-notebook session lock file.

A JSON sidecar at `<notebook>/.qnotebook/lock` holds the PID + hostname of the
process that currently owns the notebook. On open:

  - No lock, or lock's PID not alive on this host: we take the lock.
  - Lock exists and PID is alive: caller should prompt
    (read-only / force / cancel).

Force-open rewrites the lock to this process. Clean close removes it.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from .notebook import DOTDIR


def _lock_path(root: Path) -> Path:
    return root / DOTDIR / "lock"


def read(root: Path) -> dict | None:
    p = _lock_path(root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write(root: Path) -> None:
    p = _lock_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname()}),
        encoding="utf-8",
    )


def remove(root: Path) -> None:
    p = _lock_path(root)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def is_stale(data: dict | None) -> bool:
    """A lock is stale if it has no PID, was taken by *this* process (re-entrant
    open), was taken on a different host, or its PID is not currently alive."""
    if not data:
        return True
    pid = data.get("pid")
    host = data.get("host")
    if not pid:
        return True
    # Re-entrant open from the same process: treat as stale so we reclaim it.
    if int(pid) == os.getpid():
        return True
    if host and host != socket.gethostname():
        # Can't introspect; treat as live (safe default).
        return False
    try:
        os.kill(int(pid), 0)
        return False  # alive
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # exists but not signalable
    except OSError:
        return True


def acquire(root: Path) -> tuple[bool, dict | None]:
    """Try to acquire the lock. Returns (acquired, existing_lock_data).

    If returned `acquired=True`, the lock is now ours. Otherwise the caller
    can prompt the user and call `force_acquire` or open read-only."""
    existing = read(root)
    if is_stale(existing):
        write(root)
        return True, existing
    return False, existing


def force_acquire(root: Path) -> None:
    write(root)
