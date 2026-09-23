#!/usr/bin/env python3
"""Static-invariant checks for the zwlr_output_manager_v1 mutation gate
(security; cross-silo display DoS/tamper — see the todo this closes).

qdwin advertises zwlr_output_manager_v1 to EVERY client so any tool can
enumerate heads/modes (wlr-randr, kanshi, the qdshell Display tab). But the
APPLY path (qdwin_om_config_apply -> qdwin_om_config_realize) can
weston_output_disable() / move / rescale / mode-switch every display in every
silo. Before this gate any connecting client of any uid could black out or
rearrange all displays. The load-bearing properties a headless source-invariant
test can pin (the live half is tests/host/12-output-protocol.md S6, which needs
two compositors with different allowed_uids):

1. An authorization snapshot is captured at MANAGER BIND time
   (qdwin_om_client_may_mutate) and stored on the manager (mgr->may_mutate) —
   evaluated against the binding client's credentials, not re-derived per
   request (consistent with qdwin's connect-time peer-identity posture).
2. The decision is INHERITED by each configuration object at create time
   (cfg->may_mutate = mgr->may_mutate), so a config cannot outlive/escape its
   manager's authorization.
3. BOTH the apply AND the test request reject an unauthorized client
   (!cfg->may_mutate) with a protocol error and RETURN before doing any work —
   fail-closed, and before consuming the one-shot `used` flag.
4. The mutation gate is the trusted-shell rule, not an open one: it accepts the
   bound shell client, the shell's (pid,uid), or — pre-shell — the allowed_uid,
   and otherwise denies.
"""

