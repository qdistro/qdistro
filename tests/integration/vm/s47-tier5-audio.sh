#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier5-audio.
#
# Exercises the spec/29 §3 picked audio path: qemu's -audiodev pipewire
# backend bridges in-guest ALSA/PipeWire output into the admin user's
# host PipeWire daemon (/run/user/<uid>/pipewire-0). The tier-5 domain
# template already declares:
#
#     <sound model='ich9'><audio id='1'/></sound>
#     <audio id='1' type='pipewire'> ... </audio>
#
# so QEMU exposes an ich9 sound card to the guest and routes its
# playback stream to the admin's PipeWire socket. This driver verifies
# end-to-end:
#   1. host preconditions (pipewire running, pw-cli, /dev/kvm,
#      admin compositor up, tier-5 base image present),
#   2. the domain template carries the pipewire <audio> stanza,
#   3. a domain defined+started from the template reaches the running
#      state and qga becomes ready,
#   4. speaker-test launched inside the guest via qga guest-exec
#      returns a non-zero pid,
#   5. the admin's host PipeWire daemon enumerates a client/stream
#      whose application name contains "qemu" or our VM tag — i.e.
#      the guest's playback actually made it across the QEMU audiodev
#      bridge to host PipeWire.
#
# SKIPS cleanly when any prereq is missing (tier-5 base image, pw-cli,
# pipewire daemon, /dev/kvm, qemu-audio-pipewire backend). The bats
# block fail_loud's on SKIP — that's the contract.
#
# Pairs with `tests/integration/permissions-gui/22-tier5-audio.md`
# (visual variant: play a tone, confirm host speakers actually emit).

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# ---- 0. stage tier5-vm/ next to the script, like s45 ----------------
SRC=/root/qdistro-src/qdistro
TIER5_DIR=/tmp/qdistro-tier5
if [ -d "$SRC/tier5-vm" ]; then
    rm -rf "$TIER5_DIR" 2>/dev/null || true
    cp -r "$SRC/tier5-vm" "$TIER5_DIR"
    chmod -R a+rX "$TIER5_DIR"
    find "$TIER5_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER5_DIR" ] || skip "tier5-vm source not unpacked at $TIER5_DIR"

BASE=/var/lib/libvirt/images/qdistro-tier5-base.qcow2
[ -f "$BASE" ] || skip "tier-5 base image $BASE not built (rerun fresh-vm-bootstrap.sh with QDISTRO_BUILD_TIER5_BASE=1)"

command -v virsh    >/dev/null 2>&1 || skip "virsh not installed in this VM"
command -v qemu-img >/dev/null 2>&1 || skip "qemu-img not installed in this VM"
command -v pw-cli   >/dev/null 2>&1 || skip "pw-cli not installed (need pipewire-tools package)"
[ -e /dev/kvm ] || skip "/dev/kvm not present (nested-virt not enabled for this VM)"

ADMIN_UID=1000
ADMIN_USER=admin
RUNTIME_DIR="/run/user/$ADMIN_UID"

# admin compositor up (same probe s45 uses).
if ! runuser -u "$ADMIN_USER" -- test -S "$RUNTIME_DIR/wayland-1"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi

# admin's pipewire daemon reachable through pw-cli. This also acts as a
# soft-probe for the qemu-audio-pipewire backend availability — if the
# host has no PipeWire, qemu would have nothing to connect to. We do
# NOT directly probe qemu's modules list (no portable way); we let
# virsh start surface the failure if qemu-audio-pipewire is missing.
if ! runuser -u "$ADMIN_USER" -- \
        env XDG_RUNTIME_DIR="$RUNTIME_DIR" pw-cli info 0 >/dev/null 2>&1; then
    skip "admin's pipewire daemon not reachable via pw-cli (pipewire not running for uid=$ADMIN_UID)"
fi

pass "host preconditions met"

# ---- 1. assert the template carries <audio type='pipewire'> ---------
TMPL="$TIER5_DIR/domain-template.xml"
[ -f "$TMPL" ] || fail "domain template missing at $TMPL"
if grep -Eq "<audio[^>]*type='pipewire'" "$TMPL" \
   && grep -Eq "<sound[^>]*model='ich9'" "$TMPL"; then
    pass "domain template carries <audio type='pipewire'> + <sound model='ich9'>"
else
    fail "domain template at $TMPL does not declare pipewire audio + ich9 sound card"
fi

# ---- 2. define+start a tier-5 domain DIRECTLY (no spawn-tier5) ------
# Bypass spawn-tier5.sh: it waits on the waypipe-client (--oneshot)
# which exits as soon as the in-guest publisher.sh decides the inner
# app isn't a Wayland client. That tears down the VM before we can
# qga-exec speaker-test. We only need the domain itself for the
# audio routing path; everything spawn-tier5 layers on top
# (waypipe, vsock listener, publisher) is orthogonal here.

