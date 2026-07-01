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
# The path token stops at a backtick too, so a command-substitution terminator
# (`echo `cmd > /tmp/x``) is not swallowed into the path and then mis-read as a
# "dynamic" (hence exempt) path by TMP_SCOPED_HINT_RE.
_TMP_PATH = r"(/tmp/[^\s;'\"|)&<>`]+)"
# NOTE: this requires whitespace after `>`/`tee`, so a compact `>/tmp/x` is not
# matched — a separate known matcher-completeness gap (surfaces ~116 mostly
# guest-heredoc compact redirects), tracked for its own batch rather than folded
# into the host/guest-boundary change here.
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
        # Drop a trailing redirect file-descriptor left in the segment: args stop
        # at `>` but a `2>/dev/null` leaves the bare `2` as the final token, which
        # is NOT the pattern (e.g. `pgrep -f "[g]uest=X," 2>/dev/null`).
        while len(words) >= 3 and words[-1].isdigit() and (words[-1] + ">") in line:
            words.pop()
        if len(words) < 3:
            continue
        # pgrep takes one PATTERN operand; in these snippets it is the final
        # non-flag token. This intentionally is not a full pgrep option parser.
        pattern = words[-1]
        if pattern and not pattern.startswith("-"):
            patterns.append(pattern)
    return patterns
# A /tmp path token is scoped (safe) if it carries a dynamic/per-run component.
TMP_SCOPED_HINT_RE = re.compile(r"\$|`|mktemp")

# A guest-exec wrapper command word: $VMEXEC / "$QDWIN_VM_EXEC" / ${FOO_VM_EXEC},
# quoted or not. A `/tmp/...` WRITE that lands inside such a wrapper's remote
# command argument runs in a DISPOSABLE per-scenario guest VM (fresh per scenario
# AND per retry), so it cannot collide with a parallel host run — the premise of
# unscoped-tmp-path. We must NOT exempt host-side writes, so the detection is
# conservative: only a write proven to sit inside a wrapper's quoted remote arg is
# exempted; anything ambiguous stays flagged. NOTE: $VMGUI is deliberately NOT a
# wrapper — `vm-gui screenshot /tmp/x.png` writes a HOST artifact and must stay
# flagged; and a top-level `runuser -c` is host-side unless already nested inside a
# wrapper span (which the scanner covers for free).
# Env-var-style wrappers ($VMEXEC / "$QDWIN_VM_EXEC" / ${FOO_VM_EXEC}) and the
# bareword `vm_ssh` helper (run-55 defines it as an ssh-into-guest runner). The
# bareword form needs a left word-boundary check (see the scanner) so `myvm_ssh`
# does not arm.
_GUEST_WRAPPER = (
    r'"?\$\{?(?:VMEXEC|QDWIN_VM_EXEC|[A-Z][A-Z0-9_]*_VM_EXEC)\}?"?'
    r"|vm_ssh\b"
)
_GUEST_WRAPPER_RE = re.compile(_GUEST_WRAPPER)


