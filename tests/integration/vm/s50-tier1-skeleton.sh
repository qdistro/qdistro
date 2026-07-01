#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier1-skeleton.
#
# Tier-1 SELinux source inventory. This driver confirms the source
# artifacts survived the fresh-vm-bootstrap rsync into the VM at
# /root/qdistro-src/qdistro/selinux/tier1/. Runtime policy loading and AVC
# budget are covered by s55-tier1-enforcing.sh.
#
# Paired bats @test: phase7-tier1-skeleton.
# PASS strings here MUST match assert_output_contains in that block.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

TIER1_SRC=/root/qdistro-src/qdistro/selinux/tier1
[ -d "$TIER1_SRC" ] || skip "tier1 source not staged at $TIER1_SRC"

# --- existence checks ---------------------------------------------------
for f in README.md spike-checklist.md qdistro_tier1.te qdistro_tier1.fc \
         qdistro_tier1.if install-policy.sh spawn-tier1.sh Makefile; do
    if [ ! -f "$TIER1_SRC/$f" ]; then
        fail "tier1 source missing $f"
    fi
done

if [ -f "$TIER1_SRC/README.md" ]; then
    pass "tier1 README present"
fi

# --- spike-checklist covers Spikes 1-5 ----------------------------------
SPIKE_OK=1
for n in 1 2 3 4 5; do
    if ! grep -q "Spike $n" "$TIER1_SRC/spike-checklist.md" 2>/dev/null; then
        SPIKE_OK=0
        fail "spike-checklist.md missing 'Spike $n'"
    fi
done
[ "$SPIKE_OK" = "1" ] && pass "spike-checklist.md covers Spikes 1-5"

# --- .te declares the type + a Wayland-connect interface ---------------
TE="$TIER1_SRC/qdistro_tier1.te"
if grep -q "type qdistro_tier1_t" "$TE" 2>/dev/null \
   && grep -qi "wayland" "$TE" 2>/dev/null; then
    pass "qdistro_tier1.te declares type + Wayland connect interface"
else
    fail "qdistro_tier1.te missing 'type qdistro_tier1_t' or wayland mention"
fi

# --- spawn-tier1.sh syntax-checks --------------------------------------
SPAWN="$TIER1_SRC/spawn-tier1.sh"
if bash -n "$SPAWN" 2>/tmp/s50-spawn.err; then
    pass "spawn-tier1.sh syntax-checks"
else
    cat /tmp/s50-spawn.err >&2
    fail "spawn-tier1.sh syntax error"
fi

# --- summary -----------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    pass "Tier-1 SELinux source inventory present"
    echo "[s50] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s50] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
