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
  bare-relative-source  `source ./x` / `. ../lib/y` — a relative source path that
                        only resolves from one cwd (breaks when run elsewhere)
  pgrep-self-match      `pgrep -f PATTERN` without a bracket guard (`[x]`); the
                        pgrep can match its OWN argv → false orphan/hit
  unscoped-tmp-path     a WRITE to a fixed literal `/tmp/...` scratch path with no
                        scenario/run suffix — collides once lanes run in parallel;
                        route through `$QCI_SCENARIO_TMPDIR` / `mktemp`
  screenshot-only-assert  a scenario whose only evidence is a screenshot/OCR with
                        NO structured (IPC/journal/process) probe anywhere; keep a
                        pixel as corroboration, not the sole oracle
  backgrounded-wait     a readiness wait (`wait`/`await_*`/`sleep`) sent to the
                        background with `&` — the verdict can be collected before
                        the thing it waits for is ready

This is REPORTING ONLY: it flags smells to drive migration; it does NOT weaken or
rewrite anything, and exits 0 unless --strict is given. It is the lint-side
companion to the waiter library — as scenarios migrate, the finding count drops.

A documented allowlist (ci/scenario-flake-allow.tsv, TAB-separated
`path_substring  rule  reason`) waives known-acceptable findings so `--strict`
can be adopted: an allowed finding is reported separately and never fails strict.
Waive ONE rule per row (e.g. `screenshot-only-assert` for an explicit
visual-rendering test); reserve `rule=*` for a file wholly outside the lint
contract — a visual test can still have a real pgrep/tmp/source bug.

Usage:
    scenario-flake-lint.py [PATH ...] [--format gcc|summary] [--strict]
                           [--allowlist FILE] [--no-allowlist]
