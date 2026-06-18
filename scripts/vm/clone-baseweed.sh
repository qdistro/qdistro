#!/bin/bash
# clone-baseweed.sh — create a fresh qdwin VM from baseweed, with optional
# virtio-gpu accel3d for dmabuf-capable outer compositor (§6.8 dmabuf path).
#
# Usage:
#   ./clone-baseweed.sh <name-prefix>                       # default: pixman baseline
#   ./clone-baseweed.sh <name-prefix> --gpu                 # virtio-gpu accel3d for §6.8 dmabuf tests
#   ./clone-baseweed.sh <name-prefix> --from-baked          # back from baseweed-baked.qcow2 (skips zypper install-deps)
#   ./clone-baseweed.sh <name-prefix> --from-enforcing-baked # baseweed-enforcing-baked: SELinux=enforcing config + SSH-bootstrapped
#
# Outputs the new VM name to stdout. With --from-enforcing-baked, the
# assigned host SSH port is printed to stdout on a second line as
# "ssh_port=NNNN" so the caller can drive the VM via:
#     ssh -i ~/.ssh/qdistro_enforcing_id_ed25519 \
#         -p NNNN -o StrictHostKeyChecking=no root@127.0.0.1
# Flags can appear in any order; --from-baked and --from-enforcing-baked
# are mutually exclusive.
#
# Host prereq for accel3d=yes:
#   - /dev/dri/renderD128 with the invoking user in the 'render' group
#   - libvirglrenderer1 + Mesa-libEGL1 + Mesa-dri installed on host
#   - qemu has virtio-gpu-gl-pci device (qemu -device help | grep virtio-gpu-gl)
#
# --from-baked points the new disk's qcow2 backing chain at
# baseweed-baked.qcow2 instead of baseweed.qcow2; the new VM still
# needs fresh-vm-bootstrap.sh to build qdwin/qdshell from source, but
# zypper install-deps becomes a no-op (idempotent: already installed).
# Build the baked image first via build-baked-baseweed.sh.
#
set -euo pipefail

# Force session URI so we do not silently query system libvirtd.
export LIBVIRT_DEFAULT_URI="${LIBVIRT_DEFAULT_URI:-qemu:///session}"

PREFIX=""
GPU=0
FROM_BAKED=0
FROM_ENFORCING=0
FROM_GOLDEN=""
EXTRA_NIC_XML=""
for arg in "$@"; do
    case "$arg" in
        --gpu)                   GPU=1 ;;
        --from-baked)            FROM_BAKED=1 ;;
        --from-enforcing-baked)  FROM_ENFORCING=1 ;;
        # Per-run golden backing: an already-built qcow2 (compositor built once
        # per run) used as the backing for this clone, skipping fresh-vm-bootstrap.
        --from-run-golden=*)     FROM_GOLDEN="${arg#*=}" ;;
        # Multi-machine lane ONLY (clone-mmnet.sh): splice one extra
        # <interface> block (read from this file) into the cloned domain BEFORE
        # define, so the clone gets a second NIC on the isolated inter-VM segment
        # in addition to the template's user-mode NIC. The default
        # single-machine lane NEVER passes this, so its clones are byte-for-byte
        # the template's single-NIC definition. The XML is spliced verbatim
        # right before </devices>; the caller owns its validity (the mmnet lane
        # generates it from scripts/vm/mmnet-config.sh and validates by define).
        --extra-nic-xml=*)       EXTRA_NIC_XML="${arg#*=}" ;;
        --*)
            echo "ERROR: unknown flag '$arg'" >&2; exit 2 ;;
        *)
            if [ -z "$PREFIX" ]; then
                PREFIX="$arg"
            else
                echo "ERROR: extra positional arg '$arg'" >&2; exit 2
            fi ;;
    esac
done
if [ -z "$PREFIX" ]; then
    echo "usage: $0 <name-prefix> [--gpu] [--from-baked|--from-enforcing-baked]" >&2
    exit 2
fi
if [ "$FROM_BAKED" = 1 ] && [ "$FROM_ENFORCING" = 1 ]; then
    echo "ERROR: --from-baked and --from-enforcing-baked are mutually exclusive" >&2
    exit 2
fi
if [ -n "$FROM_GOLDEN" ] && { [ "$FROM_BAKED" = 1 ] || [ "$FROM_ENFORCING" = 1 ]; }; then
    echo "ERROR: --from-run-golden is mutually exclusive with --from-baked/--from-enforcing-baked" >&2
    exit 2
