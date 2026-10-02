#!/usr/bin/env python3
"""qdwin bug #4: the "install_default_cursor: no surface yet" warning is one-shot.

Replaces the agent-driven qdwin/tests/apps/04-cursor-spam-suppressed.md, whose
live run could never observe the warning on the qci images anyway: the
qdistro-cursor-sprites helper registers the default sprite at session start, so
the "no surface" branch is unreachable there and the scenario only ever passed
vacuously. The property it guarded is static: before the fix every pointer focus
change re-logged the line (1000+ lines per session). This pins the shape of the
fix in the real source:

  * the warning is emitted from exactly one site, inside
    qdwin_install_default_cursor_on_pointer's "no surface" branch;
  * that site is guarded by `if (!qdwin->cursor_default_warned)` and sets the
    flag before logging;
  * the message carries the "further occurrences suppressed" hint;
  * nothing ever re-arms the flag (no `cursor_default_warned = 0`).
"""

from pathlib import Path
import re
import sys

MESSAGE = ("qdwin: install_default_cursor: no surface yet (helper not started?) "
           "— further occurrences suppressed")


def fail(message):
    print(f"FAIL: {message}")
    return 1


def join_literals(text: str) -> str:
    """Concatenate adjacent C string literals ("a" "b" -> "ab")."""
    return re.sub(r'"\s*\n?\s*"', "", text)


def function_body(source: str, name: str) -> str | None:
    match = re.search(
        rf"^static void\s*\n?{name}\s*\([^{{}}]*\)\s*\{{",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return None
    depth, i = 1, match.end()
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[match.end(): i - 1]


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_default_cursor_warn_once.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    joined = join_literals(source)

    sites = joined.count("install_default_cursor: no surface")
    if sites != 1:
        return fail(f"expected exactly 1 'install_default_cursor: no surface' log site, found {sites}")

    body = function_body(source, "qdwin_install_default_cursor_on_pointer")
    if body is None:
        return fail("qdwin_install_default_cursor_on_pointer not found")
    body = join_literals(body)

    if MESSAGE not in body:
        return fail("the no-surface warning (with its suppression hint) is not in "
                    "qdwin_install_default_cursor_on_pointer")

    guard = re.search(r"if\s*\(\s*!\s*qdwin->cursor_default_warned\s*\)\s*\{", body)
    if not guard:
        return fail("the no-surface warning is not guarded by !qdwin->cursor_default_warned")
    log_at = body.find(MESSAGE)
    set_at = body.find("qdwin->cursor_default_warned = 1;", guard.end())
    if log_at < guard.end():
        return fail("the warning is logged outside the cursor_default_warned guard")
    if set_at == -1 or set_at > log_at:
        return fail("the guard does not set cursor_default_warned = 1 before logging")
    # The log must sit inside the guard block, i.e. before the block's closing brace.
    depth, i = 1, guard.end()
    while i < len(body) and depth:
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
        i += 1
    if not (guard.end() <= log_at < i):
        return fail("the warning is not inside the one-shot guard block")

    if re.search(r"cursor_default_warned\s*=\s*0", source):
        return fail("cursor_default_warned is re-armed somewhere (= 0)")
    assigns = re.findall(r"cursor_default_warned\s*=[^=]", source)
    if len(assigns) != 1:
        return fail(f"expected exactly one assignment of cursor_default_warned, found {len(assigns)}")

    print("PASS: install_default_cursor no-surface warning is one-shot per qdwin lifetime")
    return 0


if __name__ == "__main__":
    sys.exit(main())