Default PATHs: the GUI scenario + driver trees under the qdistro repo.
"""
from __future__ import annotations

import argparse
import re
import shlex
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
# A relative `source`/`.` path: not absolute (/), not a variable ($), not $HOME
# (~), not a process substitution (<). Handles an optional matching quote so
# `source "./x.sh"` / `. '../lib/y.sh'` are caught too. Group `src` is the path.
BARE_SOURCE_RE = re.compile(
    r"""^\s*(?:source|\.)\s+(?P<q>["']?)(?P<src>(?![/~$<])[^"'\s;]+)(?P=q)(?:\s|;|$)"""
)
# `pgrep` command segments — inspected token-wise (shlex) rather than via one
# fragile mega-regex, so `pgrep -u "$USER" -f X` / `pgrep -a -f X` / a quoted
# multi-word pattern are all handled (see pgrep_f_patterns). The lookbehind
# matches pgrep anywhere it is a real command word — crucially INSIDE a quoted
# remote-exec string (`$VMEXEC "$VM" 'pgrep -f X | head'`), the common form here,
# not just at line start. Args stop at a command separator OR a redirect so the
# final token is the PATTERN operand, not a redirect target.
PGREP_CMD_RE = re.compile(r"(?<![\w-])pgrep\b(?P<args>[^;&|<>\n#]*)")
# A WRITE to a fixed /tmp path: redirect (`>`/`>>`) or `tee`. Group holds the
# /tmp path token. A bare `NAME=/tmp/...` assignment is deliberately NOT a write
# — scenarios legitimately set hostile env vars (LD_PRELOAD=/tmp/evil.so, a
# security test) that are not scratch. Dynamic paths ($…) are caller-excluded.
_TMP_PATH = r"(/tmp/[^\s;'\"|)&<>]+)"
TMP_REDIR_RE = re.compile(r"(?:>>?|(?<![<>])\btee\b(?:\s+-a)?)\s+" + _TMP_PATH)
# Screenshot/OCR capture vs a structured (IPC/journal/process/protocol) probe.
SCREENSHOT_RE = re.compile(
    r"\b(grim|scrot|maim|import|gnome-screenshot|flameshot|tesseract|ocrmypdf)\b|\bocr\b"
)
STRUCTURED_PROBE_RE = re.compile(
    r"\b(journalctl|systemctl|virsh|busctl|dbus-send|gdbus|pgrep|pidof|podman|nsenter)\b"
    r"|\bqs\b[^\n]*\bipc\b|\bipc\b[^\n]*\bcall\b"
)
# A readiness wait sent to the background: `wait`/`await_*`/`sleep` followed by a
# single `&` that ends the command (EOL/comment/`;`/a following `pid=$!` capture).
# It must NOT cross a `;`/`&`/`|` — `sleep 1; notify_ready &` backgrounds
# notify_ready, not sleep — and it DOES catch the `await_x & pid=$!` form.
BACKGROUNDED_WAIT_RE = re.compile(
    r"(?:^|[;&|()]|\bthen\b|\bdo\b)\s*"
    r"\b(wait|await_[a-z_]+|sleep)\b[^;&|\n]*&"
    r"(?=\s*(?:$|#|;|[A-Za-z_][A-Za-z0-9_]*=))"
)


def pgrep_f_patterns(line: str) -> list[str]:
    """Return the PATTERN operand of each `pgrep -f ...` command on the line, via
    shlex so quotes/flags are handled. Only `-f` (substring-of-argv) invocations
    are returned — those are the ones that can self-match the pgrep's own argv."""
    patterns: list[str] = []
    for m in PGREP_CMD_RE.finditer(line):
        try:
            words = shlex.split("pgrep " + m.group("args"), comments=False, posix=True)
        except ValueError:
            continue
        if len(words) < 3:
            continue
        has_f = any(
            w.startswith("-") and not w.startswith("--") and "f" in w[1:]
            for w in words[1:]
        )
        if not has_f:
            continue
        # pgrep takes one PATTERN operand; in these snippets it is the final
        # non-flag token. This intentionally is not a full pgrep option parser.
        pattern = words[-1]
        if pattern and not pattern.startswith("-"):
            patterns.append(pattern)
    return patterns
# A /tmp path token is scoped (safe) if it carries a dynamic/per-run component.
TMP_SCOPED_HINT_RE = re.compile(r"\$|`|mktemp")


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
    any_screenshot = False
    any_structured = False
    for start, body in blocks:
        has_code = has_code or bool(body)
        for off, raw in enumerate(body):
            lineno = start + off
            line = raw.split("#", 1)[0]  # ignore trailing comments for matching
            if not line.strip():
                continue
            if ASSERT_RE.search(line):
                any_assert = True
            if SCREENSHOT_RE.search(line):
                any_screenshot = True
            if STRUCTURED_PROBE_RE.search(line):
                any_structured = True
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
            m = BARE_SOURCE_RE.search(line)
            if m and ("/" in m.group("src") or m.group("src").endswith(".sh")):
                findings.append((lineno, "bare-relative-source",
                                 "relative source path only resolves from one cwd; "
                                 "source via an absolute or $-anchored path"))
            for pat in pgrep_f_patterns(line):
                if "[" not in pat:
                    findings.append((lineno, "pgrep-self-match",
                                     "pgrep -f without a bracket guard can match its "
                                     "own argv; use a guard like [s]pawn or pgrep -x"))
            for m in TMP_REDIR_RE.finditer(line):
                tmp = m.group(1)
                if not TMP_SCOPED_HINT_RE.search(tmp):
                    findings.append((lineno, "unscoped-tmp-path",
                                     f"write to fixed scratch path {tmp!r} with no "
                                     "scenario/run suffix; use $QCI_SCENARIO_TMPDIR "
                                     "or mktemp so parallel runs don't collide"))
            if BACKGROUNDED_WAIT_RE.search(line):
                findings.append((lineno, "backgrounded-wait",
                                 "readiness wait sent to the background with &; the "
                                 "verdict can be collected before readiness"))
    if has_code and not any_assert:
        findings.append((1, "prose-only-assert",
                         "scenario has shell blocks but NO shell assertion; the "
                         "verdict rests entirely on the LLM oracle"))
    if any_screenshot and not any_structured:
        findings.append((1, "screenshot-only-assert",
                         "screenshot/OCR is the only evidence — no IPC/journal/"
                         "process probe; keep the pixel as corroboration, not the "
                         "sole oracle (allowlist explicit visual-rendering tests)"))
    return findings


def load_allowlist(path: Path) -> list[tuple[str, str, str]]:
    """Parse the allowlist TSV into (path_substring, rule, reason) rows. Blank
    lines and `#` comments are ignored. A malformed row (fewer than 2 fields) is
    skipped. Missing file => empty list."""
    rows: list[tuple[str, str, str]] = []
    if not path.is_file():
        return rows
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        sub, rule = parts[0].strip(), parts[1].strip()
        reason = parts[2].strip() if len(parts) > 2 else ""
        if sub and rule:
            rows.append((sub, rule, reason))
    return rows


def is_allowed(path: Path, rule: str,
               allowlist: list[tuple[str, str, str]]) -> str | None:
    """Return the waiver reason (possibly empty string) if (path, rule) is
    allowlisted, else None. `rule='*'` in an entry waives every rule for a path."""
    p = path.as_posix()
    for sub, arule, reason in allowlist:
        if sub in p and arule in ("*", rule):
            return reason
    return None


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
                    help="exit nonzero when non-allowlisted findings exist "
                         "(default: warn-only, exit 0)")
    ap.add_argument("--allowlist", help="path to the allowlist TSV "
                    "(default: ci/scenario-flake-allow.tsv beside the repo)")
    ap.add_argument("--no-allowlist", action="store_true",
                    help="ignore the allowlist (every finding counts)")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parents[1].parent  # ci/bin -> repo root
    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        targets = default_paths(repo)

    allowlist: list[tuple[str, str, str]] = []
    if not args.no_allowlist:
        al_path = Path(args.allowlist) if args.allowlist \
            else repo / "ci/scenario-flake-allow.tsv"
        allowlist = load_allowlist(al_path)

    total = 0            # strict-counting (non-allowlisted) findings
    allowed = 0          # waived findings (reported, never fail strict)
    by_rule: dict[str, int] = {}
    for path in targets:
        if not path.is_file():
            continue
        for lineno, rule, msg in lint_markdown(path):
            reason = is_allowed(path, rule, allowlist)
            if reason is not None:
                allowed += 1
                if args.format == "gcc":
                    print(f"{path}:{lineno}: {rule}: [allowed: {reason or 'documented'}] {msg}")
                continue
            total += 1
            by_rule[rule] = by_rule.get(rule, 0) + 1
            if args.format == "gcc":
                print(f"{path}:{lineno}: {rule}: {msg}")

    if args.format == "summary" or total or allowed:
        print(f"\nscenario-flake-lint: {total} finding(s) across {len(targets)} file(s)"
              f" ({allowed} allowlisted)", file=sys.stderr)
        for rule in sorted(by_rule):
            print(f"  {rule}: {by_rule[rule]}", file=sys.stderr)

    if args.strict and total:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
