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
#   ./verify.sh                 # boot + Phase E default (grow, UUID, persist, greeter login)
#   ./verify.sh --keep          # leave the VM running for manual poking
#   ./verify.sh --teardown      # destroy + undefine + clean up
#   ./verify.sh --stick         # default plus USB / second-disk / hub / Secure Boot /
#                               # nested-KVM / first-boot power-off / xzcat|dd
#   ./verify.sh --usb|--secure-boot|--second-usb|--usb-hub|--nested|--power-off|--dd
#
# Artifact selection (todo/iso/14 Phase E item 5): QDISTRO_IMAGE is a path
# (.raw / .raw.xz / .qcow2) or a 64-hex digest of the published xz.
# QDISTRO_IMAGE_SHA256 pins the digest when the path is the xz. Never
# `find | head -1`.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Same default as build.sh/build-in-vm.sh: /tmp is a tmpfs on the build
# hosts, so a default-env build followed by a default-env verify must agree
# on /var/tmp or the artifact is simply not found (iso/14 Phase A item 6).
BUILD_DIR="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
STAMP="$(date +%y%m%d-%H%M)"
VERIFY_DIR="$HERE/logs/verify-${STAMP}$(date +%S)"
# AGENTS.md requires VM names end in YYMMDD-HHMM so parallel runs don't collide.
VM="${QDISTRO_VERIFY_VM:-qdistro-verify-${STAMP}}"
SSH_PORT="${QDISTRO_VERIFY_PORT:-2299}"
SSH_USER="admin"
SSH_PASS="${QDISTRO_IMAGE_PASSWORD:-qdistro}"
URI="qemu:///session"

log()  { printf '\033[1;36m[verify]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[verify] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[verify] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

# shellcheck source=lib/select-artifact.sh
. "$HERE/lib/select-artifact.sh"

KEEP=0
STICK=0
BUS=virtio
SECUREBOOT=0
EXTRA_USB=0
USB_HUB=0
POWEROFF=0
DO_DD=0
NESTED=0
# 64 GiB overlay: the raw is 28 GiB; first-boot repart must grow the btrfs
# root. 0 = overlay matches the backing size (no grow).
GROW_GIB="${QDISTRO_VERIFY_GROW_GIB:-64}"
DO_LOGIN="${QDISTRO_VERIFY_LOGIN:-1}"
DO_PERSIST="${QDISTRO_VERIFY_PERSIST:-1}"

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --teardown)
            virsh -c "$URI" destroy "$VM" 2>/dev/null || true
            virsh -c "$URI" undefine "$VM" --nvram 2>/dev/null || true
            rm -f "$BUILD_DIR/$VM.qcow2" "$BUILD_DIR/$VM-extra.qcow2"
            exit 0
            ;;
        --stick) STICK=1 ;;
        --usb) BUS=usb ;;
        --secure-boot) SECUREBOOT=1 ;;
        --second-usb) EXTRA_USB=1; BUS=usb ;;
        --usb-hub) USB_HUB=1; BUS=usb ;;
        --power-off) POWEROFF=1 ;;
        --dd) DO_DD=1 ;;
        --nested) NESTED=1 ;;
        --no-grow) GROW_GIB=0 ;;
        --no-login) DO_LOGIN=0 ;;
        --no-persist) DO_PERSIST=0 ;;
        *) die "unknown flag: $1" ;;
    esac
    shift
done

teardown() {
    log "tearing down VM $VM"
    virsh -c "$URI" destroy   "$VM" 2>/dev/null || true
    virsh -c "$URI" undefine  "$VM" --nvram 2>/dev/null || true
    rm -f "$BUILD_DIR/$VM.qcow2" "$BUILD_DIR/$VM-extra.qcow2"
}

#-- 0. Locate the built image -------------------------------------------------
# Prefer the published xz (bundle/*.raw.xz + .sha256); otherwise exactly
# one top-level .raw. A digest selects the matching checksum file.
qdistro_resolve_image || die "could not resolve image (see select-artifact)"
qdistro_materialize_raw || die "could not materialise raw (see select-artifact)"
IMG="$QDISTRO_RESOLVED_DISK"
log "image: $IMG (kind=${QDISTRO_RESOLVED_KIND} published=${QDISTRO_RESOLVED_PATH})"
if [ -n "${QDISTRO_RESOLVED_DIGEST:-}" ]; then
    log "digest: $QDISTRO_RESOLVED_DIGEST"
fi

if [ "$DO_DD" = 1 ]; then
    [ "$QDISTRO_RESOLVED_KIND" = xz ] || die "--dd needs the published .raw.xz (got kind=$QDISTRO_RESOLVED_KIND)"
    DD_IMG="$BUILD_DIR/published/dd-${QDISTRO_RESOLVED_DIGEST:0:12}.raw"
    if [ -f "$DD_IMG" ]; then
        log "reusing dd target $DD_IMG"
        qdistro_cmp_ends "$DD_IMG" "$IMG" || die "existing dd target does not match materialised raw"
    else
        qdistro_dd_from_xz "$DD_IMG" || die "xzcat | dd / readback failed"
    fi
    IMG="$DD_IMG"
    log "dd image: $IMG (direct-raw boot of the published digest)"
fi

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
DISK_FMT=qcow2
if [ "$DO_DD" = 1 ]; then
    # Direct-raw boot of the dd target (todo/iso/14 Phase E item 5). No
    # overlay: the published bytes are the disk. Persist is off on this extra.
    OVERLAY="$IMG"
    DISK_FMT=raw
    log "direct-raw disk: $OVERLAY"