from pathlib import Path
import re
import sys


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _strip_comments(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", " ", code)
    return code


def _function_body(source, signature_regex, name):
    """Brace-balanced body of the first function *definition* matching
    signature_regex (skips forward declarations)."""
    for m in re.finditer(signature_regex, source, re.MULTILINE | re.DOTALL):
        paren = source.index("(", m.start())
        depth = 0
        close = None
        for i in range(paren, len(source)):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close is None:
            continue
        j = close + 1
        while j < len(source) and source[j].isspace():
            j += 1
        if j >= len(source) or source[j] != "{":
            continue  # forward declaration
        start = j
        depth = 0
        for i in range(start, len(source)):
            c = source[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return source[start:i + 1], None
        return None, f"{name}: unbalanced braces"
    return None, f"{name} not found (no definition)"


def check_mutate_gate_is_trusted_shell(source, logic_source):
    """qdwin_om_client_may_mutate must accept the bound shell (or its pid/uid,
    or pre-shell the allowed_uid) and otherwise deny — a real gate, not a stub
    that returns true unconditionally."""
    body, err = _function_body(
        source, r"static bool\s+qdwin_om_client_may_mutate\s*\(",
        "qdwin_om_client_may_mutate")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_client_is_bound_shell" not in code:
        return fail("qdwin_om_client_may_mutate does not accept the bound shell "
                    "client (qdwin_client_is_bound_shell)")
    if "wl_client_get_credentials" not in code:
        return fail("qdwin_om_client_may_mutate never reads peer credentials")
    if "shell_pid" not in code or "shell_uid" not in code:
        return fail("qdwin_om_client_may_mutate does not fall back to the "
                    "shell's (pid,uid)")
    if "allowed_uid" not in code:
        return fail("qdwin_om_client_may_mutate has no allowed_uid fallback "
                    "for the pre-shell window")
    if "qdwin_om_mutation_allowed" not in code:
        return fail("qdwin_om_client_may_mutate does not delegate to the "
                    "compiled pure policy helper")

    helper_body, err = _function_body(
        logic_source, r"bool\s+qdwin_om_mutation_allowed\s*\(",
        "qdwin_om_mutation_allowed")
    if err:
        return fail(err)
    helper = _strip_comments(helper_body)
    if "client_is_bound_shell" not in helper:
        return fail("qdwin_om_mutation_allowed does not accept the bound shell")
    if "shell_bound" not in helper or "shell_pid" not in helper or \
       "shell_uid" not in helper:
        return fail("qdwin_om_mutation_allowed does not enforce the shell "
                    "(pid,uid) second-connection rule")
    if "allowed_uid" not in helper:
        return fail("qdwin_om_mutation_allowed has no pre-shell allowed_uid "
                    "fallback")
    # Must be capable of denying: a credential comparison drives a return (so it
    # is not a stub that authorizes every client). Accept either a literal
    # `return false` or a `return <expr with == uid>`.
    denies = ("return false" in helper or
              re.search(r"return[^;]*uid\s*==", helper) is not None)
    if not denies:
        return fail("qdwin_om_mutation_allowed has no deny path — every client "
                    "would be authorized (not a gate)")
    return 0


def check_bind_snapshots_authorization(source):
    """bind_output_manager must snapshot the authorization onto the manager."""
    body, err = _function_body(
        source, r"static void\s+bind_output_manager\s*\(", "bind_output_manager")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if not re.search(r"mgr->may_mutate\s*=\s*qdwin_om_client_may_mutate\s*\(",
                     code):
        return fail("bind_output_manager does not snapshot the authorization "
                    "(mgr->may_mutate = qdwin_om_client_may_mutate(...))")
    return 0


def check_config_inherits_authorization(source):
    """create_configuration must inherit the manager's may_mutate onto the
    configuration object."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_om_manager_create_configuration\s*\(",
        "qdwin_om_manager_create_configuration")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if not re.search(r"cfg->may_mutate\s*=\s*mgr->may_mutate", code):
        return fail("create_configuration does not inherit mgr->may_mutate onto "
                    "cfg->may_mutate (a config could escape its manager's "
                    "authorization)")
    return 0


def _gate_returns_first(code):
    """True iff the body checks !cfg->may_mutate, posts a protocol error, and
    returns — all BEFORE it consumes the one-shot `used` flag."""
    # The gate must appear and return.
    gm = re.search(r"if\s*\(\s*!\s*cfg->may_mutate\s*\)", code)
    if not gm:
        return False, "no !cfg->may_mutate gate"
    # Locate the gate block's return and the post-error within the same window.
    window = code[gm.start():gm.start() + 600]
    if "wl_client_post_implementation_error" not in window and \
       "wl_resource_post_error" not in window:
        return False, "gate does not post a protocol error"
    if "return" not in window:
        return False, "gate does not return (would fall through to mutate)"
    # Fail-closed ordering: the gate must precede the `cfg->used = true` consume.
    used = code.find("cfg->used = true")
    if used != -1 and gm.start() > used:
        return False, ("gate runs AFTER consuming cfg->used (one-shot burned "
                       "by an unauthorized client)")
    return True, None


def check_apply_is_gated(source):
    body, err = _function_body(
        source, r"static void\s+qdwin_om_config_apply\s*\(",
        "qdwin_om_config_apply")
    if err:
        return fail(err)
    ok, why = _gate_returns_first(_strip_comments(body))
    if not ok:
        return fail(f"qdwin_om_config_apply mutation gate: {why}")
    return 0


def check_test_is_gated(source):
    body, err = _function_body(
        source, r"static void\s+qdwin_om_config_test\s*\(",
        "qdwin_om_config_test")
    if err:
        return fail(err)
    ok, why = _gate_returns_first(_strip_comments(body))
    if not ok:
        return fail(f"qdwin_om_config_test mutation gate: {why}")
    return 0


def main():
    if len(sys.argv) != 3:
        return fail("usage: test_output_manager_gate.py <qdwin.c> <qdwin-logic.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    logic_source = Path(sys.argv[2]).read_text(encoding="utf-8")

    rc = check_mutate_gate_is_trusted_shell(source, logic_source)
    if rc:
        return rc
    for check in (
            check_bind_snapshots_authorization,
            check_config_inherits_authorization,
            check_apply_is_gated,
            check_test_is_gated):
        rc = check(source)
        if rc:
            return rc

    print("PASS: zwlr_output_manager_v1 mutation gate (authorization snapshot at "
          "manager bind via qdwin_om_client_may_mutate, inherited by each "
          "configuration, apply AND test reject an unauthorized client with a "
          "protocol error fail-closed before any work; enumeration stays open)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
