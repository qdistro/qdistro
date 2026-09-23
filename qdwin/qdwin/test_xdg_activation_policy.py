#!/usr/bin/env python3
"""Regression checks for qdwin xdg-activation gate policy."""

from pathlib import Path
import re
import sys


def fail(message):
    print(f"FAIL: {message}")
    return 1


def function_body(source: str, name: str) -> str | None:
    match = re.search(
        rf"static void\s+{name}\s*\([^{{}}]*\)\s*\{{",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return None
    start = match.end()
    next_function = source.find("\nstatic ", start)
    return source[start: next_function if next_function != -1 else len(source)]


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_xdg_activation_policy.py <qdwin.c>")

    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    body = function_body(source, "qdwin_activation_activate")
    if body is None:
        return fail("qdwin_activation_activate not found")

    alloc = body.find("calloc(1, sizeof *ap)")
    if alloc == -1:
        return fail("pending activation allocation not found")

    fail_branch = body.find("if (!ap)", alloc)
    if fail_branch == -1:
        return fail("allocation-failure branch not found")

    return_stmt = body.find("return;", fail_branch)
    if return_stmt == -1:
        return fail("allocation-failure branch does not return")

    branch = body[fail_branch:return_stmt]
    if "qdwin_activation_token_free(t);" not in branch:
        return fail("allocation-failure branch does not consume/free token")
    if "qdwin_activation_perform" in branch:
        return fail("allocation-failure branch performs activation")

    locked = body.find("if (qdwin->locked)")
    broker = body.find("qdwin_shell_can_receive_v12")
    if locked == -1 or broker == -1 or locked > broker:
        return fail("activation requests are not rejected before broker dispatch while locked")
    locked_return = body.find("return;", locked)
    if locked_return == -1 or "qdwin_activation_token_free(t);" not in body[locked:locked_return]:
        return fail("locked activation request does not consume its token and return")

    perform = function_body(source, "qdwin_activation_perform")
    if perform is None:
        return fail("qdwin_activation_perform not found")
    perform_locked = perform.find("if (qdwin->locked)")
    restack = perform.find("weston_view_move_to_layer")
    if perform_locked == -1 or restack == -1 or perform_locked > restack:
        return fail("late broker decisions can restack a toplevel after lock entry")
    perform_return = perform.find("return;", perform_locked)
    if perform_return == -1 or "qdwin_activation_token_free(t);" not in perform[perform_locked:perform_return]:
        return fail("locked late-decision path does not consume its token and return")

    print("PASS: xdg-activation allocation and lock-transition paths fail closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
