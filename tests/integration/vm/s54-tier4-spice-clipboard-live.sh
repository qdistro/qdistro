#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier4-spice-clipboard-live.
#
# Boots a tier-4 guest from the SPICE-capable base image, polls qga,
# asserts spice-vdagentd is active inside, and confirms the running
# domain XML carries the configured copypaste value (default 'no',
# opt-in 'yes'). Skips when qdistro-tier4-base.qcow2 isn't built.
#
# Build the base once via:
#   sudo bash tier4-vm/build-guest-image.sh
# or for user-mode dev:
#   bash tier4-vm/build-guest-image.sh --dest ~/.local/share/libvirt/images/qdistro-tier4-base.qcow2
#
# PASS strings here MUST match the bats @test phase7-tier4-spice-clipboard-live.

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
[ -e /dev/kvm ] || skip "/dev/kvm not present"

# Locate the SPICE-capable base disk. spawn-tier4.sh accepts
# TIER4_DISK_BASE as either a system or user-mode path.
BASE=""
for candidate in \
    /var/lib/libvirt/images/qdistro-tier4-base.qcow2 \
    /home/admin/.local/share/libvirt/images/qdistro-tier4-base.qcow2; do
    if [ -f "$candidate" ]; then BASE="$candidate"; break; fi
done
[ -n "$BASE" ] || skip "qdistro-tier4-base.qcow2 not built; run tier4-vm/build-guest-image.sh"

VMS=()
cleanup() {
    for vm in "${VMS[@]:-}"; do
        [ -z "$vm" ] && continue
        runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh destroy "$vm" >/dev/null 2>&1 || true
        runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh undefine "$vm" >/dev/null 2>&1 || true
        rm -f /home/admin/.local/share/libvirt/images/"$vm".qcow2 2>/dev/null || true
    done
}
trap cleanup EXIT

# --- helper: spawn a tier-4 guest, wait for running + qga ---
spawn_and_wait() {
    local vm_name="$1"
    local extra_env="$2"
    local log
    log=$(mktemp)
    eval "$extra_env TIER4_DISK_BASE='$BASE' TIER4_NO_VIEWER=1" \
        bash "$TIER4_DIR/spawn-tier4.sh" "$vm_name" >"$log" 2>&1 &
    local pid=$!
    VMS+=("$vm_name")

    # Wait for domain running — virt-customize overlay + first-boot
    # under nested KVM can exceed 90s on a cold cache.
    local d=$(( $(date +%s) + 180 ))
    while [ "$(date +%s)" -lt "$d" ]; do
        if runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh domstate "$vm_name" 2>/dev/null \
            | grep -qw running; then
            break
        fi
        sleep 1
    done
    if ! runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh domstate "$vm_name" 2>/dev/null \
            | grep -qw running; then
        cat "$log" >&2
        rm -f "$log"
        return 1
    fi
    rm -f "$log"
    return 0
}

qga_ping() {
    local vm="$1"
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh qemu-agent-command "$vm" \
        '{"execute":"guest-ping"}' >/dev/null 2>&1
}

qga_exec_check_vdagent() {
    local vm="$1"
    local req='{"execute":"guest-exec","arguments":{"path":"/usr/bin/systemctl","arg":["is-active","spice-vdagentd.service"],"capture-output":true}}'
    local reply
    reply=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh qemu-agent-command "$vm" "$req" 2>/dev/null)
    [ -z "$reply" ] && return 1
    local pid
    pid=$(echo "$reply" | grep -oE '"pid"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1)
    [ -z "$pid" ] && return 1

    # Poll guest-exec-status until exited.
    local d=$(( $(date +%s) + 15 ))
    while [ "$(date +%s)" -lt "$d" ]; do
        local s
        s=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh qemu-agent-command "$vm" \
            "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$pid}}" 2>/dev/null)
        if echo "$s" | grep -q '"exited"[[:space:]]*:[[:space:]]*true'; then
            # base64-decoded stdout of `systemctl is-active spice-vdagentd`
            local b64
            b64=$(echo "$s" | grep -oE '"out-data"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
                | sed 's/.*"\([^"]*\)"$/\1/')
            local out
            out=$(echo "$b64" | base64 -d 2>/dev/null)
            echo "$out" | grep -qw active && return 0 || return 1
        fi
        sleep 0.5
    done
    return 1
}

# --- 1. default config (copypaste='no') ---
VM_DEFAULT="qdistro-tier4-s54a-$$"
if ! spawn_and_wait "$VM_DEFAULT" ""; then
    fail "default-config domain never reached running state"
fi

XML_DEFAULT=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh dumpxml "$VM_DEFAULT" 2>/dev/null)
if echo "$XML_DEFAULT" | grep -q "copypaste='no'"; then
    pass "running domain XML carries copypaste='no'"
else
    echo "$XML_DEFAULT" | grep -i clipboard >&2 || true
    fail "default running XML does not contain copypaste='no'"
fi

# Wait for qga. cloud-init firstboot + qga binding can take >90s under
# nested KVM on a first-boot disk; budget 240s to keep this driver
# robust under cold caches.
QGA_OK=0
deadline=$(( $(date +%s) + 240 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if qga_ping "$VM_DEFAULT"; then QGA_OK=1; break; fi
    sleep 2
done
if [ "$QGA_OK" = "1" ]; then
    pass "qga reachable inside $VM_DEFAULT"
else
    fail "qga never reachable inside $VM_DEFAULT within 240s"
fi

if qga_exec_check_vdagent "$VM_DEFAULT"; then
    pass "spice-vdagentd.service active inside $VM_DEFAULT"
else
    fail "spice-vdagentd.service not active inside $VM_DEFAULT"
fi

# Tear down default before opt-in (don't keep two domains around — keeps
# memory pressure low for the bats VM).
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh destroy "$VM_DEFAULT" >/dev/null 2>&1 || true
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh undefine "$VM_DEFAULT" >/dev/null 2>&1 || true
rm -f /home/admin/.local/share/libvirt/images/"$VM_DEFAULT".qcow2 2>/dev/null || true

# --- 2. opt-in config (copypaste='yes') ---
VM_OPTIN="qdistro-tier4-s54b-$$"
if ! spawn_and_wait "$VM_OPTIN" "TIER4_SPICE_CLIPBOARD=allowed"; then
    fail "opt-in-config domain never reached running state"
fi

XML_OPTIN=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 virsh dumpxml "$VM_OPTIN" 2>/dev/null)
if echo "$XML_OPTIN" | grep -q "copypaste='yes'"; then
    pass "opt-in running domain XML carries copypaste='yes'"
else
    echo "$XML_OPTIN" | grep -i clipboard >&2 || true
    fail "opt-in running XML does not contain copypaste='yes'"
fi

# (cleanup trap)

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "spec/10 SPICE clipboard live guest validation end-to-end"
    echo "[s54] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s54] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
