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

# Video model. Default is a pure virtio-gpu-pci device (no VGA-compat
# shim) so the guest's generic framebuffer driver (vesadrm/simpledrm)
# has no firmware framebuffer to grab and `virtio_gpu` binds the PCI
# device directly — letting wlroots/wlr-randr drive modes >640×480.
# See the QDISTRO_TEMPLATE_LEGACY_VGA escape hatch below.
#
# How the model is wired:
#   - libvirt's <video><model type='virtio'/></video> maps to qemu's
#     `virtio-vga` (WITH the VGA-compat shim). That shim exposes a
#     legacy VGA framebuffer the guest firmware programs to 640×480,
#     and the kernel's vesa/simple framebuffer platform driver claims
#     it before virtio_gpu can bind — wlroots then only ever sees the
#     640×480 vesadrm mode and `wlr-randr --custom-mode` fails.
#   - To get virtio-gpu-pci (no VGA shim) through libvirt we set the
#     video model to 'none' (suppresses libvirt's default VGA device)
#     and add the device via a <qemu:commandline> override. This is
#     the only way libvirt exposes the shim-free PCI GPU.
#
# Set QDISTRO_TEMPLATE_LEGACY_VGA=1 to fall back to the old virtio-vga
# device (for hosts whose qemu lacks virtio-gpu-pci, or to reproduce
# the historical 640×480 behaviour).
LEGACY_VGA="${QDISTRO_TEMPLATE_LEGACY_VGA:-0}"

if virsh -c qemu:///session dominfo "$NAME" >/dev/null 2>&1; then
    echo "template domain '$NAME' already exists" >&2
    exit 0
fi

mkdir -p "$IMG_DIR"

TMPXML=$(mktemp --suffix=.xml)
chmod 0600 "$TMPXML"
trap 'rm -f "$TMPXML"' EXIT

# Build the video stanza + (for the shim-free path) the qemu cmdline
# override. clone-baseweed.sh copies whatever is here verbatim into
# each test VM, so the GPU wiring is centralised in this template.
if [ "$LEGACY_VGA" = 1 ]; then
    VIDEO_XML="    <video><model type='virtio' heads='1' primary='yes'/></video>"
    QEMU_CMDLINE_XML=""
    DOMAIN_NS=""
else
    # model='none' suppresses libvirt's default VGA adapter; the GPU is
    # added below via the qemu:commandline override as a bare
    # virtio-gpu-pci (no VGA-compat shim). max_outputs=1 mirrors the
    # single-head template; the guest sees one connector.
    VIDEO_XML="    <video><model type='none'/></video>"
    QEMU_CMDLINE_XML="  <qemu:commandline>
    <qemu:arg value='-device'/>
    <qemu:arg value='virtio-gpu-pci,max_outputs=1'/>
  </qemu:commandline>"
    DOMAIN_NS=" xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'"
fi

cat > "$TMPXML" <<EOF
<domain type='kvm'$DOMAIN_NS>
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
    <channel type='spicevmc'>
      <target type='virtio' name='com.redhat.spice.0'/>
    </channel>
    <input type='tablet' bus='usb'/>
    <input type='keyboard' bus='usb'/>
    <graphics type='spice' autoport='yes'>
      <listen type='address'/>
      <image compression='off'/>
    </graphics>
$VIDEO_XML
    <memballoon model='virtio'/>
  </devices>
$QEMU_CMDLINE_XML
</domain>
EOF

virsh -c qemu:///session define "$TMPXML"
echo "defined template domain '$NAME' (disk path placeholder: $IMG_DIR/$NAME.qcow2 — never opened, only used for clone XML substitution)"