elif [ "$GROW_GIB" -gt 0 ]; then
    qemu-img create -f qcow2 -F "$FMT" -b "$IMG" -o "size=${GROW_GIB}G" "$OVERLAY" >/dev/null
    log "overlay: $OVERLAY (backing $IMG, virtual size ${GROW_GIB}G — grow test)"
else
    qemu-img create -F "$FMT" -b "$IMG" -f qcow2 "$OVERLAY" >/dev/null
    log "overlay: $OVERLAY (backing $IMG)"
fi
if [ "$EXTRA_USB" = 1 ]; then
    qemu-img create -f qcow2 "$BUILD_DIR/$VM-extra.qcow2" 1G >/dev/null
    log "second USB disk: $BUILD_DIR/$VM-extra.qcow2 (empty 1G)"
fi

#-- 2. Locate UEFI firmware (qemu:///session needs an absolute path) ----------
# Default: OVMF without Secure Boot (the Phase A–D path). --secure-boot uses
# the openSUSE-enrolled SMM vars so shim on the image can verify.
OVMF=""
OVMF_VARS=""
OVMF_SECURE=""
if [ "$SECUREBOOT" = 1 ]; then
    for c in /usr/share/qemu/ovmf-x86_64-smm-opensuse-code.bin \
             /usr/share/qemu/ovmf-x86_64-smm-opensuse.bin; do
        [ -f "$c" ] && OVMF="$c" && break
    done
    for v in /usr/share/qemu/ovmf-x86_64-smm-opensuse-vars.bin \
             /usr/share/qemu/ovmf-x86_64-smm-opensuse-vars.qcow2; do
        [ -f "$v" ] && OVMF_VARS="$v" && break
    done
    OVMF_SECURE=yes
    [ -n "$OVMF" ] && [ -n "$OVMF_VARS" ] || die "no openSUSE-enrolled OVMF (qemu-ovmf-x86_64 smm-opensuse)"
else
    for c in /usr/share/qemu/ovmf-x86_64-4m.bin \
             /usr/share/qemu/ovmf-x86_64-code.bin \
             /usr/share/qemu/ovmf-x86_64.bin \
             /usr/share/OVMF/OVMF_CODE.fd ; do
        [ -f "$c" ] && OVMF="$c" && break
    done
    for v in /usr/share/qemu/ovmf-x86_64-4m-vars.bin \
             /usr/share/qemu/ovmf-x86_64-vars.bin \
             /usr/share/OVMF/OVMF_VARS.fd; do
        [ -f "$v" ] && OVMF_VARS="$v" && break
    done
    [ -n "$OVMF" ] || die "no OVMF firmware (install qemu-ovmf-x86_64 or OVMF)"
fi
log "uefi firmware: $OVMF vars=${OVMF_VARS:-none} secureboot=$SECUREBOOT"

#-- 3. Domain XML (rootless qemu:///session, SSH port forward 2299->22) ------
NVRAM="$VERIFY_DIR/$VM.nvram.fd"
if [ -n "$OVMF_VARS" ]; then
    cp "$OVMF_VARS" "$NVRAM"
else
    # Seed nvram from the matching template if present so secure-boot vars
    # don't trip kiwi's UEFI bootloader.
    for tpl in /usr/share/qemu/ovmf-x86_64-vars.bin /usr/share/qemu/ovmf-x86_64-4m-vars.bin /usr/share/OVMF/OVMF_VARS.fd; do
        [ -f "$tpl" ] && cp "$tpl" "$NVRAM" && break
    done
fi
LOADER_ATTRS="readonly='yes' type='pflash'"
[ "$SECUREBOOT" = 1 ] && LOADER_ATTRS="$LOADER_ATTRS secure='yes'"
FEATURES="<acpi/><apic/>"
[ "$SECUREBOOT" = 1 ] && FEATURES="$FEATURES<smm state='on'/>"

DISK_BUS="$BUS"
DISK_DEV=vda
[ "$BUS" = usb ] && DISK_DEV=sda
DISK_XML="
    <disk type='file' device='disk'>
      <driver name='qemu' type='$DISK_FMT'/>
      <source file='$OVERLAY'/>
      <target dev='$DISK_DEV' bus='$DISK_BUS'/>
      <boot order='1'/>
    </disk>"
USB_CTRL=""
if [ "$BUS" = usb ]; then
    USB_CTRL="<controller type='usb' index='0' model='qemu-xhci'/>"
    if [ "$USB_HUB" = 1 ]; then
        USB_CTRL="$USB_CTRL
    <hub type='usb'>
      <address type='usb' bus='0' port='1'/>
    </hub>"
        DISK_XML="
    <disk type='file' device='disk'>
      <driver name='qemu' type='$DISK_FMT'/>
      <source file='$OVERLAY'/>
      <target dev='$DISK_DEV' bus='usb'/>
      <boot order='1'/>
      <address type='usb' bus='0' port='1.1'/>
    </disk>"
    fi
fi
EXTRA_DISK_XML=""
if [ "$EXTRA_USB" = 1 ]; then
    EXTRA_DISK_XML="
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$BUILD_DIR/$VM-extra.qcow2'/>
      <target dev='sdb' bus='usb'/>
    </disk>"
fi