fi
if [ -n "$FROM_GOLDEN" ]; then
    case "$FROM_GOLDEN" in
        /*) : ;;
        *) echo "ERROR: --from-run-golden requires an absolute path (got '$FROM_GOLDEN')" >&2; exit 2 ;;
    esac
fi
if [ -n "$EXTRA_NIC_XML" ]; then
    if [ ! -f "$EXTRA_NIC_XML" ]; then
        echo "ERROR: --extra-nic-xml file not found: $EXTRA_NIC_XML" >&2; exit 2
    fi
    if ! grep -q '<interface' "$EXTRA_NIC_XML"; then
        echo "ERROR: --extra-nic-xml file has no <interface> block: $EXTRA_NIC_XML" >&2; exit 2
    fi
fi

# Name must be unique even when many VMs are cloned in the same minute
# (parallel qci). Seconds + this process's PID + $RANDOM make a same-second
# collision effectively impossible; PID is unique across concurrent clones.
VM="${PREFIX}-$(date +%y%m%d-%H%M%S)-$$-$RANDOM"
TEMPLATE="${QDWIN_VM_TEMPLATE:-qdistro-template}"
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"

if [ -n "$FROM_GOLDEN" ]; then
    BACKING="$FROM_GOLDEN"
    BACKING_NAME=run-golden
elif [ "$FROM_ENFORCING" = 1 ]; then
    BACKING="$IMG/baseweed-enforcing-baked.qcow2"
    BACKING_NAME=baseweed-enforcing-baked
elif [ "$FROM_BAKED" = 1 ]; then
    BACKING="$IMG/baseweed-baked.qcow2"
    BACKING_NAME=baseweed-baked
else
    BACKING="$IMG/baseweed.qcow2"
    BACKING_NAME=baseweed
fi

if ! virsh -c qemu:///session dominfo "$TEMPLATE" >/dev/null 2>&1; then
    echo "ERROR: template VM '$TEMPLATE' not found." >&2
    echo "       This script clones an existing libvirt domain's XML" >&2
    echo "       (NIC/video/serial config) onto the new disk. Set" >&2
    echo "       QDWIN_VM_TEMPLATE=<existing-vm-name> to one of:" >&2
    virsh -c qemu:///session list --all --name 2>/dev/null | sed 's/^/         /' >&2
    exit 1
fi
if [ ! -f "$BACKING" ]; then
    if [ "$FROM_ENFORCING" = 1 ]; then
        echo "ERROR: $BACKING not found — build it first via build-enforcing-baseweed.sh" >&2
    elif [ "$FROM_BAKED" = 1 ]; then
        echo "ERROR: $BACKING not found — build it first via build-baked-baseweed.sh" >&2
    else
        echo "ERROR: $BACKING not found" >&2
    fi
    exit 1
fi
if [ -f "$IMG/${VM}.qcow2" ] || virsh -c qemu:///session dominfo "$VM" >/dev/null 2>&1; then
    echo "ERROR: VM '$VM' already exists" >&2
    exit 1
fi

# Self-cleanup trap for a half-provisioned clone. Everything below this point
# creates resources ($VM's overlay, then the defined+started domain) that the
# CALLER can't reclaim on failure — $VM is only echoed on success, so an INT/TERM
# or mid-build error would otherwise leak a defined domain + overlay until the
# next `qci cleanup`. The trap is installed AFTER the "already exists" guard so
# it can only ever touch the VM THIS invocation creates, and is gated by
# CLONE_OK (set to 1 right before the final name echo, and before the deliberate
# --from-enforcing-baked post-mortem exit so that path keeps the VM for triage).
CLONE_OK=0
_clone_cleanup() {
    trap - EXIT INT TERM          # disarm so INT->exit doesn't re-run via EXIT
    [ "$CLONE_OK" = 1 ] && return
    virsh -c qemu:///session destroy "$VM" >/dev/null 2>&1 || true
    virsh -c qemu:///session undefine "$VM" --nvram >/dev/null 2>&1 \
        || virsh -c qemu:///session undefine "$VM" >/dev/null 2>&1 || true
    rm -f "$IMG/${VM}.qcow2" 2>/dev/null || true
}
trap '_rc=$?; _clone_cleanup; exit $_rc' EXIT
trap '_clone_cleanup; exit 130' INT
trap '_clone_cleanup; exit 143' TERM

# 1. qcow2 overlay backed by $BACKING_NAME (baseweed or baseweed-baked or baseweed-enforcing-baked)
qemu-img create -F qcow2 -b "$BACKING" -f qcow2 "$IMG/${VM}.qcow2" \
    >/dev/null

# 2. SELinux mode in /etc/selinux/config:
#    - default chains (baseweed / baseweed-baked): pin permissive (qga
#      breaks under enforcing on first boot for these images).
#    - --from-enforcing-baked: leave SELINUX=enforcing intact; the
#      build-enforcing-baseweed pipeline already wrote it that way and
#      the VM is reached via SSH (qga is denied under enforcing).
if [ "$FROM_ENFORCING" = 1 ]; then
    : # no-op: enforcing config is already baked in
elif [ -n "$FROM_GOLDEN" ]; then
    : # no-op: the run-golden was built from baseweed-baked, which is already
      # permissive — skip the per-clone libguestfs launch (a hot-path cost once
      # the compile is removed).
else
    virt-customize --no-network -a "$IMG/${VM}.qcow2" \
        --edit '/etc/selinux/config:s/SELINUX=enforcing/SELINUX=permissive/' \
        >/dev/null
fi

# 3. Define from template, swapping name + disk path + MAC + GPU bits
NEW_MAC="52:54:00:$(printf '%02x:%02x:%02x' \
    $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"

# vCPU count for the clone. Default 4 — CPU is safe to overprovision across
# parallel VMs (RAM is the binding constraint), so we give each VM more cores
# than the 2-vcpu template without reducing how many run concurrently.
VCPUS="${QDWIN_VM_VCPUS:-4}"

XML=$(virsh -c qemu:///session dumpxml "$TEMPLATE" --inactive)
XML=$(printf '%s' "$XML" \
    | sed -e "s|<name>$TEMPLATE</name>|<name>$VM</name>|" \
          -e '/<uuid>/d' \
          -e "/<mac address=/c\\      <mac address='$NEW_MAC'/>" \
          -e "s|<vcpu[^>]*>[0-9]\{1,\}</vcpu>|<vcpu placement='static'>${VCPUS}</vcpu>|" \
          -e "s|$TEMPLATE.qcow2|${VM}.qcow2|g")

# Strip any pre-existing <backingStore> blocks from the template's
# disk XML — libvirt regenerates them from the qcow2's actual
# `backing file` metadata at start time, and an inherited
# template-era backingStore that points at baseweed.qcow2 will
# (under qemu's seccomp sandbox) cause the runtime qemu to fail to
# open the new chain (val → baseweed-baked → baseweed) and the
# guest kernel panics on root mount. Removing the element forces
# libvirt to walk qemu-img info and authorize the right paths.
XML=$(printf '%s' "$XML" \
    | python3 -c '
import sys, re
src = sys.stdin.read()
# Strip every <backingStore .../> (self-closing) AND every
# <backingStore ...>...</backingStore> open/close pair, including
# nested children. Walk character-by-character to handle the
# nesting libvirt emits (an outer block whose inner end-of-chain
# marker is itself a <backingStore/>).
out = []
i = 0
while i < len(src):
    if src.startswith("<backingStore", i):
        end_tag = src.find(">", i)
        if end_tag == -1:
            out.append(src[i:]); break
        # Self-closing <backingStore .../>
        if src[end_tag - 1] == "/":
            i = end_tag + 1
            continue
        # Find matching </backingStore> with depth tracking.
        depth = 1
        j = end_tag + 1
        while j < len(src) and depth > 0:
            o = src.find("<backingStore", j)
            c = src.find("</backingStore>", j)
            if c == -1:
                break
            if o != -1 and o < c:
                # nested open. Skip past its tag.
                inner_end = src.find(">", o)
                if inner_end == -1:
                    break
                if src[inner_end - 1] == "/":
                    # self-closing nested — no depth change
                    j = inner_end + 1
                else:
                    depth += 1
                    j = inner_end + 1
            else:
                depth -= 1
                j = c + len("</backingStore>")
        # j now sits past the outer </backingStore>.
        # Also drop any preceding whitespace + newline from the line.
        while out and out[-1] in (" ", "\t"):
            out.pop()
        if out and out[-1] == "\n":
            out.pop()
        i = j
        continue
    out.append(src[i])
    i += 1
sys.stdout.write("".join(out))
')

if [ "$GPU" = 1 ]; then
    # virtio-gpu accel3d=yes. Outer weston picks up
    # gl-renderer when started with `renderer=gl` and zwp_linux_dmabuf_v1
    # advertises real format/modifier tables. The sed assumes the
    # template carries `<image compression='off'/>` verbatim — verify
    # the substitution actually fired so a future template format
    # change doesn't silently land accel3d=yes WITHOUT the matching
    # `<gl>` element (qemu would refuse with no useful error).
    XML=$(printf '%s' "$XML" \
        | sed -e "s|<acceleration accel3d='no'/>|<acceleration accel3d='yes'/>|")
    if ! grep -q "<acceleration accel3d='yes'/>" <<<"$XML"; then
        echo "ERROR: --gpu requested but accel3d substitution didn't fire" >&2
        echo "       (template '$TEMPLATE' may have a different XML format)" >&2
        exit 1
    fi
fi

# Multi-machine lane: splice the caller-supplied extra <interface> block
# (the isolated inter-VM socket NIC) verbatim immediately before </devices>. This is
# additive — the template's existing user-mode NIC is untouched, so the clone
# keeps qga/SLIRP reachability AND gains the inter-VM segment. Only fires when
# --extra-nic-xml was passed; the default single-machine clone path skips this
# entirely.
if [ -n "$EXTRA_NIC_XML" ]; then
    EXTRA_BLOCK=$(cat "$EXTRA_NIC_XML")
    XML=$(EXTRA_BLOCK="$EXTRA_BLOCK" python3 -c '
import sys, os
src = sys.stdin.read()
extra = os.environ["EXTRA_BLOCK"].rstrip("\n") + "\n"
idx = src.rfind("</devices>")
if idx == -1:
    sys.stderr.write("ERROR: cloned XML has no </devices> to splice before\n")
    sys.exit(2)
sys.stdout.write(src[:idx] + extra + src[idx:])
' <<<"$XML") || { echo "ERROR: extra-nic splice failed" >&2; exit 1; }
    # Verify the splice actually landed an extra socket NIC (udp for the mmnet
    # lane; mcast tolerated for any future caller) rather than silently no-op'ing
    # into a one-NIC clone.
    if ! grep -qE "type='(udp|mcast)'" <<<"$XML"; then
        echo "ERROR: --extra-nic-xml splice didn't land a udp/mcast interface" >&2
        exit 1
    fi
fi

# Enforcing chain: inject <portForward> on the user-mode interface so
# host SSH clients reach guest sshd at 127.0.0.1:$SSH_PORT. SLIRP/qemu
# user-mode networking otherwise has no route from the host to the
# guest. Pick a free port between 30000 and 39999 to keep collisions
# rare across parallel clones; fall back to a random retry up to 8x.
SSH_PORT=
if [ "$FROM_ENFORCING" = 1 ]; then
    pick_port() {
        local p
        for _ in 1 2 3 4 5 6 7 8; do
            p=$((30000 + RANDOM % 10000))
            # ss is more reliable than nc on Tumbleweed.
            if ! ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
                # Also reject if any existing libvirt domain XML
                # already binds it (best-effort, may miss VMs not in
                # this user's session).
                if ! virsh -c qemu:///session list --all --name 2>/dev/null \
                        | xargs -r -n1 virsh -c qemu:///session dumpxml 2>/dev/null \
                        | grep -q "start='$p'"; then
                    echo "$p"
                    return 0
                fi
            fi
        done
        echo "ERROR: could not pick a free SSH port" >&2
        return 1
    }
    SSH_PORT=$(pick_port) || exit 1

    # libvirt 12.x requires the user-mode interface to use the `passt`
    # backend (or `vhostuser`) for <portForward> to be valid; the
    # default SLIRP backend rejects it. We splice in <backend
    # type='passt'/> + <portForward> immediately after the matching
    # <interface ...> opening tag. The first user-mode interface in
    # the template is the only one we touch; multi-interface templates
    # are out of scope (qdistro VMs are single-NIC).
    #
    # passt(1) must be on PATH on the host; openSUSE Tumbleweed ships
    # it as /usr/bin/passt in the `passt` package.
    XML=$(printf '%s' "$XML" | python3 -c '
import sys, re
src = sys.stdin.read()
port = "'"$SSH_PORT"'"
# Match: <interface type="user"> ... or <interface type='\''user'\''>
pat = re.compile(r"(<interface type=[\"\x27]user[\"\x27]>\n?)")
m = pat.search(src)
if not m:
    sys.stderr.write("ERROR: --from-enforcing-baked could not find user-mode interface in template XML\n")
    sys.exit(2)
inject = (
    "      <backend type=\x27passt\x27/>\n"
    "      <portForward proto=\x27tcp\x27 address=\x27127.0.0.1\x27>\n"
    f"        <range start=\x27{port}\x27 to=\x2722\x27/>\n"
    "      </portForward>\n"
)
sys.stdout.write(src[:m.end()] + inject + src[m.end():])
') || exit 1
    if ! grep -q "<portForward" <<<"$XML"; then
        echo "ERROR: --from-enforcing-baked: portForward injection didn't fire" >&2
        exit 1
    fi
    if ! grep -q "<backend type='passt'/>" <<<"$XML"; then
        echo "ERROR: --from-enforcing-baked: passt backend injection didn't fire" >&2
        exit 1
    fi
fi

printf '%s' "$XML" | virsh -c qemu:///session define /dev/stdin >/dev/null
virsh -c qemu:///session start "$VM" >/dev/null

# 4. Wait for the qemu-guest-agent so subsequent vm-exec works.
#    Under --from-enforcing-baked, qga is denied by SELinux; the wait
#    is short-circuited and the caller is expected to reach the VM via
#    SSH on the printed port.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VM_TOOLS="$(cd "$SCRIPT_DIR/../.." && pwd)/scripts/vm"
if [ "$FROM_ENFORCING" = 1 ]; then
    # Wait for SSH to actually accept. The TCP listener is up
    # immediately (passt forwards regardless of guest state), so a
    # bare `/dev/tcp` probe returns success before sshd has bound the
    # in-guest socket — which then trips the bats sanity probe with
    # "Connection timed out during banner exchange". Run a real ssh
    # round-trip in a loop until either the probe succeeds OR a
    # 180-second deadline elapses (slow first-boot under enforcing
    # can need >90s).
    KEY="$HOME/.ssh/qdistro_enforcing_id_ed25519"
    SSH_OPTS=(-i "$KEY"
              -o StrictHostKeyChecking=no
              -o UserKnownHostsFile=/dev/null
              -o LogLevel=ERROR
              -o ConnectTimeout=4
              -o BatchMode=yes)
    SSH_READY=0
    for _ in $(seq 1 60); do
        if ssh "${SSH_OPTS[@]}" -p "$SSH_PORT" root@127.0.0.1 \
                'true' 2>/dev/null; then
            SSH_READY=1
            break
        fi
        sleep 3
    done
    if [ "$SSH_READY" = 0 ]; then
        # 180-second budget exceeded without an SSH round-trip. Most
        # often this means the cloned VM never reached userspace (the
        # baked image's GRUB has no auto-load entry, or first-boot
        # under enforcing wedged). Surface the failure non-zero so
        # callers don't proceed with a half-built VM. The VM is left
        # defined for `virsh console` post-mortem.
        echo "ERROR: --from-enforcing-baked: SSH never came up on 127.0.0.1:$SSH_PORT" >&2
        echo "       hint: virsh -c qemu:///session screenshot $VM /tmp/$VM.png  (often shows a grub> rescue)" >&2
        echo "       hint: if the baked image is rotten, rebuild via scripts/vm/build-enforcing-baseweed.sh --force" >&2
        CLONE_OK=1   # deliberately keep this VM defined for post-mortem (see hints above)
        exit 7
    fi
else
    "$VM_TOOLS/vm-start-and-wait" "$VM" >/dev/null
fi

# Built successfully. Emit the name (and ssh_port) the caller consumes, THEN mark
# success so the EXIT trap keeps the VM. Marking BEFORE the echo would, on a
# broken-pipe echo (caller already gone) under set -e, disarm the cleanup yet
# leave the caller without the name — leaking exactly the VM this trap reclaims.
echo "$VM"
if [ "$FROM_ENFORCING" = 1 ]; then
    echo "ssh_port=$SSH_PORT"
fi
CLONE_OK=1
