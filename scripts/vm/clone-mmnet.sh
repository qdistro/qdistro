#!/bin/bash
# clone-mmnet.sh — clone ONE multi-machine-lane VM (peer "a" or "b") that joins
# the isolated inter-VM udp-over-loopback segment defined in mmnet-config.sh.
#
# Usage:
#   MMNET_SEED=<seed> ./clone-mmnet.sh <name-prefix> <a|b> [--from-baked|--from-run-golden=PATH]
#
# Prints the new VM name to stdout (one line), exactly like clone-baseweed.sh,
# so the caller (ci/lib/gates/mmnet.sh) can capture and later reap it.
#
# This is a THIN wrapper over clone-baseweed.sh: it renders the peer's udp
# <interface> from mmnet-config.sh into a temp file and passes it as
# --extra-nic-xml. The clone therefore keeps the template's user-mode NIC (qga +
# SLIRP host access, so we can configure it after boot) AND gains a second NIC
# on the private udp-over-loopback segment that carries the actual inter-VM
# traffic.
#
# The default single-machine lane never calls this script and never passes
# --extra-nic-xml, so its clones are unaffected (one NIC, as today).
#
# MMNET_SEED MUST be set by the caller to a value shared between the two peers
# of the SAME run (so both join the same group/port) but distinct from any
# concurrent sibling run (so two runs don't bridge into one segment). The qci
# gate uses its run PID. If unset we fall back to the parent PID, which is only
# safe when both peers are cloned from the same parent shell.
set -euo pipefail

export LIBVIRT_DEFAULT_URI="${LIBVIRT_DEFAULT_URI:-qemu:///session}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/vm/mmnet-config.sh
. "$SCRIPT_DIR/mmnet-config.sh"

PREFIX=""
PEER=""
PASSTHRU=()
for arg in "$@"; do
    case "$arg" in
        a|A|b|B)             PEER="$arg" ;;
        --from-baked|--from-run-golden=*) PASSTHRU+=("$arg") ;;
        --*)                 echo "ERROR: unknown flag '$arg'" >&2; exit 2 ;;
        *)
            if [ -z "$PREFIX" ]; then PREFIX="$arg"
            else echo "ERROR: extra positional arg '$arg'" >&2; exit 2; fi ;;
    esac
done
if [ -z "$PREFIX" ] || [ -z "$PEER" ]; then
    echo "usage: $0 <name-prefix> <a|b> [--from-baked|--from-run-golden=PATH]" >&2
    exit 2
fi

# Default backing: baseweed-baked (tier-2 deps + qga, SELinux permissive). The
# mmnet lane only needs a booting guest with a configurable NIC, not a built
# compositor, so --from-baked is the cheap, correct default.
HAS_BACKING=0
for a in "${PASSTHRU[@]:-}"; do
    case "$a" in --from-baked|--from-run-golden=*) HAS_BACKING=1 ;; esac
done
[ "$HAS_BACKING" = 1 ] || PASSTHRU+=(--from-baked)

# Render this peer's udp interface to a temp file for clone-baseweed.sh.
NIC_XML="$(mktemp /tmp/mmnet-nic-XXXXXX.xml)"
trap 'rm -f "$NIC_XML"' EXIT INT TERM
mmnet_interface_xml "$PEER" >"$NIC_XML"

# Hand off to the shared clone path with the extra NIC. clone-baseweed.sh prints
# the VM name on stdout and owns its own failure cleanup (overlay + domain). We
# do NOT exec — the EXIT trap must fire to remove the temp NIC file (clone-
# baseweed.sh reads it up front, so removing it after the call is safe). Forward
# the child's exit status verbatim.
"$SCRIPT_DIR/clone-baseweed.sh" "$PREFIX" "${PASSTHRU[@]}" \
    --extra-nic-xml="$NIC_XML"
