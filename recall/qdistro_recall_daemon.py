"""qdistro-recall-reaper — Phase-8 MVP TTL daemon.

Runs once per launch (systemd `--user` ``Type=oneshot`` triggered
by ``qdistro-recall-reaper.timer``). Walks every per-day DB under
``/var/lib/qdistro/recall/$USER/`` and:

  - Purges rows older than ``--ttl-days`` from each DB.
  - Deletes empty per-day DBs.
  - Drops month dirs that have no DBs left.

The actual ingest happens in-process via the SDK + bridge dispatch
+ host CLI; this daemon owns retention only. Phase-9 grows
screenshot ingest, dedup, embeddings, etc — at which point this
process becomes long-lived.

ENV:
  QDISTRO_RECALL_ROOT   override for /var/lib/qdistro/recall (tests)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

# Engine import mirrors the CLI's resolution.
def _load_engine():
    try:
        import qdistro_recall_ingest as eng  # type: ignore
        return eng
    except ImportError:
        pass
    candidates = [
        "/usr/libexec/qdistro/qdistro_recall_ingest.py",
        os.path.join(os.path.dirname(__file__),
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
    raise ImportError("qdistro_recall_ingest not found")


def reap(root: str, user: str, ttl_days: int) -> dict:
    """Run one reap pass over <root>/<user>/.

    Returns ``{purged, dbs_deleted, months_deleted}``.
    """
    eng = _load_engine()
    user_dir = os.path.join(root, user)
    if not os.path.isdir(user_dir):
        return {"purged": 0, "dbs_deleted": 0, "months_deleted": 0}
    cutoff = time.time() - (ttl_days * 86400)
    purged = 0
    dbs_deleted = 0
    months_deleted = 0
    for ym in sorted(os.listdir(user_dir)):
        ym_dir = os.path.join(user_dir, ym)
        if not os.path.isdir(ym_dir):
            continue
        for f in sorted(os.listdir(ym_dir)):
            if not f.endswith(".db"):
                continue
            db_path = os.path.join(ym_dir, f)
            try:
                conn = eng.open_db(db_path)
            except Exception:
                continue
            try:
                purged += int(eng.purge_older_than(conn, cutoff) or 0)
                # Empty? Drop the file.
                row = conn.execute(
                    "SELECT COUNT(*) FROM recall_entries"
                ).fetchone()
                empty = bool(row and row[0] == 0)
            finally:
                conn.close()
            if empty:
                # Also drop WAL/SHM siblings.
                for sib in (db_path,
                            db_path + "-wal", db_path + "-shm"):
                    try:
                        os.remove(sib)
                    except OSError:
                        pass
                dbs_deleted += 1
        # Empty month dir? Drop it.
        try:
            if not os.listdir(ym_dir):
                os.rmdir(ym_dir)
                months_deleted += 1
        except OSError:
            pass
    return {"purged": purged,
            "dbs_deleted": dbs_deleted,
            "months_deleted": months_deleted}


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qdistro-recall-reaper")
    p.add_argument("--root", default=None,
                   help="root dir (default $QDISTRO_RECALL_ROOT or "
                        "/var/lib/qdistro/recall)")
    p.add_argument("--user", default=None,
                   help="silo name (default $USER)")
    p.add_argument("--ttl-days", type=int, default=30)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = args.root \
        or os.environ.get("QDISTRO_RECALL_ROOT") \
        or "/var/lib/qdistro/recall"
    user = args.user or getpass.getuser()
    stats = reap(root, user, args.ttl_days)
    print(f"[reaper] root={root} user={user} "
          f"purged={stats['purged']} "
          f"dbs_deleted={stats['dbs_deleted']} "
          f"months_deleted={stats['months_deleted']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
