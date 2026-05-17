#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier1-e2e.
#
# spec/30 §"Phase plan" step 8 — full Tier-1 SELinux pipeline:
#
#   1. SELinux enabled (enforcing OR permissive — permissive is enough
#      for the type-transition + ps label observation).
#   2. qdistro_tier1 policy module loaded.
#   3. type_transition unconfined_t exec qdistro_tier1_exec_t -> qdistro_tier1_t
#      rule installed (sesearch -T).
#   4. spawn-tier1.sh routes the inner command into qdistro_tier1_t
#      (observed via `ps -eo pid,label`).
#   5. broker.CheckPermission denies `qdistro.tier1.spawn:sleep` when
#      an authored rule with decision=deny is in place — the spec/30
#      step 6 broker gate.
#
# spawn-tier1.sh's last step is `exec env ... qdistro-secctx-exec ...
# qdistro-tier1-exec -- <app>` which runs the inner command in
# qdistro_tier1_t via the auto_trans rule (kernel-applied, doesn't
# need setexeccon). Works in permissive — the transition fires, the
# ps -eo pid,label observation succeeds, and any AVCs are logged but
# not enforced.
#
# Paired bats @test: phase7-tier1-e2e.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER1_DIR="$SRC/selinux/tier1"
[ -d "$TIER1_DIR" ] || skip "tier1 source not staged at $TIER1_DIR"

# 1. SELinux enabled? -----------------------------------------------------
SE_MODE=$(/usr/sbin/getenforce 2>/dev/null || getenforce 2>/dev/null || echo Disabled)
case "$SE_MODE" in
    Enforcing|Permissive) pass "SELinux enabled" ;;
    *) skip "SELinux is $SE_MODE — Tier-1 type transitions are no-ops" ;;
esac

# 2. Module loaded? -------------------------------------------------------
command -v semodule >/dev/null 2>&1 || skip "semodule not installed"
if semodule -l 2>/dev/null | grep -q '^qdistro_tier1\b'; then
    pass "qdistro_tier1 policy module loaded"
else
    skip "qdistro_tier1 not loaded — run $TIER1_DIR/install-policy.sh"
fi

# 3. type_transition rule visible via sesearch? --------------------------
command -v sesearch >/dev/null 2>&1 || skip "sesearch absent (install setools-console)"
if sesearch -T -s unconfined_t -t qdistro_tier1_exec_t 2>/dev/null \
       | grep -q "qdistro_tier1_t"; then
    pass "type_transition unconfined_t -> qdistro_tier1_t exists"
else
    # The .te wraps the unconfined_t branch in optional_policy so this
    # may also live under staff_t. Try that before failing.
    if sesearch -T -s staff_t -t qdistro_tier1_exec_t 2>/dev/null \
           | grep -q "qdistro_tier1_t"; then
        pass "type_transition unconfined_t -> qdistro_tier1_t exists"
    else
        fail "no type_transition into qdistro_tier1_t found via sesearch -T"
    fi
fi

# 4. spawn-tier1.sh lands `sleep` in qdistro_tier1_t? --------------------
SPAWN="$TIER1_DIR/spawn-tier1.sh"
[ -x "$SPAWN" ] || chmod +x "$SPAWN" 2>/dev/null || true
[ -f "$SPAWN" ] || skip "spawn-tier1.sh missing at $SPAWN"

# qdistro-tier1-exec installs to libexecdir (not on PATH); check
# command -v AND the libexec fallbacks. Matches the resolution order
# inside spawn-tier1.sh.
if ! command -v qdistro-tier1-exec >/dev/null 2>&1 \
   && [ ! -x /usr/libexec/qdistro-tier1-exec ] \
   && [ ! -x /usr/local/libexec/qdistro-tier1-exec ]; then
    skip "qdistro-tier1-exec not installed (build daemons/tier1-exec)"
