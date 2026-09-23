#!/usr/bin/env python3
"""Static-invariant checks that the live qdwin call sites for two of the three
S13 deliberate fail-open points actually DELEGATE to their compiled pure
policy helpers (security; see todo/fable-release 02-security-gate.md S13).

The pure helpers (qdwin_layershell_pre_shell_uid_allowed,
qdwin_secctx_root_launcher_attested) are exhaustively pinned by qdwin-logic-unit,
and the third S13 point (output-manager) has its call site pinned by
test_output_manager_gate.py. But a unit test on the helper proves nothing if the
LIVE path stops calling it (or wraps it in `if (0 && ...)`): the fable M3 review
showed that neutering the layer-shell pre-shell gate passed every headless test,
because no invariant tied the call site to the helper. This closes that gap for
the two remaining points:

1. bind_qdwin_layer_shell's pre-shell branch (no shell bound yet) must gate on
   qdwin_layershell_pre_shell_uid_allowed and reject (post error + return) when
   it denies — not bind unconditionally.
2. qdwin_secctx_helper_has_root_launcher_parent must derive its attestation
   from qdwin_secctx_root_launcher_attested and RETURN that result, not a
   hand-rolled inline copy that could drift from the pinned policy.

These are token/structure presence checks (the source_invariant idiom); the
end-to-end behavioural negatives are VM/B1-gated.

LIVENESS CAVEAT (shared by every source_invariant in this suite): a text scan
proves a matching *witness exists*, not that it is the *reachable* control flow.
To shrink that gap to its irreducible core, each check: (a) forbids ANY
preprocessor conditional in the function — no `#if 0`/`#elif 0`/`#if 1` dead
branch can host a witness, and the live functions have none; (b) strips literal
C `if (0)`/`if (false)` dead blocks before analysis; (c) requires the helper be
referenced EXACTLY once (no dead duplicate beside a neutered live path); and
(d) rejects divergent or widened verdict returns (the secctx verdict may only be
returned as `false`, the delegated variable, or an EXACT bare helper call —
`helper(...) || true` is not a delegation). That defeats every constant-foldable
neuter. The irreducible residual is an opaque NON-constant predicate wrapping the
single live reference (`if (runtime_never_true) { ...the only gate... }`), which
no text grep can fold; closing THAT is the behavioural negative on the VM/B1 lane
(tracked under 02-security-gate.md S13), the accepted compensating control for
this whole headless suite — not this layer.
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


def _paren_group(code, open_idx):
    """(inner_text, close_idx) for the balanced parens whose '(' is at
    open_idx; (None, None) if unbalanced."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return code[open_idx + 1:i], i
    return None, None


def _brace_group_after(code, idx):
    """(inner_text, close_idx) for the first brace block at/after idx, but only
    if nothing other than whitespace separates idx from its opening '{' (so we
    grab the block that idx introduces, not a later unrelated one)."""
    j = idx
    while j < len(code) and code[j].isspace():
        j += 1
    if j >= len(code) or code[j] != "{":
        return None, None
    depth = 0
    for i in range(j, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[j + 1:i], i
    return None, None


# A pinned S13 call site is small, straight-line C. Rather than re-implement the
# preprocessor to decide which `#if`/`#elif` branch is live (an unwinnable
# regex game vs. `#if 0`, `#elif 0`, `#if 1-1`, `#if defined(NEVER)` ...), we
# forbid conditional compilation in these functions outright: any directive here
# is a manual-review event, and the live functions have none.
_PP_COND = re.compile(r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else)\b",
                      re.MULTILINE)


def _reject_preprocessor(body, fn):
    if _PP_COND.search(body):
        return fail(f"{fn} contains a preprocessor conditional (#if/#elif/#else) "
                    "— a pinned S13 security call site must not be conditionally "
                    "compiled; remove it or update this invariant after review "
                    "(this blocks dead `#if 0`/`#elif 0` witness branches)")
    return 0


def _require_single_reference(code, name, fn):
    """The helper must be referenced EXACTLY once (the live wiring). Zero means
    the wiring is gone; more than one means a dead duplicate witness could sit
    alongside a neutered live path."""
    n = len(re.findall(r"\b" + re.escape(name) + r"\b", code))
    if n != 1:
        return fail(f"{fn}: expected exactly one reference to {name}, found "
                    f"{n} — a dead duplicate witness, or the live wiring is "
                    "missing after dead-code stripping")
    return 0