VM_NAME="qdistro-tier5-audio-$$"
OVERLAY="/home/$ADMIN_USER/.local/share/libvirt/images/$VM_NAME.qcow2"
TMP_XML="/tmp/qdistro-tier5-audio-$$.xml"
SPAWN_LOG=/tmp/s47-spawn.log
: >"$SPAWN_LOG"

cleanup_vm() {
    runuser -u "$ADMIN_USER" -- virsh destroy  "$VM_NAME" >/dev/null 2>&1 || true
    runuser -u "$ADMIN_USER" -- virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    rm -f "$OVERLAY" "$TMP_XML" 2>/dev/null || true
}
trap cleanup_vm EXIT

# Overlay disk — linked clone of $BASE.
install -d -m 0755 "$(dirname "$OVERLAY")"
chown "$ADMIN_USER" "$(dirname "$OVERLAY")"
qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$OVERLAY" >>"$SPAWN_LOG" 2>&1
chown "$ADMIN_USER" "$OVERLAY"

# Per-VM vsock CID — pick one high enough to avoid collision with
# other tier-5 spawns + above CID=2 (host reserved). s47 doesn't
# care about the CID's actual contents.
CID=$(( 100 + (RANDOM % 100) ))

# Substitute the template. Mirrors spawn-tier5.sh's logic but inlined.
NIC_XML="<!-- TIER5_NETWORK=none: no NIC by default -->"
NIC_XML_ESCAPED=$(printf '%s' "$NIC_XML" | sed 's|[\\/&]|\\&|g')
sed \
    -e "s|__NIC_XML__|$NIC_XML_ESCAPED|g" \
    -e "s|__VM_NAME__|$VM_NAME|g" \
    -e "s|__MAC__|52:54:00:11:22:33|g" \
    -e "s|__MEM_KIB__|524288|g" \
    -e "s|__CID__|$CID|g" \
    -e "s|__DISK_PATH__|$OVERLAY|g" \
    "$TMPL" >"$TMP_XML"
chown "$ADMIN_USER" "$TMP_XML"; chmod 0644 "$TMP_XML"

if grep -q '__[A-Z_]*__' "$TMP_XML"; then
    fail "unsubstituted markers in domain XML: $(grep -oE '__[A-Z_]*__' "$TMP_XML" | sort -u | xargs)"
fi

if ! runuser -u "$ADMIN_USER" -- env XDG_RUNTIME_DIR=/run/user/$ADMIN_UID \
        virsh define "$TMP_XML" >>"$SPAWN_LOG" 2>&1; then
    cat "$SPAWN_LOG" >&2 || true
    fail "virsh define refused the domain XML"
fi

if ! runuser -u "$ADMIN_USER" -- env XDG_RUNTIME_DIR=/run/user/$ADMIN_UID \
        virsh start "$VM_NAME" >>"$SPAWN_LOG" 2>&1; then
    cat "$SPAWN_LOG" >&2 || true
    # qemu-audio-pipewire missing surfaces here: virsh start refuses
    # the audio stanza. Soft-probe before hard-failing.
    if grep -qiE "audiodev|pipewire.*not (found|supported)|unknown audio" \
            "$SPAWN_LOG" 2>/dev/null; then
        skip "qemu-audio-pipewire backend not available on this host (virsh start refused the audio stanza)"
    fi
    fail "virsh start refused the domain"
fi

# Wait for domain to reach running (qcow2 overlay creation + boot
# can take 60s+ on a slow nested VM).
DOMAIN_OK=0
for _ in $(seq 1 240); do
    if runuser -u "$ADMIN_USER" -- env XDG_RUNTIME_DIR=/run/user/$ADMIN_UID \
        virsh domstate "$VM_NAME" 2>/dev/null | grep -qw running; then
        DOMAIN_OK=1; break
    fi
    sleep 0.5
done
if [ "$DOMAIN_OK" = "1" ]; then
    pass "domain $VM_NAME running"
else
    cat "$SPAWN_LOG" >&2 || true
    fail "libvirt domain never reached running state within 120s"
fi

# Wait for qga inside the guest to respond. cloud-init firstboot
# can take 60s+; qemu-guest-agent doesn't bind until after that.
QGA_OK=0
for _ in $(seq 1 120); do
    if runuser -u "$ADMIN_USER" -- env XDG_RUNTIME_DIR=/run/user/$ADMIN_UID \
        virsh qemu-agent-command "$VM_NAME" '{"execute":"guest-ping"}' \
        >/dev/null 2>&1; then
        QGA_OK=1; break
    fi
    sleep 1
done
if [ "$QGA_OK" = "1" ]; then
    pass "qga ready"
else
    fail "guest qemu-guest-agent never responded within 120s"
fi

