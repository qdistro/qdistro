#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier4-spice-clipboard.
#
# Validates spec/10's SPICE main-channel clipboard defense-in-depth:
#   - Default tier-4 domain XML carries <clipboard copypaste='no'/>
#     (forces all host↔guest clipboard via the wayland selection
#     bridge, which qdistro already gates).
#   - Each spawn embeds a fresh 16-hex SPICE password via
#     <graphics passwd='...'> — defense against unauthorised SPICE
#     attaches even on 127.0.0.1.
#   - TIER4_SPICE_CLIPBOARD=allowed opt-in flips copypaste to 'yes'.
#   - Re-spawning rotates the ticket (passwd value differs).
#
# We use TIER4_DOMAIN_DEFINE_ONLY=1 throughout so this test exercises
# pure XML substitution without booting any guest.
#
# PASS strings here MUST match the bats @test phase7-tier4-spice-clipboard.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER4_DIR=/tmp/qdistro-tier4
if [ -d "$SRC/tier4-vm" ]; then
    rm -rf "$TIER4_DIR" 2>/dev/null || true
    cp -r "$SRC/tier4-vm" "$TIER4_DIR"
    chmod -R a+rX "$TIER4_DIR"
    find "$TIER4_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER4_DIR" ] || skip "tier4-vm source not unpacked at $TIER4_DIR"
command -v virsh >/dev/null 2>&1 || skip "virsh not installed in this VM"

VMS=()
cleanup() {
    for vm in "${VMS[@]:-}"; do
        [ -z "$vm" ] && continue
        runuser -u admin -- virsh destroy "$vm" >/dev/null 2>&1 || true
        runuser -u admin -- virsh undefine "$vm" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT

# Helper: define the domain via spawn-tier4.sh (DEFINE_ONLY) and dump
# its XML. Returns 0 on success and writes XML to $2.
define_and_dump() {
    local vm_name="$1"
    local out_xml="$2"
    local log=/tmp/s49-define-$$.log
    if ! TIER4_DOMAIN_DEFINE_ONLY=1 \
         bash "$TIER4_DIR/spawn-tier4.sh" "$vm_name" >"$log" 2>&1; then
        cat "$log" >&2
        rm -f "$log"
        return 1
    fi
    rm -f "$log"
    runuser -u admin -- virsh dumpxml "$vm_name" >"$out_xml" 2>/dev/null
}

# Round 1 — default config: copypaste='no', generates SPICE passwd.
VM1="qdistro-tier4-s49a-$$"
VMS+=("$VM1")
XML1=/tmp/s49-vm1.xml
if ! define_and_dump "$VM1" "$XML1"; then
    fail "spawn-tier4 define-only failed for default config"
fi

if grep -q "copypaste='no'" "$XML1"; then
    pass "default XML disables SPICE clipboard channel"
else
    grep -i "copypaste\|clipboard" "$XML1" >&2 || true
    fail "default XML does not contain copypaste='no'"
fi

PWD1=$(grep -oE "passwd='[0-9a-f]+'" "$XML1" | head -1 | grep -oE "[0-9a-f]+")
if [ -n "$PWD1" ] && [ "${#PWD1}" -eq 16 ]; then
    pass "default XML carries 16-hex SPICE password"
else
    fail "default XML SPICE passwd missing or wrong length: '$PWD1'"
fi

# Round 2 — opt-in: TIER4_SPICE_CLIPBOARD=allowed should yield copypaste='yes'.
VM2="qdistro-tier4-s49b-$$"
VMS+=("$VM2")
XML2=/tmp/s49-vm2.xml
log=/tmp/s49-define-optin.log
if ! TIER4_SPICE_CLIPBOARD=allowed TIER4_DOMAIN_DEFINE_ONLY=1 \
     bash "$TIER4_DIR/spawn-tier4.sh" "$VM2" >"$log" 2>&1; then
    cat "$log" >&2
    fail "spawn-tier4 define-only failed for opt-in config"
fi
rm -f "$log"
runuser -u admin -- virsh dumpxml "$VM2" >"$XML2" 2>/dev/null

if grep -q "copypaste='yes'" "$XML2"; then
    pass "opt-in TIER4_SPICE_CLIPBOARD=allowed enables clipboard channel"
else
    grep -i "copypaste\|clipboard" "$XML2" >&2 || true
    fail "opt-in XML does not contain copypaste='yes'"
fi

# Round 3 — rotation: spawn the same vm name fresh (delete first) and
# confirm the SPICE passwd changes from $PWD1.
runuser -u admin -- virsh destroy "$VM1" >/dev/null 2>&1 || true
runuser -u admin -- virsh undefine "$VM1" >/dev/null 2>&1 || true

XML3=/tmp/s49-vm3.xml
if ! define_and_dump "$VM1" "$XML3"; then
    fail "spawn-tier4 re-define failed for ticket-rotation check"
fi
PWD3=$(grep -oE "passwd='[0-9a-f]+'" "$XML3" | head -1 | grep -oE "[0-9a-f]+")
if [ -n "$PWD3" ] && [ "$PWD3" != "$PWD1" ]; then
    pass "SPICE password rotates per spawn"
else
    fail "SPICE password did not rotate: '$PWD1' vs '$PWD3'"
fi

rm -f "$XML1" "$XML2" "$XML3"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "spec/10 SPICE clipboard gate (tier-4 defense-in-depth) end-to-end"
    echo "[s49] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s49] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
