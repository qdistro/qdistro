#!/usr/bin/env python3
"""Fail if any host-side `2>&1` hands vm-exec's fd 2 to a pipe.

THE DEFECT. `x=$(vm-exec ... 2>&1)` and `vm-exec ... 2>&1 | reader` both put
vm-exec's fd 2 on a PIPE. vm-exec redirects its own children's fd 1 to an
internal unlinked capture file, but fd 2 is inherited straight through to every
virsh/jq descendant it starts. A descendant that outlives vm-exec and keeps
that descriptor holds the pipe open, and the shell waits for the PIPE to reach
EOF -- not for vm-exec to exit. The call hangs after the guest command is dead,
and an outer `timeout` on vm-exec cannot help, because the shell is blocked on a
read rather than on the child. Writing both descriptors to a regular file
removes the dependency.

EXACTLY WHAT THIS SEARCHES FOR. For every text file under the given roots
(skipping .git, binaries and source languages that are not shell), physical
lines are first joined on trailing backslashes -- every instance round 9 found
that round 8's grep had missed was hidden by a continuation.

The join KEEPS the backslash rather than removing it, which is a second
conservative false positive: a safe `>/dev/null` ending in a line
continuation, with `2>&1` on the next physical line, is
REPORTED, because the retained backslash defeats the end-anchor in FD1_REDIR.
No such site exists in either repo today, but the first legitimate one will
fail this gate with a misleading message. Said here rather than left for
whoever hits it.

A joined line is reported when:

  (a) it mentions a vm-exec invocation in any spelling used in these repos.
      The token is /vm.?exec/i, which covers `vm-exec`, `$VMEXEC`, `$VM_EXEC`,
      `"$QDWIN_VM_EXEC"`, `$vmx` sites that spell the path, and the `vm_exec`
      shell wrappers; AND
  (b) it is OUTSIDE every quote (a host redirection is never inside one) and is
      not preceded, IMMEDIATELY BEFORE THE MERGE, by a `>`/`>>` redirect of
      fd 1 to an ordinary path -- so `>file 2>&1` and `>/dev/null 2>&1`, which
      put fd 1 on a FILE, are not reported.

      "Immediately before" is literal: `>file arg 2>&1` IS reported even though
      it is safe, because the redirect is not adjacent to the merge. That is a
      conservative false positive, stated here rather than smoothed over. So is
      `>"$(mktemp)" 2>&1`: the process-substitution rejection below keys on a
      parenthesis in the target, which a command substitution also contains.

      The digit must be `1` or absent. `>&N` is NOT accepted (where fd N points
      is not knowable here), and neither is a target that is a process
      substitution `>(...)` or a /dev/fd,/proc/self/fd path -- those are pipes
      or descriptor aliases, not files, so fd 1 is still the pipe.

"the same command" means the text since the last top-level `;`, `|`, `&&`,
`||`, `&` or newline; `$(`, `` ` `` and `(` do not end it, so the wrapper token
in `out=$("$VMEXEC" "$VM" "cmd" 2>&1)` is still in view.

A `2>&1` INSIDE the quoted remote-command string runs in the guest and inherits
no host descriptor, so it is skipped. That is decided by the quote scan in (b),
not by a name allowlist.

WHAT IT DOES NOT COVER. It sees only a `2>&1` written at the call site. A
vm-exec call whose fd 2 is a pipe INHERITED from an enclosing context is
invisible to it: a wrapper function invoked inside `$( ... 2>&1 )`, a whole
block redirected with `{ ...; } 2>&1 | reader`, or bats' own `run`, which
merges streams with `"$@" 2>&1` inside a command substitution
(bats-core 1.14, lib/bats-core/test_functions.bash). Nor does it evaluate
`eval`, a `$( )` nested inside a double-quoted guest string (which the host
expands before the guest sees it), or a heredoc body (skipped wholesale).

It matches the merge token (`2>&1`, `2>& 1`) at the call site and does NOT
check whether fd 1 is actually piped -- a bare `vm-exec ... 2>&1` with no pipe
in sight is reported, because an enclosing tool may supply one. An earlier
version of this text claimed the site had to be "SYNTACTICALLY piped"; that was
never what the code did.

These real defects are invisible, and none is reported as UNPARSEABLE -- they
pass silently (round 10, sol; section 2, fable):
  * `exec 3> >(cat); vm-exec ... >&3 2>&1` -- fd 3 is a pipe, but the pipe-ness
    is in the earlier `exec`, not at the call. Seeing this needs fd dataflow
    across statements, which this scanner does not do.
  * `vm-exec ... |& reader`, `2>&$fd`, `2> >(reader)` -- no literal `2>&1`.
  * a wrapper whose own name is outside /vm.?exec|\bvmx\b/ that pipes its
    inherited fd 2.
  * `x=$(vm-exec ... >&3 2>&1)` and `&>file 2>&1` -- SILENT, but not for the
    reason the FD1_REDIR comment suggests. The `&` is treated as a command
    separator by the scan below, so `cmd` is reset and the vm-exec token is
    gone before the merge is reached: these are DROPPED, not judged safe.
    Do not "fix" the redirect regex and expect a report.
  * a fenced block this file's markdown reader does not open: it opens only on
    ```` ``` ```` with an optional bare language word, so `~~~`, four-backtick,
    ```` ```bash {attr} ```` and indented blocks are never scanned. Checked
    2026-09-17: no such block in either repo contains a vm-exec `2>&1`.

THE BLIND SPOT THAT MATTERS MOST is not in tracked source at all. `ci/runs` is
skipped by design (see SKIP_DIRS), and that is where the scenario agent's own
run-time driver scripts live -- 499 of them in this checkout, 121 opening with
`exec > >(tee "$LOG") 2>&1` and 33 of those going on to call vm-exec. That
shape is the defect, it is the dominant caller in a GUI run, and NO static scan
of the repository can see it because the files do not exist until the run does.
It is addressed in the agent PROMPT instead (`ci/prompts/gui-scenario-agent.md`
item 11, pinned by tests/integration/qci/gui-skip-reason.bats), which is the
only place it can be addressed.
Passing this scan is therefore a necessary condition, not a proof that the
caller audit is complete. Treat it as a regression lint: it stops NEW directly
spelled instances from landing. It does not certify the tree.

Usage: vmexec-fd2-scan.py <root> [<root> ...]   (a missing root is skipped)
Exit 0 with no FINDINGS; exit 1 listing every hit otherwise. Files the quote
scan cannot parse are printed as UNPARSEABLE on stderr and still exit 0 --
unparseable is not clean, it is unexamined, and each one needs a hand audit.
"""
import os
import re
import sys

