#!/bin/bash
# s06 — net-VM image immutability / rebuild invariants (Probe 11).
#
# Proves the invariant: "no durable user state in the net VM; the root is
# read-only; config is qdistro-owned text regenerated from baseline; a rebuild
# returns to a known state."
#
# Invariants exercised:
#   I1  Root is read-only squashfs; overlay is distinct; /rom writes fail.
#   I2  Baseline config files exist; qdistro-netvm-apply regenerates live
#       config; egress_reload applies and is idempotent.
#   I3  Base qcow2 sha256 is identical before boot and after teardown;
#       per-run overlay is a separate file.
#   I4  Mutation in the overlay is gone after --purge + fresh boot.
#
# NOTE (honest limits): I4 exercises the ephemeral-overlay path, NOT an
# in-place "firstboot -y && reboot" reset.  The harness launches qemu with
# -no-reboot so a firstboot reboot exits the VM; firstboot is therefore NOT
# called.  The ephemeral overlay (a fresh qcow2 overlay over the pristine base)
# provides equivalent proof of rebuild-to-known-state.
#
# Boot strategy: the harness `up` provisions the qdistro-admin rpcd login itself
# (it drains the console to quiescence before issuing commands, so it is robust
# even under concurrent-VM load).
#
# Env vars (with defaults):
#   NETVM_BASE   path to the base qcow2 image
#   NETVM_PORT   host HTTP port for uhttpd /ubus
#   NETVM_RUNDIR rundir for the VM session

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
LIBDIR="$REPO/tests/integration/vm/lib"
SESSION_PY="$LIBDIR/netvm_session.py"

NETVM_BASE="${NETVM_BASE:-$HOME/.local/share/libvirt/images/qdistro-netvm-base.qcow2}"
NETVM_PORT="${NETVM_PORT:-8092}"
NETVM_RUNDIR="${NETVM_RUNDIR:-/tmp/netvm-p11}"

pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }
hdr() { echo; echo "=== $1 ==="; }

# ---- cleanup trap -----------------------------------------------------------
cleanup() {
    echo
    echo "--- cleanup: tearing down VM ---"
    python3 "$SESSION_PY" down --rundir "$NETVM_RUNDIR" --purge 2>/dev/null || true
    # Verify no qemu process for p11 remains.
    if pgrep -f "qemu-system-x86_64.*-name p11" >/dev/null 2>&1; then
        echo "WARNING: p11 qemu process still running after teardown"
    else
        echo "Teardown confirmed: no p11 qemu process."
    fi
}
trap cleanup EXIT

# Boot the VM; the harness provisions the qdistro-admin rpcd login during `up`
# (drain + 60s console timeout, load-robust). Sets SESSION_JSON, OVERLAY_IMAGE,
# BASE_REPORTED, URL, PW.
boot_and_provision() {
    local json
    json="$(python3 "$SESSION_PY" up \
        --name p11 \
        --base "$NETVM_BASE" \
        --http-port "$NETVM_PORT" \
        --rundir "$NETVM_RUNDIR")"
    echo "$json"  # print for logging

    # Export the parsed fields for the caller.
    SESSION_JSON="$json"
    OVERLAY_IMAGE="$(echo "$json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["image"])')"
    BASE_REPORTED="$(echo "$json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["base"])')"
    URL="$(echo "$json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["url"])')"
    PW="$(echo "$json" | python3 -c 'import sys,json; print(json.load(sys.stdin)["password"])')"
}

vm() {
    python3 "$SESSION_PY" run --rundir "$NETVM_RUNDIR" "$1"
}

# ---- record base sha256 BEFORE boot -----------------------------------------
hdr "Pre-boot: base image sha256"
if [ ! -f "$NETVM_BASE" ]; then
    echo "FATAL: base image not found: $NETVM_BASE" >&2
    exit 2
fi
BASE_SHA_BEFORE="$(sha256sum "$NETVM_BASE" | awk '{print $1}')"
echo "base sha256 (before boot): $BASE_SHA_BEFORE"

