#!/usr/bin/env python3
"""Bind an extracted image to the run's captured source pins and build inputs."""
import argparse
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

REPOS = {"qdistro", "qdwin", "qdshell", "qdgreeter", "qdlocker"}


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


def verify(manifest, release, config, profile):
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
    snapshots = [re.search(r"/history/(\d{8})/", r.get("path", ""))
                 for r in tree.findall("repository/source")]
    if len(snapshots) != 2 or any(s is None for s in snapshots) or len({s[1] for s in snapshots}) != 1:
        raise ValueError("expected config does not pin one snapshot")
    version = tree.findtext("preferences/version")
    expected = {"VERSION": version, "SNAPSHOT": snapshots[0][1], "PROFILE": profile,
                "ARTIFACT": f"qdistro-{version}-{snapshots[0][1]}.raw.xz"}
    observed, sources = {}, {}
    for line in Path(release).read_text().splitlines():
        if line.startswith("SOURCE "):
            fields = line.split()
            if len(fields) != 4 or fields[1] not in REPOS or fields[1] in sources or fields[3] != "clean":
                raise ValueError(f"image must have exactly one clean source per component: {line}")
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
    args = parser.parse_args()
    try:
        verify(args.manifest, image_file(args.image_root, "/etc/qdistro/release"), args.config, args.profile)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: image matches captured release identity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
