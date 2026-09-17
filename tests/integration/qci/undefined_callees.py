#!/usr/bin/env python3
"""Report qdwin_*/capture_* functions a shell file CALLS but nothing defines.

Deliberately narrow, and the narrowness is the contract: ONLY the two project
function-name prefixes `qdwin_*` and `capture_*` -- this is not a general
undefined-function checker -- and only names in COMMAND POSITION. A mention in a comment, a string or a heredoc body
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
DEF = re.compile(r'^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)', re.M)
# Command position: start of line, after a separator or an opening brace, after
# a substitution opener, after `!`, or after a keyword that introduces a
# command. `if X`, `if ! X`, `while X`, `do X` and `{ X` were all missed before
# (sol and fable, B round 2), and all of them are `bash -n` clean.
KEYWORD = r'(?:if|then|elif|else|while|until|do|done|\{)'
CALL = re.compile(
    r'(?:^|[;&|({]|\$\(|\|\||&&|\b' + KEYWORD + r'\b)\s*(?:!\s+)?(' + PREFIX + r')\b',
    re.M)
# The argument can itself contain quoted substitutions, so take the rest of the
# line and drop the quoting rather than try to match balanced quotes.
SOURCE = re.compile(r'^\s*(?:\.|source)\s+(.+?)\s*$', re.M)


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
        out.append(re.sub(r'(^|\s)#.*$', r'\1', line))
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
        out.append(strip_strings(re.sub(r'(^|\s)#.*$', r'\1', line)))
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
    names = set(DEF.findall(text))
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
    missing = sorted({n for n in CALL.findall(code) if n not in defined})
    # A call guarded by `declare -f NAME` is an intentional optional dependency.
    text = open(path, encoding='utf-8', errors='replace').read()
    missing = [n for n in missing if f'declare -f {n}' not in text]
    for n in missing:
        print(n)
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