# ---- boot the VM (first run) ------------------------------------------------
hdr "Booting VM (p11, first run)"
echo "base: $NETVM_BASE  port: $NETVM_PORT  rundir: $NETVM_RUNDIR"
SESSION_JSON=""
OVERLAY_IMAGE=""
BASE_REPORTED=""
URL=""
PW=""
boot_and_provision

echo "overlay image : $OVERLAY_IMAGE"
echo "base (reported): $BASE_REPORTED"

# ---- I3 part A: overlay is a different file from base -----------------------
hdr "I3a: overlay is a separate file from the base"
if [ "$OVERLAY_IMAGE" != "$BASE_REPORTED" ]; then
    ok "overlay path ($OVERLAY_IMAGE) differs from base path ($BASE_REPORTED)"
else
    bad "overlay and base paths are identical — base would be mutated"
fi

# ---- I1: root filesystem is read-only squashfs with overlay -----------------
hdr "I1: root filesystem and overlay mounts"

echo "--- full mount table ---"
vm "mount" 2>&1
echo

# squashfs on /rom
SQUASHFS_ROM="$(vm "mount" 2>&1 | grep 'squashfs' || true)"
echo "squashfs mounts: $SQUASHFS_ROM"
if echo "$SQUASHFS_ROM" | grep -q 'squashfs'; then
    ok "squashfs mount present (read-only root backing)"
else
    bad "no squashfs mount found"
fi

# overlayfs on /
OVERLAYFS="$(vm "mount" 2>&1 | grep 'overlayfs\|overlay' || true)"
echo "overlay mounts: $OVERLAYFS"
if echo "$OVERLAYFS" | grep -qE 'overlayfs|overlay'; then
    ok "overlayfs mount present (writable overlay layer)"
else
    bad "no overlayfs mount found"
fi

# /overlay is a distinct mount
OVERLAY_MOUNT="$(vm "mount" 2>&1 | grep '/overlay' || true)"
echo "overlay mount: $OVERLAY_MOUNT"
if echo "$OVERLAY_MOUNT" | grep -q '/overlay'; then
    ok "/overlay is a distinct mounted filesystem"
else
    bad "/overlay not visible as a distinct mount"
fi

# Write to a squashfs-backed path under /rom must fail
echo "--- testing write to /rom (squashfs-backed, must fail) ---"
vm "touch /rom/PROBE_WRITE_TEST" >/dev/null 2>&1 \
    && ROM_WRITE_RESULT="succeeded" || ROM_WRITE_RESULT="failed-as-expected"
echo "write to /rom/PROBE_WRITE_TEST: $ROM_WRITE_RESULT"
if [ "$ROM_WRITE_RESULT" = "failed-as-expected" ]; then
    ok "write to squashfs /rom path is rejected (read-only)"
else
    bad "write to /rom succeeded — squashfs not read-only"
fi

# Write to /etc lands in overlay upper
echo "--- testing write to /etc (lands in overlay upper) ---"
vm "touch /etc/PROBE_OVERLAY_WRITE" >/dev/null 2>&1 \
    && ETCWRITE_RC=0 || ETCWRITE_RC=$?
if [ "$ETCWRITE_RC" = 0 ]; then
    ok "write to /etc succeeded (overlay accepts it)"
    OVERLAY_UPPER="$(vm "ls /overlay/upper/etc/PROBE_OVERLAY_WRITE 2>&1" 2>&1 || true)"
    echo "overlay upper check: $OVERLAY_UPPER"
    if echo "$OVERLAY_UPPER" | grep -q 'PROBE_OVERLAY_WRITE'; then
        ok "written file appears in /overlay/upper/etc (overlay upper confirmed)"
    else
        bad "written file NOT found in /overlay/upper/etc"
    fi
else
    bad "write to /etc failed (overlay not accepting writes)"
fi

# ---- I2: baseline config + apply idempotency + egress_reload ----------------
hdr "I2: baseline config files and qdistro-netvm-apply idempotency"

for f in network firewall dhcp; do
    FPATH="/etc/qdistro-netvm/baseline/$f"
    CHECK_OUT="$(vm "ls $FPATH 2>&1" 2>&1 || true)"
    if echo "$CHECK_OUT" | grep -q "$FPATH"; then
        ok "baseline file exists: $FPATH"
    else
        bad "baseline file MISSING: $FPATH (got: $CHECK_OUT)"
    fi
