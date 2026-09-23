#!/usr/bin/env python3
"""Static-invariant check that qdwin's privileged-protocol identity GATES fail
CLOSED when the binding peer's identity string is NULL — the state an OOM'd
`strdup` of a /proc-derived identity leaves behind (finding F7).

qdwin gates the locker, layer-shell and IME protocols by matching the binding
client's process identity (exe / SELinux label) against an operator-configured
allow value. Those identity strings come from qdwin_proc_exe /
qdwin_proc_selinux_label, which heap-copy with strdup and can therefore yield
NULL under memory pressure. The security property F7 demands: a NULL
(unreadable / OOM'd) identity must be treated as "does not match" => REJECT,
never as a match and never dereferenced.

The full F7 audit (all 54 strdup sites in qdwin.c) found every security-critical
site already fails closed. This test PINS the live identity gates so a future
edit can't silently drop a guard and turn an OOM into a gate BYPASS. For each
registered gate function it asserts the canonical fail-closed shape, end to end:

  (A) GUARD — the comparison is `<id> && strcmp(<id>, qdwin->allowed_<field>)
      == 0`: the SAME identifier that is strcmp'd short-circuits it, so a NULL
      identity never reaches strcmp (UB) and never evaluates as a match. The
      exact set of `allowed_<field>` gates per function is pinned in GATE_FUNCS,
      so a deleted/renamed gate (or a comparison hidden behind a new wrapper)
      trips this test rather than silently shrinking the covered surface.

  (B) VERDICT consumed to DENY — the boolean the guard assigns (`int ok = ...`)
      must feed an `if (!ok) { ...post error...; return <fail-closed>; }`, so a
      refactor can't keep the guarded strcmp yet ignore its result. The verdict
      must NOT be reassigned between the guard and the deny (an `if (!exe) ok =
      1;` "tolerate unreadable /proc" edit fail-OPENS on OOM — forbidden).

  (C) ORDERING — in the bind handlers no `wl_resource_create()` /
      `wl_resource_set_implementation()` appears before the first gate, so the
      privileged object never becomes usable ahead of the identity check. The
      IME helper instead returns its verdict to the caller before any object is
      created; (B) pins its deny path to `return false`.

  (D) NO IDENTITY-SKIP — the gate is never reachable-around on the NULL path.
      Structurally (brace-nesting, not reading the condition's truth, so the
      whole class is closed rather than enumerating forms): the gate is not
      nested inside an `if (...)` whose condition references the identity var
      (`if (exe)`, `if ((exe))`, `if (exe != NULL)`, `if (!(exe == NULL))`, … —
      all skip the gate when exe is NULL), nor inside ANY loop (a `continue`/
      `break` could skip it); and the handler contains no `goto` (which could
      jump past the gate on `if (!exe) goto …`). A legitimate `if (!exe) {
      reject; return; }` is a guard clause BEFORE the gate (a sibling, not an
      ancestor) and is fine; the only NULL test the gate needs is its own
      `exe &&` guard.

  (E) NO EARLY ALLOW — a bool gate helper (qdwin_ime_family_bind_allowed) may
      only `return false` (deny) before all its gates pass; a pre-gate
      `if (!exe) return true;` would ALLOW the bind on a NULL identity while the
      canonical witness stays intact below it. (Void bind handlers have no
      allow-return; an early bind-on-NULL needs a wl_resource_create before the
      gate, caught by (C).)

  (F) IDENTITY PROVENANCE — the identity var feeding a gate/predicate-call comes
      straight from qdwin_proc_*(): its nearest preceding assignment must be
      `idvar = qdwin_proc_…`, so an `if (!exe) exe = qdwin->allowed_X;` rewrite
      of a NULL identity into the allowed value (passing the strcmp/predicate) is
      caught.

Plus the predicate-helper gate the locker-entrypoint path uses instead of a
direct allowlist compare: qdwin_exe_is_system_interpreter() /
qdwin_path_is_trusted_entrypoint() must NULL-reject their pointer argument in
their FIRST statement (`if (!<arg> ...) return false;`), AND their call sites in
bind_qdwin_locker must invoke them UNCONDITIONALLY on the raw /proc identity var
(not `exe ? helper(exe) : true`) with both verdicts negated in a returning deny
— otherwise a NULL identity could skip the helper entirely.

LIVENESS CAVEAT (shared with test_s13_failopen_pins.py): a text scan proves a
matching witness EXISTS, not that it is the reachable control flow. To shrink
that gap each analysed body: forbids ANY preprocessor conditional (no
`#if 0`/`#elif 0` dead branch can host a fake guard), strips literal
`if (0)`/`if (false)` blocks, and requires each gate witness EXACTLY once (a
duplicate could sit a passing witness beside a live neutered path). The strict
canonical form (the whole `;`-terminated initializer) rejects every
constant-foldable neuter — `id || strcmp(...)`, a `id ? strcmp(...) : ""`
ternary, an inner-widened `id && (strcmp(...)==0 || x)`, an OUTER-widened
`(id && strcmp(...)==0) || x;` (the `;`-anchor: a trailing disjunct that
constant-trues the verdict is not part of the initializer), or a guard on a
different variable. The predicate call sites are likewise `;`-anchored
(`bool v = helper(id);`, so `helper(id) || !id;` is rejected). Control-flow skips are closed STRUCTURALLY by (D)
(no enclosing loop/switch/else/statement-expr, no identity-referencing enclosing
`if`, no `goto`). The irreducible residual is INDIRECT DATAFLOW: a value derived
from the identity through a level of indirection a text scan cannot follow — a
separate `bool ok2 = (exe != NULL); if (ok2) {gate}`, a pointer-alias write
`*p = 1`, a `memset(&ok, …)`, or a macro expanding to `ok = 1`. Those are the
accepted compensating-control boundary for the whole headless source_invariant
suite (a non-constant opaque predicate is the same class); the behavioural
negative is VM/seat-gated (a real impostor client failing to bind).
"""