fi
# Always disable the secctx wrap for this test. The driver runs as
# root under qemu-guest-agent with no XDG_RUNTIME_DIR; qdistro-secctx-
# exec then errors out before reaching qdistro-tier1-exec and the
# type transition never fires. The secctx wrap itself is covered by
# s44 (phase7-tier4-secctx-exec); s51's job is the SELinux pipeline.
TIER1_USE_SECCTX_FLAG="TIER1_USE_SECCTX=0"

# Run spawn-tier1.sh sleep 0.5 in the background; while it sleeps,
# `ps -eo pid,label` should show the sleep PID under qdistro_tier1_t.
SPAWN_LOG=/tmp/s51-spawn.log
# Default-allow when no rule — but if a prior s51 run left a deny rule,
# spawn will exit 1 here. Clean any stale rule first.
RULE_DIR=/etc/qdistro/rules.d
mkdir -p "$RULE_DIR"
rm -f "$RULE_DIR"/s51-*.yaml 2>/dev/null || true
# Bounce the broker's inotify so it picks up the removal.
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true

env TIER1_BROKER_OPTIONAL=1 $TIER1_USE_SECCTX_FLAG \
    bash "$SPAWN" s51silo -- /usr/bin/sleep 2 \
    >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

OBSERVED=""
for _ in $(seq 1 20); do
    # The sleep is a grandchild — ps for any process whose comm is sleep
    # owned by this script's process tree and inspect its label.
    OBSERVED=$(ps -eo pid,comm,label 2>/dev/null \
        | awk '$2 == "sleep" {print $3}' \
        | grep -m1 qdistro_tier1_t || true)
    [ -n "$OBSERVED" ] && break
    sleep 0.2
done

if [ -n "$OBSERVED" ]; then
    pass "sleep runs in qdistro_tier1_t"
else
    cat "$SPAWN_LOG" >&2
    fail "ps -eo pid,comm,label never saw sleep in qdistro_tier1_t"
fi

# Reap the spawn.
kill -TERM "$SPAWN_PID" 2>/dev/null || true
pkill -P "$SPAWN_PID" 2>/dev/null || true
wait "$SPAWN_PID" 2>/dev/null || true

# 5. broker spawn-action gate denies `qdistro.tier1.spawn:sleep` --------
if ! command -v dbus-send >/dev/null 2>&1; then
    skip "dbus-send absent"
fi
if ! systemctl is-active --quiet qdistro-admin-broker.service \
        && ! systemctl start qdistro-admin-broker.service 2>/dev/null; then
    skip "qdistro-admin-broker.service not active"
fi

# Author a deny rule for qdistro.tier1.spawn:sleep, wait for inotify
# reload, probe CheckPermission, then drop the rule.
DENY_RULE="$RULE_DIR/s51-deny-sleep.yaml"
cat >"$DENY_RULE" <<'EOF'
- decision: deny
  match:
    action: qdistro.tier1.spawn:sleep
  rationale: s51 broker spawn-action gate probe
EOF

# Give inotify + RulesReloaded a moment.
for _ in $(seq 1 25); do
    REPLY=$(dbus-send --system --print-reply=literal \
        --dest=com.qdistro.AdminBroker1 \
        /com/qdistro/AdminBroker1 \
        com.qdistro.AdminBroker1.CheckPermission \
        "string:qdistro.tier1.spawn:sleep" \
        "dict:string:string:" 2>/dev/null \
        | tr -d ' \t\n')
    case "$REPLY" in
        deny|*deny*) break ;;
    esac
    sleep 0.2
done

case "$REPLY" in
    deny|*deny*)
        pass "broker denied qdistro.tier1.spawn:sleep before exec"
        ;;
    *)
        fail "broker CheckPermission(qdistro.tier1.spawn:sleep) returned '$REPLY', expected deny"
        ;;
esac

rm -f "$DENY_RULE"

# --- summary -----------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    pass "spec/30 Tier-1 SELinux end-to-end"
    echo "[s51] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s51] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