def _is_bare_helper_call(expr, name):
    """True iff `expr` (whitespace-stripped) is EXACTLY a call to `name(...)`
    with balanced parens and nothing trailing — so `name(...) || true` or
    `name(...) && x` are rejected."""
    expr = expr.strip()
    if not expr.startswith(name):
        return False
    rest = expr[len(name):].lstrip()
    if not rest.startswith("("):
        return False
    depth = 0
    for i, c in enumerate(rest):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return rest[i + 1:].strip() == ""
    return False


_DEAD_IF = re.compile(r"\bif\s*\(\s*(?:0|0[uU]|false)\s*\)\s*")


def _strip_dead_if(code):
    """Remove `if (0)` / `if (false)` dead blocks (braced or single-statement)
    so a witness hidden in never-taken code cannot satisfy a presence check.
    Does not attempt to handle a trailing `else` (the live call sites have
    none); a dead `if (0) {...} else {...}` would leave a bare `else` that the
    downstream checks ignore. Opaque non-constant false predicates are out of
    scope — see the module LIVENESS CAVEAT."""
    out = code
    while True:
        m = _DEAD_IF.search(out)
        if not m:
            return out
        j = m.end()
        if j < len(out) and out[j] == "{":
            depth = 0
            k = j
            while k < len(out):
                if out[k] == "{":
                    depth += 1
                elif out[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            end = k + 1
        else:
            semi = out.find(";", j)
            end = len(out) if semi == -1 else semi + 1
        out = out[:m.start()] + out[end:]


# The exact pre-shell gate condition the live bind must use, whitespace-removed.
# Pinning the EXACT predicate (not just "contains the helper") is what makes a
# neuter like `if (!helper(...) && 0)` or `if (!helper(...) || some_other)`
# fail here instead of sliding through on a prefix match.
_LAYERSHELL_EXPECTED_COND = \
    "!qdwin_layershell_pre_shell_uid_allowed(uid,qdwin->allowed_uid)"


def check_layershell_pre_shell_delegates(source):
    """The pre-shell layer-shell bind gate must be EXACTLY
    `if (!qdwin_layershell_pre_shell_uid_allowed(uid, qdwin->allowed_uid)) {
    ...post error...; return; }` — the helper alone is the deny predicate, and
    the deny block posts a protocol error and returns. A widened/neutered
    predicate or a removed return is visible here."""
    fn = "bind_qdwin_layer_shell"
    body, err = _function_body(
        source, r"\bbind_qdwin_layer_shell\s*\(", fn)
    if err:
        return fail(err)
    rc = _reject_preprocessor(body, fn)
    if rc:
        return rc
    code = _strip_dead_if(_strip_comments(body))
    rc = _require_single_reference(
        code, "qdwin_layershell_pre_shell_uid_allowed", fn)
    if rc:
        return rc
    # Find the `if (...)` whose condition references the helper; capture the
    # full balanced condition and compare it EXACTLY (whitespace-normalised).
    gate = None
    for m in re.finditer(r"\bif\s*\(", code):
        open_idx = code.index("(", m.start())
        cond, close = _paren_group(code, open_idx)
        if cond is not None and \
                "qdwin_layershell_pre_shell_uid_allowed" in cond:
            gate = (m.start(), cond, close)
            break
    if gate is None:
        return fail("bind_qdwin_layer_shell has no `if (...)` gating on "
                    "qdwin_layershell_pre_shell_uid_allowed — the live "
                    "pre-shell uid check is not wired to the pinned helper")
    gate_start, cond, close = gate
    norm = re.sub(r"\s+", "", cond)
    if norm != _LAYERSHELL_EXPECTED_COND:
        return fail(
            f"bind_qdwin_layer_shell pre-shell gate condition is {norm!r}, not "
            f"exactly {_LAYERSHELL_EXPECTED_COND!r} — an extra && / || term "
            "(e.g. `&& 0`) would neuter the gate while still naming the helper")
    block, _ = _brace_group_after(code, close + 1)
    if block is None:
        return fail("bind_qdwin_layer_shell pre-shell gate has no braced deny "
                    "block immediately after the condition")
    if "wl_client_post_implementation_error" not in block and \
       "wl_resource_post_error" not in block:
        return fail("bind_qdwin_layer_shell deny block does not post a protocol "
                    "error")
    if not re.search(r"\breturn\s*;", block):
        return fail("bind_qdwin_layer_shell deny block does not `return;` — "
                    "would fall through and bind an unpermitted client")
    # Ordering: the gate must run BEFORE the resource is created — a reorder
    # that binds first fails open even with a perfect gate downstream. (Same
    # property output-manager-gate pins via gate-before-`cfg->used`.) The first
    # wl_resource_create / wl_resource_set_implementation must come AFTER the
    # gate's `if`.
    bind_m = re.search(
        r"\bwl_resource_(?:create|set_implementation)\s*\(", code)
    if bind_m and bind_m.start() < gate_start:
        return fail("bind_qdwin_layer_shell creates/implements the layer-shell "
                    "resource BEFORE the pre-shell uid gate — the gate must be "
                    "able to deny+return before any bind happens")
    return 0


def check_secctx_attestation_delegates(source):
    """qdwin_secctx_helper_has_root_launcher_parent must derive its verdict from
    qdwin_secctx_root_launcher_attested — either `return qdwin_secctx_root_
    launcher_attested(...)` directly, or assign it into a variable and return
    that variable. EVERY return in the function must be `false` (an early-exit),
    that delegated variable, or the helper call itself: no constant-true and no
    divergent inline verdict (e.g. `return parent_uid == 0;`). Combined with the
    dead-`if (0)` strip, this rejects every concrete neuter — a dead witness
    plus a live `return parent_uid == 0;`, an injected `ok = true;`, or a
    `return true;`."""
    fn = "qdwin_secctx_helper_has_root_launcher_parent"
    body, err = _function_body(
        source,
        r"\bqdwin_secctx_helper_has_root_launcher_parent\s*\(", fn)
    if err:
        return fail(err)
    rc = _reject_preprocessor(body, fn)
    if rc:
        return rc
    code = _strip_dead_if(_strip_comments(body))

    helper = "qdwin_secctx_root_launcher_attested"
    rc = _require_single_reference(code, helper, fn)
    if rc:
        return rc
    # A direct delegating return must be EXACTLY `return helper(...);` — a
    # widened `return helper(...) || true;` is not a delegation.
    direct = any(
        _is_bare_helper_call(rm.group(1), helper)
        for rm in re.finditer(r"\breturn\b([^;]*);", code))
    am = re.search(rf"\b(\w+)\s*=\s*{helper}\s*\(", code)
    var = am.group(1) if am else None
    if not direct and not var:
        return fail("qdwin_secctx_helper_has_root_launcher_parent does not "
                    "delegate to qdwin_secctx_root_launcher_attested — the "
                    "live attestation is not wired to the pinned helper")
    if not direct:
        # Assigned into a variable: that variable must actually be returned.
        if re.search(rf"\breturn\s+{re.escape(var)}\s*;", code) is None:
            return fail(f"qdwin_secctx_helper_has_root_launcher_parent computes "
                        f"the attestation into `{var}` but never returns it "
                        f"(`return {var};`) — the pinned policy is not "
                        "load-bearing")
        # Every assignment to the verdict variable must be the helper call or
        # its `false` init — catches an injected `ok = true;`. The
        # lookbehind/lookahead skip ==, !=, <=, >= comparisons.
        for asn in re.finditer(
                rf"(?<![<>=!]){re.escape(var)}\s*=(?!=)\s*([^;]+);", code):
            rhs = asn.group(1).strip()
            if rhs != "false" and not _is_bare_helper_call(rhs, helper):
                return fail(
                    f"qdwin_secctx_helper_has_root_launcher_parent assigns "
                    f"`{var} = {rhs}` — the verdict variable must only be set "
                    "by the pinned helper (or its `false` init)")
    # No return may shadow the delegated verdict: the only legal returns are a
    # `false` early-exit, the delegated variable, or an EXACT bare helper call
    # (`return helper(...);` with nothing trailing — `helper(...) || true` is
    # rejected as a widened verdict).
    allowed = {"false"}
    if var:
        allowed.add(var)
    for rm in re.finditer(r"\breturn\b([^;]*);", code):
        expr = rm.group(1).strip()
        if expr in allowed:
            continue
        if _is_bare_helper_call(expr, helper):
            continue
        return fail(
            f"qdwin_secctx_helper_has_root_launcher_parent has `return {expr};` "
            "— the verdict may only be returned as the pinned helper result, "
            "the delegated variable, or a `false` early-exit (a divergent or "
            "widened return like `parent_uid == 0` or `helper(...) || true` "
            "bypasses the attestation)")
    return 0


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_s13_failopen_pins.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")

    for check in (check_layershell_pre_shell_delegates,
                  check_secctx_attestation_delegates):
        rc = check(source)
        if rc:
            return rc

    print("PASS: S13 fail-open call sites delegate to their pinned helpers "
          "(layer-shell pre-shell gate -> qdwin_layershell_pre_shell_uid_allowed "
          "with deny+return; secctx root-launcher attestation -> "
          "qdwin_secctx_root_launcher_attested returned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
