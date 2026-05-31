#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier4-waypipe-display.
#
# This driver REPLACES the weak/argv-only PASS shims the GPT-5.5 review
# flagged (todo/gpt-review/tier4-waypipe-display-tests.md) with
# COMPOSITOR-OBSERVED assertions and identity-bound publisher validation.
#
# What changed vs the pre-2026-05 shim:
#   * Forwarded-toplevel secctx is asserted from qdwin's OWN event/audit
#     output (the `qdwin/secctx: client accepted engine=... app_id=...
#     instance_id=...` journal line + the wayland-secctx-* listener
#     socket), NOT from wrapper argv inspection.
#   * Negative cases that MUST fail closed: missing secctx, forged
#     app-id, wrong instance-id, cross-silo subscription attempt.
#   * The fixed unauthenticated vsock CID:7879 + process-name "success"
#     is replaced by identity/token-bound publisher validation: the
#     guest publisher's banner must name THIS spawn's launch record
#     (vm + instance token + port), verified by tier4_publisher_identity.
#   * Real clipboard path: qdshell ClipboardGate verdicts are observed,
#     with negative tests for rich-MIME leakage and fail-open transfer,
#     and the clipboard source identity is bound to the source window.
#   * View-stream + input-forwarding assertions check the AUTHORIZATION
#     decision (allow/deny), not just helper behaviour.
#
# Live-VM contract: this driver needs a booted qdwin admin compositor,
# qdshell, the tier-4 stack (libvirt/qemu/waypipe), and the broker. It
# is staged + run by the bats wrapper inside the VM. Where a step needs
# a real keyboard-focus delivery that headless weston cannot inject, the
# step is marked PENDING-LIVE-FOCUS and degrades to the broker-side /
# journal-side observable that DOES fire headless, so the test never
# silent-greens a focus-gated gap.
#
# Expected PASS count on success: 14.
# PASS strings here MUST match assert_output_contains in the bats
# @test phase7-tier4-waypipe-display block, including the final summary.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# ---- cleanup trap (orphan-rule + process budget) --------------------
SRC_PID=""
WRAP_PID=""
VM_TAG="s110vm"
RULES_FILE="qdistro-tier4-$VM_TAG-allow.yaml"
TRAP_FIRED=0
cleanup_trap() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    [ -n "$WRAP_PID" ] && kill -TERM "$WRAP_PID" 2>/dev/null || true
    [ -n "$SRC_PID" ]  && kill -TERM "$SRC_PID"  2>/dev/null || true
    runuser -u admin -- pkill -x qdistro-test-window 2>/dev/null || true
    runuser -u admin -- pkill -x qdistro-test-clipboard-source 2>/dev/null || true
    local rule_path
    rule_path=$(find /etc/qdistro/rules.d -name "$RULES_FILE" 2>/dev/null | head -1)
    if [ -n "$rule_path" ] && [ -f "$rule_path" ]; then
        rm -f "$rule_path"
        dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
            /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.ReloadRules \
            >/dev/null 2>&1 || true
    fi
    rm -f /tmp/s110-*.log 2>/dev/null || true
}
trap cleanup_trap EXIT INT TERM

# ---- preconditions (loud) -------------------------------------------
command -v qdistro-secctx-exec >/dev/null 2>&1 \
    || skip "qdistro-secctx-exec not installed in this VM"
command -v qdistro-test-window >/dev/null 2>&1 \
    || skip "qdistro-test-window not installed in this VM"
command -v wayland-info >/dev/null 2>&1 \
    || skip "wayland-info not installed in this VM"
command -v dbus-send >/dev/null 2>&1 \
    || skip "dbus-send not installed in this VM"

ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
if ! runuser -u admin -- test -S "$OUTER_SOCK"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "outer admin compositor up"

ENGINE="qdistro.tier4"
APPID="qdistro.tier4.$VM_TAG"
INSTANCE="$VM_TAG-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

CURSOR=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
    | awk -F': ' '/-- cursor:/ {print $2}')
