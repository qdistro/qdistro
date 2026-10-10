#!/usr/bin/env python3
"""Bind an extracted image to the run's captured source pins and build inputs."""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# One pin: the qdistro monorepo (the components are in-tree and covered by
# its commit). Before the monorepo migration there were five sibling pins.
REPOS = {"qdistro"}


def image_file(root, name):
    """Resolve absolute symlinks in the image namespace, never on the host."""
    root = Path(root).resolve(strict=True)
    current, pending, hops = root, list(Path(name).parts), 0
    while pending:
        part = pending.pop(0)
        if part in ("/", "."):
            continue
        if part == "..":
            if current != root:
                current = current.parent
            continue
        candidate = current / part
        if candidate.is_symlink():
            hops += 1
            if hops > 40:
                raise ValueError("too many image symlinks")
            target = candidate.readlink()
            if target.is_absolute():
                current = root
            pending = list(target.parts) + pending
        else:
            if not candidate.exists():
                raise ValueError(f"image path missing: {candidate}")
            current = candidate
    if not current.is_file():
        raise ValueError(f"image path is not a regular file: {current}")
    return current


def pinned_snapshot(path):
    """The one snapshot= line of the repo-root snapshot.conf."""
    values = [line.split("=", 1)[1] for line in Path(path).read_text().splitlines()
              if line.startswith("snapshot=")]
    if len(values) != 1 or not re.fullmatch(r"\d{8}", values[0]):
        raise ValueError(f"{path} does not pin one snapshot")
    return values[0]


def verify(manifest, release, config, profile, snapshot_conf):
    pins = {}
    for line in Path(manifest).read_text().splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        repo = fields[0]
        if repo not in REPOS:
            continue
        if repo in pins or len(fields) < 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[1]):
            raise ValueError(f"invalid/duplicate expected pin: {repo}")
        pins[repo] = fields[1]
    if pins.keys() != REPOS:
        raise ValueError(f"expected manifest missing components: {sorted(REPOS - pins.keys())}")
    tree = ET.parse(config).getroot()
    snapshot = pinned_snapshot(snapshot_conf)
    version = tree.findtext("preferences/version")
    expected = {"VERSION": version, "SNAPSHOT": snapshot, "PROFILE": profile,
                "ARTIFACT": f"qdistro-{version}-{snapshot}.raw.xz"}
    observed, sources = {}, {}
    for line in Path(release).read_text().splitlines():
        if line.startswith("SOURCE "):
            fields = line.split()
            if len(fields) != 4 or fields[1] not in REPOS or fields[1] in sources or fields[3] != "clean":
                raise ValueError(f"image must have exactly one clean SOURCE line per repository: {line}")
            sources[fields[1]] = fields[2]
        elif "=" in line:
            key, value = line.split("=", 1)
            if key in observed:
                raise ValueError(f"duplicate image field: {key}")
            observed[key] = value
    failures = []
    for key, value in expected.items():
        print(f"{key}: expected={value} observed={observed.get(key)}")
        if observed.get(key) != value:
            failures.append(key)
    for repo, pin in sorted(pins.items()):
        print(f"SOURCE {repo}: expected={pin} clean observed={sources.get(repo)}")
        if sources.get(repo) != pin:
            failures.append(repo)
    if failures:
        raise ValueError(f"image identity mismatch: {', '.join(failures)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("image_root")
    parser.add_argument("config")
    parser.add_argument("--profile", choices=("dev", "release"), required=True)
    parser.add_argument("--snapshot-conf",
                        help="snapshot pin (default: snapshot.conf beside the image/ directory holding config)")
    args = parser.parse_args()
    snapshot_conf = args.snapshot_conf or Path(args.config).resolve().parent.parent / "snapshot.conf"
    try:
        verify(args.manifest, image_file(args.image_root, "/etc/qdistro/release"), args.config, args.profile,
               snapshot_conf)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: image matches captured release identity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
