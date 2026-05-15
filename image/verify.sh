#!/bin/bash
# verify.sh — boot the built qdistro image in libvirt (qemu:///session,
# rootless), assert via SSH that the qdistro stack came up cleanly,
# capture screenshots and journal evidence to logs/verify/.
#
# Pattern mirrors qdistro/scripts/install/ui-agent-test.sh and the
# bats VM tests under qdistro/tests/integration/vm/.
#
# Load-bearing assertions are journal lines (per project memory:
# test_harness — "the load-bearing assertion is journal lines, not
# pixels"). Screenshots are evidence, not assertion fuel.
#
# Usage:
#   ./verify.sh                 # full lifecycle
#   ./verify.sh --keep          # leave the VM running for manual poking
#   ./verify.sh --teardown      # destroy + undefine + clean up

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${QDISTRO_BUILD_DIR:-/tmp/qdistro-build}"
VERIFY_DIR="$HERE/logs/verify-$(date +%y%m%d-%H%M%S)"
VM="qdistro-verify"
SSH_PORT=2299
SSH_USER="admin"
SSH_PASS="qdistro"
URI="qemu:///session"

log()  { printf '\033[1;36m[verify]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[verify] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[verify] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

teardown() {
    log "tearing down VM $VM"
    virsh -c "$URI" destroy   "$VM" 2>/dev/null || true
    virsh -c "$URI" undefine  "$VM" --nvram 2>/dev/null || true
    rm -f "$BUILD_DIR/$VM.qcow2"
}

case "${1:-}" in
  --teardown) teardown; exit 0 ;;
esac

#-- 0. Locate the built image -------------------------------------------------
IMG=$(find "$BUILD_DIR" -maxdepth 2 -name '*.raw' -o -name '*.qcow2' 2>/dev/null | grep -v "$VM" | head -1)
[ -n "$IMG" ] || die "no image in $BUILD_DIR; run build.sh first"
log "image: $IMG"

mkdir -p "$VERIFY_DIR/screenshots" "$VERIFY_DIR/journal"

#-- 1. Make a qcow2 overlay so we don't trash the master ----------------------
# teardown() must happen BEFORE we create the overlay, otherwise it
# deletes the file we just made.
teardown
OVERLAY="$BUILD_DIR/$VM.qcow2"
case "$IMG" in
  *.raw)   FMT=raw   ;;
  *.qcow2) FMT=qcow2 ;;
  *) die "unknown image format: $IMG" ;;
esac
qemu-img create -F "$FMT" -b "$IMG" -f qcow2 "$OVERLAY" >/dev/null
log "overlay: $OVERLAY (backing $IMG)"

#-- 2. Locate UEFI firmware (qemu:///session needs an absolute path) ----------
for c in /usr/share/qemu/ovmf-x86_64-4m.bin \
         /usr/share/qemu/ovmf-x86_64-code.bin \
         /usr/share/qemu/ovmf-x86_64.bin \
         /usr/share/OVMF/OVMF_CODE.fd ; do
    [ -f "$c" ] && OVMF="$c" && break
done
[ -n "${OVMF:-}" ] || die "no OVMF firmware (install qemu-ovmf-x86_64 or OVMF)"
log "uefi firmware: $OVMF"

#-- 3. Domain XML (rootless qemu:///session, SSH port forward 2299->22) ------
NVRAM="$VERIFY_DIR/$VM.nvram.fd"
# Seed nvram from the matching template if present so secure-boot vars
# don't trip kiwi's UEFI bootloader.
for tpl in /usr/share/qemu/ovmf-x86_64-vars.bin /usr/share/qemu/ovmf-x86_64-4m-vars.bin /usr/share/OVMF/OVMF_VARS.fd; do
    [ -f "$tpl" ] && cp "$tpl" "$NVRAM" && break
done
DOMAIN_XML=$(cat <<XML
<domain type='kvm'>
  <name>$VM</name>
  <memory unit='MiB'>4096</memory>
  <vcpu>2</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' type='pflash'>$OVMF</loader>
    <nvram template='$OVMF'>$NVRAM</nvram>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$OVERLAY'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='user'>
      <backend type='passt'/>
      <model type='virtio'/>
      <portForward proto='tcp' address='127.0.0.1'>
        <range start='$SSH_PORT' to='22'/>
      </portForward>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <graphics type='spice' autoport='yes' listen='127.0.0.1'/>
    <video><model type='virtio' heads='1'/></video>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
  </devices>
</domain>
XML
)

#-- 4. Define + start ---------------------------------------------------------
log "defining $VM"
echo "$DOMAIN_XML" | virsh -c "$URI" define /dev/stdin
log "starting $VM"
virsh -c "$URI" start "$VM"

#-- 5. Screenshot helper ------------------------------------------------------
shoot() {
    local label="$1"
    local out="$VERIFY_DIR/screenshots/$label.png"
    local tmp
    tmp=$(mktemp --suffix=.ppm)
    if virsh -c "$URI" screenshot "$VM" "$tmp" >/dev/null 2>&1; then
        if command -v convert >/dev/null 2>&1; then
            convert "$tmp" "$out" 2>/dev/null && log "screenshot: $out"
        elif command -v ffmpeg >/dev/null 2>&1; then
            ffmpeg -y -loglevel error -i "$tmp" "$out" && log "screenshot: $out"
        fi
        rm -f "$tmp"
    else
        warn "screenshot failed (display not ready yet)"
    fi
}

shoot 00-just-booted