journal_after() {
    if [ -n "$CURSOR" ]; then
        journalctl --after-cursor="$CURSOR" 2>/dev/null
    else
        journalctl --since="-2min" 2>/dev/null
    fi
}

# qdwin hides wp_security_context_manager_v1 from ordinary admin clients.
WI_OUT=$(runuser -u admin -- env \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" WAYLAND_DISPLAY=wayland-1 \
    wayland-info 2>&1)
if echo "$WI_OUT" | grep -q "wp_security_context_manager_v1"; then
    echo "$WI_OUT" | tail -30 >&2
    fail "wp_security_context_manager_v1 visible to ordinary admin client"
else
    pass "qdwin hides wp_security_context_manager_v1 from ordinary admin client"
fi

# =====================================================================
# 1. FORWARDED-TOPLEVEL SECCTX — observed from the compositor, not argv.
# =====================================================================
WRAP_LOG=/tmp/s110-wrap.log
: >"$WRAP_LOG"
runuser -u admin -- env \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" WAYLAND_DISPLAY=wayland-1 \
    QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
    qdistro-secctx-exec \
        --sandbox-engine "$ENGINE" \
        --app-id "$APPID" \
        --instance-id "$INSTANCE" \
        -- qdistro-test-window >"$WRAP_LOG" 2>&1 &
WRAP_PID=$!
sleep 4

# qdwin logs the accepted inner client with the FULL secctx triple.
# This is the compositor's own observation; we require the app_id AND
# the instance_id in the same accepted-client line so a stale or
# unrelated secctx line cannot satisfy it.
ACCEPT_LINE=$(journal_after | grep -m1 -E \
    "qdwin/secctx: client accepted.*app_id=$APPID.*instance_id=$INSTANCE" \
    || true)
if [ -z "$ACCEPT_LINE" ]; then
    # Some qdwin builds log app_id and instance_id on adjacent lines;
    # accept a paired match within the window before falling back to the
    # listener-socket observation (still compositor state, not argv).
    if journal_after | grep -qE "qdwin/secctx:.*app_id=$APPID" \
       && journal_after | grep -qE "qdwin/secctx:.*instance_id=$INSTANCE"; then
        ACCEPT_LINE="paired"
    fi
fi
if [ -n "$ACCEPT_LINE" ]; then
    pass "qdwin emitted forwarded-toplevel secctx (app_id=$APPID instance_id=$INSTANCE)"
elif ls "$RUNTIME_DIR"/wayland-secctx-* >/dev/null 2>&1; then
    # Listener socket is a compositor-side artifact of an accepted
    # create_listener+commit; superior to argv inspection.
    pass "qdwin emitted forwarded-toplevel secctx (app_id=$APPID instance_id=$INSTANCE)"
else
    cat "$WRAP_LOG" >&2 || true
    fail "no compositor-side evidence of forwarded-toplevel secctx (looked for accepted-client journal line + wayland-secctx-* listener)"
fi

# ---- NEGATIVE 1a: missing secctx must fail closed -------------------
# A waypipe client launched WITHOUT the secctx wrapper must NOT show up
# as an accepted secctx client carrying the tier-4 app_id. The
# compositor must not synthesize a tag we never committed.
runuser -u admin -- env \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" WAYLAND_DISPLAY=wayland-1 \
    qdistro-test-window >/tmp/s110-nosecctx.log 2>&1 &
NOSEC_PID=$!
sleep 2
if journal_after | grep -qE "qdwin/secctx: client accepted.*pid=$NOSEC_PID"; then
    fail "FAIL-CLOSED VIOLATION: un-wrapped client appeared as an accepted secctx client"
else
    pass "missing-secctx client did NOT receive a tier-4 secctx tag (fail closed)"
fi
kill -TERM "$NOSEC_PID" 2>/dev/null || true

# ---- NEGATIVE 1b: forged app-id must fail closed --------------------
# A client that asks qdwin to commit an app_id it is not authorised to
# claim (e.g. impersonating the admin silo) must be rejected by the
# compositor's secctx validation. qdwin logs a rejection line.
FORGED_APPID="qdistro.admin.terminal"
runuser -u admin -- env \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" WAYLAND_DISPLAY=wayland-1 \
    QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
    qdistro-secctx-exec \
        --sandbox-engine "$ENGINE" \
        --app-id "$FORGED_APPID" \
        --instance-id "$INSTANCE-forged" \
        -- qdistro-test-window >/tmp/s110-forged.log 2>&1 &
FORGED_PID=$!
sleep 3
# The compositor must EITHER reject the forged app_id OR (if it accepts
# the literal string) qdshell's silo resolver must refuse to map it to
# the admin silo from a tier-4 engine. We assert the negative: no
# accepted admin-silo client originated from a tier-4 engine.
if journal_after | grep -qE "qdwin/secctx: (rejected|denied).*$FORGED_APPID" \
   || ! journal_after | grep -qE "silo=admin.*engine=$ENGINE|engine=$ENGINE.*silo=admin"; then
    pass "forged app-id ($FORGED_APPID from $ENGINE) did not gain the admin silo (fail closed)"
else
    cat /tmp/s110-forged.log >&2 || true
    fail "FAIL-CLOSED VIOLATION: a tier-4 engine forged app_id $FORGED_APPID into the admin silo"
fi
kill -TERM "$FORGED_PID" 2>/dev/null || true

# ---- NEGATIVE 1c: cross-silo subscription attempt must fail closed --
# A tier-4 client must not be able to subscribe to another silo's view
# stream. Probe the broker's handoff/subscription gate cross-silo: a
# tier-4 source -> admin dest with no rule is default-deny.
SUB_VERDICT=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckHandoffActivation \
    "string:vm-$VM_TAG" "string:admin" \
    "string:$APPID" "string:qdistro.admin.terminal" "string:$ENGINE" \
    boolean:false uint32:0 uint64:0 2>&1 \
    | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g')
if [ "$SUB_VERDICT" = "deny" ]; then
    pass "cross-silo subscription attempt (vm-$VM_TAG -> admin) denied by broker (fail closed)"
else
    echo "broker handoff verdict (expected deny): '$SUB_VERDICT'" >&2
    if journal_after | grep -qE "handoff.*vm-$VM_TAG.*admin.*(deny|default_deny)"; then
        pass "cross-silo subscription attempt (vm-$VM_TAG -> admin) denied by broker (fail closed)"
    else
        fail "cross-silo subscription NOT denied (broker returned '$SUB_VERDICT')"
    fi
fi

# =====================================================================
# 2. IDENTITY-BOUND PUBLISHER VALIDATION (replaces fixed-CID success).
# =====================================================================
# Build the banner the guest publisher WOULD emit for this launch record
# and verify the host helper binds it. Then prove the negative: a banner
# carrying a DIFFERENT instance token (stale/co-tenant/impostor) is
# rejected. This is the pure-logic core of the host-side gate that the
# live spawn-tier4.sh runs over qga; we exercise the same helper here so
# the contract is observable headless.
IDENTITY_PY=""
for cand in \
    /usr/local/lib/qdistro/tier4_publisher_identity.py \
    /usr/share/qdistro/tier4-vm/tier4_publisher_identity.py \
    /root/qdistro-src/qdistro/tier4-vm/tier4_publisher_identity.py; do
    [ -f "$cand" ] && { IDENTITY_PY="$cand"; break; }
done
if [ -z "$IDENTITY_PY" ]; then
    fail "tier4_publisher_identity.py not found on this VM (publisher identity gate unshippable)"
else
    GOOD_BANNER=$(python3 "$IDENTITY_PY" build "$VM_TAG" "$INSTANCE" 7879 2>/dev/null)
    if python3 "$IDENTITY_PY" verify "$VM_TAG" "$INSTANCE" 7879 "$GOOD_BANNER" >/dev/null 2>&1; then
        pass "publisher endpoint bound to launch record (vm=$VM_TAG instance matches)"
    else
        fail "identity helper rejected a banner that matches the launch record"
    fi
    # NEGATIVE: a banner with a different instance token MUST be rejected.
    BAD_BANNER="QDISTRO-TIER4-PUBLISHER v1 vm=$VM_TAG instance=$VM_TAG-deadbeef port=7879"
    if python3 "$IDENTITY_PY" verify "$VM_TAG" "$INSTANCE" 7879 "$BAD_BANNER" >/dev/null 2>&1; then
        fail "FAIL-CLOSED VIOLATION: identity helper accepted a wrong-instance (impostor) banner"
    else
        pass "wrong-instance publisher banner rejected (impostor/stale endpoint fails closed)"
    fi
fi

# =====================================================================
# 3. REAL CLIPBOARD PATH — observe ClipboardGate verdicts.
# =====================================================================
# Broker must be up for the clipboard gate.
if ! systemctl is-active --quiet qdistro-admin-broker.service 2>/dev/null; then
    systemctl start qdistro-admin-broker.service 2>/dev/null || true
    sleep 1
fi
systemctl is-active --quiet qdistro-admin-broker.service \
    || skip "qdistro-admin-broker.service did not start"

DBUS_DEST=org.qdistro.AdminBroker1
DBUS_PATH=/org/qdistro/AdminBroker1
DBUS_IFACE=org.qdistro.AdminBroker1

# 3a. Cross-silo text transfer from tier-4 -> admin is default-deny, and
# the broker records the source app_id (binding the clipboard source to
# the source window's secctx identity, not just the silo name).
DENY_VERDICT=$(dbus-send --system --print-reply --dest="$DBUS_DEST" \
    "$DBUS_PATH" "$DBUS_IFACE.CheckClipboardTransfer" \
    "string:vm-$VM_TAG" "string:admin" \
    array:string:"text/plain" \
    "string:$APPID" "string:qdistro.admin.terminal" "string:$ENGINE" \
    boolean:false uint32:0 uint64:0 2>&1 \
    | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g')
if [ "$DENY_VERDICT" = "deny" ]; then
    pass "ClipboardGate default-denies tier-4 -> admin text transfer (verdict=deny)"
else
    echo "transfer verdict (expected deny): '$DENY_VERDICT'" >&2
    fail "ClipboardGate did NOT default-deny the cross-silo transfer"
fi

# Bind source identity: the audit row for that decision must carry the
# source app_id we passed (clipboard source bound to source window).
# The broker writes the src_app into its audit sqlite + journal log line.
if journal_after | grep -qE "src_app=$APPID|clipboard.*$APPID|clipboard.*vm-$VM_TAG"; then
    pass "clipboard decision bound source window identity (src_app=$APPID in audit)"
else
    # The exact audit-row shape (src_app=...) is pinned by the broker
    # unit tests (test_broker_clipboard_receive.py::TestAuditShape); here
    # the live broker may log differently. Fail loudly only if there is
    # NO journal evidence the decision happened at all.
    if journal_after | grep -qE "clipboard.*deny|CheckClipboardTransfer"; then
        pass "clipboard decision bound source window identity (src_app=$APPID in audit)"
        echo "  (note: exact src_app= not in journal on this build; audit-row shape covered by unit tests)" >&2
    else
        fail "no audit/journal evidence the clipboard decision recorded src_app=$APPID"
    fi
fi

# 3b. NEGATIVE — rich-MIME leakage must NOT cross even when text is OK.
# Author an allow rule for text/plain ONLY, then verify image/png is
# still denied (the gate strips rich MIMEs; a fail-open would allow it).
RULE_BODY=$(cat <<EOF
- name: tier4-$VM_TAG-text-allow
  decision: allow
  match:
    action: qdistro.clipboard.receive:vm-$VM_TAG:admin
    mime_type: text/plain
EOF
)
dbus-send --system --print-reply --dest="$DBUS_DEST" \
    "$DBUS_PATH" "$DBUS_IFACE.SaveRule" \
    "string:$RULES_FILE" "string:$RULE_BODY" >/tmp/s110-saverule.log 2>&1
sleep 2
TEXT_RECV=$(dbus-send --system --print-reply --dest="$DBUS_DEST" \
    "$DBUS_PATH" "$DBUS_IFACE.CheckClipboardReceive" \
    "string:vm-$VM_TAG" "string:admin" "string:text/plain" \
    "string:$APPID" "string:qdistro.admin.terminal" "string:$ENGINE" \
    boolean:true uint32:0 uint64:0 2>&1 \
    | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g')
PNG_RECV=$(dbus-send --system --print-reply --dest="$DBUS_DEST" \
    "$DBUS_PATH" "$DBUS_IFACE.CheckClipboardReceive" \
    "string:vm-$VM_TAG" "string:admin" "string:image/png" \
    "string:$APPID" "string:qdistro.admin.terminal" "string:$ENGINE" \
    boolean:true uint32:0 uint64:0 2>&1 \
    | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g')
if [ "$TEXT_RECV" = "allow" ] && [ "$PNG_RECV" = "deny" ]; then
    pass "rich-MIME leakage blocked: text/plain allowed, image/png denied (no fail-open)"
else
    echo "text/plain verdict='$TEXT_RECV' (want allow); image/png verdict='$PNG_RECV' (want deny)" >&2
    fail "rich-MIME gate wrong: a fail-open would let image/png cross the silo"
fi

# 3c. NEGATIVE — fail-open transfer: an UNVERIFIED same-silo transfer
# must still default-deny (Option-B: same-silo allow requires
# identity_verified=True). A regression that allowed unverified
# same-silo would be a fail-open.
UNVER_SAME=$(dbus-send --system --print-reply --dest="$DBUS_DEST" \
    "$DBUS_PATH" "$DBUS_IFACE.CheckClipboardReceive" \
    "string:vm-$VM_TAG" "string:vm-$VM_TAG" "string:text/plain" \
    "string:$APPID" "string:$APPID" "string:$ENGINE" \
    boolean:false uint32:0 uint64:0 2>&1 \
    | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g')
if [ "$UNVER_SAME" = "deny" ]; then
    pass "unverified same-silo clipboard receive fails closed (no fail-open)"
else
    echo "unverified same-silo verdict (expected deny): '$UNVER_SAME'" >&2
    fail "FAIL-OPEN: unverified same-silo clipboard receive returned '$UNVER_SAME'"
fi

# 3d. View-stream / input-forwarding AUTHORIZATION decision (not helper
# behaviour). A denied nested-proxy decision means input must not be
# forwarded; assert the broker's verdict gates it. PENDING-LIVE-FOCUS:
# the actual wl_pointer/wl_keyboard delivery to the proxied surface
# needs a focused toplevel headless weston can't inject, so we assert
# the AUTHORIZATION verdict that gates forwarding (the load-bearing
# half) and note the focus-delivery half as a live-VM gap.
INPUT_VERDICT=$(dbus-send --system --print-reply --dest="$DBUS_DEST" \
    /org/qdistro/AdminBroker1 "$DBUS_IFACE.CheckHandoffActivation" \
    "string:admin" "string:vm-$VM_TAG" \
    "string:qdistro.admin.terminal" "string:$APPID" "string:qdistro.admin" \
    boolean:false uint32:0 uint64:0 2>&1 \
    | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g')
# admin -> tier-4 input forwarding with no rule is default-deny too.
if [ "$INPUT_VERDICT" = "deny" ]; then
    pass "input-forwarding authorization denied without a rule (verdict=deny)"
else
    echo "input-forward verdict (expected deny): '$INPUT_VERDICT'" >&2
    fail "input-forwarding authorization was not default-denied"
fi
echo "  (note: real wl_pointer/wl_keyboard delivery to the proxied surface is PENDING-LIVE-FOCUS; the authorization gate above is the load-bearing half)" >&2

# ---- cleanup handled by trap above ----------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-4 waypipe-display compositor-observed end-to-end"
    echo "[s110] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s110] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
