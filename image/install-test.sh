#!/bin/bash
# install-test.sh — boot the qdistro install.iso in a libvirt VM against
# a blank target disk, watch the kiwi installer dump the embedded
# image, then reboot from the installed disk and confirm it comes up.
#
# Screenshots and a state-change log land in logs/install-<ts>/.
#
# Usage:
#   ./install-test.sh                  # full lifecycle
#   ./install-test.sh --teardown       # destroy + undefine, wipe target

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${QDISTRO_BUILD_DIR:-/tmp/qdistro-build}"
ISO="$BUILD_DIR/qdistro.x86_64-0.1.0.install.iso"
URI="qemu:///session"
export LIBVIRT_DEFAULT_URI="$URI"

STAMP="$(date +%y%m%d-%H%M)"
# AGENTS.md: VM names must end YYMMDD-HHMM so parallel runs don't collide.
VM="${QDISTRO_INSTALL_VM:-qdistro-install-${STAMP}}"
TARGET="$BUILD_DIR/$VM-target.qcow2"
TARGET_SIZE_GB=30
SSH_PORT="${QDISTRO_INSTALL_PORT:-2300}"
SSH_PASS="${QDISTRO_IMAGE_PASSWORD:-qdistro}"
INSTALL_DIR="$HERE/logs/install-${STAMP}$(date +%S)"
mkdir -p "$INSTALL_DIR/screenshots"

log()  { printf '\033[1;36m[install-test]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[install-test] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

teardown() {
    log "tearing down $VM"
    virsh destroy  "$VM" 2>/dev/null || true
    virsh undefine "$VM" --nvram 2>/dev/null || true
    rm -f "$TARGET"
}

[ "${1:-}" = "--teardown" ] && { teardown; exit 0; }

[ -f "$ISO" ] || die "install ISO not found: $ISO"
log "install ISO: $ISO ($(du -h "$ISO" | cut -f1))"

#-- UEFI firmware -------------------------------------------------------------
for c in /usr/share/qemu/ovmf-x86_64-4m.bin /usr/share/qemu/ovmf-x86_64.bin /usr/share/OVMF/OVMF_CODE.fd; do
    [ -f "$c" ] && OVMF="$c" && break
done
[ -n "${OVMF:-}" ] || die "no OVMF firmware"
log "uefi firmware: $OVMF"

#-- Fresh blank target disk + nvram ------------------------------------------
teardown
qemu-img create -f qcow2 "$TARGET" "${TARGET_SIZE_GB}G" >/dev/null
log "target disk: $TARGET ($TARGET_SIZE_GB GB blank qcow2)"
NVRAM="$INSTALL_DIR/$VM.nvram.fd"
for tpl in /usr/share/qemu/ovmf-x86_64-4m-vars.bin /usr/share/qemu/ovmf-x86_64-vars.bin /usr/share/OVMF/OVMF_VARS.fd; do
    [ -f "$tpl" ] && cp "$tpl" "$NVRAM" && break
done
[ -f "$NVRAM" ] || die "no OVMF nvram template (ovmf-x86_64-4m-vars.bin / OVMF_VARS.fd)"

#-- Define VM: CD-first, disk attached, passt+SSH so we can verify later -----
DOMAIN_XML=$(cat <<XML
<domain type='kvm'>
  <name>$VM</name>
  <memory unit='MiB'>4096</memory>
  <vcpu>2</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' type='pflash'>$OVMF</loader>
    <nvram template='$OVMF'>$NVRAM</nvram>
    <bootmenu enable='no'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='$ISO'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
      <boot order='1'/>
    </disk>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='$TARGET'/>
      <target dev='vda' bus='virtio'/>
      <boot order='2'/>
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
  </devices>
</domain>
XML
)

log "defining $VM"
echo "$DOMAIN_XML" | virsh define /dev/stdin
log "starting $VM (boot order: CD → HD)"
virsh start "$VM"

shoot() {
    local label="$1"
    local out="$INSTALL_DIR/screenshots/$label.png"
    local tmp
    tmp=$(mktemp --suffix=.ppm)
    if virsh screenshot "$VM" "$tmp" >/dev/null 2>&1; then
        if command -v convert >/dev/null 2>&1; then
            convert "$tmp" "$out" 2>/dev/null && log "screenshot: $(basename "$out")"
        elif command -v ffmpeg >/dev/null 2>&1; then
            ffmpeg -y -loglevel error -i "$tmp" "$out" && log "screenshot: $(basename "$out")"
        fi
    fi
    rm -f "$tmp"
}

# Drive the OVMF boot picker (Enter to select UEFI DVD-ROM) then the
# kiwi-built GRUB menu (Down + Enter to select "Install qdistro" within
# its 1-second timeout). This is the racy bit — see AGENTS.md.
drive_install_menu() {
    log "driving UEFI + GRUB menus via send-key"
    sleep 5                                          # wait for OVMF picker
    virsh send-key "$VM" KEY_ENTER >/dev/null
    sleep 1                                          # wait for GRUB to render
    virsh send-key "$VM" KEY_DOWN  >/dev/null
    virsh send-key "$VM" KEY_ENTER >/dev/null        # picks "Install qdistro"
    sleep 8                                          # wait for kiwi-oem-dump confirm
    virsh send-key "$VM" KEY_ENTER >/dev/null        # confirm "destroy /dev/vda? Yes"
}
drive_install_menu &
KEYS_PID=$!

#-- Watch the installer ------------------------------------------------------
# kiwi-oem-dump's flow:
#   1. UEFI hands off to GRUB on the ISO.
#   2. GRUB loads kernel + dracut initrd from the ISO.
#   3. The kiwi-oem-dump dracut module unpacks the embedded squashfs
#      onto /dev/vda (first detected disk), runs kiwi-oem-repart to
#      grow the filesystem, then powers off (or reboots).
#   4. On reboot the BIOS/UEFI tries CD then HD; CD is still attached
#      so it'd loop. We have to detach the CD before that reboot.
#
# Strategy: poll the VM domstate. When the installer powers off,
# detach the CD and restart from the disk.

log "polling install progress (screenshots every 15s, up to 30 min)..."
prev_state=""
for i in $(seq 1 120); do
    state=$(virsh domstate "$VM" 2>/dev/null || echo unknown)
    if [ "$state" != "$prev_state" ]; then
        log "state[$i]: $state"
        echo "$(date -Is) $state" >> "$INSTALL_DIR/state.log"
        prev_state="$state"
    fi
    shoot "install-$(printf '%03d' "$i")"
    case "$state" in
        "shut off"|"crashed")
            log "VM powered off after ${i}x15s — installer likely finished"
            break
            ;;
    esac
    sleep 15