from pathlib import Path
import re
import sys


# The privileged identity-gate functions and the EXACT set of configured-allow
# comparisons each must contain. A new gate MUST be registered here (forcing a
# deliberate review), so the pin can never be satisfied by a renamed field or a
# silently-removed gate. `bind` => a Wayland global bind handler that creates a
# protocol resource (ordering pinned); the IME entry is a bool-returning helper.
GATE_FUNCS = {
    "bind_qdwin_locker": {
        "fields": ["allowed_locker_exe", "allowed_locker_label"],
        "bind": True,
    },
    "bind_qdwin_layer_shell": {
        "fields": ["allowed_layershell_exe", "allowed_layershell_label"],
        "bind": True,
    },
    "qdwin_ime_family_bind_allowed": {
        "fields": ["allowed_ime_exe", "allowed_ime_label"],
        "bind": False,
    },
}

# Predicate-helper gates: (function, pointer arg the first statement must reject).
PREDICATE_GATES = [
    ("qdwin_exe_is_system_interpreter", "exe"),
    ("qdwin_path_is_trusted_entrypoint", "cand"),
]

# The locker-entrypoint path gates via the predicate helpers above instead of a
# direct allowlist strcmp. Pin the CALL SITES so a NULL identity can't bypass
# them: each helper must be called UNCONDITIONALLY with the raw /proc identity
# variable (not `exe ? helper(exe) : true`), and the verdicts must feed a deny
# that returns. {function: [(helper, identity_arg)]}.
PREDICATE_CALL_SITES = {
    "bind_qdwin_locker": [
        ("qdwin_exe_is_system_interpreter", "exe"),
        ("qdwin_path_is_trusted_entrypoint", "argv1"),
    ],
}

POST_ERROR = ("wl_resource_post_error",
              "wl_client_post_implementation_error",
              "wl_client_post_no_memory",
              "wl_resource_post_no_memory")
# Function-name stems for the privileged-resource creation calls. Matched as
# `<stem>\s*\(` so `wl_resource_create (` (legal C whitespace) cannot slip the
# ordering gate (C) — a forbidden-token check where a miss would FAIL OPEN.
RESOURCE_CREATE = ("wl_resource_create", "wl_resource_set_implementation")


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _strip_comments(code):
    """Remove comments AND the CONTENTS of string/char literals in a single
    left-to-right pass (a real lexer order, so a `/*` inside a string or a `"`
    inside a comment can't desync the two). String/char bodies are blanked so a
    fake canonical witness hidden in a `const char *` literal can't satisfy the
    pin while the live gate fails open (every check matches code tokens —
    function names, not message text — so blanking literal bodies is safe)."""
    out, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            j = code.find("*/", i + 2)
            i = j + 2 if j >= 0 else n
            out.append(" ")
        elif c == "/" and i + 1 < n and code[i + 1] == "/":
            j = code.find("\n", i)
            i = j if j >= 0 else n
            out.append(" ")
        elif c == '"' or c == "'":
            q = c
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                    continue
                if code[i] == q:
                    i += 1
                    break
                i += 1
            out.append(" ")   # literal body blanked
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_dead_if_zero(code):
    """Remove literal `if (0) { ... }` / `if (false) { ... }` blocks so no guard
    or deny witness can hide in constant-false dead code."""
    pat = re.compile(r"\bif\s*\(\s*(?:0|false)\s*\)\s*\{")
    out, i = [], 0
    while True:
        m = pat.search(code, i)
        if not m:
            out.append(code[i:])
            return "".join(out)
        out.append(code[i:m.start()])
        depth, j = 0, m.end() - 1  # j at the '{'
        while j < len(code):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        i = j


