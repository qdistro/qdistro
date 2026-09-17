#!/usr/bin/env python3
"""Report qdwin_*/capture_* functions a shell file CALLS but nothing defines.

Deliberately narrow, and the narrowness is the contract: ONLY the two project
function-name prefixes `qdwin_*` and `capture_*` -- this is not a general
undefined-function checker -- and only names in COMMAND POSITION.

KNOWN LIMITS, stated because this file has twice claimed a parsing model it did
not implement. It is a REGEX over lines, not a shell parser:
  * a string spanning several lines is only stripped on the line where it
    opens, so a name on a later line of it can still be reported;
  * `trap`, `time`, `timeout`, `xargs` and `exec` arguments are commands but are
    not recognised as command position;
  * a case-arm pattern containing a parenthesis, e.g. `(a|b)` , is not matched;
  * `$(...)` nested inside `$(...)` inside double quotes is approximated.
It is a cheap guard against calling a function nobody defines, which is the
exact regression it was written for, and it must never be described as more. A mention in a comment, a string or a heredoc body
is not a call, and this must not manufacture findings out of prose -- the file
under test documents a deleted helper by name in a NOTE, and that note is not
a bug.

Reachable definitions are the file's own, plus those of any library it sources
by a literal path (relative to the file, or via $QDWIN_WORKSPACE/qdistro).
"""
import os
import re
import sys

PREFIX = r'(?:qdwin_[a-z0-9_]+|capture_[a-z0-9_]+)'
# A definition need not start the line: `foo; bar() { :; }` defines `bar`.
DEF = re.compile(r'(?:^|[;&|{}])\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)', re.M)
# Command position: start of line, after a separator or an opening brace, after
# a substitution opener, after `!`, or after a keyword that introduces a
# command. `if X`, `if ! X`, `while X`, `do X` and `{ X` were all missed before
# (sol and fable, B round 2), and all of them are `bash -n` clean.
KEYWORD = r'(?:if|then|elif|else|while|until|do|done|coproc|\{)'
# `FOO=1 cmd` and `>/dev/null cmd` are command position with a prefix in front.
PREFIXED = r'(?:^|[;&|({`]|\|\||&&)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+|[<>]{1,2}\S+\s+)+'
# `)` closes a case-arm pattern, so `weston) qdwin_foo ;;` is command position
# and was missed at a LIVE site. A backtick opens a substitution. `$` is
# excluded before `{` so that `${qdwin_x}` -- a parameter expansion, not a
# command -- is not reported (both reviewers, B round 3).
CALL = re.compile(
    r'(?:^|(?<!\$)[;&|({`]|\$\(|\|\||&&|\b' + KEYWORD + r'\b)\s*(?:!\s+)?('
    + PREFIX + r')\b',
    re.M)
# A case arm, and ONLY a case arm: from the start of the line to the first `)`
# with no paren in between. The generic `)` separator this replaces also matched
# a word after `$( ... )`, which is an argument, not a command (sol, B round 4).
CASE_ARM = re.compile(r'^[^()\n]*\)\s*(?:!\s+)?(' + PREFIX + r')\b', re.M)
PREFIXED_CALL = re.compile(PREFIXED + r'(?:!\s+)?(' + PREFIX + r')\b', re.M)
# `qdwin_dir=/tmp/x` is an assignment and `$((qdwin_n + 1))` is arithmetic;
# neither is a call, and both were reported (sol, B round 5).
NOT_A_CALL = re.compile(r'(' + PREFIX + r')\s*=|\$\(\([^)]*?(' + PREFIX + r')')
# The argument can itself contain quoted substitutions, so take the rest of the
# line and drop the quoting rather than try to match balanced quotes.
SOURCE = re.compile(r'^\s*(?:\.|source)\s+(.+?)\s*(?:\|\||&&|;|$)', re.M)


def strip_strings(line):
    """Blank out quoted literals, KEEPING `$( ... )` inside double quotes.

    A name in a string is prose, not a call -- `echo "see qdwin_gone"` used to
    be reported as an undefined callee (sol, B round 2). But `"$(qdwin_real)"`
    IS a call, so double-quoted text cannot simply be dropped.
    """
    out, i, n = [], 0, len(line)
    quote = None
    while i < n:
        c = line[i]
        if quote is None:
            if c in ('"', "'"):
                quote = c
                out.append(' ')
            else:
                out.append(c)
            i += 1
            continue
        if c == '\\' and quote == '"':
            out.append(' ')
            i += 2
            continue
        if c == quote:
            quote = None
            out.append(' ')
            i += 1
            continue
        if quote == '"' and line[i] == '`':
            j = line.find('`', i + 1)
            if j == -1:
                j = n - 1
            out.append(line[i:j + 1])
            i = j + 1
            continue
        if quote == '"' and line.startswith('$(', i):
            depth, j = 0, i + 1
            while j < n:
                if line[j] == '(':
                    depth += 1
                elif line[j] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(line[i:j + 1])
            i = j + 1
            continue
        out.append(' ')
        i += 1
    return ''.join(out)