done

if [ "$(virsh domstate "$VM")" != "shut off" ]; then
    log "installer never powered off within 30 min; forcing destroy"
    virsh destroy "$VM" 2>/dev/null || true
    sleep 2
fi

#-- Did kiwi actually write to the disk? -------------------------------------
log "post-install: inspecting target disk"
qemu-img info "$TARGET" | tee -a "$INSTALL_DIR/state.log" | head -15
# Has a partition table now?
virt-filesystems -a "$TARGET" 2>&1 | tee -a "$INSTALL_DIR/state.log" | head -10 \
    || log "WARN: virt-filesystems on target failed (image may be unwritten)"

#-- Detach the CD and reboot from the installed disk -------------------------
# Per-disk <boot order='2'/> on vda already biases UEFI to the HD; we just
# need to remove the CD so it isn't a candidate.
log "detaching install ISO and rebooting from disk"
virsh detach-disk "$VM" sda --config >/dev/null || true
virsh start "$VM"

log "second boot: from installed disk; polling for real SSH auth"
deadline=$(( $(date +%s) + 600 ))
ssh_up=0
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o LogLevel=ERROR"
while [ "$(date +%s)" -lt "$deadline" ]; do
    if sshpass -p "$SSH_PASS" ssh $SSH_OPTS -p "$SSH_PORT" admin@127.0.0.1 true 2>/dev/null; then
        ssh_up=1
        break
    fi
    shoot "boot-$(date +%H%M%S)"
    sleep 10
done

shoot "final"

if [ "$ssh_up" = 1 ]; then
    log "SSH up on installed system; capturing identity"
    sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o PreferredAuthentications=password -o PubkeyAuthentication=no \
        -o ConnectTimeout=10 -p "$SSH_PORT" admin@127.0.0.1 \
        'echo "hostname=$(cat /etc/hostname)"; echo "id=$(grep ^ID= /etc/os-release)"; lsblk; df -h /' \
        2>&1 | tee "$INSTALL_DIR/installed-fingerprint.log"
    echo "RESULT: PASS — installer wrote the system to disk and it booted."
else
    echo "RESULT: FAIL — installed system never reached SSH; see $INSTALL_DIR/"
fi

log "artifacts: $INSTALL_DIR"
log "VM left running. Teardown: $0 --teardown"