done

echo "--- apply helper exists ---"
APPLY_CHECK="$(vm "ls /usr/libexec/qdistro-netvm-apply 2>&1" 2>&1 || true)"
if echo "$APPLY_CHECK" | grep -q 'qdistro-netvm-apply'; then
    ok "qdistro-netvm-apply helper present"
else
    bad "qdistro-netvm-apply helper NOT found"
fi

# vm() exits with the guest command's rc, so key the assertions on rc (not on a
# swallowed exit) — an apply that fails must turn the PASS into a FAIL.
echo "--- run apply (round 1) ---"
APPLY_R1="$(vm "/usr/libexec/qdistro-netvm-apply" 2>&1)"; APPLY_RC1=$?
echo "apply round 1 (rc=$APPLY_RC1): $APPLY_R1"

echo "--- run apply (round 2, idempotency check) ---"
APPLY_R2="$(vm "/usr/libexec/qdistro-netvm-apply" 2>&1)"; APPLY_RC2=$?
echo "apply round 2 (rc=$APPLY_RC2): $APPLY_R2"

if [ "$APPLY_RC1" -eq 0 ] && [ "$APPLY_RC2" -eq 0 ]; then
    ok "qdistro-netvm-apply succeeded on both runs (rc=0 each; idempotent)"
else
    bad "qdistro-netvm-apply failed (round1 rc=$APPLY_RC1, round2 rc=$APPLY_RC2)"
fi

echo "--- firstboot binary presence (informational) ---"
FB="$(vm "which firstboot 2>&1 || echo NOT_FOUND" 2>&1 || true)"
echo "firstboot: $FB"

echo "--- egress_reload via control-plane client ---"
PYTHONPATH="$REPO/session_manager" python3 - "$URL" "$PW" "qdistro-admin" <<'PY'
import sys
from qdistro_silo_egress import EgressPolicy
from qdistro_netvm_uci import SiloNet, compile_all
from qdistro_netvm_client import NetVMClient

url, pw, user = sys.argv[1], sys.argv[2], sys.argv[3]
c = NetVMClient(base_url=url, password=pw, username=user, timeout=15)

_fails = 0
def chk(name, cond):
    global _fails
    if not cond:
        _fails += 1
    print(("PASS: " if cond else "FAIL: ") + name)

direct = SiloNet(name="work", index=5, policy=EgressPolicy.parse("direct"))
dark   = SiloNet(name="vault", index=6, policy=EgressPolicy.parse("none"))
frags  = compile_all([direct, dark])
# Seed silo L3 interfaces (the test exercises policy reload, not vif lifecycle).
frags["network"] = (
    "config interface 'silo5'\n\toption proto 'static'\n\toption device 'qdsilo5'\n"
    "\toption ipaddr '10.10.6.1'\n\toption netmask '255.255.255.0'\n"
    "config interface 'silo6'\n\toption proto 'static'\n\toption device 'qdsilo6'\n"
    "\toption ipaddr '10.10.7.1'\n\toption netmask '255.255.255.0'\n"
) + frags["network"]

r1 = c.egress_reload(frags)
chk("egress_reload applies compiled silo policy", r1.get("applied") is True)

r2 = c.egress_reload(frags)
chk("egress_reload is idempotent (second call also applied)", r2.get("applied") is True)
sys.exit(1 if _fails else 0)
PY
EGRESS_RC=$?
if [ "$EGRESS_RC" -eq 0 ]; then
    ok "egress_reload applied + idempotent via control plane"
else
    bad "egress_reload python block reported failures (exit $EGRESS_RC)"
fi

# ---- I4: mutation + teardown + fresh boot = clean state ---------------------
hdr "I4: mutation in overlay is gone after --purge + fresh boot"

echo "--- applying mutations ---"
vm "touch /etc/PROBE_MUTATION" >/dev/null 2>&1
vm "uci set system.@system[0].hostname=PROBED_MUTANT" >/dev/null 2>&1
vm "uci commit system" >/dev/null 2>&1