def guest_exec_write_columns(body: list[str]) -> tuple[list[set[int]], list[int]]:
    """For each line in a fenced block return (guest_columns, code_len):
      guest_columns[off] - the set of column indices that lie inside a guest-exec
        wrapper's quoted remote-command argument (possibly opened on an earlier
        line — remote strings routinely span real newlines). A tmp-write whose
        redirect operator starts at such a column is guest-side and exempt.
      code_len[off] - the length of the code portion of the line, i.e. the column
        where a QUOTE-AWARE top-level `#` comment begins (or len(line) if none).
        Callers must slice `raw[:code_len]` instead of the quote-unaware
        `raw.split("#")[0]`, else a `#` literal inside a remote string truncates a
        following same-line HOST write out of view.

    Conservative char scanner. State carried across lines:
      guest_open  - inside an unclosed quote that is a wrapper remote arg
      gq          - that quote char ('"' honours \\-escapes; "'" does not)
      plain_quote - inside a NON-wrapper quote (so separators/quotes inside it,
                    e.g. the "$VM" name arg, don't disarm or mis-open)
      armed       - a wrapper token was seen; the next quote(s) it introduces are
                    its args -> treat as guest. Persists across a trailing-`\\`
                    continuation and until a top-level command separator or a
                    newline that does not continue an open guest span.

    Correctness-critical exclusions (never exempt a HOST write):
    - Inside a DOUBLE-quoted guest span, an UNescaped `$(...)` / backtick is
      expanded by the HOST shell before the wrapper runs, so a redirect there
      writes on the host — those columns are NOT marked guest. (In a
      single-quoted span the same text is literal and stays guest; an escaped
      `\\$(` in a "..." span is likewise guest.)
    - `#` starts a comment only OUTSIDE any quote (quote-aware), so a literal `#`
      inside a remote string can't truncate the line and leak an open guest span
      into the next (host) line; and a comment apostrophe can't open a stray quote.
    - A line with a top-level `eval` re-parses its arguments as shell, which can
      turn a quoted `>` into a host redirect — refuse all exemptions on such lines.
    """
    masks: list[set[int]] = []
    code_lens: list[int] = []
    guest_open = False
    gq = ""
    subst_depth = 0   # >0: inside $(...) host command-sub in a "..." guest span
    subst_q = ""      # quote char open INSIDE $(...) (so a quoted `)` doesn't close it)
    in_backtick = False  # inside `...` host command-sub in a "..." guest span
    armed = False
    eval_active = False  # a top-level `eval` governs this logical command (carries
    #                      across `\`-continuations); refuse exemptions while set
    for raw in body:
        line = raw
        plain_quote = ""
        cols: set[int] = set()
        code_len = len(line)
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if guest_open:
                # Host command substitution inside a "..." guest span: its columns
                # are host-expanded, so do NOT mark them guest (a `>` there flags).
                # Track quotes INSIDE the substitution so a quoted `)` (e.g.
                # `$(printf ')' > /tmp/x)`) doesn't end it early and re-expose the
                # host redirect as guest.
                if subst_depth > 0:
                    if subst_q:
                        if subst_q == '"' and c == "\\":
                            i += 2
                            continue
                        if c == subst_q:
                            subst_q = ""
                    elif c in "\"'":
                        subst_q = c
                    elif c == "(":
                        subst_depth += 1
                    elif c == ")":
                        subst_depth -= 1
                    i += 1
                    continue
                if in_backtick:
                    if c == "`":
                        in_backtick = False
                    i += 1
                    continue
                if gq == '"' and c == "\\":
                    cols.add(i)
                    if i + 1 < n:
                        cols.add(i + 1)
                    i += 2
                    continue
                if gq == '"' and c == "$" and i + 1 < n and line[i + 1] == "(":
                    subst_depth = 1
                    i += 2
                    continue
                if gq == '"' and c == "`":
                    in_backtick = True
                    i += 1
                    continue
                cols.add(i)
                if c == gq:
                    guest_open = False
                    gq = ""
                i += 1
                continue
            if plain_quote:
                if plain_quote == '"' and c == "\\":
                    i += 2
                    continue
                if c == plain_quote:
                    plain_quote = ""
                i += 1
                continue
            # Outside any quote. `#` at a word boundary starts a comment -> the
            # rest of the physical line is not code (quote-aware, unlike a naive
            # split: a `#` inside a remote string never reaches here).
            if c == "#" and (i == 0 or line[i - 1] in " \t;&|("):
                code_len = i
                break
            # A wrapper token arms the guest state and is consumed whole (including
            # its own optional surrounding quotes) so we don't misread
            # `"$QDWIN_VM_EXEC"`'s quotes as a plain string. It arms only at a left
            # word-boundary, so `myvm_ssh` / `FOO_VMEXEC` in a longer word does not.
            if i == 0 or not (line[i - 1].isalnum() or line[i - 1] == "_"):
                if line.startswith("eval", i) and (
                    i + 4 >= n or not (line[i + 4].isalnum() or line[i + 4] == "_")
                ):
                    eval_active = True
                m = _GUEST_WRAPPER_RE.match(line, i)
                if m:
                    armed = True
                    i = m.end()
                    continue
            if c in "\"'":
                if armed:
                    guest_open = True
                    gq = c
                    cols.add(i)
                else:
                    plain_quote = c
                i += 1
                continue
            if c in ";|&" or line[i:i + 2] in ("&&", "||"):
                armed = False
            i += 1
        # A top-level `eval` can re-parse a quoted `>` into a host redirect; refuse
        # every exemption on the whole logical command (the eval line AND its
        # `\`-continuation lines) rather than reason about the re-parse.
        masks.append(set() if eval_active else cols)
        code_lens.append(code_len)
        # End of physical line. If no guest span is still open, `armed`/`eval_active`
        # only survive a trailing-backslash continuation (wrapper/eval on one line,
        # its arg on the next); otherwise a fresh command starts next line -> reset.
        if not guest_open and not line.rstrip().endswith("\\"):
            armed = False
            eval_active = False
    return masks, code_lens


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
        guest_cols, code_lens = guest_exec_write_columns(body)
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
            # Use the QUOTE-AWARE code length, not the naive `#`-split `line`, so a
            # host write after a `#` that is literal-inside-a-remote-string is still
            # seen (a naive split would truncate it out of view).
            for m in TMP_REDIR_RE.finditer(raw[:code_lens[off]]):
                tmp = m.group(1)
                # Exempt writes inside a guest-exec wrapper's remote arg: they
                # land in a disposable per-scenario VM and cannot collide.
                if m.start() in guest_cols[off]:
                    continue
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


# The qdistro-OWNED scenario roots: the migration metric for scenarios this repo
# owns and can fix directly.
def qdistro_owned_roots(repo: Path) -> list[Path]:
    return [
        repo / "tests/integration/permissions-gui",
        repo / "tests/integration/qdwin-noctalia",
    ]


# The UMBRELLA scenario roots: the qdistro-owned set PLUS the sibling qdwin/
# qdlocker roots — the same path set `qci gui` actually schedules. The readiness
# metric for widening `qci gui` across the whole suite. Umbrella STRICT-clean must
# never be conflated with qdistro-owned strict-clean.
def umbrella_roots(repo: Path) -> list[Path]:
    ws = repo.parent  # sibling repos live next to the qdistro checkout
    return qdistro_owned_roots(repo) + [
        ws / "qdwin/tests/gui",
        ws / "qdwin/tests/apps",
        ws / "qdlocker/tests/gui",
    ]


def _roots_to_files(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for r in roots:
        if r.is_dir():
            out.extend(sorted(r.glob("[0-9][0-9]-*.md")))
    return out


def paths_for_set(repo: Path, which: str) -> list[Path]:
    if which == "qdistro":
        return _roots_to_files(qdistro_owned_roots(repo))
    return _roots_to_files(umbrella_roots(repo))


def default_paths(repo: Path) -> list[Path]:
    # Back-compat default = the umbrella set (the historic behaviour).
    return paths_for_set(repo, "umbrella")


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
    ap.add_argument("--set", choices=("qdistro", "umbrella"), default="umbrella",
                    dest="path_set",
                    help="which default path set to lint when no explicit paths "
                         "are given: `qdistro` (qdistro-owned strict = "
                         "permissions-gui + qdwin-noctalia) or `umbrella` (the "
                         "full set qci gui schedules; default)")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parents[1].parent  # ci/bin -> repo root
    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        targets = paths_for_set(repo, args.path_set)

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
