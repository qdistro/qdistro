#!/bin/bash
# host-port.sh — pick a free loopback port for a host-side listener or a
# passt <portForward>. Sourced; no side effects on source.
#
# The port space is HOST-GLOBAL: a listener or a libvirt portForward bound by
# one user occupies the port for every user on the machine. Probing must
# therefore be global too: `ss` sees listeners of all users, but the domain
# XML check below only sees the caller's own qemu:///session — a port bound
# by another user's defined-but-stopped domain is missed. Random draws from a
# 10000-wide range keep that residual collision window small; callers that
# need strictness should retry on bind failure.

# qdistro_pick_free_port [lo hi] — echo a free port in [lo,hi] (default
# 30000-39999), or return 1 after 8 tries.
qdistro_pick_free_port() {
    local lo=${1:-30000} hi=${2:-39999} p _
    for _ in 1 2 3 4 5 6 7 8; do
        p=$(( lo + RANDOM % (hi - lo + 1) ))
        # ss is more reliable than nc on Tumbleweed.
        if ! ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
            # Also reject if any existing libvirt domain XML already binds
            # it (best-effort: only this user's session is inspected).
            if ! virsh -c qemu:///session list --all --name 2>/dev/null \
                    | xargs -r -n1 virsh -c qemu:///session dumpxml 2>/dev/null \
                    | grep -q "start='$p'"; then
                echo "$p"
                return 0
            fi
        fi
    done
    echo "ERROR: could not pick a free port in $lo-$hi" >&2
    return 1
}
