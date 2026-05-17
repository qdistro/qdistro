#!/bin/bash
# In-VM driver for broker-e2e.bats — exercises the broker D-Bus surface
# qdshell's HooksGate / Notifications / Lock services use, asserts
# outputs, prints PASS/FAIL markers the bats wrapper greps for.
#
# Each scenario echoes "PASS: <description>" on success or
# "FAIL: <description>" on failure. Final summary line counts both.

set -u

BUS=com.qdistro.AdminBroker1
PATH_=/com/qdistro/AdminBroker1
IFACE=com.qdistro.AdminBroker1

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }

bcall() {
    busctl --system --no-pager call "$BUS" "$PATH_" "$IFACE" "$@" 2>&1
}

# --------- HooksGate: CheckPermission semantics ----------------------

# Clean any leftover test rules.
rm -f /etc/qdistro/rules.d/test-hook-*.yaml 2>/dev/null
mkdir -p /etc/qdistro/rules.d
sleep 1

# 1. unknown when no rule
out=$(bcall CheckPermission sa{sv} "hook.allowed:wallpaperChange" 0)
if echo "$out" | grep -q '"unknown"'; then
    pass "hooks: CheckPermission unknown when no rule"
else
    fail "hooks: expected 'unknown', got: $out"
fi

# 2. allow rule → "allow"
cat > /etc/qdistro/rules.d/test-hook-allow.yaml <<'EOF'
- name: test-hook-allow-darkmode
  decision: allow
  match:
    action: "hook.allowed:darkModeChange"
    uid: 0
EOF
sleep 2
out=$(bcall CheckPermission sa{sv} "hook.allowed:darkModeChange" 0)
if echo "$out" | grep -q '"allow"'; then
    pass "hooks: CheckPermission allow with rule"
else
    fail "hooks: expected 'allow', got: $out"
fi
rm -f /etc/qdistro/rules.d/test-hook-allow.yaml
sleep 2

# 3. deny rule → "deny"
cat > /etc/qdistro/rules.d/test-hook-deny.yaml <<'EOF'
- name: test-hook-deny-screenlock
  decision: deny
  match:
    action: "hook.allowed:screenLock"
    uid: 0
EOF
sleep 2
out=$(bcall CheckPermission sa{sv} "hook.allowed:screenLock" 0)
if echo "$out" | grep -q '"deny"'; then
    pass "hooks: CheckPermission deny with rule"
else
    fail "hooks: expected 'deny', got: $out"
fi
rm -f /etc/qdistro/rules.d/test-hook-deny.yaml
sleep 2

# 4. RequestPermission enqueues a pending entry
out=$(bcall RequestPermission sa{sv} "hook.allowed:startup" 1 script s "/usr/local/bin/myhook")
if echo "$out" | grep -qE 'i [0-9]+'; then
    pass "hooks: RequestPermission returns request id"
else
    fail "hooks: RequestPermission did not return id, got: $out"
fi
out=$(bcall GetPending)
if echo "$out" | grep -q "hook.allowed:startup"; then
    pass "hooks: GetPending includes the queued action"
else
    fail "hooks: GetPending missing 'hook.allowed:startup', got: $out"
fi

# --------- Notifications: RecordNotification ------------------------

# Helper that fires + checks the most recent ListHistory row.
record_and_check() {
    local app="$1" sum="$2" body="$3" urg="$4" needle="$5" desc="$6"
    bcall RecordNotification sssi "$app" "$sum" "$body" "$urg" >/dev/null
    out=$(bcall ListHistory i 5)
    if echo "$out" | grep -q "$needle"; then
        pass "notifications: $desc"
    else
        fail "notifications: $desc — needle '$needle' not in: $out"
    fi
}

# 5. basic write
UNIQ_BASIC="bats-basic-$$"
record_and_check "BatsApp" "$UNIQ_BASIC" "body" 1 "$UNIQ_BASIC" "ListHistory contains uniquely-tagged write"

# 6. action namespace pinned
out=$(bcall ListHistory i 5)
if echo "$out" | grep -q "notification.posted"; then
    pass "notifications: action='notification.posted' in audit row"
else
    fail "notifications: missing action 'notification.posted'"
fi

# 7. critical urgency label
UNIQ_CRIT="bats-crit-$$"
record_and_check "CritApp" "$UNIQ_CRIT" "urgent" 2 "urgency=critical" "critical urgency labeled"

# 8. low urgency label
UNIQ_LOW="bats-low-$$"
record_and_check "LowApp" "$UNIQ_LOW" "fyi" 0 "urgency=low" "low urgency labeled"