DOMAIN_XML=$(cat <<XML
<domain type='kvm'>
  <name>$VM</name>
  <memory unit='MiB'>4096</memory>
  <vcpu>2</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader $LOADER_ATTRS>$OVMF</loader>
    <nvram template='$OVMF_VARS'>$NVRAM</nvram>
  </os>
  <features>$FEATURES</features>
  <cpu mode='host-passthrough'/>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    $USB_CTRL
    $DISK_XML
    $EXTRA_DISK_XML
    <interface type='user'>
      <backend type='passt'/>
      <model type='virtio'/>
      <portForward proto='tcp' address='127.0.0.1'>
        <range start='$SSH_PORT' to='22'/>
      </portForward>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
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
BOOT_T0=$(date +%s)
log "starting $VM (boot t0=$BOOT_T0 bus=$BUS secureboot=$SECUREBOOT grow=${GROW_GIB}G)"
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

#-- 5b. Start sshd via the guest agent (sshd is NOT enabled by default) -------
# The shipped image intentionally does not enable sshd at boot (avoids a
# network-reachable default-credential exposure; see image/config.sh). The
# qemu-guest-agent provides an out-of-band virtio-serial channel we use to
# start sshd on demand for these verification assertions — no network path is
# baked into the image. guest-exec runs as root inside the guest.
qga() {
    virsh -c "$URI" qemu-agent-command "$VM" "$1" 2>/dev/null
}
# qga_root <shell> — run <shell> as ROOT in the guest through the agent
# (guest-exec + guest-exec-status), print its stdout, relay its stderr, and
# return its exit code. This is the verifier's root channel: it works on every
# profile (the release image deletes admin's sudoers rule), so an assertion
# that needs root reads through here, not through `sudo -n` over SSH.
# The status polling has a 60 s deadline (each agent call is bounded by
# libvirt, not by this shell); a timeout or an agent error returns 97/98
# (never 0).
command -v jq >/dev/null 2>&1 || die "jq not installed; install with: sudo zypper in jq"
qga_root() {
    local cmd="$1" out pid st="" deadline
    out=$(qga "$(jq -cn --arg c "$cmd" '{execute:"guest-exec",arguments:{path:"/bin/bash",arg:["-c",$c],"capture-output":true}}')")
    pid=$(printf '%s' "$out" | jq -r '.return.pid // empty' 2>/dev/null)
    [ -n "$pid" ] || { echo "qga_root: guest-exec failed: $out" >&2; return 97; }
    deadline=$(( $(date +%s) + 60 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        st=$(qga "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$pid}}")
        [ "$(printf '%s' "$st" | jq -r '.return.exited' 2>/dev/null)" = true ] && break
        sleep 1
    done
    [ "$(printf '%s' "$st" | jq -r '.return.exited' 2>/dev/null)" = true ] \
        || { echo "qga_root: timed out after 60s: $cmd" >&2; return 98; }
    printf '%s' "$st" | jq -r '.return."out-data" // empty' | base64 -d
    printf '%s' "$st" | jq -r '.return."err-data" // empty' | base64 -d >&2
    return "$(printf '%s' "$st" | jq -r '.return.exitcode // 99')"
}
log "waiting for qemu-guest-agent (max 180s)..."
qga_deadline=$(( $(date +%s) + 180 ))
qga_up=0
while [ "$(date +%s)" -lt "$qga_deadline" ]; do
    if qga '{"execute":"guest-ping"}' | grep -q '"return"'; then qga_up=1; break; fi
    sleep 3
done
[ "$qga_up" = 1 ] || { shoot 99-qga-timeout; die "guest agent never responded; cannot start sshd (see $VERIFY_DIR/screenshots/99-qga-timeout.png)"; }
QGA_T=$(( $(date +%s) - BOOT_T0 ))
log "guest agent up in ${QGA_T}s"
if [ "$POWEROFF" = 1 ]; then
    # Hard power-off during first boot, then a successful second boot
    # (todo/iso/14 Phase E item 4). Destroy is qemu -s, not ACPI.
    log "POWER-OFF: destroying $VM during first boot, then restarting"
    virsh -c "$URI" destroy "$VM"
    sleep 2
    BOOT_T0=$(date +%s)
    virsh -c "$URI" start "$VM"
    qga_deadline=$(( $(date +%s) + 180 ))
    qga_up=0
    while [ "$(date +%s)" -lt "$qga_deadline" ]; do
        if qga '{"execute":"guest-ping"}' | grep -q '"return"'; then qga_up=1; break; fi
        sleep 3
    done
    [ "$qga_up" = 1 ] || { shoot 99-qga-timeout-after-poweroff; die "guest agent never responded after hard power-off"; }
    QGA_T=$(( $(date +%s) - BOOT_T0 ))
    log "guest agent up after power-off in ${QGA_T}s"
fi
log "guest agent up; starting sshd over the agent channel"
exec_out=$(qga '{"execute":"guest-exec","arguments":{"path":"/usr/bin/systemctl","arg":["start","sshd.service"],"capture-output":true}}')
qga_pid=$(printf '%s' "$exec_out" | grep -oE '"pid":[0-9]+' | grep -oE '[0-9]+' | head -1)
[ -n "$qga_pid" ] || { shoot 99-qga-exec; die "guest-exec to start sshd failed: $exec_out"; }
# Poll guest-exec-status until the systemctl call exits (bounded).
st_deadline=$(( $(date +%s) + 60 ))
while [ "$(date +%s)" -lt "$st_deadline" ]; do
    st=$(qga "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$qga_pid}}")
    printf '%s' "$st" | grep -q '"exited":true' && break
    sleep 2
done
ec=$(printf '%s' "$st" | grep -oE '"exitcode":[0-9]+' | grep -oE '[0-9]+' | head -1)
[ "${ec:-0}" = 0 ] || warn "systemctl start sshd returned exitcode=$ec (continuing; SSH wait loop will confirm)"

#-- 6. SSH wrapper + wait for auth to actually succeed -----------------------
# First-boot resize + greetd autologin together take ~2-5 min; we poll
# real SSH auth (not /dev/tcp — passt-forwarded ports accept connections
# even when sshd isn't listening yet) until it returns 0.
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o LogLevel=ERROR"
command -v sshpass >/dev/null 2>&1 || die "sshpass not installed; install with: sudo zypper in sshpass"
remote() {
    sshpass -p "$SSH_PASS" ssh $SSH_OPTS -p "$SSH_PORT" "$SSH_USER@127.0.0.1" "$@"
}

log "waiting for sshd to accept auth (max 600s)..."
deadline=$(( $(date +%s) + 600 ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if remote 'true' 2>/dev/null; then
        ready=1; break
    fi
    sleep 5
done
[ "$ready" = 1 ] || { shoot 99-ssh-timeout; die "SSH never came up; see $VERIFY_DIR/screenshots/99-ssh-timeout.png"; }
shoot 01-ssh-ready

# admin's user systemd manager comes up under linger right after boot and
# reaches default.target, which trails the moment sshd accepts auth by ~5-10s
# and can flap under load. The assertions below query admin's user manager
# (systemctl --user cat/show/is-active for qdwin-session.target, qdlocker, ...),
# so they need that manager up and settled. Instead of a fixed empirical 30s
# sleep, poll `systemctl --user is-system-running` until the manager reports it
# has finished bringing up default.target. Bounded: if it never settles within
# the budget we fall through and let the assertions below report the real
# failure rather than hanging here.
#
# `is-system-running` prints (and exits 0 for) `running`; during bring-up it
# prints `initializing`/`starting`, and on a unit failure it prints `degraded`
# (exit 1). We accept `running` OR `degraded` as "settled" — a degraded manager
# has still finished its startup transaction, and the individual assertions
# below are what should adjudicate any unit failure, not this gate.
# The SYSTEM manager first: sshd is started through the agent within a few
# seconds of boot now (run 28 onward), so the assertions can otherwise run
# while Type=notify units are still activating -- run 29 sampled the admin
# broker four seconds before it finished starting and failed two rows that
# run 28 had passed. Bounded; running or degraded both mean "startup done".
sys_wait_deadline=$(( $(date +%s) + 180 ))
log "waiting for the system manager to finish startup (max 180s)..."
sys_state=""
while [ "$(date +%s)" -lt "$sys_wait_deadline" ]; do
    sys_state="$(remote 'systemctl is-system-running' 2>/dev/null || true)"
    case "$sys_state" in
        running|degraded) log "system manager settled (is-system-running=$sys_state)"; break ;;
    esac
    sleep 3
done
case "$sys_state" in
    running|degraded) ;;
    *) warn "system manager did not settle within 180s (is-system-running='$sys_state'); running assertions anyway" ;;
esac

user_mgr_check() {
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-system-running" 2>/dev/null
}
mgr_wait_deadline=$(( $(date +%s) + 120 ))
log "waiting for admin's user manager to settle (max 120s)..."
mgr_state=""
while [ "$(date +%s)" -lt "$mgr_wait_deadline" ]; do
    mgr_state="$(user_mgr_check || true)"
    case "$mgr_state" in
        running|degraded)
            log "admin user manager settled (is-system-running=$mgr_state)"
            break
            ;;
    esac
    sleep 3
done
case "$mgr_state" in
    running|degraded) ;;
    *) warn "admin user manager did not settle within 120s (is-system-running='$mgr_state'); running assertions anyway" ;;
