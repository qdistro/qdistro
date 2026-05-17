"""qdistro-recall-admin — admin CLI for cross-uid recall + browser
state.

Subcommands:

- ``containers --uid N``  Firefox containers for that user's browser
- ``tabs --uid N``        open tabs in that user's browser
- ``search <query> --uid N [--silo S]``
                          FTS recall search, with each row annotated
                          by the matching live tab (if any) via
                          ``recall_admin.annotate_with_live_tabs``

The first two are thin pretty-printers; the third is the
load-bearing one — historical recall results joined to live browser
state in a single command.

Authorization: this CLI calls
``org.qdistro.UserRelay.uid<NNNN>.ForwardBrowserBridgeOp`` on the
system bus, which is restricted to root by the relay's
``org.qdistro.UserRelay.conf`` policy. It also reads recall DBs
under ``/var/lib/qdistro/recall/`` which are root-owned. Therefore
the CLI requires root; it fails closed otherwise so the failure mode
isn't a confusing D-Bus AccessDenied. There is no env-var escape
hatch — tests monkey-patch :func:`_require_root` directly.

Exit codes:
  0  success
  1  command/usage error or relay/bridge failure for read subcommands
  2  search annotation failed (recall results still printed) OR
     --silo filter matched zero silos OR FTS query malformed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Lazy engine import — mirrors qdistro_recall_cli's resolution so
# tests can drop a fake on sys.path.
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


# Importable separately so tests can monkey-patch.
def _load_admin():
    import qdistro_recall_admin as ra  # type: ignore
    return ra


def _resolve_root(arg_root: str | None) -> str:
    if arg_root:
        return arg_root
    env = os.environ.get("QDISTRO_RECALL_ROOT", "").strip()
    if env:
        return env
    return "/var/lib/qdistro/recall"


def _require_root() -> None:
    if os.geteuid() != 0:
        print(
            "qdistro-recall-admin: must be run as root "
            "(reads /var/lib/qdistro/recall and calls the system-bus "
            "user-relay)",
            file=sys.stderr)
        sys.exit(1)


def _validate_silo(silo: str) -> None:
    """Reject silo names that would traverse outside <root>/<silo>/.
    Silos are silo-name-shaped (alnum + `-_`), never paths.
    """
    if not silo or "/" in silo or ".." in silo or silo.startswith("."):
        raise ValueError(f"bad silo name: {silo!r}")


def _walk_dbs(root: str, silo: str | None) -> list[str]:
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    if silo is not None:
        _validate_silo(silo)
    silo_dirs = [silo] if silo else [
        d for d in sorted(os.listdir(root))
        if os.path.isdir(os.path.join(root, d))]
    for s in silo_dirs:
        s_dir = os.path.join(root, s)
        if not os.path.isdir(s_dir):
            continue
        for ym in sorted(os.listdir(s_dir)):
            ym_dir = os.path.join(s_dir, ym)
            if not os.path.isdir(ym_dir):
                continue
            for f in sorted(os.listdir(ym_dir)):
                if f.endswith(".db"):
                    out.append(os.path.join(ym_dir, f))
    return out


# ---- subcommand: containers ---------------------------------------

def cmd_containers(args) -> int:
    ra = _load_admin()
    reply = ra.list_user_containers(args.uid)
    if args.json:
        print(json.dumps(reply, indent=2))
        return 0 if reply.get("ok") else 1
    if not reply.get("ok"):
        print(f"error: {reply.get('error')} "
              f"detail={reply.get('detail') or ''}",
              file=sys.stderr)
        return 1
    containers = reply.get("containers") or []
    if not containers:
        print("(no containers)")
        return 0
    for c in containers:
        print(f"{c.get('cookie_store_id', '?'):30}  "
              f"{c.get('name', '')}  "
              f"[{c.get('color', '')}/{c.get('icon', '')}]")
    return 0


# ---- subcommand: tabs ---------------------------------------------

def cmd_tabs(args) -> int:
    ra = _load_admin()
    reply = ra.list_user_tabs(args.uid)
    if args.json:
        print(json.dumps(reply, indent=2))
        return 0 if reply.get("ok") else 1
    if not reply.get("ok"):
        print(f"error: {reply.get('error')} "
              f"detail={reply.get('detail') or ''}",
              file=sys.stderr)
        return 1
    tabs = reply.get("tabs") or []
    if not tabs:
        print("(no tabs)")
        return 0
    for t in tabs:
        active = "*" if t.get("active") else " "
        tid = t.get("id", "?")
        win = t.get("window_id", "?")
        url = t.get("url", "")
        title = t.get("title", "")
        print(f"{active} w{win} t{tid}  {url}  {title}")
    return 0


# ---- subcommand: search -------------------------------------------

def cmd_search(args) -> int:
    eng = _load_engine()
    ra = _load_admin()
    root = _resolve_root(args.root)
    try:
        paths = _walk_dbs(root, args.silo)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not paths:
        # Distinguish "operator typo on --silo" from "no recall yet".
        if args.silo is not None:
            print(f"error: silo {args.silo!r} has no recall DBs under "
                  f"{root!r}", file=sys.stderr)
            return 2
        print("(no recall DBs)", file=sys.stderr)
        return 0
    hits: list[dict] = []
    fts_error: str | None = None
    for p in paths:
        try:
            conn = eng.open_db(p)
        except Exception as e:
            print(f"warn: open {p}: {e}", file=sys.stderr)
            continue
        try:
            try:
                rows = eng.search(conn, args.query,
                                  user=args.silo, limit=args.limit)
            except Exception as e:
                # FTS5 errors look the same across DBs — a malformed
                # MATCH query fails everywhere. Surface it once and
                # stop rather than spamming N warn lines.
                if _is_fts_error(e):
                    fts_error = str(e)
                    break
                print(f"warn: search {p}: {e}", file=sys.stderr)
                continue
            for row in rows:
                row["_db"] = p
                hits.append(row)
        finally:
            conn.close()
    if fts_error is not None:
        print(f"error: malformed FTS query: {fts_error}",
              file=sys.stderr)
        return 2
    hits.sort(key=lambda r: r.get("ts", 0), reverse=True)
    hits = hits[:args.limit]
    annotated = ra.annotate_with_live_tabs(hits, args.uid)
    rows = annotated.get("rows", hits)
    live_ok = bool(annotated.get("ok"))
    live_status = {
        "ok": live_ok,
        "error": annotated.get("error") if not live_ok else None,
        "detail": annotated.get("detail") if not live_ok else None,
    }
    if args.json:
        out = {"ok": True, "rows": rows, "live": live_status}
        print(json.dumps(out, indent=2, default=str))
        return 0 if live_ok else 2
    if not live_ok:
        print(f"warn: live-tab annotation unavailable "
              f"({live_status['error']})", file=sys.stderr)
    for h in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.gmtime(h.get("ts") or 0))
        head = (h.get("text") or "")[:60].replace("\n", " ")
        url = h.get("url") or ""
        live = h.get("live_tab")
        live_marker = (f"  [live: w{live.get('window_id')} "
                       f"t{live.get('id')}]") if live else ""
        print(f"{ts} [{h.get('source')}] {url}  {head}{live_marker}")
    return 0 if live_ok else 2


def _is_fts_error(e: Exception) -> bool:
    """Best-effort sniff for an FTS5-syntax-level failure. Matches the
    sqlite3.OperationalError shapes we've actually seen
    (`"fts5: syntax error..."` and `"malformed MATCH expression"`).
    """
    msg = str(e).lower()
    return ("fts5" in msg
            or "match expression" in msg
            or "no such cursor" in msg)


# ---- argparse + main ----------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qdistro-recall-admin",
        description="Admin CLI for cross-uid recall + live browser state.")
    p.add_argument("--root", default=None,
                   help="recall DB root "
                        "(default $QDISTRO_RECALL_ROOT or /var/lib/qdistro/recall)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("containers",
                         help="list Firefox containers for a user's browser")
    p_c.add_argument("--uid", type=int, required=True,
                     help="target user uid")
    p_c.add_argument("--json", action="store_true")
    p_c.set_defaults(fn=cmd_containers)

    p_t = sub.add_parser("tabs",
                         help="list open tabs in a user's browser")
    p_t.add_argument("--uid", type=int, required=True,
                     help="target user uid")
    p_t.add_argument("--json", action="store_true")
    p_t.set_defaults(fn=cmd_tabs)

    p_s = sub.add_parser(
        "search",
        help="FTS recall search joined to the user's live tabs")
    p_s.add_argument("query")
    p_s.add_argument("--uid", type=int, required=True,
                     help="target user uid (for live-tab join via "
                          "the system-bus relay)")
    p_s.add_argument("--silo", default=None,
                     help="filter recall results by silo name "
                          "(the recall DB layout key; default: all "
                          "silos). Distinct from --uid because the "
                          "relay keys on uid while recall DBs key on "
                          "silo name.")
    p_s.add_argument("--limit", type=int, default=50)
    p_s.add_argument("--json", action="store_true")
    p_s.set_defaults(fn=cmd_search)

    return p


def main(argv: list[str] | None = None) -> int:
    # No env-var escape hatch. Tests monkey-patch _require_root
    # directly; production runs through it.
    _require_root()
    args = _build_argparser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
