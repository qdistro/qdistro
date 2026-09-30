"""qdistro-presentation-publish: validate stdin JSON and atomically replace current.json."""

from __future__ import annotations

import argparse
import os
import sys

from .model import (
    MAX_BYTES,
    SnapshotError,
    example_snapshot,
    loads_strict,
    normalize_producer,
    parse_snapshot,
)
from .paths import MANAGED_DIR, developer_state_file, managed_dir_exists
from .publish import write_disabled_envelope, write_snapshot


def _destination(explicit: str | None) -> str:
    if explicit:
        return explicit
    if managed_dir_exists():
        return MANAGED_DIR
    return os.path.dirname(developer_state_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-presentation-publish")
    parser.add_argument(
        "--dir",
        dest="directory",
        default=None,
        help="destination directory (default: managed path, else XDG state)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="write an enabled=false envelope from stdin or the built-in dark palette",
    )
    parser.add_argument(
        "--owner-uid",
        type=int,
        default=None,
        help="required owner of the destination directory and file",
    )
    args = parser.parse_args(argv)

    raw_b = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw_b) > MAX_BYTES:
        print("qdistro-presentation-publish: stdin exceeds 64 KiB", file=sys.stderr)
        return 1
    raw = raw_b.decode("utf-8")
    directory = _destination(args.directory)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    require_unwritable = os.path.abspath(directory) == os.path.abspath(MANAGED_DIR)
    try:
        if args.reset:
            if raw.strip():
                template = parse_snapshot(loads_strict(raw))
            else:
                template = example_snapshot()
            result = write_disabled_envelope(
                directory,
                template,
                owner_uid=args.owner_uid,
                require_unwritable_dirs=require_unwritable,
            )
        else:
            if not raw.strip():
                raise SnapshotError("stdin JSON is required")
            payload = loads_strict(raw)
            if "colors" in payload and "mode" in payload and "version" not in payload:
                snapshot = normalize_producer(
                    mode=payload.get("mode"),
                    colors=payload.get("colors"),
                    settings=payload.get("settings") or {},
                    enabled=bool(payload.get("enabled", True)),
                    default_ui_family=str(payload.get("default_ui_family") or "Sans Serif"),
                )
            else:
                snapshot = parse_snapshot(payload)
            result = write_snapshot(
                directory,
                snapshot,
                owner_uid=args.owner_uid,
                require_unwritable_dirs=require_unwritable,
            )
    except (SnapshotError, OSError, UnicodeDecodeError) as exc:
        print(f"qdistro-presentation-publish: {exc}", file=sys.stderr)
        return 1
    print(result.generation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