#-- 6. Wait for SSH (~3-5 min for first-boot resize + firstboot) -------------
log "waiting for SSH on 127.0.0.1:$SSH_PORT (max 600s)..."
deadline=$(( $(date +%s) + 600 ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if exec 3<>/dev/tcp/127.0.0.1/$SSH_PORT 2>/dev/null; then
        exec 3<&-; exec 3>&-
        ready=1
        break
    fi
    sleep 5
done
[ "$ready" = 1 ] || { shoot 99-ssh-timeout; die "SSH never came up; see $VERIFY_DIR/screenshots/99-ssh-timeout.png"; }

shoot 01-ssh-ready
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o LogLevel=ERROR"
if ! command -v sshpass >/dev/null 2>&1; then
    die "sshpass not installed; install with: sudo zypper in sshpass"
fi
remote() {
    sshpass -p "$SSH_PASS" ssh $SSH_OPTS -p "$SSH_PORT" "$SSH_USER@127.0.0.1" "$@"
}
log "waiting for sshd to actually accept auth (kex banner can race)..."
for i in $(seq 1 30); do
    if remote 'true' 2>/dev/null; then
        log "sshd ready after ${i}x5s"
        break
    fi
    sleep 5
done
log "giving services 30s to come up (broker, greetd, user units)..."
sleep 30

#-- 8. Assertions (journal-side, per project memory) --------------------------
PASS=0; FAIL=0
expect() {
    local label="$1"; shift
    if "$@" >>"$VERIFY_DIR/assert.log" 2>&1; then
        echo "PASS: $label" | tee -a "$VERIFY_DIR/report.txt"
        PASS=$((PASS+1))
    else
        echo "FAIL: $label" | tee -a "$VERIFY_DIR/report.txt"
        FAIL=$((FAIL+1))
    fi
}

log "running in-VM assertions..."

# Basic boot
expect "ssh reachable as admin"      remote 'id -un | grep -qx admin'
expect "/etc/os-release is qdistro"  remote "grep -Eq '^ID=\"?qdistro\"?$' /etc/os-release"
expect "hostname is qdistro"         remote "grep -qx qdistro /etc/hostname"

# Greetd brought up the graphical target
expect "greetd.service active"       remote 'systemctl is-active greetd.service'
expect "default target is graphical" remote 'systemctl get-default | grep -qx graphical.target'

# qdistro admin broker on the system bus
expect "qdistro-admin-broker active" remote 'systemctl is-active qdistro-admin-broker.service || sudo -n systemctl is-active qdistro-admin-broker.service'
expect "broker owns dbus name"       remote "sudo -n busctl list --no-pager | grep -q com.qdistro.AdminBroker1"

# qdwin + qdshell user units for admin
expect "noctalia-session user unit enabled" \
    remote "sudo -n machinectl shell admin@.host /usr/bin/systemctl --user is-enabled noctalia-session.service 2>/dev/null | grep -qx enabled || \
            sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-enabled noctalia-session.service | grep -qx enabled"
expect "noctalia-shell user unit enabled" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-enabled noctalia-shell.service | grep -qx enabled"

# qdwin compositor process check (best-effort; may not have started yet
# on serial-console login since DRM seat goes to greetd)
expect "weston (qdwin) on disk" \
    remote 'test -f /usr/lib64/weston/qdwin-shell.so || test -f /usr/lib/weston/qdwin-shell.so'
expect "qdshell QML installed"  remote 'test -d /usr/share/quickshell/qdshell'

# No fatal errors in this boot's journal
expect "no priority=0/1 errors in journal" \
    remote 'sudo -n journalctl -b -p emerg..alert --no-pager -q | wc -l | grep -qx 0'

#-- 9. Capture journals + systemctl state for evidence -----------------------
log "capturing journals + systemd state"
remote 'sudo -n journalctl -b --no-pager'                > "$VERIFY_DIR/journal/full.log"      2>&1 || true
remote 'sudo -n journalctl -b -p err --no-pager'         > "$VERIFY_DIR/journal/errors.log"    2>&1 || true
remote 'sudo -n journalctl -u qdistro-admin-broker --no-pager' > "$VERIFY_DIR/journal/broker.log" 2>&1 || true
remote 'sudo -n journalctl -u greetd --no-pager'         > "$VERIFY_DIR/journal/greetd.log"    2>&1 || true
remote 'systemctl --failed --no-pager'                   > "$VERIFY_DIR/journal/failed-units.log" 2>&1 || true
remote 'sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user --no-pager status noctalia-session.service noctalia-shell.service' \
    > "$VERIFY_DIR/journal/user-units.log" 2>&1 || true

shoot 02-fully-booted

#-- 10. Try to advance into graphical session via tty switch -----------------
# greetd lives on tty1 in our config; trying to take a screenshot of the
# qdwin compositor when greetd auto-logs admin in.
log "waiting 30s for graphical session to come up, then capturing..."
sleep 30
shoot 03-after-30s
sleep 30
shoot 04-after-60s

#-- 11. Report -----------------------------------------------------------------
TOTAL=$((PASS+FAIL))
{
    echo ""
    echo "=========================================="
    echo " qdistro verify summary"
    echo " image:   $IMG"
    echo " when:    $(date -Is)"
    echo " pass:    $PASS / $TOTAL"
    echo " fail:    $FAIL / $TOTAL"
    echo " artifacts: $VERIFY_DIR"
    echo "=========================================="
} | tee -a "$VERIFY_DIR/report.txt"

case "${1:-}" in
  --keep)
    log "VM left running (--keep). To tear down: $0 --teardown"
    ;;
  *)
    teardown
    ;;
esac

[ "$FAIL" -eq 0 ]
