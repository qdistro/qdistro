"""qdistro-recall — host CLI for spec/17 §step 0 MVP.

Subcommands:

- ``push <text> [--source bridge|sdk|wm] [--app-id ...] [--url ...]``
- ``search <query> [--user ...] [--limit N] [--root /path]``
- ``info`` — print resolved DB path + row count for today.
- ``purge --older-than-days N`` — delete entries older than N days.

The CLI runs as the invoking user; the per-day DB layout is the
same as the SDK + bridge dispatch use, so all three producers
agree on a single source of truth.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

# Engine import mirrors qdistro_app.recall — try installed path
# first, fall back to in-tree.
def _load_engine():
    try:
        import qdistro_recall_ingest as eng  # type: ignore
        return eng
    except ImportError:
        pass
    candidates = [
        "/usr/libexec/qdistro/qdistro_recall_ingest.py",
        os.path.join(os.path.dirname(__file__),
                     "..", "recall",
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


def _resolve_root(arg_root: str | None) -> str:
    if arg_root:
        return arg_root
    env = os.environ.get("QDISTRO_RECALL_ROOT", "").strip()
    if env:
        return env
    return "/var/lib/qdistro/recall"


def _open_for_today(root: str, user: str):
    eng = _load_engine()
    path = eng.db_path_for(root, user)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return eng, eng.open_db(path), path


def _walk_dbs(root: str, user: str | None) -> list[str]:
    """Return every recall DB path under root, optionally filtered
    by user. The DB layout is <root>/<user>/<YYYY-MM>/<day>.db so we
    scan two levels down.
    """
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    user_dirs = [user] if user else [
        d for d in sorted(os.listdir(root))
        if os.path.isdir(os.path.join(root, d))]
    for u in user_dirs:
        u_dir = os.path.join(root, u)
        if not os.path.isdir(u_dir):
            continue
        for ym in sorted(os.listdir(u_dir)):
            ym_dir = os.path.join(u_dir, ym)
            if not os.path.isdir(ym_dir):
                continue
            for f in sorted(os.listdir(ym_dir)):
                if f.endswith(".db"):
                    out.append(os.path.join(ym_dir, f))
    return out


def cmd_push(args) -> int:
    user = args.user or getpass.getuser()
    root = _resolve_root(args.root)
    eng, conn, path = _open_for_today(root, user)
    try:
        rowid = eng.push_text(
            conn, user=user, text=args.text, source=args.source,
            app_id=args.app_id, exe=args.exe, secctx=args.secctx,
            url=args.url, title=args.title)
    except eng.PwdDomainRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 3
    finally:
        conn.close()
    print(f"OK row={rowid} path={path}")
    return 0


def cmd_search(args) -> int:
    eng = _load_engine()
    root = _resolve_root(args.root)
    paths = _walk_dbs(root, args.user)
    if not paths:
        print("(no recall DBs)", file=sys.stderr)
        return 0
    hits: list[dict] = []
    for p in paths:
        conn = eng.open_db(p)
        try:
            for row in eng.search(conn, args.query,
                                  user=args.user, limit=args.limit):
                row["_db"] = p
                hits.append(row)
        except Exception as e:
            print(f"warn: {p}: {e}", file=sys.stderr)
        finally:
            conn.close()
    hits.sort(key=lambda r: r.get("ts", 0), reverse=True)
    hits = hits[:args.limit]
    if args.json:
        print(json.dumps(hits, indent=2, default=str))
    else:
        for h in hits:
            ts = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.gmtime(h.get("ts") or 0))
            head = (h.get("text") or "")[:80].replace("\n", " ")
            print(f"{ts} [{h.get('source')}] {head}")
    return 0


def cmd_info(args) -> int:
    eng = _load_engine()
    root = _resolve_root(args.root)
    user = args.user or getpass.getuser()
    path = eng.db_path_for(root, user)
    print(f"root={root}")
    print(f"user={user}")
    print(f"today_db={path}")
    if os.path.isfile(path):
        conn = eng.open_db(path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM recall_entries").fetchone()[0]
            print(f"today_count={n}")
        finally:
            conn.close()
    else:
        print("today_count=0 (no DB yet)")
    return 0


def cmd_purge(args) -> int:
    eng = _load_engine()
    root = _resolve_root(args.root)
    cutoff = time.time() - (args.older_than_days * 86400)
    paths = _walk_dbs(root, args.user)
    total = 0
    for p in paths:
        conn = eng.open_db(p)
        try:
            n = eng.purge_older_than(conn, cutoff)
            total += int(n or 0)
        finally:
            conn.close()
    print(f"purged={total}")
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qdistro-recall",
        description="Phase-8 MVP recall ingest + search CLI.")
    # Cheap, side-effect-free health smoke (no DB, no root, no bus). The
    # action="version" path prints + exits 0 before subcommand dispatch.
    p.add_argument("--version", action="version",
                   version="%(prog)s (qdistro)")
    p.add_argument("--root", default=None,
                   help="recall DB root "
                        "(default $QDISTRO_RECALL_ROOT or /var/lib/qdistro/recall)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="ingest one text snapshot")
    p_push.add_argument("text", help="snapshot text")
    p_push.add_argument("--user", default=None,
                        help="silo name (default $USER)")
    p_push.add_argument("--source", default="sdk",
                        choices=("sdk", "bridge", "wm"))
    p_push.add_argument("--app-id", default=None)
    p_push.add_argument("--exe", default=None)
    p_push.add_argument("--secctx", default=None)
    p_push.add_argument("--url", default=None)
    p_push.add_argument("--title", default=None)
    p_push.set_defaults(fn=cmd_push)

    p_search = sub.add_parser("search", help="FTS5 search")
    p_search.add_argument("query")
    p_search.add_argument("--user", default=None,
                          help="filter by user (default: all silos)")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(fn=cmd_search)

    p_info = sub.add_parser("info", help="print resolved paths + counts")
    p_info.add_argument("--user", default=None)
    p_info.set_defaults(fn=cmd_info)

    p_purge = sub.add_parser("purge", help="delete old entries")
    p_purge.add_argument("--older-than-days", type=int, default=30)
    p_purge.add_argument("--user", default=None)
    p_purge.set_defaults(fn=cmd_purge)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