# Every spelling a vm-exec invocation takes in these repos: the path
# (`vm-exec`, `$VM_TOOLS/vm-exec`), the env-var wrappers (`$VMEXEC`,
# `"$QDWIN_VM_EXEC"`), the shell wrappers (`vm_exec`), and the local `$vmx`
# variable the gates use to hold the path (round 9's missed mmnet site).
TOKEN = re.compile(r"vm.?exec|\bvmx\b", re.I)
# Only SHELL is scanned: .sh/.bash/.bats, markdown (fenced shell blocks), and
# extensionless files whose first line is a shell shebang -- feeding XML/patches/
# policy files to a shell scanner produces nothing but noise. This is what the
# scanner CONSIDERS, not a guarantee about where callers can live: a caller in a
# file over MAX_BYTES, behind a symlink, with a NUL early in it, or in an
# unrecognised extension is simply not examined.
SHELL_EXT = (".sh", ".bash", ".bats")
MD_EXT = (".md",)
# `runs` and `qdistro-src` are neither of those: `ci/runs/` is gitignored run
# ARCHIVE (468 dirs, 357 hits on this workstation -- historical records of calls
# that already happened, none of them editable source), and
# `image/root/root/qdistro-src/` is an untracked VENDORED snapshot baked into
# the image, fixed by re-vendoring rather than here. Scanning either buries the
# real findings and makes the gate permanently red.
#
# TWO HONEST CAVEATS about that rule, both flagged in review:
#   * This walks the FILESYSTEM and never consults git, so "source is what git
#     tracks" describes the intent, not the mechanism. An untracked file in a
#     scanned directory IS scanned; a tracked file in a skipped one is not.
#   * Skipping `ci/runs` skips the agent's run-time driver scripts, which are
#     the dominant vm-exec callers in a GUI run and do carry the defect (121 of
#     499 open with `exec > >(tee …) 2>&1`). They are not "historical records"
#     in the sense of being harmless -- they are unscannable, because they are
#     written during the run this gate precedes. That class is handled by the
#     agent prompt, not here; see the module docstring.
# MAX_BYTES is likewise a heuristic about where source lives, not a fact about
# where a caller can live.
SKIP_DIRS = {".git", "__pycache__", "build", ".worktrees", "node_modules",
             ".venv", "artifacts", "triage-artifacts", "results",
             "runs", "qdistro-src"}
