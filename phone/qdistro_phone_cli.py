"""qdistro-phone — host CLI for spec/18 Phase-8 MVP.

Subcommands:

- ``pair <phone-id> [--trust limited|trusted|full]
  [--feature key=value ...]`` — write a per-phone .conf file at
  /etc/qdistro/phone/<phone-id>.conf.
- ``list`` — enumerate paired phones + trust levels.
- ``trust <phone-id> <level>`` — change a paired phone's trust level.
- ``unpair <phone-id>`` — delete the .conf file.
- ``decisions [--limit N]`` — dump the recent rows from the
  decisions queue.
- ``push --request-id <r> --action <a> --user <u> --phone <id>
  --secret <secret>`` — render a single approval push body
  (does NOT POST to ntfy; admin pipes through `curl` for that).

Refuses to write configs outside the configured config dir (so a
``--phone-id ../../etc/foo`` can't escape).
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _load_phone():
    try:
        import qdistro_phone as ph  # type: ignore
        return ph
    except ImportError:
        pass
    candidates = [
        "/usr/libexec/qdistro/qdistro_phone.py",
        os.path.join(os.path.dirname(__file__), "qdistro_phone.py"),
    ]
    import importlib.util
    for p in candidates:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location(
                "qdistro_phone", p)
            m = importlib.util.module_from_spec(spec)
            sys.modules["qdistro_phone"] = m
            spec.loader.exec_module(m)
            return m
    raise ImportError("qdistro_phone not found")


def _load_daemon():
    try:
        import qdistro_phone_daemon as pd  # type: ignore
        return pd
    except ImportError:
        pass
    candidates = [
        "/usr/libexec/qdistro/qdistro_phone_daemon.py",
        os.path.join(os.path.dirname(__file__),
                     "qdistro_phone_daemon.py"),
    ]
    import importlib.util
    for p in candidates:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location(
                "qdistro_phone_daemon", p)
            m = importlib.util.module_from_spec(spec)
            sys.modules["qdistro_phone_daemon"] = m
            spec.loader.exec_module(m)
            return m
    raise ImportError("qdistro_phone_daemon not found")


DEFAULT_CONFIG_DIR = "/etc/qdistro/phone"


def cmd_pair(args) -> int:
    pd = _load_daemon()
    trust: dict = {"trust": args.trust}
    for kv in (args.feature or []):
        if "=" not in kv:
            print(f"feature must be key=value, got {kv!r}",
                  file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        trust[k.strip()] = v.strip()
    path = pd.save_phone_conf(args.config_dir, args.phone_id, trust)
    print(f"OK paired phone={args.phone_id} path={path}")
    return 0


def cmd_list(args) -> int:
    pd = _load_daemon()
    confs = pd.load_phone_configs(args.config_dir)
    if args.json:
        print(json.dumps(confs, indent=2, sort_keys=True))
        return 0
    if not confs:
        print("(no paired phones)")
        return 0
    for phone_id in sorted(confs):
        body = confs[phone_id]
        trust = body.get("trust", "?")
        print(f"{phone_id}  trust={trust}  features={body}")
    return 0


def cmd_trust(args) -> int:
    pd = _load_daemon()
    confs = pd.load_phone_configs(args.config_dir)
    body = confs.get(args.phone_id)
    if body is None:
        print(f"unknown phone: {args.phone_id}", file=sys.stderr)
        return 2
    body["trust"] = args.level
    pd.save_phone_conf(args.config_dir, args.phone_id, body)
    print(f"OK trust phone={args.phone_id} -> {args.level}")
    return 0


def cmd_unpair(args) -> int:
    if "/" in args.phone_id or args.phone_id.startswith("."):
        print("invalid phone-id", file=sys.stderr); return 2
    path = os.path.join(args.config_dir, f"{args.phone_id}.conf")
    if not os.path.isfile(path):
        print(f"no such phone: {path}", file=sys.stderr); return 2
    os.remove(path)
    print(f"OK unpaired phone={args.phone_id}")
    return 0


def cmd_decisions(args) -> int:
    pd = _load_daemon()
    if not os.path.isfile(args.queue_path):
        print("(no decisions queue yet)")
        return 0
    conn = pd.open_queue(args.queue_path)
    rows = conn.execute(
        "SELECT id, ts, request_id, decision, expires_at, phone_id "
        "FROM phone_decisions ORDER BY ts DESC LIMIT ?",
        (int(args.limit),)
    ).fetchall()
    conn.close()
    cols = ("id", "ts", "request_id", "decision", "expires_at", "phone_id")
    out = [dict(zip(cols, r, strict=True)) for r in rows]
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for r in out:
            print(f"#{r['id']} {r['decision']:5s} req={r['request_id']} "
                  f"exp={r['expires_at']} phone={r['phone_id']}")
    return 0


def cmd_push(args) -> int:
    ph = _load_phone()
    body = ph.build_approval_push(
        request_id=args.request_id,
        action_id=args.action,
        user=args.user,
        title=args.title,
        body=args.body,
        callback_base_url=args.callback_base,
        callback_secret=args.secret.encode(),
        ttl_seconds=int(args.ttl),
    )
    print(json.dumps(body, indent=2))
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qdistro-phone")
    # Cheap, side-effect-free health smoke (no config, no bus, no root).
    p.add_argument("--version", action="version",
                   version="%(prog)s (qdistro)")
    p.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pair")
    pp.add_argument("phone_id")
    pp.add_argument("--trust", default="limited",
                    choices=("limited", "trusted", "full"))
    pp.add_argument("--feature", action="append", default=None,
                    metavar="KEY=VALUE")
    pp.set_defaults(fn=cmd_pair)

    pl = sub.add_parser("list")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(fn=cmd_list)

    pt = sub.add_parser("trust")
    pt.add_argument("phone_id")
    pt.add_argument("level",
                    choices=("limited", "trusted", "full"))
    pt.set_defaults(fn=cmd_trust)

    pu = sub.add_parser("unpair")
    pu.add_argument("phone_id")
    pu.set_defaults(fn=cmd_unpair)

    pd = sub.add_parser("decisions")
    pd.add_argument("--queue-path",
                    default="/var/lib/qdistro/phone/decisions.sqlite")
    pd.add_argument("--limit", type=int, default=20)
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(fn=cmd_decisions)

    pp2 = sub.add_parser("push", help="render approval push body")
    pp2.add_argument("--request-id", required=True)
    pp2.add_argument("--action", required=True)
    pp2.add_argument("--user", required=True)
    pp2.add_argument("--title", default=None)
    pp2.add_argument("--body", default=None)
    pp2.add_argument("--callback-base", required=True)
    pp2.add_argument("--secret", required=True)
    pp2.add_argument("--ttl", type=int, default=120)
    pp2.set_defaults(fn=cmd_push)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
