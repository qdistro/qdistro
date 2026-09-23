"""Per-page snapshot store.

Before every SafeWriter save, the on-disk version of a page is archived
to ``.qnotebook/snapshots/<page-hash>/<ts>.md``. Last 10 snapshots per page
are kept (FIFO rotation). Used by File → Snapshots to restore any recent
state without needing git.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import safe_save

SNAP_DIR = ".qnotebook/snapshots"
KEEP = 10
TS_RE = re.compile(r"^(\d{14})\.md$")


def _page_key(page_path: Path, root: Path) -> str:
    try:
        rel = page_path.relative_to(root)
    except ValueError:
        rel = page_path
    return hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:12]


def _snap_dir(root: Path, page_path: Path) -> Path:
    return root / SNAP_DIR / _page_key(page_path, root)


@dataclass(frozen=True)
class Snapshot:
    page_path: Path
    snap_path: Path
    timestamp: str  # YYYYMMDDHHMMSS

    @property
    def iso(self) -> str:
        t = self.timestamp
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"

    def read_bytes(self) -> bytes:
        return self.snap_path.read_bytes()


def take_snapshot(root: Path, page_path: Path) -> Snapshot | None:
    """Archive the current on-disk bytes of ``page_path``. No-op if missing."""
    if not page_path.is_file():
        return None
    data = page_path.read_bytes()
    d = _snap_dir(root, page_path)
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d%H%M%S")
    snap = d / f"{ts}.md"
    if snap.exists():
        # Avoid collisions within the same second.
        for i in range(1, 100):
            alt = d / f"{ts}{i:02d}.md"
            if not alt.exists():
                snap = alt
                break
    safe_save.atomic_write(snap, data)
    rotate(root, page_path)
    return Snapshot(page_path=page_path, snap_path=snap, timestamp=ts)


def list_snapshots(root: Path, page_path: Path) -> list[Snapshot]:
    d = _snap_dir(root, page_path)
    if not d.is_dir():
        return []
    out: list[Snapshot] = []
    for entry in sorted(d.iterdir(), reverse=True):
        if not entry.is_file():
            continue
        stem = entry.stem
        if len(stem) < 14 or not stem[:14].isdigit():
            continue
        out.append(Snapshot(
            page_path=page_path, snap_path=entry, timestamp=stem[:14],
        ))
    return out


def rotate(root: Path, page_path: Path, keep: int = KEEP) -> None:
    snaps = list_snapshots(root, page_path)
    for old in snaps[keep:]:
        try:
            old.snap_path.unlink()
        except OSError:
            pass


def restore(root: Path, snap: Snapshot) -> None:
    """Restore snapshot bytes to the live page file (through SafeWriter)."""
    data = snap.read_bytes()
    # Take a fresh snapshot of the current state first so the restore is
    # itself reversible.
    take_snapshot(root, snap.page_path)
    safe_save.atomic_write(snap.page_path, data)
