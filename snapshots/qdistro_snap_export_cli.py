"""qdistro-snap-export — host CLI wrapping render_backup_command.

Per spec/19 §"Encryption pipeline" + §"Phase-8 MVP scope". Drives a
``btrfs send | rage -e | ssh`` pipeline against a snapshot path
plus an admin-supplied recipients file.

Usage:

  qdistro-snap-export run \\
      --snap /.snapshots/42/snapshot \\
      --parent /.snapshots/41/snapshot \\
      --recipients /etc/qdistro/backup-recipients.txt \\
      --ssh user@backup-host \\
      --remote 'snap-42.btrfs.age'

  qdistro-snap-export print-cmd  ...     # render only, no execution

  qdistro-snap-export check-recipients /etc/qdistro/backup-recipients.txt

The CLI does NOT shell out to btrfs/rage/ssh in `print-cmd` mode —
that's the dry-run path the bats harness drives. `run` calls
``subprocess.run(check=True)`` on the pipeline returned by
``render_backup_command``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


# Engine import — try installed path first, fall back to in-tree.
def _load_eng():
    try:
        import qdistro_snapshots as eng  # type: ignore
        return eng
    except ImportError:
        pass
    candidates = [
        "/usr/libexec/qdistro/qdistro_snapshots.py",
        os.path.join(os.path.dirname(__file__),
                     "qdistro_snapshots.py"),
    ]
    import importlib.util
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(
                "qdistro_snapshots", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["qdistro_snapshots"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("qdistro_snapshots not found")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qdistro-snap-export")
    # Cheap, side-effect-free health smoke (no btrfs, no ssh, no root).
    p.add_argument("--version", action="version",
                   version="%(prog)s (qdistro)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser(
        "run", help="execute the btrfs send | rage -e | ssh pipeline")
    for q in (p_run, sub.add_parser(
            "print-cmd",
            help="render the pipeline as JSON (no execution)")):
        q.add_argument("--snap", required=True,
                       help="path to a read-only btrfs snapshot")
        q.add_argument("--parent", default=None,
                       help="parent snapshot for incremental send "
                            "(omit for full send)")
        q.add_argument("--recipients", required=True,
                       help="path to backup-recipients.txt "
                            "(one age1... per line)")
        q.add_argument("--ssh", required=True,
                       help="ssh target (user@host or alias)")
        q.add_argument("--remote", required=True,
                       help="remote file path (single-quoted on the "
                            "remote shell)")
        q.set_defaults(fn=cmd_run if q is p_run else cmd_print)

    p_check = sub.add_parser(
        "check-recipients",
        help="parse recipients file + print the canonical list")
    p_check.add_argument("path")
    p_check.set_defaults(fn=cmd_check)

    return p


def cmd_run(args) -> int:
    eng = _load_eng()
    cmd = eng.render_backup_command(
        snap_path=args.snap,
        parent_snap_path=args.parent,
        recipients_file=args.recipients,
        ssh_target=args.ssh,
        remote_path=args.remote)
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


def cmd_print(args) -> int:
    import json
    eng = _load_eng()
    cmd = eng.render_backup_command(
        snap_path=args.snap,
        parent_snap_path=args.parent,
        recipients_file=args.recipients,
        ssh_target=args.ssh,
        remote_path=args.remote)
    print(json.dumps(cmd))
    return 0


def cmd_check(args) -> int:
    eng = _load_eng()
    try:
        with open(args.path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"open: {e}", file=sys.stderr)
        return 2
    rcpts = eng.parse_backup_recipients(text)
    if not rcpts:
        print("(no recipients)", file=sys.stderr)
        return 1
    for r in rcpts:
        print(r)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