# ---- 3. launch speaker-test inside the guest via qga guest-exec -----
# A short sine burst at 440Hz, 2 channels, looped once (~3s) is enough
# for the host PipeWire side to enumerate the stream. We don't care
# about the exit code — we care that qemu opened the playback path.
#
# qga guest-exec returns {"return":{"pid":N}}. Anything else (no pid,
# error, or the binary missing in the guest) is a hard fail.
SPK_REQ='{"execute":"guest-exec","arguments":{"path":"/usr/bin/speaker-test","arg":["-t","sine","-f","440","-l","2","-c","2"],"capture-output":false}}'
SPK_REPLY=$(runuser -u "$ADMIN_USER" -- env XDG_RUNTIME_DIR=/run/user/$ADMIN_UID \
    virsh qemu-agent-command "$VM_NAME" "$SPK_REQ" 2>/dev/null || true)

if [ -z "$SPK_REPLY" ]; then
    # Some builds path speaker-test at /usr/sbin or alsa-utils may be
    # missing entirely. Retry with a bare command name to let the guest
    # PATH resolve it, in case absolute path was wrong for this image.
    SPK_REQ2='{"execute":"guest-exec","arguments":{"path":"speaker-test","arg":["-t","sine","-f","440","-l","2","-c","2"],"capture-output":false}}'
    SPK_REPLY=$(runuser -u "$ADMIN_USER" -- env XDG_RUNTIME_DIR=/run/user/$ADMIN_UID \
        virsh qemu-agent-command "$VM_NAME" "$SPK_REQ2" 2>/dev/null || true)
fi

SPK_PID=""
if echo "$SPK_REPLY" | grep -q '"pid"'; then
    SPK_PID=$(echo "$SPK_REPLY" \
        | grep -oE '"pid"[[:space:]]*:[[:space:]]*[0-9]+' \
        | grep -oE '[0-9]+' | head -1)
fi

if [ -n "$SPK_PID" ] && [ "$SPK_PID" -gt 0 ] 2>/dev/null; then
    pass "guest speaker-test started (guest pid=$SPK_PID)"
else
    echo "INFO: qga reply was: $SPK_REPLY" >&2
    fail "qga guest-exec did not return a usable pid for speaker-test (alsa-utils missing in guest image?)"
fi

# ---- 4. observe a QEMU stream on host PipeWire ----------------------
# pw-cli ls Node lists every node; the qemu-audio-pipewire backend
# registers as a stream/output node with application.name containing
# "qemu" (and often application.process.binary = qemu-system-x86_64).
# We poll for up to ~12s — speaker-test takes ~1.5s to open ALSA and
# PipeWire's auto-link can take a beat on first stream.
#
# Heuristic surface (in order of preference; first hit wins):
#   a) pw-cli ls Node           — application.name *qemu*
#   b) pw-cli ls Node           — node.name        *qemu* / *VM_NAME*
#   c) pactl list short clients — application name *qemu*
#      (only if pipewire-pulse compat is up; not required)
#
# This is the trickiest assertion in the file: PipeWire's text output
# isn't stable across versions, so we keep the match permissive
# (case-insensitive substring on multiple key=value lines). False
# positives here would require a host process that already advertises
# itself as "qemu" — unlikely on a fresh bats VM.
PW_SEEN=0
deadline=$(( $(date +%s) + 15 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    NODES=$(runuser -u "$ADMIN_USER" -- \
        env XDG_RUNTIME_DIR="$RUNTIME_DIR" pw-cli ls Node 2>/dev/null || true)
    if echo "$NODES" | grep -iqE 'application\.name[[:space:]]*=[[:space:]]*"[^"]*qemu'; then
        PW_SEEN=1; break
    fi
    if echo "$NODES" | grep -iqE 'node\.name[[:space:]]*=[[:space:]]*"[^"]*(qemu|'"$VM_NAME"')'; then
        PW_SEEN=1; break
    fi
    if command -v pactl >/dev/null 2>&1; then
        CLIENTS=$(runuser -u "$ADMIN_USER" -- \
            env XDG_RUNTIME_DIR="$RUNTIME_DIR" pactl list short clients 2>/dev/null || true)
        if echo "$CLIENTS" | grep -iq "qemu"; then
            PW_SEEN=1; break
        fi
    fi
    sleep 1
done

if [ "$PW_SEEN" = "1" ]; then
    pass "host PipeWire saw QEMU audio stream"
else
    echo "INFO: pw-cli ls Node output for the admin daemon:" >&2
    runuser -u "$ADMIN_USER" -- \
        env XDG_RUNTIME_DIR="$RUNTIME_DIR" pw-cli ls Node 2>/dev/null \
        | head -200 >&2 || true
    fail "host PipeWire never enumerated a node/client tagged 'qemu' within 15s after speaker-test launched in guest"
fi

# (cleanup_vm trap handles teardown)

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-5-Linux audio (qemu -audiodev pipewire) end-to-end"
    echo "[s47] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s47] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
