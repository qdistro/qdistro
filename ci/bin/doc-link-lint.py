#!/usr/bin/env python3
"""Validate local Markdown links in qdistro's maintained documentation."""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit


LINK_RE = re.compile(
    r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)"
)
EXPLICIT_ANCHOR_RE = re.compile(
    r"<(?:a|span)\s+[^>]*(?:id|name)=[\"']([^\"']+)[\"'][^>]*>", re.I
)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def markdown_files(root: Path) -> list[Path]:
    files = [root / "README.md"]
    files.extend(sorted((root / "doc").rglob("*.md")))
    files.extend(sorted((root / "ci").glob("*.md")))
    return [path for path in files if path.is_file()]


def visible_lines(path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None:
            lines.append((number, line))
    return lines


def heading_slugs(text: str) -> set[str]:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        char
        for char in text
        if char.isalnum() or char in {" ", "\t", "-", "_"}
    )
    slug = re.sub(r"\s", "-", text.strip())
    return {slug, re.sub(r"-+", "-", slug)} if slug else set()


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    for _number, line in visible_lines(path):
        found.update(unquote(value) for value in EXPLICIT_ANCHOR_RE.findall(line))
        match = HEADING_RE.match(line)
        if match:
            found.update(heading_slugs(match.group(1)))
    return found


def local_target(source: Path, raw_target: str, root: Path) -> tuple[Path, str] | None:
    target = raw_target[1:-1] if raw_target.startswith("<") else raw_target
    target = target.replace("\\ ", " ")
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("//"):
        return None
    path_text = unquote(parsed.path)
    if not path_text:
        destination = source
    elif path_text.startswith("/"):
        destination = root / path_text.lstrip("/")
    else:
        destination = source.parent / path_text
    return destination.resolve(), unquote(parsed.fragment)


def check(root: Path) -> list[str]:
    findings: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source in markdown_files(root):
        for number, line in visible_lines(source):
            for match in LINK_RE.finditer(line):
                resolved = local_target(source, match.group("target"), root)
                if resolved is None:
                    continue
                destination, fragment = resolved
                rel_source = source.relative_to(root)
                if not destination.exists():
                    findings.append(
                        f"{rel_source}:{number}: missing local link target: "
                        f"{match.group('target')}"
                    )
                    continue
                if fragment and destination.is_file() and destination.suffix.lower() == ".md":
                    known = anchor_cache.setdefault(destination, anchors(destination))
                    if fragment not in known:
                        findings.append(
                            f"{rel_source}:{number}: missing Markdown anchor "
                            f"#{fragment} in {destination.relative_to(root)}"
                        )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not (root / "doc").is_dir() or not (root / "ci").is_dir():
        parser.error("--root must be the qdistro repository root")
    findings = check(root)
    for finding in findings:
        print(finding)
    print(
        f"doc-link-lint: checked {len(markdown_files(root))} Markdown files; "
        f"{len(findings)} finding(s)"
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