esac

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

# qdistro admin broker on the system bus. Root channel (qga_root), not
# `sudo -n` over SSH: the release profile deletes admin's sudoers rule.
expect "qdistro-admin-broker active" qga_root 'systemctl is-active qdistro-admin-broker.service'
expect "broker owns dbus name"       qga_root 'busctl list --no-pager | grep -q org.qdistro.AdminBroker1'

# Greeter boot path: greetd execs /usr/bin/qdgreeter (greetd-config.toml),
# which after auth runs qdwin-session-launcher -> `systemctl --user start
# qdwin-session.target`. Assert the exact binary + units that path needs.
expect "qdgreeter binary present" \
    remote 'test -x /usr/bin/qdgreeter'
expect "greetd execs an existing greeter (no exec failure in journal)" \
    qga_root 'journalctl -u greetd -b --no-pager | grep -Eiq "No such file|exec.*qdgreeter.*fail|failed to execute" && exit 1 || exit 0'
expect "qdwin-session.target user unit installed" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user cat qdwin-session.target >/dev/null 2>&1"
expect "qdshell wanted by qdwin-session.target" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p Wants qdwin-session.target | grep -q qdshell.service"
expect "qdlocker wanted by qdwin-session.target" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p Wants qdwin-session.target | grep -q qdlocker.service"