def _function_body(source, name):
    """Brace-balanced body of the first DEFINITION of `name` (skips forward
    declarations). Matches a definition whose signature begins at column 0."""
    sig = re.compile(r"^" + re.escape(name) + r"\s*\(", re.MULTILINE)
    for m in sig.finditer(source):
        paren = source.index("(", m.start())
        depth, close = 0, None
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
        depth = 0
        for i in range(j, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    return source[j:i + 1], None
        return None, f"{name}: unbalanced braces"
    return None, f"{name}: no definition found"


def _brace_group_after(code, idx):
    """Inner text of the first brace block at/after idx, only if nothing but
    whitespace separates idx from its '{'. None if not a brace block there."""
    j = idx
    while j < len(code) and code[j].isspace():
        j += 1
    if j >= len(code) or code[j] != "{":
        return None
    depth = 0
    for i in range(j, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[j + 1:i]
    return None


def _paren_group(code, open_idx):
    """Inner text of the balanced parens whose '(' is at open_idx; None if
    unbalanced."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return code[open_idx + 1:i], i
    return None, None


def _enclosing_blocks(flat, pos):
    """(kind, condition) for every braced block enclosing `pos`. kind is the
    construct that introduces the block: 'if'/'while'/'for'/'switch' (preceded
    by `<kw> (...)`, condition captured), 'else' (preceded by the else keyword),
    'stmtexpr' (a GNU `({ ... })`, preceded by `(`), or 'block' (a plain `{`)."""
    stack = []
    for i in range(pos):
        c = flat[i]
        if c == "{":
            j = i - 1
            while j >= 0 and flat[j].isspace():
                j -= 1
            kind, cond = "block", None
            if j >= 0 and flat[j] == ")":   # `<kw> (...) {`
                depth, k = 0, j
                while k >= 0:
                    if flat[k] == ")":
                        depth += 1
                    elif flat[k] == "(":
                        depth -= 1
                        if depth == 0:
                            break
                    k -= 1
                e = k - 1
                while e >= 0 and flat[e].isspace():
                    e -= 1
                s = e
                while s >= 0 and flat[s].isalpha():
                    s -= 1
                word = flat[s + 1:e + 1]
                if word in ("if", "while", "for", "switch"):
                    kind, cond = word, flat[k + 1:j]
                    if word == "if":
                        # `else if (...)`: the gate sits in a chained branch, so
                        # an earlier `if (!exe)` arm can skip it on NULL even if
                        # THIS condition is identity-free. Treat as `else`.
                        q = s
                        while q >= 0 and flat[q].isspace():
                            q -= 1
                        ps = q
                        while ps >= 0 and flat[ps].isalpha():
                            ps -= 1
                        if flat[ps + 1:q + 1] == "else":
                            kind, cond = "else", None
            elif j >= 0 and flat[j] == "(":   # GNU statement expression `({`
                kind = "stmtexpr"
            elif j >= 0 and flat[j].isalpha():
                s = j
                while s >= 0 and flat[s].isalpha():
                    s -= 1
                word = flat[s + 1:j + 1]
                if word in ("else", "do"):   # bare-keyword block constructs
                    kind = word
            stack.append((kind, cond))
        elif c == "}":
            if stack:
                stack.pop()
    return stack


def check_identity_provenance(flat, idvar, pos, where):
    """The identity var feeding the gate/predicate-call at `pos` must come
    STRAIGHT from a qdwin_proc_*() read: the nearest preceding assignment to it
    must be `idvar = qdwin_proc_...`. This catches a `if (!exe) exe =
    qdwin->allowed_locker_exe;` rewrite that turns a NULL (OOM) identity into the
    allowed value — making the canonical `exe && strcmp(exe, allowed) == 0` (or
    the predicate) pass while the witness stays intact. `free(exe)`, the logging
    `exe ? exe : "…"`, and `exe &&` are not assignments, so they don't match."""
    pre = flat[:pos]
    last = None
    for am in re.finditer(r"\b" + re.escape(idvar) + r"\b\s*=(?!=)\s*", pre):
        last = am
    if last is None:
        return [f"{where}: identity var '{idvar}' is used by the gate but has no "
                f"assignment before it (cannot confirm it came from /proc)"]
    rhs = pre[last.end():]
    if not rhs.startswith("qdwin_proc_"):
        snippet = rhs[:24].strip()
        return [f"{where}: identity var '{idvar}' is assigned from `{snippet}…` "
                f"immediately before the gate, not qdwin_proc_*() — a NULL/OOM "
                f"identity may be rewritten to the allowed value (fail open)"]
    return []


def check_no_identity_skip(flat, idvar, pos, where):
    """The gate at `pos` must be reachable on the NULL/OOM path — i.e. NOT
    skippable by an enclosing control construct. Structurally (brace-nesting,
    not reading any condition's truth, so the whole class is closed rather than
    enumerated):

      * No enclosing loop (`while`/`for`/`do`) / switch / else / GNU
        statement-expression. Each can skip the gate body on the NULL path
        (`continue`/`break`; a `do {...break;} while (0)`; a `switch
        (exe != NULL)` default; an `else` of `if (!exe)`; a `({...})` value
        path) while leaving the canonical witness intact inside.
      * No enclosing `if (...)` whose condition references the identity var —
        `if (exe)`, `if ((exe))`, `if (exe != NULL)`, `if (!(exe == NULL))`, …
        all skip the gate when exe is NULL.

    A legitimate `if (!exe) { reject; return; }` is a guard clause BEFORE the
    gate (a sibling, not an ancestor), and the configured-field wrapper
    `if (qdwin->allowed_X) { ... }` does not reference the identity var, so both
    pass. The gate's own `exe &&` guard is an assignment, not an enclosing `if`.
    Non-local `goto` and indirect/opaque dataflow (a separate `bool` derived
    from the identity, a pointer alias, a macro) are out of this structural
    scope — goto is forbidden wholesale in the handler body, and the dataflow
    residual is the documented liveness boundary (see module docstring)."""
    for kind, cond in _enclosing_blocks(flat, pos):
        if kind in ("while", "for", "do", "switch", "else", "stmtexpr"):
            return [f"{where}: the gate for identity var '{idvar}' is nested "
                    f"inside a `{kind}` construct — control could skip it on the "
                    f"NULL (OOM) path (fail open). Gates must sit in straight-"
                    f"line flow, guarded only by their own `{idvar} &&`."]
        if kind == "if" and re.search(r"\b" + re.escape(idvar) + r"\b", cond):
            return [f"{where}: the gate for identity var '{idvar}' is nested "
                    f"inside an `if (...{idvar}...)` block — a NULL (OOM) "
                    f"identity could skip the gate and fall through open. The "
                    f"NULL test must be the gate's own `{idvar} &&` guard or a "
                    f"returning `if (!{idvar})` BEFORE the gate, not a wrapper."]
    return []


def check_predicate_call_sites(source):
    """Pin that the locker-entrypoint predicate helpers are called
    UNCONDITIONALLY on the raw /proc identity var (defeating an
    `exe ? helper(exe) : true` NULL-bypass) and their verdicts feed a deny that
    returns."""
    errors = []
    for fn, calls in PREDICATE_CALL_SITES.items():
        body, err = _function_body(source, fn)
        if err:
            errors.append(err)
            continue
        if re.search(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b", body):
            errors.append(f"{fn}: preprocessor conditional present — predicate "
                          f"call site could hide in a dead branch")
            continue
        flat = re.sub(r"\s+", " ", _strip_dead_if_zero(body))
        if re.search(r"\bif\s*\(\s*(?:0|false)\s*\)", flat):
            errors.append(f"{fn}: residual constant-false `if` — can't safely "
                          f"analyse predicate call sites")
            continue
        if re.search(r"\bgoto\b", flat):
            errors.append(f"{fn}: contains a `goto` — a control-flow jump can "
                          f"skip the predicate gate (fail open)")
            continue
        verdicts = []
        for helper, arg in calls:
            # `bool <v> = helper(<arg> ...);` exactly once, the helper call being
            # the WHOLE initializer (ends in `;` right after its close paren) —
            # so a ternary `exe ? helper(exe) : true` (no leading `helper(`) AND
            # a trailing widening `helper(exe) || !exe;` (close paren not
            # followed by `;`) both fail to match. `[^;()]` keeps the match to a
            # single, paren-free argument list ending at the call's own `)`.
            cm = list(re.finditer(
                r"(?:bool|int)\s+(\w+)\s*=\s*" + re.escape(helper) +
                r"\(\s*" + re.escape(arg) + r"\b[^;()]*\)\s*;", flat))
            if len(cm) != 1:
                errors.append(
                    f"{fn}: expected exactly one unconditional "
                    f"`bool v = {helper}({arg} ...)` call ({len(cm)} found) — a "
                    f"conditional/ternary call could bypass it on a NULL "
                    f"identity")
                continue
            # No NULL-skip wrapper, and the identity must come from /proc (not be
            # rewritten to the allowed value) before the predicate call.
            errors += check_no_identity_skip(flat, arg, cm[0].start(), fn)
            errors += check_identity_provenance(flat, arg, cm[0].start(), fn)
            verdicts.append((cm[0].group(1), cm[0].end()))
        if len(verdicts) != len(calls):
            continue
        names = [v for v, _ in verdicts]
        # A single deny `if ( ...!v1... || ...!v2... ) { ...return... }` must
        # negate every predicate verdict and return.
        found = False
        deny_pos = None
        for im in re.finditer(r"\bif\s*\(", flat):
            inner, close = _paren_group(flat, flat.index("(", im.start()))
            if inner is None:
                continue
            if all(re.search(r"!\s*" + re.escape(v) + r"\b", inner)
                   for v in names):
                block = _brace_group_after(flat, close + 1)
                if block is not None and re.search(r"\breturn\b", block) \
                        and any(p in block for p in POST_ERROR):
                    found = True
                    deny_pos = im.start()
                    break
        if not found:
            errors.append(
                f"{fn}: predicate verdicts {names} are not all negated in a "
                f"single deny `if (!v1 || !v2 ...)` that posts an error and "
                f"returns — entrypoint gate may not fail closed")
            continue
        # No REASSIGNMENT of a predicate verdict between its helper-call
        # assignment and the deny — `if (!exe) exe_ok = true;` is the same
        # "tolerate unreadable /proc" fail-open the direct gates forbid in (B).
        for vname, vend in verdicts:
            ra = re.search(
                r"\b" + re.escape(vname) + r"\b\s*(?:[+\-|&^]?=(?!=)|\+\+|--)",
                flat[vend:deny_pos])
            if ra:
                errors.append(
                    f"{fn}: predicate verdict '{vname}' is REASSIGNED between "
                    f"its helper call and the deny — an OOM/NULL fail-open could "
                    f"be smuggled in (e.g. `if (!exe) {vname} = true;`)")
    return errors


def check_gate_function(name, spec, source):
    errors = []
    body, err = _function_body(source, name)
    if err:
        return [err]
    if re.search(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b", body):
        return [f"{name}: contains a preprocessor conditional — a guard/deny "
                f"witness could hide in a dead branch; not allowed for a pin"]
    body = _strip_dead_if_zero(body)
    flat = re.sub(r"\s+", " ", body)
    # _strip_dead_if_zero only removes BRACED `if (0) { ... }`. An UNBRACED
    # `if (0) if (!ok) { ...deny... }` would survive and host a dead deny
    # witness that satisfies (B) while the live verdict is ignored. Refuse to
    # analyse a body with any residual constant-false `if` rather than risk it.
    resid = re.search(r"\bif\s*\(\s*(?:0|false)\s*\)", flat)
    if resid:
        return [f"{name}: residual `if (0)`/`if (false)` after dead-block "
                f"stripping (unbraced dead branch?) — can't be safely analysed "
                f"for a pin; rewrite to remove it"]
    # A `goto` in a gate handler can jump PAST the gate on the NULL path
    # (`if (!exe) goto after_gate;`) — a control-flow skip the brace-nesting
    # analysis can't see. These handlers legitimately contain none.
    if re.search(r"\bgoto\b", flat):
        return [f"{name}: contains a `goto` — a control-flow jump can skip the "
                f"identity gate (fail open). Not allowed in a gate handler; "
                f"restructure, or update the pin deliberately if truly needed"]

    fail_return = "return false" if not spec["bind"] else "return"

    first_gate_pos = None
    last_gate_end = 0
    for field in spec["fields"]:
        # (A) GUARD: `<type> <ok> = ( <id> [!= NULL] && strcmp(<id>,
        # qdwin->FIELD) == 0 )` — same identifier guards and is compared; the
        # `== 0` sense and the closing paren make a widened `|| x` not match.
        gate = re.compile(
            r"(?:int|bool)\s+(\w+)\s*=\s*\(\s*"
            r"(\w+)\s*(?:!=\s*NULL\s*)?&&\s*"
            r"strcmp\(\s*\2\s*,\s*qdwin->" + re.escape(field) +
            r"\s*\)\s*==\s*0\s*\)\s*;")  # `;`-anchored: the guard is the WHOLE
        # initializer, so a trailing `|| x` widening (`(id && strcmp==0) || x;`,
        # which constant-trues the verdict) does not match -> trips the pin.
        hits = list(gate.finditer(flat))
        if len(hits) == 0:
            errors.append(
                f"{name}: gate on qdwin->{field} is missing its canonical "
                f"NULL-short-circuited form `id && strcmp(id, qdwin->{field}) "
                f"== 0` (renamed field, dropped guard, or hidden in a wrapper "
                f"=> manual review + update GATE_FUNCS)")
            continue
        if len(hits) > 1:
            errors.append(
                f"{name}: gate on qdwin->{field} appears {len(hits)} times — a "
                f"duplicate witness can mask a live neutered path; expected 1")
            continue
        m = hits[0]
        verdict = m.group(1)
        idvar = m.group(2)
        if first_gate_pos is None or m.start() < first_gate_pos:
            first_gate_pos = m.start()
        last_gate_end = max(last_gate_end, m.end())
        # (D) NO IDENTITY-SKIP: the gate must not be nested in a conditional
        # that references the identity var (would fall through open on NULL).
        errors += check_no_identity_skip(flat, idvar, m.start(), name)
        # (F) PROVENANCE: the identity var must come straight from qdwin_proc_*()
        # — not be rewritten to the allowed value on the NULL path.
        errors += check_identity_provenance(flat, idvar, m.start(), name)
        # (B) VERDICT -> DENY: an `if (!verdict) { ...post error...; return
        # <fail-closed>; }` after the assignment. The verdict name (`ok`) is
        # reused per gate, so bound the search to THIS gate's scope — the text
        # before the SAME variable's next declaration — or a later gate's deny
        # block would be mistaken for this one's (masking a neutered gate).
        rest = flat[m.end():]
        nxt = re.search(r"(?:int|bool)\s+" + re.escape(verdict) + r"\s*=", rest)
        scope = rest[:nxt.start()] if nxt else rest
        deny = re.search(r"if\s*\(\s*!\s*" + re.escape(verdict) + r"\s*\)", scope)
        if not deny:
            errors.append(f"{name}: gate verdict '{verdict}' (qdwin->{field}) "
                          f"is never consumed by `if (!{verdict})` — guard "
                          f"result may be ignored")
            continue
        # No REASSIGNMENT of the verdict between the guard and its deny. A
        # `if (!exe) ok = 1;` / `ok |= uid_fallback;` reads as a plausible
        # "tolerate an unreadable /proc entry" change but fail-OPENS on OOM —
        # the exact inversion this pin forbids. Match `verdict =` / `|=` / `+=`
        # … but never `==` / `!=` (the `(?!=)` after the `=`, and requiring the
        # op char or bare `=` right after the name, excludes comparisons).
        between = scope[:deny.start()]
        reassign = re.search(
            r"\b" + re.escape(verdict) + r"\b\s*(?:[+\-|&^]?=(?!=)|\+\+|--)",
            between)
        if reassign:
            errors.append(f"{name}: verdict '{verdict}' (qdwin->{field}) is "
                          f"REASSIGNED between the guard and `if (!{verdict})` "
                          f"(`{between[reassign.start():reassign.start()+24].strip()}`) "
                          f"— an OOM fail-open could be smuggled in here")
            continue
        block = _brace_group_after(scope, deny.end())
        if block is None:
            errors.append(f"{name}: `if (!{verdict})` deny for qdwin->{field} "
                          f"is not a brace block")
            continue
        # Absolute end of this gate's deny block in `flat` — the point past
        # which this gate has fully cleared. (C) bars resource creation before
        # the LAST such barrier, so a bind can't be issued after an earlier gate
        # but before a later one.
        bo = flat.find("{", m.end() + deny.end())
        depth, be = 0, None
        for i in range(bo, len(flat)):
            if flat[i] == "{":
                depth += 1
            elif flat[i] == "}":
                depth -= 1
                if depth == 0:
                    be = i + 1
                    break
        if be is not None:
            last_gate_end = max(last_gate_end, be)
        if not any(p in block for p in POST_ERROR):
            errors.append(f"{name}: deny for qdwin->{field} does not post a "
                          f"protocol/implementation error to the client")
        if fail_return == "return false":
            if "return false" not in block:
                errors.append(f"{name}: deny for qdwin->{field} must `return "
                              f"false` (fail closed to caller); not found")
        elif not re.search(r"\breturn\b", block):
            errors.append(f"{name}: deny for qdwin->{field} does not return "
                          f"(would fall through past the gate)")

    # (E) NO EARLY ALLOW (bool gate helper): before all gates pass, the helper
    # may ONLY `return false` (deny). A pre-gate `if (!exe) return true;` would
    # ALLOW the bind on a NULL (OOM) identity while leaving the canonical gate
    # witness intact below it. (Void bind handlers have no allow-return; an
    # early bind-on-NULL needs a wl_resource_create before the gate, caught by
    # (C).) The early uid `return false`s and each gate's own `return false`
    # deny are permitted.
    if not spec["bind"] and last_gate_end:
        for rm in re.finditer(r"\breturn\b([^;]*);", flat[:last_gate_end]):
            val = rm.group(1).strip()
            if val not in ("false", "0"):
                errors.append(
                    f"{name}: `return {val};` appears before the identity gates "
                    f"— a bool gate helper may only `return false` (deny) until "
                    f"all gates pass; returning allow on a NULL path fails open")
                break

    # (C) ORDERING: for bind handlers, no resource creation until ALL gates have
    # cleared (before last_gate_end, the end of the last gate's deny block) — so
    # a bind can't be issued after an earlier gate but before a later one
    # (`if (!label) { wl_resource_create(...); return; }` between two gates).
    if spec["bind"] and last_gate_end:
        pre = flat[:last_gate_end]
        for tok in RESOURCE_CREATE:
            if re.search(re.escape(tok) + r"\s*\(", pre):
                errors.append(f"{name}: `{tok}(` occurs BEFORE all identity "
                              f"gates clear — the privileged resource must not "
                              f"be created until every peer check has passed")
    return errors


def check_predicate_gates(source):
    errors = []
    for name, arg in PREDICATE_GATES:
        body, err = _function_body(source, name)
        if err:
            errors.append(err)
            continue
        if re.search(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b", body):
            errors.append(f"{name}: preprocessor conditional present — a guard "
                          f"witness could hide in a dead branch")
            continue
        flat = re.sub(r"\s+", " ", _strip_dead_if_zero(body)).lstrip("{ ").strip()
        guard = re.compile(
            r"^if\s*\(\s*!\s*" + re.escape(arg) + r"\b[^)]*\)\s*return\s+false\s*;")
        if not guard.match(flat):
            errors.append(
                f"{name}: first statement is not `if (!{arg} ...) return "
                f"false;` — the NULL/empty fail-closed contract the identity "
                f"gate relies on is not pinned. Got: {flat[:90]}...")
    return errors


def main():
    if len(sys.argv) < 2:
        print("usage: test_identity_gate_oom_failclosed.py <qdwin.c>")
        return 2
    source = _strip_comments(Path(sys.argv[1]).read_text())

    errors = []
    for name, spec in GATE_FUNCS.items():
        errors += check_gate_function(name, spec, source)
    errors += check_predicate_gates(source)
    errors += check_predicate_call_sites(source)

    if errors:
        for e in errors:
            fail(e)
        return 1
    n_gates = sum(len(s["fields"]) for s in GATE_FUNCS.values())
    print(f"OK: {n_gates} identity gates across {len(GATE_FUNCS)} functions are "
          f"NULL-short-circuited, deny on mismatch, and gate before resource "
          f"creation; {len(PREDICATE_GATES)} predicate gates fail-closed on NULL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
