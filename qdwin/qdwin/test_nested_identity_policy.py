#!/usr/bin/env python3
"""Static-invariant checks for qdwin nested-proxy identity hardening.

These are negative-by-construction source checks (same style as
test_xdg_activation_policy.py): they parse qdwin.c and assert that the
nested-proxy paths bind client-asserted identity (origin_uid, input_sink,
bind_proxy_pixels) to the kernel/peer-resolved identity of the advertising
client rather than trusting the client's word.

The live multi-uid spoof scenarios (a client of uid B actually advertising
origin_uid=A, or connecting an input_sink owned by another uid) require a
running compositor with two uids and are exercised by the VM-only probe
host scenario; here we lock the enforcement in place so a regression that
drops a check fails the host test suite without a VM.
"""

from pathlib import Path
import re
import sys


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _function_body(source, signature_regex, name):
    """Return the brace-balanced body of the first function *definition*
    whose opening matches signature_regex, or (None, error_message).

    Skips forward declarations (signature followed by ';') by requiring the
    parameter-list close ')' to be followed by an opening '{'."""
    for m in re.finditer(signature_regex, source, re.MULTILINE | re.DOTALL):
        # Find the ')' that closes this signature's parameter list, then the
        # first non-space char after it. A definition has '{'; a forward
        # declaration has ';'.
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
            continue  # forward declaration; keep looking
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