# qdlocker.service ExecStart must point at the binary the image actually
# installed (/usr/bin/qdlocker from the --prefix=/usr pip install). If the
# unit still carries the upstream /usr/local/bin/qdlocker ExecStart, the
# locker 203/EXECs at boot and never starts — wiring it into the session
# Wants= is then a no-op (finding #16, BROKEN remediation). These are GATING
# assertions: the unit must load with a resolvable ExecStart AND not show an
# exec failure in its journal. The Wants= check above alone could not catch
# an ExecStart/binary-path mismatch.
expect "qdlocker.service ExecStart resolves to an installed binary" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p ExecStart qdlocker.service | grep -oE 'path=[^ ;]+' | head -n1 | cut -d= -f2 | xargs test -x"
expect "qdlocker.service is loaded (not error/masked/not-found)" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p LoadState qdlocker.service | grep -qx 'LoadState=loaded'"
expect "qdlocker.service did not 203/EXEC (ExecStart/binary-path match)" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u qdlocker.service -b --no-pager 2>/dev/null | grep -Eiq '203/EXEC|No such file or directory|Failed to locate executable|Failed at step EXEC' && exit 1 || exit 0"
# qdlocker.service is Type=simple, so it should reach `active` promptly. We
# require `active` (NOT merely `activating`) — a unit stuck activating or
# flapping under Restart=always is a real failure, not something to tolerate.
# A short bounded settle absorbs only first-boot bring-up timing; the permanent
# ExecStart-failure case is already caught definitively by the 203/EXEC journal
# check above.
#
# Crash-loop hardening: reaching `active` once is not enough — a unit that
# becomes active briefly and then exits under Restart=always could be sampled
# during an active window. So after first reaching active we settle, then
# require ActiveState=active AND SubState=running AND a low restart count
# (NRestarts<=1) so a flapping locker fails the gate.
#
# qdlocker is WantedBy qdwin-session.target, which only a real greeter
# authentication starts (see the VT-escape note below): on the shipped
# image this verifier never logs in through qdgreeter, so with the greeter
# on tty3 the locker is legitimately inactive/dead with NRestarts=0. The
# row therefore adjudicates by session state: session up -> must hold
# active/running; session exactly inactive -> locker must be loaded,
# inactive/dead, never restarted (a locker that started and died without
# a session IS flapping; a missing unit would otherwise look identical
# to "never started"; a failed/activating session is not the no-login
# state and fails this row). Until run 29 this row demanded `active`
# unconditionally, which no password-gated image can satisfy; it had
# never passed.
expect "qdlocker.service is healthy: holds active/running with a session, inactive and never failed without one" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 sh -c '
        sess=\$(systemctl --user is-active qdwin-session.target)
        if [ \"\$sess\" = active ]; then
            for i in 1 2 3 4 5 6 7 8 9 10; do
                [ \"\$(systemctl --user is-active qdlocker.service)\" = active ] && break
                sleep 1
            done
            sleep 3
        fi
        read load as ss nr <<EOF2
\$(systemctl --user show -p LoadState -p ActiveState -p SubState -p NRestarts --value qdlocker.service | tr \"\\n\" \" \")
EOF2
        echo \"qdwin-session.target=\$sess qdlocker LoadState=\$load ActiveState=\$as SubState=\$ss NRestarts=\$nr\"
        if [ \"\$sess\" = active ]; then
            [ \"\$as\" = active ] && [ \"\$ss\" = running ] && [ \"\${nr:-99}\" -le 1 ]
        elif [ \"\$sess\" = inactive ]; then
            [ \"\$load\" = loaded ] && [ \"\$as\" = inactive ] && [ \"\$ss\" = dead ] && [ \"\${nr:-99}\" -eq 0 ]
        else
            false
        fi'"

# NOT asserted here: the locked-session VT escape
# (tests/integration/vm/probes/vt-escape-lockdown.sh). Two reasons, both
# specific to this verifier, and both worth writing down so nobody adds it
# back without fixing them first:
#
#  1. It needs root. This verifier's `remote` is an SSH login as admin, and
#     a release-profile build deliberately deletes /etc/sudoers.d/99-admin
#     (image/config.sh) — so `sudo -n` cannot work there. Root IS available
#     profile-independently through the guest agent (qga_root above; the
#     Phase D rows use it), and the older `sudo -n` rows in this file should
#     migrate to it rather than be copied. Weakening the shipped image's sudo
#     policy to suit the verifier is not an option.
#  2. It would measure the wrong process. The shipped greetd config has only
#     a `_greeter` default_session and its initial_session is commented out,
#     and image/config.sh removes qdwin-session's default-target enablement
#     so only a real greeter authentication starts it. This verifier logs in
#     over SSH and never authenticates through qdgreeter, so tty3 belongs to
#     qdgreeter, not the post-auth qdwin session whose LOCKED screen the
#     probe exists to protect. A green result here would be green for the
#     wrong thing.
#
# The runtime probe therefore lives in tests/integration/vm/vt-escape-lockdown.bats,
# which converts its own disposable VM (enable-qdgreeter.sh + a test-only
# autologin) and so does reach a real qdwin session on tty3 — and which runs
# in the bats lane that `qci full` actually invokes.

expect "weston (qdwin) on disk" \
    remote 'test -f /usr/lib64/weston/qdwin-shell.so || test -f /usr/lib/weston/qdwin-shell.so'

# One chain (todo/iso/14 Phase D): the image ran the bootstrap's installer
# chain, so the isolation ladder above tier 2 is on the stick. Tier 3 is the
# top of the SUPPORTED ladder (spawn helper + group + polkit action); the
# tier-4 host control script is what spawn-tier4.sh falls back to on an
# installed image (experimental tier, host launch code only).
# The spawn/cleanup helpers are symlinks into /root/qdistro-src (mode 0700:
# admin cannot resolve them, root can; the helper runs as root via polkit).
# So: the link and its target's name from admin's view, the target's
# executability from root's.
expect "tier-3 spawn helper installed (chain step tier3)" \
    remote 'test -L /usr/local/bin/qdistro-tier3-spawn && [ "$(readlink /usr/local/bin/qdistro-tier3-spawn)" = /root/qdistro-src/qdistro/tier3/spawn-tier3.sh ] && getent group qdistro-tier3 >/dev/null && test -f /usr/share/polkit-1/actions/org.qdistro.tier3.policy'
expect "tier-3 spawn/cleanup helper targets executable (root view)" \
    qga_root 'test -x /root/qdistro-src/qdistro/tier3/spawn-tier3.sh && test -x /root/qdistro-src/qdistro/tier3/qdistro-tier3-cleanup.sh && test -x /usr/local/bin/qdistro-tier3-spawn'
# passwd -S needs root; an empty or unexpected status line is a FAIL (the
# case pattern matches the second field exactly, so silence cannot pass).
expect "tier-3 silo users exist with locked passwords" \
    qga_root 'for u in user1 user2; do id -u "$u" >/dev/null || exit 1; s=$(passwd -S "$u") || exit 1; case "$s" in "$u L "*|"$u LK "*) ;; *) echo "not locked: ${s:-<no output>}"; exit 1;; esac; done'
expect "tier-3 runtime dir created at boot by tmpfiles" \
    remote 'test -d /run/qdistro-tier3'
expect "tier-4 host control script installed (chain step tier4-host)" \
    remote 'test -f /usr/share/qdistro/tier4-vm/tier4_control.py && test -f /usr/share/qdistro/tier4-vm/tier4_chrome.py'
expect "sdk (qdistro_app) importable" \
    remote 'python3 -c "import qdistro_app"'
# The DONE bar, on the booted image: the steps recorded as installed equal
# the bootstrap's chain for this image's profile (dev-only steps excluded
# outside dev). chain_expected_names is the bootstrap's own definition, read
# from the on-image source tree -- under /root, hence the root channel.
expect "installer chain record equals the bootstrap chain for this profile" \
    qga_root 'p=$(sed -n "s/^PROFILE=//p" /etc/qdistro/release); [ -n "$p" ] || { echo "no PROFILE in /etc/qdistro/release"; exit 1; };
            exp=$(QDISTRO_PROFILE="$p" bash -c ". /root/qdistro-src/qdistro/scripts/install/qdistro-bootstrap.sh; resolve_profile >/dev/null; chain_expected_names") || { echo "chain_expected_names failed"; exit 1; };
            rec=$(grep -vE "^[[:space:]]*(#|$)" /var/lib/qdistro/bootstrap/installer-chain.state);
            [ -n "$exp" ] && [ "$exp" = "$rec" ] && { echo "chain ($p): $(echo $exp)"; exit 0; };
            echo "expected: $(echo $exp)"; echo "recorded: $(echo $rec)"; exit 1'
expect "no media/multimachine/recall artefacts (not in the chain)" \
    remote 'for f in /etc/systemd/system/qdistro-media-exec.socket /usr/local/bin/qdistro-mm-broker /usr/local/bin/qdistro-recall; do test -e "$f" && exit 1; done; exit 0'
expect "qdshell QML installed"  remote 'test -d /usr/share/quickshell/qdshell'

# Priority 0/1 journal entries. The single benign one we tolerate is the
# kernel's "RDSEED32 is broken" CPUID-quirk notice on qemu hosts.
expect "no unexpected priority=0/1 errors in journal" \
    qga_root 'journalctl -b -p emerg..alert --no-pager -q | grep -v "RDSEED32 is broken" | grep -q . && exit 1 || exit 0'

#-- Phase E: removable identity, grow, first-boot observability --------------
# fstab / GRUB / swap must name UUID (or PARTUUID), never a kernel name:
# a stick that moved from /dev/sda to /dev/sdb (or a second USB disk) would
# otherwise fail to find root. Asserted on the booted image so initrd
# discovery is the same contract (todo/iso/14 Phase E item 3).
expect "fstab uses UUID/PARTUUID, never /dev/sdX or /dev/vdX" \
    qga_root 'grep -E "^[[:space:]]*[^#[:space:]]" /etc/fstab | grep -E "/dev/(sd|vd|nvme|mmcblk)" && exit 1
              grep -qE "^(UUID|PARTUUID)=" /etc/fstab'
expect "grub linux lines use root=UUID= (not a kernel device name)" \
    qga_root 'grep -E "^[[:space:]]*(linux|linuxefi)[/[:space:]]" /boot/grub2/grub.cfg | grep -E "/dev/(sd|vd|nvme)" && exit 1
              grep -E "^[[:space:]]*(linux|linuxefi)[/[:space:]]" /boot/grub2/grub.cfg | grep -q "root=UUID="'
expect "swap is active and fstab names it by UUID" \
    qga_root 'swapon --show=NAME,UUID --noheadings | grep -q .
              grep -E "^UUID=[^ ]+[[:space:]]+swap[[:space:]]" /etc/fstab >/dev/null'
expect "EFI fallback path EFI/BOOT/bootx64.efi is present" \
    qga_root 'test -f /boot/efi/EFI/BOOT/bootx64.efi || test -f /boot/efi/EFI/BOOT/BOOTX64.EFI'

# Grow: 64 GiB overlay of a 28 GiB raw. After first-boot repart the root
# btrfs must be clearly larger than the built size (~25.5 GiB of root).
if [ "$GROW_GIB" -gt 0 ]; then
    expect "root btrfs grew onto the ${GROW_GIB} GiB disk (repart)" \
        qga_root 'sz=$(df -B1 / | awk "NR==2{print \$2}"); echo SIZE=$sz; [ -n "$sz" ] && [ "$sz" -gt 42949672960 ]'
    expect "kiwi oem-repart ran this boot (or recorded a resize)" \
        qga_root 'journalctl -b --no-pager | grep -Eiq "Resize device id|resized root|expanded.*btrfs|dracut-kiwi-oem-repart|kiwi-oem-repart"'
fi
expect "no failed systemd units after first boot" \
    qga_root 'out=$(systemctl --failed --legend=no --plain --no-pager | awk "NF && \$1 != \"UNIT\""); [ -z "$out" ] || { echo "$out"; exit 1; }'
printf 'boot: guest-agent %ss (bus=%s grow=%sG sb=%s)\n' "$QGA_T" "$BUS" "$GROW_GIB" "$SECUREBOOT" \
    | tee -a "$VERIFY_DIR/report.txt" >/dev/null

#-- 9. Capture journals + systemctl state for evidence -----------------------
log "capturing journals + systemd state"
qga_root 'journalctl -b --no-pager'                > "$VERIFY_DIR/journal/full.log"      2>&1 || true
qga_root 'journalctl -b -p err --no-pager'         > "$VERIFY_DIR/journal/errors.log"    2>&1 || true
qga_root 'journalctl -u qdistro-admin-broker --no-pager' > "$VERIFY_DIR/journal/broker.log" 2>&1 || true
qga_root 'journalctl -u greetd --no-pager'         > "$VERIFY_DIR/journal/greetd.log"    2>&1 || true
qga_root 'systemctl --failed --no-pager'           > "$VERIFY_DIR/journal/failed-units.log" 2>&1 || true
qga_root 'cat /var/lib/qdistro/bootstrap/installer-chain.state' > "$VERIFY_DIR/journal/installer-chain.state" 2>&1 || true
qga_root 'cat /etc/qdistro/release'                > "$VERIFY_DIR/journal/release.txt"       2>&1 || true
remote 'sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user --no-pager status qdwin-session.target qdwin-compositor.service qdshell.service qdlocker.service' \
    > "$VERIFY_DIR/journal/user-units.log" 2>&1 || true

shoot 02-fully-booted
sleep 30
shoot 03-after-30s
sleep 30
shoot 04-after-60s

#-- 10. Greeter login (N19) + persistence + nested KVM -----------------------
# qdgreeter forcePasswordFocus() on startup; username is read-only "admin".
# Type the baked password and Enter, then the locker row's session-up
# branch is the one that has been unexercised since the P01 boot path.
if [ "$DO_LOGIN" = 1 ]; then
    log "logging in through qdgreeter (send-key password)"
    sess_before="$(remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdwin-session.target" 2>/dev/null || true)"
    expect "qdwin-session.target is inactive immediately before send-key" \
        [ "${sess_before:-inactive}" = inactive ]
    shoot 05-before-login
    for ch in Q D I S T R O; do
        virsh -c "$URI" send-key "$VM" --codeset linux --holdtime 40 "KEY_$ch" >/dev/null
        sleep 0.08
    done
    virsh -c "$URI" send-key "$VM" --codeset linux --holdtime 40 KEY_ENTER >/dev/null
    login_deadline=$(( $(date +%s) + 90 ))
    sess=""
    while [ "$(date +%s)" -lt "$login_deadline" ]; do
        sess="$(remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdwin-session.target" 2>/dev/null || true)"
        [ "$sess" = active ] && break
        sleep 2
    done
    shoot 06-after-login
    expect "qdgreeter login started qdwin-session.target" \
        [ "$sess" = active ]
    # Re-assert the locker now that a session is up (the crash-loop branch).
    expect "qdlocker.service holds active/running after greeter login (NRestarts<=1)" \
        remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 sh -c '
            sess=\$(systemctl --user is-active qdwin-session.target)
            read load as ss nr <<EOF2
\$(systemctl --user show -p LoadState -p ActiveState -p SubState -p NRestarts --value qdlocker.service | tr \"\\n\" \" \")
EOF2
            echo \"qdwin-session.target=\$sess qdlocker LoadState=\$load ActiveState=\$as SubState=\$ss NRestarts=\$nr\"
            [ \"\$sess\" = active ] && [ \"\$as\" = active ] && [ \"\$ss\" = running ] && [ \"\${nr:-99}\" -le 1 ]'"
fi

if [ "$NESTED" = 1 ]; then
    expect "nested KVM: /dev/kvm is usable in the guest" \
        qga_root 'test -c /dev/kvm && test -w /dev/kvm'
    expect "nested KVM: qemu -accel kvm starts" \
        qga_root 'rm -f /tmp/qdistro-nested-kvm.pid
                  qemu-system-x86_64 -accel kvm -machine q35 -cpu host -m 32 -nographic -display none -serial none -monitor none -nodefaults -S -daemonize -pidfile /tmp/qdistro-nested-kvm.pid
                  rc=$?
                  if [ -f /tmp/qdistro-nested-kvm.pid ]; then kill "$(cat /tmp/qdistro-nested-kvm.pid)" 2>/dev/null || true; rm -f /tmp/qdistro-nested-kvm.pid; fi
                  [ $rc -eq 0 ]'
    # Opt-in spawn: stub disk, define-only. spawn-tier4.sh talks to
    # qemu:///session as admin (not qemu:///system). The tester image
    # does not ship the guest base (--tier4-base); this proves the host
    # spawn path can define a nested domain.
    expect "tier-4 opt-in spawn defines a nested domain (stub disk, define-only)" \
        qga_root 'set -e
                  qemu-img create -f qcow2 /tmp/qdistro-t4-stub.qcow2 64M >/dev/null
                  # spawn-tier4.sh uses qemu:///session as admin; linger is on
                  # but virtqemud is socket-activated and may not be up yet.
                  as_admin() { runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/admin LIBVIRT_DEFAULT_URI=qemu:///session "$@"; }
                  as_admin systemctl --user start virtqemud.socket virtqemud.service 2>/dev/null || true
                  for i in 1 2 3 4 5 6 7 8 9 10; do
                    as_admin virsh list >/dev/null 2>&1 && break
                    sleep 1
                  done
                  export TIER4_GUEST_DISK=/tmp/qdistro-t4-stub.qcow2
                  export TIER4_DOMAIN_DEFINE_ONLY=1
                  bash /root/qdistro-src/qdistro/tier4-vm/spawn-tier4.sh qdistro-verify-t4
                  as_admin virsh undefine qdistro-verify-t4 2>/dev/null || true'
fi

if [ "$DO_PERSIST" = 1 ]; then
    log "writing persistence marker + btrfs snapshot, then rebooting"
    expect "wrote persist marker and btrfs snapshot" \
        qga_root 'mkdir -p /var/lib/qdistro
                  echo persist-ok > /var/lib/qdistro/verify-persist-marker
                  test -s /var/lib/qdistro/verify-persist-marker
                  btrfs subvolume snapshot / /verify-persist-snap
                  btrfs subvolume list / | grep -q verify-persist-snap'
    shoot 07-before-reboot
    virsh -c "$URI" reboot "$VM"
    # Wait for the agent to drop, then come back (a fast poll can race
    # and see the pre-reboot agent). If ping never fails, persist checks
    # would pass on the same boot (iso/14 Phase E independent B3).
    sleep 8
    drop_deadline=$(( $(date +%s) + 60 ))
    dropped=0
    while [ "$(date +%s)" -lt "$drop_deadline" ]; do
        if qga '{"execute":"guest-ping"}' | grep -q '"return"'; then
            sleep 2
            continue
        fi
        dropped=1
        break
    done
    [ "$dropped" = 1 ] || { shoot 99-reboot-never-dropped; die "guest agent never dropped after virsh reboot; persist checks would be same-boot"; }
    qga_deadline=$(( $(date +%s) + 180 ))
    qga_up=0
    while [ "$(date +%s)" -lt "$qga_deadline" ]; do
        if qga '{"execute":"guest-ping"}' | grep -q '"return"'; then qga_up=1; break; fi
        sleep 3
    done
    [ "$qga_up" = 1 ] || { shoot 99-qga-timeout-reboot; die "guest agent never came back after reboot"; }
    qga '{"execute":"guest-exec","arguments":{"path":"/usr/bin/systemctl","arg":["start","sshd.service"],"capture-output":true}}' >/dev/null || true
    ssh_deadline=$(( $(date +%s) + 180 ))
    while [ "$(date +%s)" -lt "$ssh_deadline" ]; do
        remote 'true' 2>/dev/null && break
        sleep 4
    done
    expect "persist marker survived reboot" \
        qga_root 'grep -qx persist-ok /var/lib/qdistro/verify-persist-marker'
    expect "btrfs snapshot survived reboot" \
        qga_root 'btrfs subvolume list / | grep -q verify-persist-snap && test -d /verify-persist-snap'
    expect "swap still active after reboot" \
        qga_root 'swapon --show --noheadings | grep -q .'
    shoot 08-after-reboot
fi

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

if [ "$KEEP" = 1 ] && [ "$STICK" = 1 ]; then
    die "refusing --stick --keep: extras would be skipped and the matrix would still exit 0"
fi
if [ "$KEEP" = 1 ]; then
    log "VM left running (--keep). To tear down: $0 --teardown"
else
    teardown
fi

# --stick: extra boots of the SAME published bytes (USB / SB / nested /
# power-off / dd). Each re-exec uses a unique VM name and skips login +
# persist (those are proven on the default virtio boot). Nested and
# power-off are their own boots so a failure is attributed.
if [ "$STICK" = 1 ] && [ -z "${QDISTRO_VERIFY_PARENT:-}" ] && [ "$KEEP" != 1 ]; then
    log "stick matrix: extra boots of $IMG"
    export QDISTRO_VERIFY_PARENT=1
    export QDISTRO_IMAGE="$IMG"
    export QDISTRO_BUILD_DIR="$BUILD_DIR"
    extra_fail=0
    extra_n=0
    run_extra() {
        local name="$1"; shift
        extra_n=$((extra_n + 1))
        log "stick extra: $name $* (port=$((SSH_PORT + extra_n)))"
        if QDISTRO_VERIFY_VM="${VM}-${name}" \
           QDISTRO_VERIFY_PORT=$((SSH_PORT + extra_n)) \
           QDISTRO_VERIFY_LOGIN=0 QDISTRO_VERIFY_PERSIST=0 \
           bash "$HERE/verify.sh" "$@"; then
            echo "PASS: stick extra $name" | tee -a "$VERIFY_DIR/report.txt"
        else
            echo "FAIL: stick extra $name" | tee -a "$VERIFY_DIR/report.txt"
            extra_fail=$((extra_fail + 1))
        fi
    }
    run_extra usb --usb
    run_extra usb2 --second-usb
    run_extra hub --usb-hub
    run_extra sb --secure-boot --no-grow
    run_extra nested --nested --no-grow
    run_extra poweroff --power-off
    if [ -n "${QDISTRO_RESOLVED_XZ:-}" ]; then
        QDISTRO_IMAGE="$QDISTRO_RESOLVED_XZ" run_extra dd --dd --no-grow
    else
        echo "FAIL: stick extra dd (parent did not resolve a published xz; will not rediscover)" | tee -a "$VERIFY_DIR/report.txt"
        extra_fail=$((extra_fail + 1))
    fi
    [ "$extra_fail" -eq 0 ] || FAIL=$((FAIL + extra_fail))
    TOTAL=$((PASS + FAIL))
    echo "stick extras: $extra_fail failed" | tee -a "$VERIFY_DIR/report.txt"
fi

[ "$FAIL" -eq 0 ]
