#!/bin/bash
# create-template-domain.sh — define a minimal libvirt domain whose XML
# clone-baseweed.sh copies for fresh test VMs.
#
# clone-baseweed.sh expects an existing libvirt domain to use as a
# template: it dumpxml's the template, swaps name + disk path + MAC,
# and defines the clone. The template never needs to run; it just
# needs to be defined. This script creates that template on a fresh
# libvirt session.
#
# Usage:
#   scripts/vm/create-template-domain.sh [<name>]
#
# Default name is "qdistro-template".
# Idempotent: if the domain already exists, exits 0 without changes.

set -euo pipefail

NAME="${1:-qdistro-template}"
IMG_DIR="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"

if virsh -c qemu:///session dominfo "$NAME" >/dev/null 2>&1; then
    echo "template domain '$NAME' already exists" >&2
    exit 0
fi

mkdir -p "$IMG_DIR"

TMPXML=$(mktemp --suffix=.xml)
trap 'rm -f "$TMPXML"' EXIT

cat > "$TMPXML" <<EOF
<domain type='kvm'>
  <name>$NAME</name>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>4194304</currentMemory>
  <vcpu placement='static'>2</vcpu>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough' check='none' migratable='off'/>
  <clock offset='utc'/>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$IMG_DIR/$NAME.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='user'>
      <mac address='52:54:00:de:ad:01'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <input type='tablet' bus='usb'/>
    <input type='keyboard' bus='usb'/>
    <video><model type='virtio' heads='1' primary='yes'/></video>
    <memballoon model='virtio'/>
  </devices>
</domain>
EOF

virsh -c qemu:///session define "$TMPXML"
echo "defined template domain '$NAME' (disk path placeholder: $IMG_DIR/$NAME.qcow2 — never opened, only used for clone XML substitution)"