# Source files are small, so anything larger is USUALLY a log or a blob.
# This is a heuristic about where source lives, not a fact about where a
# caller can live: a caller in a file over this size is simply not examined.
MAX_BYTES = 4 * 1024 * 1024
# The fd-1 redirect that makes a following `2>&1` safe: `> f`, `1> f`, `>>f`.
#
# The digit MUST be `1` or absent. It was `\d?` until 2026-09-17, which counted
# a redirect of ANY descriptor as a redirect of fd 1 and so silently passed two
# real host hazards (sol, review `qci-A-260917-sol-review.md` §2, both measured
# at ~1.01s of hang against a fake vm-exec leaving a descendant on fd 2):
#
#     x=$(vm-exec vm true 3>/tmp/log 2>&1)   # fd 1 is still the substitution pipe
#     x=$(vm-exec vm true 2>/dev/null 2>&1)  # the LAST dup wins; fd 2 is the pipe again
#
# `>&N` is deliberately NOT accepted as safe any more either: where fd N points
# is not decidable from the call site, and `>&1` is a no-op that leaves fd 1
# exactly where it was.
FD1_REDIR = re.compile(r"(?:^|[\s;&|(])1?>>?\s*([^\s;|&<>]+)\s*$")

# A redirect TARGET that is a path does not imply a regular file. These reopen
# or alias an existing descriptor -- which in a command substitution is the
# pipe itself -- so `>"/dev/fd/1" 2>&1` puts BOTH descriptors back on the pipe.
# Named FIFOs have the same property but cannot be recognised by name, which is
# stated in the docstring rather than papered over here.
UNSAFE_REDIR_TARGET = re.compile(
    r"^['\"]?(?:/dev/fd/\d+|/dev/std(?:out|err|in)|/proc/(?:self|\d+)/fd/\d+)['\"]?$")

# A PROCESS SUBSTITUTION target is a pipe, not a file. `> >(tee log) 2>&1` and
# `>>(reader) 2>&1` put fd 1 -- and then fd 2 -- on the reader's pipe, which is
# the whole defect. The target class above happily accepts `(tee log)` because
# `(` is not one of its excluded characters, so these read as a safe fd-1
# redirect unless they are rejected here (fable, qci-A-260917-fable-review.md
# section 2). This shape is not hypothetical: it is how the scenario agent
# opens its own driver scripts.
PROCSUB_TARGET = re.compile(r"[()]")
MERGE = re.compile(r"2>&\s*1")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def is_shell(path):
    """True for the files a vm-exec caller can live in."""
    name = os.path.basename(path)
    if name.endswith(SHELL_EXT) or name.endswith(MD_EXT):
        return True
    if "." in name:
        return False
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return False
        with open(path, "rb") as fh:
            first = fh.readline(128)
    except OSError:
        return False
    return first.startswith(b"#!") and (b"sh" in first or b"bats" in first)


def _md_shell_blocks(lines):
    """[[(lineno, line), ...], ...] -- one list per ``` fenced SHELL block.

    Fences strictly alternate open/close. A bare ``` is a fence DELIMITER, not
    always an opening one: reading it as "open a shell block" made the closing
    fence of a ```text block open a prose block, and prose apostrophes then
    wrecked the quote scanner.
    """
    blocks, cur, lang, in_fence = [], None, None, False
    for lineno, raw in lines:
        m = re.match(r"^\s*```(\S+)?\s*$", raw)
        if m:
            if in_fence:
                if cur is not None:
                    blocks.append(cur)
                cur, lang, in_fence = None, None, False
            else:
                lang = (m.group(1) or "").lower()
                in_fence = True
                cur = [] if lang in ("", "bash", "sh", "shell") else None
            continue
        if in_fence and cur is not None:
            cur.append((lineno, raw))
    if cur:
        blocks.append(cur)
    return blocks