def _uncomment(line):
    """Drop a `#` comment without being fooled by a `#` inside quotes."""
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in ('"', "'"):
            quote = c
        elif c == '#' and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def strip_comments(text):
    """Comments and heredoc bodies only, keeping quoted text.

    `source` arguments are quoted paths, so the call-site stripper below (which
    blanks string literals) cannot be used to find them.
    """
    out, lines, i = [], text.split('\n'), 0
    while i < len(lines):
        line = lines[i]
        body = re.sub(r'<<<', '', line)
        m = re.search(r'<<-?\s*[\'"]?([A-Za-z_][A-Za-z0-9_]*)[\'"]?', body)
        out.append(_uncomment(line))
        i += 1
        if m:
            term = m.group(1)
            while i < len(lines) and lines[i].strip().rstrip(';') != term:
                i += 1
            i += 1
    return '\n'.join(out)


def strip_noise(text):
    """Remove comments, quoted literals and heredoc bodies; keep code."""
    out, lines, i = [], text.split('\n'), 0
    while i < len(lines):
        line = lines[i]
        # `<<<word` is a HERE-STRING, not a heredoc: treating it as one used to
        # swallow the rest of the file (fable, B round 2).
        body = re.sub(r'<<<', '', line)
        m = re.search(r'<<-?\s*[\'"]?([A-Za-z_][A-Za-z0-9_]*)[\'"]?', body)
        out.append(re.sub(r'(^|\s)#.*$', r'\1', strip_strings(line)))
        i += 1
        if m:
            term = m.group(1)
            while i < len(lines) and lines[i].strip().rstrip(';') != term:
                i += 1
            i += 1
    return '\n'.join(out)


def _from_script_dir(raw, base):
    """`$(dirname "${BASH_SOURCE[0]}")/../lib/x.sh` and its many spellings.

    Rather than parse the nest of substitutions, take the plain-text tail after
    the last `)` and hang it off the script's own directory -- which is what
    every one of these constructs computes.
    """
    if 'BASH_SOURCE' not in raw:
        return raw
    tail = raw[raw.rindex(')') + 1:] if ')' in raw else ''
    return base + tail
ASSIGN = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)=(.+)$', re.M)


def resolve(raw, path, text):
    """Best-effort expansion of a source path. Returns None when unresolvable.

    Handles the two shapes these files actually use: a path built from the
    script's own directory, and a variable assigned such a path earlier in the
    same file. Anything else is left alone -- guessing would manufacture
    findings, and the point of this check is to be boring and right.
    """
    base = os.path.dirname(os.path.realpath(path))
    raw = _from_script_dir(raw, base)
    for _ in range(4):
        if '$' not in raw:
            return raw
        m = re.search(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?', raw)
        if not m:
            return None
        name, repl = m.group(1), None
        if name == 'QDWIN_WORKSPACE':
            repl = os.path.realpath(os.path.join(base, '..', '..', '..'))
        else:
            for var, val in ASSIGN.findall(text):
                if var == name:
                    repl = _from_script_dir(val.strip().strip('"').rstrip(';'), base)
        if repl is None or '$' in repl:
            return None
        raw = raw[:m.start()] + repl + raw[m.end():]
    return None


def defs_of(path, seen):
    path = os.path.realpath(path)
    if path in seen or not os.path.isfile(path):
        return set()
    seen.add(path)
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except OSError:
        return set()
    names = set(DEF.findall(strip_comments(text)))
    base = os.path.dirname(path)
    for raw in SOURCE.findall(strip_comments(text)):
        resolved = resolve(raw.replace('"', '').rstrip(';'), path, text)
        if resolved is None:
            continue
        names |= defs_of(resolved if os.path.isabs(resolved)
                         else os.path.join(base, resolved), seen)
    return names


def main():
    path = sys.argv[1]
    code = strip_noise(open(path, encoding='utf-8', errors='replace').read())
    defined = defs_of(path, set())
    called = (set(CALL.findall(code)) | set(CASE_ARM.findall(code))
              | set(PREFIXED_CALL.findall(code)))
    for a, b in NOT_A_CALL.findall(code):
        called.discard(a or b)
    missing = sorted({n for n in called if n not in defined})
    # A call guarded by `declare -f NAME` is an intentional optional dependency.
    # Checked against the SAME stripped representation as the calls: reading raw
    # text let the phrase inside a comment suppress a real call (sol, round 4).
    missing = [n for n in missing
               if not re.search(r'declare -f\s+' + re.escape(n) + r'\b', code)]
    for n in missing:
        print(n)
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
