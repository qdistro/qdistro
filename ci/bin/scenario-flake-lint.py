#!/usr/bin/env python3
"""Flake-smell linter for qdistro GUI scenarios (WARN-ONLY by default).

Scans markdown GUI scenarios (and shell driver scripts) for the patterns that
the hardening research identified as the recurring flake sources, so they can be
found and migrated to the bounded waiter library (ci/lib/guest/gui-waiters.sh):

  sleep-before-assert   a whole-second `sleep N` immediately before an assertion
                        (a bet on host load; use a readiness gate instead)
  unscoped-journal      `journalctl --since ...` without --after-cursor/--cursor
                        (a stale pre-action line can satisfy it)
  oneshot-systemctl     a single `systemctl is-active` not inside a poll/await
  oneshot-domstate      a single `virsh domstate` not inside a poll/await
  virsh-head-vm-select  `virsh ... | head` to pick a VM (races a parallel run)
  prose-only-assert     a scenario whose code blocks contain NO shell assertion
                        at all (the verdict rests entirely on the LLM oracle)

This is REPORTING ONLY: it flags smells to drive migration; it does NOT weaken or
rewrite anything, and exits 0 unless --strict is given. It is the lint-side
companion to the waiter library — as scenarios migrate, the finding count drops.

Usage:
    scenario-flake-lint.py [PATH ...] [--format gcc|summary] [--strict]
Default PATHs: the GUI scenario + driver trees under the qdistro repo.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Per-line smell patterns. Each: (rule, compiled-regex, message). Loop/await
# context suppression is handled separately for the one-shot rules.
SLEEP_RE = re.compile(r"^\s*sleep\s+[1-9][0-9]*(\s|;|$)")
JOURNAL_RE = re.compile(r"\bjournalctl\b")
JOURNAL_SINCE_RE = re.compile(r"--since\b")
JOURNAL_SCOPED_RE = re.compile(r"--(after-cursor|cursor)\b")
ISACTIVE_RE = re.compile(r"\bsystemctl\b.*\bis-active\b")
DOMSTATE_RE = re.compile(r"\bvirsh\b.*\bdomstate\b")
VIRSH_HEAD_RE = re.compile(r"\bvirsh\b.*\|\s*\S*\bhead\b")
# A line is "loop/await context" if it is part of a poll construct.
LOOP_CTX_RE = re.compile(r"\b(while|until|for|await_[a-z_]+|poll_until|retry)\b")
# Assertion-ish shell on a line (used for prose-only detection + sleep-before).
ASSERT_RE = re.compile(
    r"(\bgrep\b|\btest\b|^\s*\[\[?\s|\bassert\b|\bexit\s+[1-9]|\|\|\s*exit\b"
    r"|\bdiff\b|\bcmp\b|=~|-eq\b|-ne\b|\bawait_[a-z_]+\b)"
)


def fenced_bash_blocks(text: str) -> list[tuple[int, list[str]]]:
    """Return (start_line_1based, lines) for each ``` fenced block that looks
    like shell (```bash / ```sh / bare ```)."""
    blocks: list[tuple[int, list[str]]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*```(\w+)?\s*$", lines[i])
        if m and (m.group(1) in (None, "bash", "sh", "shell")):
            start = i + 2  # first content line, 1-based
            body: list[str] = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            blocks.append((start, body))
        i += 1
    return blocks


def lint_markdown(path: Path) -> list[tuple[int, str, str]]:
    """Return findings (line, rule, message) for one markdown scenario."""
    findings: list[tuple[int, str, str]] = []
    text = path.read_text(errors="replace")
    blocks = fenced_bash_blocks(text)
    any_assert = False
    has_code = False
    for start, body in blocks:
        has_code = has_code or bool(body)
        for off, raw in enumerate(body):
            lineno = start + off
            line = raw.split("#", 1)[0]  # ignore trailing comments for matching
            if not line.strip():
                continue
            if ASSERT_RE.search(line):
                any_assert = True
            ctx = "\n".join(body[max(0, off - 2): off + 3])
            looped = bool(LOOP_CTX_RE.search(ctx))
            if SLEEP_RE.search(line):
                # Only a smell when an assertion follows soon after.
                following = body[off + 1: off + 4]
                if any(ASSERT_RE.search(f.split("#", 1)[0]) for f in following):
                    findings.append((lineno, "sleep-before-assert",
                                     "fixed sleep immediately before an assertion; "
                                     "use a waiter (await_*) instead"))
            if JOURNAL_RE.search(line) and JOURNAL_SINCE_RE.search(line) \
                    and not JOURNAL_SCOPED_RE.search(line):
                findings.append((lineno, "unscoped-journal",
                                 "journalctl --since without --after-cursor/--cursor; "
                                 "a stale pre-action line can satisfy it"))
            if ISACTIVE_RE.search(line) and not looped:
                findings.append((lineno, "oneshot-systemctl",
                                 "one-shot systemctl is-active; poll with "
                                 "await_user_unit_active"))
            if DOMSTATE_RE.search(line) and not looped:
                findings.append((lineno, "oneshot-domstate",
                                 "one-shot virsh domstate; poll with await_domstate"))
            if VIRSH_HEAD_RE.search(line):
                findings.append((lineno, "virsh-head-vm-select",
                                 "virsh ... | head selects a VM by position; races a "
                                 "parallel run — select by explicit name"))
    if has_code and not any_assert:
        findings.append((1, "prose-only-assert",
                         "scenario has shell blocks but NO shell assertion; the "
                         "verdict rests entirely on the LLM oracle"))
    return findings


def default_paths(repo: Path) -> list[Path]:
    roots = [
        repo / "tests/integration/permissions-gui",
        repo / "tests/integration/qdwin-noctalia",
    ]
    # Sibling repos' GUI scenarios live next to the qdistro checkout.
    ws = repo.parent
    for sib in ("qdwin/tests/gui", "qdwin/tests/apps", "qdlocker/tests/gui"):
        roots.append(ws / sib)
    out: list[Path] = []
    for r in roots:
        if r.is_dir():
            out.extend(sorted(r.glob("[0-9][0-9]-*.md")))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="markdown scenarios to lint")
    ap.add_argument("--format", choices=("gcc", "summary"), default="gcc")
    ap.add_argument("--strict", action="store_true",
                    help="exit nonzero when findings exist (default: warn-only, exit 0)")
    args = ap.parse_args(argv)

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        repo = Path(__file__).resolve().parents[1].parent  # ci/bin -> repo root
        targets = default_paths(repo)

    total = 0
    by_rule: dict[str, int] = {}
    for path in targets:
        if not path.is_file():
            continue
        for lineno, rule, msg in lint_markdown(path):
            total += 1
            by_rule[rule] = by_rule.get(rule, 0) + 1
            if args.format == "gcc":
                print(f"{path}:{lineno}: {rule}: {msg}")

    if args.format == "summary" or total:
        print(f"\nscenario-flake-lint: {total} finding(s) across {len(targets)} file(s)",
              file=sys.stderr)
        for rule in sorted(by_rule):
            print(f"  {rule}: {by_rule[rule]}", file=sys.stderr)

    if args.strict and total:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