MUTATION_CHECK="$(vm "ls /etc/PROBE_MUTATION && uci get system.@system[0].hostname" 2>&1 || true)"
echo "mutation installed: $MUTATION_CHECK"
if echo "$MUTATION_CHECK" | grep -q 'PROBED_MUTANT'; then
    ok "mutation confirmed in running VM (/etc/PROBE_MUTATION + hostname=PROBED_MUTANT)"
else
    bad "could not confirm mutation in running VM"
fi

echo "--- tearing down with --purge ---"
python3 "$SESSION_PY" down --rundir "$NETVM_RUNDIR" --purge
echo "teardown done"

# ---- I3 part B: base sha256 AFTER teardown (must equal before) --------------
hdr "I3b: base image sha256 after teardown"
BASE_SHA_AFTER="$(sha256sum "$NETVM_BASE" | awk '{print $1}')"
echo "base sha256 (after teardown): $BASE_SHA_AFTER"
if [ "$BASE_SHA_BEFORE" = "$BASE_SHA_AFTER" ]; then
    ok "base qcow2 sha256 unchanged: before=$BASE_SHA_BEFORE after=$BASE_SHA_AFTER"
else
    bad "base qcow2 sha256 CHANGED: before=$BASE_SHA_BEFORE after=$BASE_SHA_AFTER"
fi

# ---- fresh boot to prove rebuild-to-known-state -----------------------------
hdr "I4 continued: fresh boot (new overlay over pristine base)"
boot_and_provision

OVERLAY_IMAGE2="$(echo "$SESSION_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["image"])')"
echo "fresh overlay: $OVERLAY_IMAGE2"

# Probe mutations must be absent in the fresh boot. Key on the rc of `test -e`
# (vm() propagates the guest rc) rather than scraping an error string — an empty
# or unexpected console reply must NOT read as "gone".
vm "test -e /etc/PROBE_MUTATION" >/dev/null 2>&1; MUT_PRESENT=$?
echo "test -e /etc/PROBE_MUTATION rc=$MUT_PRESENT (nonzero = absent)"
if [ "$MUT_PRESENT" -ne 0 ]; then
    ok "/etc/PROBE_MUTATION is GONE after fresh boot (overlay is fresh)"
else
    bad "/etc/PROBE_MUTATION still present after fresh boot — overlay not fresh"
fi

vm "test -e /etc/PROBE_OVERLAY_WRITE" >/dev/null 2>&1; OW_PRESENT=$?
echo "test -e /etc/PROBE_OVERLAY_WRITE rc=$OW_PRESENT (nonzero = absent)"
if [ "$OW_PRESENT" -ne 0 ]; then
    ok "/etc/PROBE_OVERLAY_WRITE is GONE after fresh boot (overlay is fresh)"
else
    bad "/etc/PROBE_OVERLAY_WRITE still present — overlay not fresh"
fi

HOSTNAME_CLEAN="$(vm "uci get system.@system[0].hostname 2>&1" 2>&1 || true)"
echo "hostname after fresh boot: $HOSTNAME_CLEAN"
# Use a negated exact-match (NOT `grep -qv`, which is true whenever ANY line
# fails to match and would false-green on multi-line output).
if ! echo "$HOSTNAME_CLEAN" | grep -q 'PROBED_MUTANT'; then
    ok "hostname reset to baseline (not PROBED_MUTANT): $HOSTNAME_CLEAN"
else
    bad "hostname is still PROBED_MUTANT after fresh boot"
fi

# NOTE (honest limits): firstboot -y && reboot in-place NOT exercised.
# The harness uses -no-reboot (reboot exits qemu); the ephemeral-overlay
# approach above (--purge + fresh 'up') is the equivalent proof of
# rebuild-to-known-state.
FIRSTBOOT_PRESENT="$(vm "which firstboot 2>&1 || echo NOT_FOUND" 2>&1 || true)"
echo "firstboot binary in fresh VM: $FIRSTBOOT_PRESENT"
echo "NOTE: in-place firstboot reset NOT exercised (qemu -no-reboot; reboot exits VM)"

# ---- summary ----------------------------------------------------------------
hdr "Summary"
echo "PASS count: $pass"
echo "FAIL count: $fail"
echo
[ "$fail" -eq 0 ]
