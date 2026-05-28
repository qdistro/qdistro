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
for arg in "$@"; do
    case "$arg" in
        --gpu)                   GPU=1 ;;
        --from-baked)            FROM_BAKED=1 ;;
        --from-enforcing-baked)  FROM_ENFORCING=1 ;;
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

VM="${PREFIX}-$(date +%y%m%d-%H%M)"
TEMPLATE="${QDWIN_VM_TEMPLATE:-qdistro-template}"
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"

if [ "$FROM_ENFORCING" = 1 ]; then
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
else
    virt-customize --no-network -a "$IMG/${VM}.qcow2" \
        --edit '/etc/selinux/config:s/SELINUX=enforcing/SELINUX=permissive/' \
        >/dev/null
fi

# 3. Define from template, swapping name + disk path + MAC + GPU bits
NEW_MAC="52:54:00:$(printf '%02x:%02x:%02x' \
    $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"

XML=$(virsh -c qemu:///session dumpxml "$TEMPLATE" --inactive)
XML=$(printf '%s' "$XML" \
    | sed -e "s|<name>$TEMPLATE</name>|<name>$VM</name>|" \
          -e '/<uuid>/d' \
          -e "/<mac address=/c\\      <mac address='$NEW_MAC'/>" \
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
        exit 7
    fi
else
    "$VM_TOOLS/vm-start-and-wait" "$VM" >/dev/null
fi

echo "$VM"
if [ "$FROM_ENFORCING" = 1 ]; then
    echo "ssh_port=$SSH_PORT"
fi