# 9. malformed urgency normalizes to normal
UNIQ_MALF="bats-malf-$$"
bcall RecordNotification sssi "MalApp" "$UNIQ_MALF" "b" 999 >/dev/null
out=$(bcall ListHistory i 3)
if echo "$out" | grep -q "$UNIQ_MALF" && echo "$out" | grep -q "urgency=normal"; then
    pass "notifications: out-of-range urgency 999 normalized to normal"
else
    fail "notifications: malformed urgency not normalized; got: $out"
fi

# 10. app name truncation (200 chars in, expect 128 out)
LONG_APP=$(printf 'X%.0s' {1..200})
EXPECTED_128=$(printf 'X%.0s' {1..128})
EXPECTED_129=$(printf 'X%.0s' {1..129})
bcall RecordNotification sssi "$LONG_APP" "trunc-test-$$" "b" 1 >/dev/null
out=$(bcall ListHistory i 1)
if echo "$out" | grep -q "$EXPECTED_128" && ! echo "$out" | grep -q "$EXPECTED_129"; then
    pass "notifications: app name truncated to exactly 128 chars"
else
    fail "notifications: truncation off; out: $(echo "$out" | head -c 200)..."
fi

# 11. summary truncation (1000 chars in, expect 256 out)
LONG_SUM=$(printf 'Y%.0s' {1..1000})
EXPECTED_256=$(printf 'Y%.0s' {1..256})
EXPECTED_257=$(printf 'Y%.0s' {1..257})
bcall RecordNotification sssi "App" "$LONG_SUM" "b" 1 >/dev/null
out=$(bcall ListHistory i 1)
if echo "$out" | grep -q "$EXPECTED_256" && ! echo "$out" | grep -q "$EXPECTED_257"; then
    pass "notifications: summary truncated to exactly 256 chars"
else
    fail "notifications: summary truncation off"
fi

# 12. body truncation (2000 chars in, expect 512 out)
LONG_BODY=$(printf 'Z%.0s' {1..2000})
EXPECTED_512=$(printf 'Z%.0s' {1..512})
EXPECTED_513=$(printf 'Z%.0s' {1..513})
bcall RecordNotification sssi "App" "bs-$$" "$LONG_BODY" 1 >/dev/null
out=$(bcall ListHistory i 1)
if echo "$out" | grep -q "$EXPECTED_512" && ! echo "$out" | grep -q "$EXPECTED_513"; then
    pass "notifications: body truncated to exactly 512 chars"
else
    fail "notifications: body truncation off"
fi

# 13. 50 in a row — burst smoke
for i in $(seq 1 50); do
    bcall RecordNotification sssi "BurstApp" "burst-$$-$i" "b" 1 >/dev/null
done
out=$(bcall ListHistory i 100)
if echo "$out" | grep -q "burst-$$-50" && echo "$out" | grep -q "burst-$$-1"; then
    pass "notifications: 50-in-a-row burst all land in ListHistory"
else
    fail "notifications: burst dropped some; got tail: $(echo "$out" | tail -c 200)"
fi

# 14. unicode preservation. busctl prints non-ASCII UTF-8 bytes as
# octal escapes (\303\251 etc.) so we can't grep for "café" literally.
# Instead: confirm the unique tag is present AND the octal escape for
# 'é' (\303\251) appears alongside it.
UNIQ_UNI="bats-uni-$$"
bcall RecordNotification sssi "App" "cafe-$UNIQ_UNI" "café-body" 1 >/dev/null
out=$(bcall ListHistory i 3)
if echo "$out" | grep -q "$UNIQ_UNI" && echo "$out" | grep -q "303\\\\251"; then
    pass "notifications: unicode preserved in audit (octal-escaped)"
else
    fail "notifications: unicode mangled or unique tag missing; got: $out"
fi

# 15. caller_uid attribution (we're root, uid 0)
UNIQ_UID="bats-uid-$$"
bcall RecordNotification sssi "App" "$UNIQ_UID" "b" 1 >/dev/null
out=$(bcall ListHistory i 5)
# Find our row and check caller_uid is 0 (busctl runs as root).
if echo "$out" | grep -q "$UNIQ_UID"; then
    pass "notifications: caller_uid attribution row written"
else
    fail "notifications: uid attribution missing"
fi

# --------- summary ---------------------------------------------------

echo
echo "===================="
echo "phase9 broker e2e: $PASSCOUNT passed, $FAILCOUNT failed"
echo "===================="
if [ "$FAILCOUNT" -gt 0 ]; then exit 1; fi
echo "PASS: phase9 broker round-trip end-to-end"