def check_advertise_binds_origin_uid(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_nested_manager_advertise_toplevel\s*\(",
        "qdwin_nested_manager_advertise_toplevel",
    )
    if err:
        return fail(err)
    # The handler must resolve the advertising client's peer uid and
    # override a disagreeing client-asserted origin_uid.
    if "wl_client_get_credentials" not in body:
        return fail("advertise_toplevel does not resolve peer credentials")
    if not re.search(r"origin_uid\s*!=\s*\(uint32_t\)\s*peer_uid", body):
        return fail("advertise_toplevel does not compare origin_uid to "
                    "the advertising client's peer uid")
    if not re.search(r"origin_uid\s*=\s*\(uint32_t\)\s*peer_uid", body):
        return fail("advertise_toplevel does not override origin_uid with "
                    "the verified peer uid on mismatch")
    return 0


def check_nested_manager_bind_restricts_secctx(source):
    body, err = _function_body(
        source,
        r"static void\s+bind_qdwin_nested_manager\s*\(",
        "bind_qdwin_nested_manager",
    )
    if err:
        return fail(err)
    if "qdwin_secctx_client_find(qdwin, client)" not in body:
        return fail("nested_manager bind does not resolve secctx identity")
    if "qdwin_nested_secctx_publisher_allowed" not in body:
        return fail("nested_manager bind lacks the narrow nested-publisher "
                    "identity exception")
    secctx_pos = body.find("qdwin_secctx_client_find(qdwin, client)")
    identity_pos = body.find("qdwin_nested_secctx_publisher_allowed")
    reject_pos = body.find("wl_client_post_implementation_error", identity_pos)
    return_pos = body.find("return;", reject_pos)
    create_pos = body.find("wl_resource_create")
    if min(secctx_pos, identity_pos, reject_pos, return_pos, create_pos) < 0 or \
            not (secctx_pos < identity_pos < reject_pos < return_pos < create_pos):
        return fail("nested_manager secctx identity gate must reject an "
                    "unauthorized publisher before wl_resource_create")
    return 0


def check_locker_bind_rejects_secctx(source):
    body, err = _function_body(
        source,
        r"static void\s+bind_qdwin_locker\s*\(",
        "bind_qdwin_locker",
    )
    if err:
        return fail(err)
    if "qdwin_secctx_client_find(qdwin, client)" not in body:
        return fail("locker bind does not reject secctx clients")
    secctx_pos = body.find("qdwin_secctx_client_find(qdwin, client)")
    uid_pos = body.find("uid != qdwin->allowed_locker_uid")
    create_pos = body.find("wl_resource_create")
    if create_pos < 0 or secctx_pos < 0 or secctx_pos > create_pos:
        return fail("locker secctx bind gate must run before "
                    "wl_resource_create")
    if uid_pos >= 0 and secctx_pos > uid_pos:
        return fail("locker secctx bind gate should run before the uid-only "
                    "locker gate")
    return 0


def check_nested_advertise_has_cap(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_nested_manager_advertise_toplevel\s*\(",
        "qdwin_nested_manager_advertise_toplevel",
    )
    if err:
        return fail(err)
    if "QDWIN_NESTED_TOPLEVEL_CAP_PER_UID" not in source:
        return fail("nested advertise has no named per-uid/per-client cap")
    if "qdwin_nested_count_for_uid" not in body:
        return fail("advertise_toplevel does not count existing nested "
                    "toplevels before allocation")
    if "QDWIN_NESTED_MANAGER_V1_ERROR_POLICY_DENIED" not in body:
        return fail("advertise_toplevel cap does not fail closed with a "
                    "protocol error")
    count_pos = body.find("qdwin_nested_count_for_uid")
    create_pos = body.find("wl_resource_create")
    if create_pos < 0 or count_pos < 0 or count_pos > create_pos:
        return fail("nested advertise cap must run before wl_resource_create "
                    "and calloc")
    return 0


def check_nested_broker_gate_defaults_closed(source):
    body, err = _function_body(
        source,
        r"static struct qdwin_toplevel \*\s+qdwin_nested_proxy_create\s*\(",
        "qdwin_nested_proxy_create",
    )
    if err:
        return fail(err)
    if "QDWIN_NESTED_BROKER_OPTIONAL" not in body:
        return fail("nested broker gate lacks an explicit dev/test optional "
                    "override")
    if re.search(r"bool\s+gate_required\s*=\s*false\s*;", body):
        return fail("nested broker gate still defaults optional/open")
    if not re.search(r"bool\s+gate_required\s*=\s*true\s*;", body):
        return fail("nested broker gate does not default to required/closed")
    if "strcmp(opt_env, \"1\") == 0" not in body:
        return fail("nested broker optional mode is not explicitly gated by "
                    "QDWIN_NESTED_BROKER_OPTIONAL=1")
    return 0


def check_input_sink_peercred(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_nested_manager_advertise_toplevel\s*\(",
        "qdwin_nested_manager_advertise_toplevel",
    )
    if err:
        return fail(err)
    # After connecting to the client-asserted input_sink, the handler must
    # verify the connected peer's uid (SO_PEERCRED) equals origin_uid and
    # fail closed (close fd) otherwise.
    if "qdwin_fd_peer_uid" not in body:
        return fail("advertise_toplevel does not peer-cred the input sink "
                    "socket (qdwin_fd_peer_uid missing)")
    if not re.search(r"sink_uid\s*!=\s*\(uid_t\)\s*origin_uid", body):
        return fail("advertise_toplevel does not compare input-sink peer "
                    "uid to origin_uid")
    # The mismatch branch must drop the fd (fail closed → display-only).
    guard = re.search(
        r"if\s*\(\s*!qdwin_fd_peer_uid\([^)]*\)\s*\|\|\s*"
        r"sink_uid\s*!=\s*\(uid_t\)\s*origin_uid\s*\)\s*\{(.*?)\}",
        body, re.DOTALL)
    if not guard:
        return fail("input-sink peer-uid guard not found in expected shape")
    if "close(fd)" not in guard.group(1):
        return fail("input-sink peer-uid mismatch does not close the fd "
                    "(fail closed)")
    return 0


def check_fd_peer_uid_fails_closed(source):
    body, err = _function_body(
        source,
        r"static int\s+qdwin_fd_peer_uid\s*\(",
        "qdwin_fd_peer_uid",
    )
    if err:
        return fail(err)
    if "SO_PEERCRED" not in body:
        return fail("qdwin_fd_peer_uid does not use SO_PEERCRED")
    # getsockopt failure (and short read) must return 0 (unverifiable).
    if "getsockopt" not in body or "return 0" not in body:
        return fail("qdwin_fd_peer_uid does not fail closed on getsockopt "
                    "failure")
    return 0


def check_bind_proxy_pixels_ownership(source):
    body, err = _function_body(
        source,
        r"qdwin_handle_bind_proxy_pixels\s*\(\s*struct wl_client",
        "qdwin_handle_bind_proxy_pixels",
    )
    if err:
        return fail(err)
    if "wl_client_get_credentials" not in body:
        return fail("bind_proxy_pixels does not resolve caller credentials")
    if not re.search(r"cuid\s*!=\s*\(uid_t\)\s*tl->proxy_origin_uid", body):
        return fail("bind_proxy_pixels does not compare caller uid to the "
                    "proxy's verified origin uid")
    # The mismatch branch must reject (post_error + return), not proceed.
    guard = re.search(
        r"if\s*\(\s*cuid\s*!=\s*\(uid_t\)\s*tl->proxy_origin_uid\s*\)\s*\{"
        r"(.*?)\n\t\}",
        body, re.DOTALL)
    if not guard:
        return fail("bind_proxy_pixels ownership guard not found")
    if "wl_resource_post_error" not in guard.group(1) or \
            "return;" not in guard.group(1):
        return fail("bind_proxy_pixels ownership mismatch does not reject")
    return 0


def check_bind_proxy_pixels_preserves_pending_gate(source):
    """F6#2 mechanism A (stash-and-defer): while a nested proxy is still
    PENDING the admin broker decision, bind_proxy_pixels must NOT make the
    client pixel surface input-routable. A client pixel surface has a
    default-FULL input region, so creating ANY view for it (even below the
    input-transparent background curtain) would let weston_compositor_pick_view
    route pointer input to an UNAPPROVED proxy. The current (commit 55620bc)
    design therefore stashes the surface with NO view created — it sets
    tl->proxy_pixel_surface = ws and returns early — and defers the real
    curtain->pixel swap to qdwin_nested_proxy_activate_pixel_surface() on allow.

    This used to be enforced by parking a pending view on held_layer via a
    `pending ? &held_layer.view_list : &normal_layer.view_list` ternary; that
    older shape was *input-routable* and was replaced by the strictly safer
    stash-and-defer. We re-lock the CURRENT invariant: the pending branch
    consults the broker gate, stashes the surface, returns early, and creates
    NO view / does no layer placement while pending. A regression that
    reintroduced a view-on-any-layer (held or normal) for a pending proxy would
    fail this check.

    Two ordering clauses strengthen this against a "pickable before/after the
    gate" regression that leaves the pending *block* itself clean:
      - clause (b) requires the pending block to END with an unconditional
        `return;` (and forbids `goto`), so the pending path PROVABLY exits at
        the gate and can never fall through (conditional return) or jump (goto)
        to the view-creating swap that follows the block. This is also why a
        SECOND `if (tl->nested_proxy_pending_decision) { ...create... }` placed
        after the first block is not a hole: the first block has already
        returned for the pending case, so that later branch is dead code for a
        pending proxy (only reached on the !pending fall-through).
      - clause (e) scans the function PREFIX up to and including the pending
        block and rejects the libweston view create/place/map *primitives*
        (weston_view_create / _move_to_layer / _set_position[_with_offset] /
        weston_surface_map[_with_input]) before the gate, excepting the
        teardown/read ops that can never make a NEW surface pickable
        (weston_view_destroy, weston_view_get_pos_offset_global).

    Residual, by design: this is a source-SHAPE invariant, not an
    interprocedural C analyzer. It catches the direct libweston create/place/
    map primitives before or inside the pending gate, and (via clause (b))
    proves the pending path cannot reach the post-gate direct primitives. It
    does NOT catch a pre-gate helper or object/function-like macro whose BODY
    hides such a primitive (e.g. `qdwin_prepare_pixels(tl, ws);` before the
    gate, or a `QDWIN_CREATE(ws)` alias) — such a wrapper runs before the gate
    and CAN create/place the pending pixel view. That limitation is accepted
    for consistency with the rest of this token-lock test suite (every check
    here locks a source shape, not the absence of behaviour hidden behind an
    arbitrary wrapper in another function); growing this into a whole-file
    macro scanner + blanket prefix call-allowlist would worsen false positives
    on safe refactors (helperized validation, an `if (!pending) {create;return;}`
    approved-first shape) while still only approximating C semantics. The load-
    bearing signals are the positive shape (stash + unconditional return + no
    view/list in the block) plus the direct-primitive bans."""
    body, err = _function_body(
        source,
        r"qdwin_handle_bind_proxy_pixels\s*\(\s*struct wl_client",
        "qdwin_handle_bind_proxy_pixels",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in body:
        return fail("bind_proxy_pixels does not consult pending broker gate")

    # Extract the brace-balanced body of the pending-decision branch:
    #   if (tl->nested_proxy_pending_decision) { ... }
    m = re.search(r"if\s*\(\s*tl->nested_proxy_pending_decision\s*\)\s*\{",
                  body)
    if not m:
        return fail("bind_proxy_pixels has no pending-decision branch "
                    "(if (tl->nested_proxy_pending_decision) { ... })")
    open_brace = body.index("{", m.start())
    depth = 0
    pending_block = None
    for i in range(open_brace, len(body)):
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                pending_block = body[open_brace:i + 1]
                break
    if pending_block is None:
        return fail("bind_proxy_pixels pending-decision branch has "
                    "unbalanced braces")

    # (a) It must STASH the pixel surface (mechanism A) ...
    if not re.search(r"tl->proxy_pixel_surface\s*=\s*ws\s*;", pending_block):
        return fail("bind_proxy_pixels does not stash the pixel surface "
                    "(tl->proxy_pixel_surface = ws) while pending")
    # (b) ... and defer the curtain swap by UNCONDITIONALLY returning, so the
    #     pending path can never fall through to the view-creating swap below.
    #     A mere `return;` *token* is not enough: a conditional return
    #         if (tl->nested_proxy_pending_decision) {
    #             ...stash...; if (rare) return;       /* token present */
    #         }                                         /* pending FALLS THROUGH */
    #         struct weston_view *pv = weston_view_create(ws);  /* now pickable */
    #     would satisfy a token check while letting the pending case reach the
    #     create after the block. Likewise a `goto` out of the block could jump
    #     past the gate into the create. So require the pending block's LAST
    #     statement to be a bare unconditional `return;`, and forbid `goto`
    #     inside the block. With an unconditional terminal return, the pending
    #     path provably exits at the gate, so the prefix-only scan in clause (e)
    #     covers every direct primitive the pending path can reach (before OR
    #     after the block; a pre-gate helper/macro hiding a primitive is the
    #     accepted residual documented in the function docstring).
    if not re.search(r"\breturn\s*;", pending_block):
        return fail("bind_proxy_pixels does not defer the curtain swap "
                    "(early return) while pending")
    if re.search(r"\bgoto\b", pending_block):
        return fail("bind_proxy_pixels pending branch uses goto — the pending "
                    "path must exit via an unconditional terminal return, not "
                    "jump past the gate; a goto could reach the view-creating "
                    "swap (must stash-and-defer)")
    # The last statement of the pending block must be a bare `return;`. Strip
    # comments + literals, drop the block's own outer braces, and check the
    # final non-empty token sequence is exactly `return ;`.
    inner = pending_block[pending_block.index("{") + 1:
                          pending_block.rindex("}")]
    inner = re.sub(r"/\*.*?\*/", " ", inner, flags=re.DOTALL)
    inner = re.sub(r"//[^\n]*", " ", inner)
    inner = re.sub(r'"(?:\\.|[^"\\])*"', '""', inner)
    inner = re.sub(r"'(?:\\.|[^'\\])*'", "''", inner)
    inner = inner.strip()
    # The block must END with a bare `return;` ...
    if not re.search(r"return\s*;$", inner):
        return fail("bind_proxy_pixels pending branch does not END with a "
                    "`return;` — a missing terminal return lets the pending "
                    "case fall through to the view-creating swap below (must "
                    "stash-and-defer)")
    # ... and that terminal return must be UNCONDITIONAL: the statement before
    # it must be complete (end in ';' or '}'), so the return is not the
    # consequent of an inline `if (...) return;` / loop. A conditional return
    # would let the pending case fall through when the condition is false.
    before_return = inner[:inner.rfind("return")].rstrip()
    if before_return and before_return[-1] not in ";}":
        return fail("bind_proxy_pixels pending branch's terminal `return;` is "
                    "CONDITIONAL (e.g. `if (...) return;`) — the pending case "
                    "can fall through to the view-creating swap when the "
                    "condition is false; the gate must unconditionally return "
                    "(must stash-and-defer)")
    # (c) Teeth: the pending branch must NOT make the proxy input-routable.
    #     No view may be created or moved onto ANY layer while pending — that
    #     is precisely the regression (an input-routable view on held_layer or
    #     normal_layer) this invariant guards against.
    if "weston_view_create" in pending_block:
        return fail("bind_proxy_pixels CREATES a view for a pending proxy "
                    "(weston_view_create) — pending pixels would become "
                    "input-routable; must stash-and-defer instead")
    if "weston_view_move_to_layer" in pending_block:
        return fail("bind_proxy_pixels places a pending proxy view on a "
                    "layer (weston_view_move_to_layer) — pending pixels "
                    "would become input-routable; must stash-and-defer")
    if "view_list" in pending_block:
        return fail("bind_proxy_pixels references a view_list while pending "
                    "— a pending proxy must hold NO view on any layer "
                    "(held or normal); must stash-and-defer")
    # (d) Close the static-grep bypass: a helper call whose name does not
    #     contain the forbidden tokens above (e.g. a future
    #     qdwin_make_pending_proxy_pickable()) could still create/place a
    #     view from inside the pending branch. The deferral contract is that
    #     while pending we ONLY stash the surface + register its destroy
    #     listener + log, then return — so allowlist exactly those calls and
    #     reject any other function call in the pending block.
    allowed_calls = {"wl_signal_add", "wl_list_remove", "wl_list_init",
                     "wl_list_empty", "weston_log"}
    # Scan for calls on CODE only: strip C string/char literals and comments
    # first so that e.g. a "%p (" inside a weston_log format string is not
    # mistaken for a call to "p(...)".
    code = re.sub(r"/\*.*?\*/", " ", pending_block, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", " ", code)
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    code = re.sub(r"'(?:\\.|[^'\\])*'", "''", code)
    for call in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", code):
        name = call.group(1)
        # Skip the control-flow keywords that the tokenizer also matches.
        if name in {"if", "for", "while", "switch", "return", "sizeof"}:
            continue
        if name not in allowed_calls:
            return fail("bind_proxy_pixels pending branch calls "
                        f"'{name}' — while pending it may only stash the "
                        "surface, register its destroy listener and log "
                        "before returning; any other call risks creating an "
                        "input-routable view (must stash-and-defer)")
    # (e) Ordering teeth: clauses (a)-(d) only inspect the *pending block*, so
    #     they cannot see a view created on the code path BEFORE the gate. A
    #     regression could make the proxy pickable up front and THEN stash +
    #     return inside the pending branch (the pending block is clean, yet the
    #     pending proxy already holds an input-routable view):
    #         struct weston_view *pv = weston_view_create(ws);
    #         weston_view_move_to_layer(pv, &qdwin->normal_layer.view_list);
    #         if (tl->nested_proxy_pending_decision) { ...stash...; return; }
    #     So assert the pending gate runs BEFORE any view is created/placed/
    #     mapped: the function PREFIX up to and including the pending block must
    #     not create, place, position or map a view. The legitimate pre-gate
    #     work is credential/handle/surface validation plus tearing down a
    #     prior pixel view/stash (consumer respawn) and reading the current
    #     position for a seamless swap — none of which makes a *new* surface
    #     pickable — so allowlist exactly the view ops that only DESTROY or READ
    #     (never create/place/map) and reject the concrete libweston create/
    #     place/map primitives before the gate. (A pre-gate helper/macro that
    #     hides such a primitive in another function is the accepted text-grep
    #     residual documented above, not covered here.)
    prefix = body[:body.index(pending_block) + len(pending_block)]
    pcode = re.sub(r"/\*.*?\*/", " ", prefix, flags=re.DOTALL)
    pcode = re.sub(r"//[^\n]*", " ", pcode)
    pcode = re.sub(r'"(?:\\.|[^"\\])*"', '""', pcode)
    pcode = re.sub(r"'(?:\\.|[^'\\])*'", "''", pcode)
    # View ops that only destroy a prior view or read its geometry — they can
    # never turn an unapproved surface into a pickable view, so they are safe
    # before the gate.
    prefix_view_allow = {"weston_view_destroy",
                         "weston_view_get_pos_offset_global"}
    for call in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", pcode):
        name = call.group(1)
        if name in prefix_view_allow:
            continue
        # Reject the concrete libweston primitives that create/place/position
        # or map a view — these are the only ways a surface becomes pickable,
        # and (with the allowlisted destroy/read ops excepted) none of them has
        # any business running before the pending gate. We key on the weston_*
        # TCB names (not a generic `*_show`/`*_map` suffix, which would wrongly
        # reject benign project getters like qdwin_should_show() /
        # qdwin_input_remap()): any qdwin wrapper that creates a view does so by
        # itself calling one of these weston_view_* / weston_surface_map
        # primitives, so the primitive — not the wrapper name — is the real
        # signal. (A wrapper *called* before the gate that hides the primitive
        # in another function is the deliberate residual noted in the docstring;
        # the unconditional-terminal-return proof in clause (b) means the
        # pending path still cannot REACH any post-gate create regardless.)
        if (re.match(r"weston_view_(?:create|move_to_layer|set_position|"
                     r"set_position_with_offset)$", name)
                or name in ("weston_surface_map",
                            "weston_surface_map_with_input")):
            return fail("bind_proxy_pixels reaches a view create/place/map "
                        f"call '{name}' BEFORE the pending-decision gate — a "
                        "pending proxy would become input-routable before the "
                        "stash-and-defer; the gate must run before any view is "
                        "created, placed or mapped")
    return 0


def check_pixel_destroy_preserves_pending_gate(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_proxy_pixel_surface_destroyed\s*\(",
        "qdwin_proxy_pixel_surface_destroyed",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in body:
        return fail("pixel-surface destroy path does not consult pending gate")
    if "&qdwin->held_layer.view_list" not in body:
        return fail("pixel-surface destroy path cannot restore held curtain")
    return 0


def check_geometry_resize_preserves_pending_gate(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_nested_proxy_set_geometry\s*\(",
        "qdwin_nested_proxy_set_geometry",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in body:
        return fail("nested-proxy geometry resize does not consult pending gate")
    if "&qdwin->held_layer.view_list" not in body:
        return fail("nested-proxy geometry resize cannot keep pending curtain "
                    "on held layer")
    return 0


def check_stale_decisions_are_idempotent(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_handle_nested_proxy_decision\s*\(",
        "qdwin_handle_nested_proxy_decision",
    )
    if err:
        return fail(err)
    deny_case = re.search(
        r"case\s+1:\s*/\*\s*deny\s*\*/(.*?)case\s+2:",
        body, re.DOTALL)
    if not deny_case:
        return fail("nested_proxy_decision deny case not found")
    text = deny_case.group(1)
    if "!tl->nested_proxy_pending_decision" not in text:
        return fail("nested_proxy_decision deny does not ignore non-pending "
                    "handles")
    if not re.search(
            r"if\s*\(\s*!tl->nested_proxy_pending_decision\s*\)\s*\{"
            r".*?return;",
            text, re.DOTALL):
        return fail("nested_proxy_decision stale deny does not return before "
                    "destroying proxy")

    defer_case = re.search(
        r"case\s+2:\s*/\*\s*defer\s*\*/(.*?)default:",
        body, re.DOTALL)
    if not defer_case:
        return fail("nested_proxy_decision defer case not found")
    text = defer_case.group(1)
    if "!tl->nested_proxy_pending_decision" not in text:
        return fail("nested_proxy_decision defer does not ignore non-pending "
                    "handles")
    if not re.search(
            r"if\s*\(\s*!tl->nested_proxy_pending_decision\s*\)\s*\{"
            r".*?return;",
            text, re.DOTALL):
        return fail("nested_proxy_decision stale defer does not return as "
                    "an idempotent no-op")
    return 0


def check_focus_requests_preserve_pending_gate(source):
    for fn in ("qdwin_handle_set_keyboard_focus",
               "qdwin_handle_set_keyboard_focus_v2"):
        body, err = _function_body(
            source,
            rf"static void\s+{fn}\s*\(",
            fn,
        )
        if err:
            return fail(err)
        if "tl->nested_proxy_pending_decision" not in body:
            return fail(f"{fn} does not reject pending nested proxies")
        if not re.search(
                r"if\s*\(\s*tl->nested_proxy_pending_decision\s*\)\s*\{"
                r".*?return;",
                body, re.DOTALL):
            return fail(f"{fn} pending nested-proxy branch does not return "
                        "before focusing")
    return 0


def check_focus_requests_shell_role_gated(source):
    """The set_keyboard_focus handlers (and clear_selection) redirect keyboard
    focus and/or clear the seat/primary selections — privileged shell-only
    operations ("Sent by the shell" in qdwin-shell-v1.xml). They must gate on
    qdwin_shell_require_bound (fail-closed: rejects until a client claims the
    shell role via bind_as_shell), like the rest of the shell request surface
    (move_toplevel_to_workspace, set_workspace_name, ...). Without it a
    non-shell allowed_uid client holding a qdwin_shell_v1 resource could steer
    focus and wipe selections before any shell binds (second-bind rejection
    only triggers once shell_bound is set)."""
    # Privileged side effects that must never run before the shell-bound gate.
    sinks = ("weston_seat_set_selection",
             "weston_keyboard_set_focus",
             "weston_seat_set_keyboard_focus",
             "qdwin_primary_seat_clear_selection")
    for fn in ("qdwin_handle_set_keyboard_focus",
               "qdwin_handle_set_keyboard_focus_v2",
               "qdwin_handle_clear_selection"):
        body, err = _function_body(
            source,
            rf"static void\s+{fn}\s*\(",
            fn,
        )
        if err:
            return fail(err)
        if "qdwin_shell_require_bound" not in body:
            return fail(f"{fn} does not gate on the shell-bound check "
                        "(qdwin_shell_require_bound)")
        # The gate must guard the privileged side effects: it has to appear
        # before the first selection-clearing / focus-setting call, not after.
        gate = body.index("qdwin_shell_require_bound")
        for sink in sinks:
            at = body.find(sink)
            if at != -1 and at < gate:
                return fail(f"{fn} reaches {sink} before the shell-bound "
                            "gate")
    return 0


def check_minimize_preserves_pending_gate(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_toplevel_set_minimized\s*\(",
        "qdwin_toplevel_set_minimized",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in body:
        return fail("minimize path does not reject pending nested proxies")
    if not re.search(
            r"if\s*\(\s*tl->nested_proxy_pending_decision\s*\)\s*\{"
            r".*?return;",
            body, re.DOTALL):
        return fail("minimize pending nested-proxy branch does not return "
                    "before moving layers")
    return 0


def check_view_stream_preserves_pending_gate(source):
    subscribe_body, err = _function_body(
        source,
        r"static void\s+qdwin_handle_subscribe_view_stream\s*\(",
        "qdwin_handle_subscribe_view_stream",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in subscribe_body:
        return fail("subscribe_view_stream does not reject pending nested "
                    "proxies")
    if "qdwin_view_stream_v1_send_denied" not in subscribe_body:
        return fail("subscribe_view_stream pending nested-proxy branch does "
                    "not send a denied event")

    pin_body, err = _function_body(
        source,
        r"static void\s+qdwin_view_stream_pin\s*\(",
        "qdwin_view_stream_pin",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in pin_body:
        return fail("view_stream_pin lacks defense-in-depth pending gate")

    focus_body, err = _function_body(
        source,
        r"static void\s+qdwin_stream_seat_assert_focus\s*\(",
        "qdwin_stream_seat_assert_focus",
    )
    if err:
        return fail(err)
    if "s->tl->nested_proxy_pending_decision" not in focus_body:
        return fail("stream input focus lacks defense-in-depth pending gate")
    return 0


def check_state_requests_preserve_pending_gate(source):
    for fn in ("qdwin_toplevel_set_maximized",
               "qdwin_toplevel_set_fullscreen",
               "qdwin_toplevel_set_tiled"):
        body, err = _function_body(
            source,
            rf"static void\s+{fn}\s*\(",
            fn,
        )
        if err:
            return fail(err)
        if "tl->nested_proxy_pending_decision" not in body:
            return fail(f"{fn} does not reject pending nested proxies")
        if not re.search(
                r"if\s*\(\s*tl->nested_proxy_pending_decision\s*\)\s*\{"
                r".*?return;",
                body, re.DOTALL):
            return fail(f"{fn} pending nested-proxy branch does not return "
                        "before moving/resizing")

    move_body, err = _function_body(
        source,
        r"static void\s+qdwin_handle_begin_interactive_move\s*\(",
        "qdwin_handle_begin_interactive_move",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in move_body:
        return fail("begin_interactive_move does not reject pending nested "
                    "proxies")
    return 0


def check_pixel_destroy_preserves_position(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_proxy_pixel_surface_destroyed\s*\(",
        "qdwin_proxy_pixel_surface_destroyed",
    )
    if err:
        return fail(err)
    if "weston_view_get_pos_offset_global" not in body:
        return fail("pixel-surface destroy path does not preserve current "
                    "pixel-feed position")
    if "qdwin_primary_output" in body:
        return fail("pixel-surface destroy path recenters fallback curtain "
                    "instead of preserving position")
    return 0


def check_generic_raise_paths_preserve_pending_gate(source):
    move_body, err = _function_body(
        source,
        r"static void\s+qdwin_toplevel_move_to_layer\s*\(",
        "qdwin_toplevel_move_to_layer",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in move_body:
        return fail("generic toplevel move helper does not guard pending "
                    "nested proxies")
    if "&tl->qdwin->held_layer" not in move_body:
        return fail("generic toplevel move helper does not redirect pending "
                    "normal-layer moves to held_layer")

    request_body, err = _function_body(
        source,
        r"static void\s+qdwin_handle_request_raise\s*\(",
        "qdwin_handle_request_raise",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in request_body:
        return fail("request_raise does not ignore pending nested proxies")

    hit_body, err = _function_body(
        source,
        r"static struct qdwin_toplevel \*\s+qdwin_toplevel_at_pos\s*\(",
        "qdwin_toplevel_at_pos",
    )
    if err:
        return fail(err)
    # The stacking-aware implementation names the matched toplevel `cand`,
    # while the older list-order implementation named it `tl`.  Pin the
    # security state check itself instead of a local variable spelling.
    if "nested_proxy_pending_decision" not in hit_body:
        return fail("click hit-test does not skip pending nested proxies")

    chrome_body, err = _function_body(
        source,
        r"static struct qdwin_toplevel \*\s+qdwin_chrome_at_pos\s*\(",
        "qdwin_chrome_at_pos",
    )
    if err:
        return fail(err)
    if "tl->nested_proxy_pending_decision" not in chrome_body:
        return fail("chrome hit-test does not skip pending nested proxies")
    return 0


def check_stream_input_helper_bound(source):
    """The view-stream input claim remains filter-visible by design, so the
    request-time capability must be unguessable, one-shot, and tied to the
    spawned helper pid."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_stream_input_handle_claim\s*\(",
        "qdwin_stream_input_handle_claim",
    )
    if err:
        return fail(err)
    if "wl_client_get_credentials" not in body:
        return fail("stream_input claim does not resolve peer credentials")
    if "pid != s->forward_pid" not in body:
        return fail("stream_input claim no longer binds the claiming pid to "
                    "the spawned forward helper")
    if "s->input_claimed" not in body:
        return fail("stream_input claim no longer enforces one-shot token use")
    if "QDWIN_STREAM_INPUT_V1_ERROR_ALREADY_CLAIMED" not in body:
        return fail("stream_input duplicate claim does not fail closed")
    token_fn, err = _function_body(
        source,
        r"static int\s+qdwin_hex_token\s*\(",
        "qdwin_hex_token",
    )
    if err:
        return fail(err)
    if "getrandom" not in token_fn:
        return fail("stream_input token source is not kernel getrandom")
    stream_body, err = _function_body(
        source,
        r"static void\s+qdwin_handle_subscribe_view_stream\s*\(",
        "qdwin_handle_subscribe_view_stream",
    )
    if err:
        return fail(err)
    if "qdwin_hex_token(s->access_token, sizeof s->access_token, 16)" not in stream_body:
        return fail("stream_input access_token is not generated from 16 "
                    "random bytes")
    if "qdwin_view_stream_spawn_forward" not in stream_body:
        return fail("stream_input access_token is not handed only to the "
                    "spawned forward helper path")
    return 0


def check_stream_input_token_one_shot(source):
    """iso2 `10` E4: the protocol calls the stream access_token one-shot.
    Releasing the claimed handle must ERASE the token (not just clear
    input_claimed), and the token lookup must never match an erased token,
    so a second claim with the same token after release fails closed."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_stream_input_handle_resource_destroyed\s*\(",
        "qdwin_stream_input_handle_resource_destroyed",
    )
    if err:
        return fail(err)
    if "memset(s->access_token, 0, sizeof s->access_token)" not in body:
        return fail("stream_input handle release no longer erases the "
                    "access_token (token would be re-claimable)")
    if "s->input_claimed = 0" not in body:
        return fail("stream_input handle release no longer clears "
                    "input_claimed")
    lookup, err = _function_body(
        source,
        r"static struct qdwin_view_stream \*\s*qdwin_view_stream_by_token\s*\(",
        "qdwin_view_stream_by_token",
    )
    if err:
        return fail(err)
    if "!token[0]" not in lookup or "s->access_token[0] &&" not in lookup:
        return fail("qdwin_view_stream_by_token can match an erased "
                    "(empty) access_token")
    # iso2 `11` E4: no clock-derived token fallback when getrandom fails.
    gen, err = _function_body(
        source,
        r"static int\s+qdwin_generate_token\s*\(",
        "qdwin_generate_token",
    )
    if err:
        return fail(err)
    if "time(NULL)" in gen:
        return fail("qdwin_generate_token fabricates tokens from time(NULL)")
    if "qdwin_getrandom_full" not in gen:
        return fail("qdwin_generate_token does not use the EINTR/short-read "
                    "safe getrandom wrapper")
    rnd, err = _function_body(
        source,
        r"static int\s+qdwin_getrandom_full\s*\(",
        "qdwin_getrandom_full",
    )
    if err:
        return fail(err)
    if "EINTR" not in rnd:
        return fail("qdwin_getrandom_full does not retry on EINTR")
    if "qdwin_generate_token(token_buf) < 0" not in source:
        return fail("xdg-activation commit does not check "
                    "qdwin_generate_token failure")
    return 0


def check_nested_proxy_forwards_secctx(source):
    """J12 Fix B: a nested tier-2 proxy has no backing app wl_client
    (desktop_surface == NULL), so qdwin_send_toplevel_security_context cannot
    resolve the app's tag from the proxy itself. Instead the *advertising*
    container's own secctx tag (it is the sandboxed silo in the per-uid
    tier-2 model) is captured onto the proxy at advertise time and forwarded
    to the shell as toplevel_security_context. Lock both halves so a
    regression that reverts to the old unconditional skip — which left tier-2
    windows with no silo/app identity for the launcher badge + placeholder
    resolution — fails host CI without a VM."""
    # (1) proxy_create captures the advertiser's tag BEFORE the first
    #     qdwin_send_toplevel_added (which drives the emit), keyed off the
    #     advertising client's kernel-resolved secctx, not client-asserted data.
    create, err = _function_body(
        source,
        r"static struct qdwin_toplevel \*\s+qdwin_nested_proxy_create\s*\(",
        "qdwin_nested_proxy_create",
    )
    if err:
        return fail(err)
    if "qdwin_secctx_client_lookup" not in create:
        return fail("qdwin_nested_proxy_create does not look up the "
                    "advertising client's secctx tag (J12 Fix B)")
    # The looked-up client MUST be the advertiser (owner->resource's client),
    # not some other in-scope client — otherwise the proxy could be stamped
    # with the wrong identity.
    if "wl_resource_get_client(owner->resource)" not in create:
        return fail("qdwin_nested_proxy_create does not derive the secctx "
                    "lookup client from owner->resource (the advertiser) — "
                    "the tag could be the wrong client's identity")
    if "proxy_secctx_set" not in create:
        return fail("qdwin_nested_proxy_create does not stash the advertiser "
                    "secctx tag on the proxy (proxy_secctx_set)")
    # The stash must be armed all-or-nothing. The accessors are guaranteed
    # non-NULL (""), so a NULL field can only be a failed strdup — and the
    # send path normalises NULL to "", so arming on a PARTIAL capture would
    # emit a tagged-looking event with an empty field (an empty instance_id
    # the shell cannot match, so the placeholder times out anyway while the
    # event claims the window IS identified). Emitting nothing is the honest
    # degradation, and it keeps "an event was sent" meaning "all three fields
    # are the advertiser's".
    m = re.search(
        r"proxy_secctx_set\s*=\s*tl->proxy_secctx_engine\s*&&\s*"
        r"tl->proxy_secctx_app_id\s*&&\s*tl->proxy_secctx_instance", create)
    if not m:
        return fail("qdwin_nested_proxy_create arms proxy_secctx_set without "
                    "requiring all three stashed fields — a partial (OOM) "
                    "capture would emit a secctx event with an empty field")
    lookup_pos = create.find("qdwin_secctx_client_lookup")
    added_pos = create.find("qdwin_send_toplevel_added")
    if added_pos < 0 or lookup_pos < 0 or lookup_pos > added_pos:
        return fail("qdwin_nested_proxy_create must capture the advertiser "
                    "secctx BEFORE qdwin_send_toplevel_added so the tag is "
                    "sent on the first advertise, not only on a shell rebind")
    # (2) The secctx sender emits the stashed tag for nested proxies instead
    #     of unconditionally skipping them.
    send, err = _function_body(
        source,
        r"static void\s+qdwin_send_toplevel_security_context\s*\(",
        "qdwin_send_toplevel_security_context",
    )
    if err:
        return fail(err)
    m = re.search(r"if\s*\(\s*tl->is_nested_proxy\s*\)\s*\{", send)
    if not m:
        return fail("qdwin_send_toplevel_security_context no longer has an "
                    "explicit nested-proxy branch (J12 Fix B regressed to an "
                    "unconditional skip)")
    open_brace = send.index("{", m.start())
    depth = 0
    branch = None
    for i in range(open_brace, len(send)):
        c = send[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                branch = send[open_brace:i + 1]
                break
    if branch is None:
        return fail("nested-proxy branch has unbalanced braces")
    if "qdwin_shell_v1_send_toplevel_security_context" not in branch:
        return fail("nested-proxy branch does not forward the secctx tag to "
                    "the shell (still skips proxies — J12 Fix B regressed)")
    if "proxy_secctx_set" not in branch:
        return fail("nested-proxy branch does not gate on proxy_secctx_set "
                    "(an untagged advertiser must emit nothing — the "
                    "absence==not-sandboxed contract)")
    # The emit must forward the *stashed* tag (proxy_secctx_*), not some other
    # in-scope strings — otherwise the shell could receive a wrong/empty tag.
    send_call = branch[branch.find("qdwin_shell_v1_send_toplevel_security_context"):]
    for field in ("proxy_secctx_engine", "proxy_secctx_app_id",
                  "proxy_secctx_instance"):
        if field not in send_call:
            return fail("nested-proxy secctx emit does not pass the stashed "
                        f"{field} to the shell (J12 Fix B)")
    return 0


def check_nested_input_peer_replacement(source):
    """A replacement peer must retire its old event source before the sink
    helper closes the old fd, and a stale callback must not touch the new fd."""
    listen, err = _function_body(
        source,
        r"static int\s+qdwin_nested_input_sink_listen_cb\s*\(",
        "qdwin_nested_input_sink_listen_cb",
    )
    if err:
        return fail(err)
    remove_at = listen.find("wl_event_source_remove(")
    accept_at = listen.find("qdwin_nested_input_sink_accept(")
    if remove_at < 0 or accept_at < 0 or remove_at > accept_at:
        return fail("nested input replacement does not remove the old event "
                    "source before accept closes/replaces its peer fd")
    if "old_peer_fd" not in listen or "peer_fd == old_peer_fd" not in listen:
        return fail("nested input replacement does not restore the old watch "
                    "when accept loses a readiness race")

    peer, err = _function_body(
        source,
        r"static int\s+qdwin_nested_input_sink_peer_cb\s*\(",
        "qdwin_nested_input_sink_peer_cb",
    )
    if err:
        return fail(err)
    current_at = peer.find("qdwin_nested_input_peer_event_current(")
    hangup_at = peer.find("WL_EVENT_HANGUP")
    if current_at < 0 or hangup_at < 0 or current_at > hangup_at:
        return fail("nested input peer callback can process HUP before proving "
                    "the event fd is still current")
    if "ignoring stale input-sink peer event" not in peer:
        return fail("stale nested input peer rejection is not observable")
    return 0


def check_remote_identity_is_protected_and_immutable(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_nested_toplevel_set_remote_identity\s*\(",
        "qdwin_nested_toplevel_set_remote_identity",
    )
    if err:
        return fail(err)
    required = (
        "wl_client_get_credentials",
        "qdwin_proc_exe(pid)",
        "qdwin_remote_nested_publisher_allowed(peer_exe)",
        "QDWIN_NESTED_TOPLEVEL_V1_ERROR_UNAUTHORIZED_REMOTE_IDENTITY",
        "QDWIN_NESTED_TOPLEVEL_V1_ERROR_INVALID_REMOTE_IDENTITY",
        "QDWIN_NESTED_TOPLEVEL_V1_ERROR_REMOTE_IDENTITY_IMMUTABLE",
        "qdwin_emit_nested_proxy_remote_identity",
    )
    for token in required:
        if token not in body:
            return fail("remote identity setter lacks protected step: " + token)
    identity_at = body.find("qdwin_remote_nested_publisher_allowed")
    assign_at = body.find("tl->proxy_remote_source_machine = source_copy")
    if identity_at < 0 or assign_at < 0 or identity_at > assign_at:
        return fail("remote publisher identity must be verified before storing")
    return 0


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_nested_identity_policy.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")

    checks = (
        check_fd_peer_uid_fails_closed,
        check_nested_manager_bind_restricts_secctx,
        check_locker_bind_rejects_secctx,
        check_nested_advertise_has_cap,
        check_nested_broker_gate_defaults_closed,
        check_advertise_binds_origin_uid,
        check_input_sink_peercred,
        check_bind_proxy_pixels_ownership,
        check_bind_proxy_pixels_preserves_pending_gate,
        check_pixel_destroy_preserves_pending_gate,
        check_geometry_resize_preserves_pending_gate,
        check_stale_decisions_are_idempotent,
        check_focus_requests_preserve_pending_gate,
        check_focus_requests_shell_role_gated,
        check_minimize_preserves_pending_gate,
        check_view_stream_preserves_pending_gate,
        check_state_requests_preserve_pending_gate,
        check_pixel_destroy_preserves_position,
        check_generic_raise_paths_preserve_pending_gate,
        check_stream_input_helper_bound,
        check_stream_input_token_one_shot,
        check_nested_proxy_forwards_secctx,
        check_nested_input_peer_replacement,
        check_remote_identity_is_protected_and_immutable,
    )
    for check in checks:
        rc = check(source)
        if rc:
            return rc

    print("PASS: nested-proxy identity hardening (origin_uid bound to peer "
          "uid, input_sink peer-cred-verified, bind_proxy_pixels "
          "owner-gated, stream-input helper-bound)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