def scan_file(path):
    """[(lineno, snippet)] for each host-side `2>&1` on a vm-exec command."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return []
        text = open(path, errors="replace").read()
    except OSError:
        return []
    if "\0" in text[:4096]:
        return []
    all_lines = list(enumerate(text.split("\n"), 1))
    if path.endswith(".md"):
        # Only FENCED shell blocks are code (markdown prose is full of
        # apostrophes), and each block is scanned SEPARATELY: a fence is a hard
        # command boundary, so state must not leak from one block to the next.
        groups = _md_shell_blocks(all_lines)
    else:
        groups = [all_lines]
    hits = []
    for lines in groups:
        hits.extend(_scan_lines(lines, text))
    return hits


def _scan_lines(lines, text):
    hits = []
    quote = ""          # persists across physical lines, like bash
    # `$( ... )` re-enters a fresh quoting context, so quote state must be
    # STACKED, not flat. Without this a line like
    #   sleep "$(awk "BEGIN { printf \\"%.3f\\", $X }")"
    # leaves the scanner permanently inside a double quote and every later
    # `2>&1` in the file reads as guest-side -- i.e. the scanner goes blind
    # instead of reporting. (That is exactly what it did before this fix; the
    # unterminated-quote report at the end of this function exists so a future
    # construct that defeats the scanner is LOUD rather than silently exempt.)
    qstack = []
    heredoc = None      # terminator word while inside a heredoc body
    pending_heredocs = []
    cmd = ""            # text of the command in progress
    for lineno, line in lines:
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = pending_heredocs.pop(0) if pending_heredocs else None
            continue
        i, n, commented = 0, len(line), False
        while i < n:
            c = line[i]
            if quote == "'":
                cmd += c
                if c == "'":
                    quote = qstack.pop() if qstack else ""
                i += 1
                continue
            if quote == '"':
                if c == "\\":
                    cmd += c
                    if i + 1 < n:
                        cmd += line[i + 1]
                    i += 2
                    continue
                if line.startswith("$(", i) or c == "`":
                    qstack.append(quote)
                    quote = ""
                    cmd += "$(" if c == "$" else c
                    i += 2 if c == "$" else 1
                    continue
                cmd += c
                if c == '"':
                    quote = qstack.pop() if qstack else ""
                i += 1
                continue
            if line.startswith("$(", i):
                qstack.append("")
                cmd += "$("
                i += 2
                continue
            if c == "`":
                # A backtick both opens and closes; treat it as a nesting
                # context so the `...` body's own quotes stay local to it.
                if qstack and qstack[-1] == "`":
                    qstack.pop()
                    quote = qstack.pop() if qstack else ""
                else:
                    qstack.append(quote)
                    qstack.append("`")
                    quote = ""
                cmd += c
                i += 1
                continue
            if c == ")" and qstack:
                quote = qstack.pop()
                cmd += c
                i += 1
                continue
            if c in "\"'":
                qstack.append(quote)
                quote = c
                cmd += c
                i += 1
                continue
            if c == "#" and (i == 0 or line[i - 1] in " \t;&|("):
                commented = True
                break
            m = HEREDOC.match(line, i)
            if m:
                pending_heredocs.append(m.group(2))
                cmd += m.group(0)
                i = m.end()
                continue
            # `2>& 1` is the same redirection with a space, and was invisible
            # until 2026-09-17 (fable, section 2).
            m2 = MERGE.match(line, i)
            if m2:
                # A preceding fd-1 redirect only makes this safe when it sends
                # fd 1 to something that is NOT the enclosing pipe. A
                # /dev/fd/N-style target reopens whatever that descriptor
                # already is, so it is treated as no redirect at all.
                fd1 = FD1_REDIR.search(cmd)
                if fd1 and (UNSAFE_REDIR_TARGET.match(fd1.group(1))
                            or PROCSUB_TARGET.search(fd1.group(1))):
                    fd1 = None
                if TOKEN.search(cmd) and not fd1:
                    hits.append((lineno, line.strip()[:180]))
                cmd += "2>&1"
                i = m2.end()
                continue
            if c in ";|&" or line[i:i + 2] in ("&&", "||"):
                cmd = ""
                i += 1
                continue
            cmd += c
            i += 1
        # End of physical line. A trailing backslash or an open quote continues
        # the command; anything else starts a fresh one. A heredoc body, if one
        # was introduced on this line, begins now.
        if pending_heredocs and heredoc is None and not quote:
            heredoc = pending_heredocs.pop(0)
        if not quote and (commented or not line.rstrip().endswith("\\")):
            cmd = ""
        else:
            cmd += " "
    if (quote or qstack) and TOKEN.search(text):
        # The scanner lost track in a file that DOES call vm-exec. Report it
        # rather than returning "clean": a construct it cannot parse must never
        # read as an exemption. (A file with no vm-exec token has nothing to
        # exempt, so an unbalanced quote there is not interesting.)
        hits.append((0, "UNPARSEABLE"))
    return hits


def main(argv):
    found, blind = [], []
    for root in argv[1:]:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                p = os.path.join(dirpath, name)
                if os.path.islink(p) or not is_shell(p):
                    continue
                for lineno, snippet in scan_file(p):
                    if lineno == 0:
                        blind.append(os.path.relpath(p, root))
                    else:
                        found.append(f"{p}:{lineno}: {snippet}")
    if blind:
        # NOT fatal, but never silent: these are files the scanner's quote
        # tracking could not follow to the end (a backtick-comment, a heredoc
        # inside `$( )`, an illustrative snippet in a doc). Their `2>&1` sites
        # were NOT checked, so a clean exit does not cover them. Printed so the
        # blind spot is visible, and pinned by the caller-bounds bats test so a
        # NEW blind spot is a test failure rather than a silent exemption.
        for b in sorted(set(blind)):
            print(f"UNPARSEABLE: {b}", file=sys.stderr)
    if found:
        print("host-side 2>&1 on a vm-exec call (fd 2 would go to a pipe):")
        for f in sorted(found):
            print("  " + f)
        print("\nCapture through a regular FILE instead -- see bounded_run() in "
              "scripts/vm/vm-exec and qdwin_vmx_merged() in "
              "qdwin/tests/gui/qdwin-helpers.sh.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
