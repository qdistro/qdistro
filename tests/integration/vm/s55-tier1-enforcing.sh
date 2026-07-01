#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier1-enforcing.
#
# spec/30 enforcing-mode pass for Tier-1. Flips SELinux to Enforcing,
# runs the tier-1 happy-path workload through qdistro-tier1-spawn,
# captures any new qdistro_tier1_t AVCs against the audit baseline,
# then restores Permissive on exit (EXIT trap fires on natural exit
# or on signal, so a failed run still resets the mode).
#
# Workload — small, representative of "an app inside the sandbox":
#
#   /usr/bin/id          getpwuid_r + getgrnam → systemd-userdbd reach
#   /usr/bin/cat /etc/passwd
#   dbus-send --session ListNames   session-bus connect path
#
# Any new AVC against qdistro_tier1_t after baseline counts as DIRTY —
# we dump audit2allow output to stderr to feed the next iteration of
# selinux/tier1/qdistro_tier1.te.
#
# SKIP semantics: when SELinux is Disabled, or /etc/selinux/config
# pins SELINUX=permissive (the runtime flip is silently refused by
# the kernel), or qdistro_tier1 isn't loaded, we emit SKIP. The bats
# wrapper treats SKIP as a fail_loud, which is correct — the test is
# explicit about its prerequisites.
#
# Paired bats @test: phase7-tier1-enforcing.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER1_DIR="$SRC/selinux/tier1"
SPAWN="$TIER1_DIR/spawn-tier1.sh"

# --- pre-reqs ----------------------------------------------------------
SE_MODE_INITIAL=$(/usr/sbin/getenforce 2>/dev/null || getenforce 2>/dev/null || echo Disabled)
[ "$SE_MODE_INITIAL" = "Disabled" ] && skip "SELinux is Disabled"
if grep -E '^SELINUX=permissive' /etc/selinux/config >/dev/null 2>&1; then
    skip "/etc/selinux/config pins SELINUX=permissive — runtime flip refused"
fi
command -v semodule >/dev/null 2>&1 || skip "semodule absent"
semodule -l 2>/dev/null | grep -q '^qdistro_tier1\b' \
    || skip "qdistro_tier1 policy module not loaded"
[ -x "$SPAWN" ] || [ -f "$SPAWN" ] || skip "spawn-tier1.sh missing"
# qdistro-tier1-exec installs to libexecdir (not on PATH); check
# command -v AND the libexec fallbacks. Matches spawn-tier1.sh.
if ! command -v qdistro-tier1-exec >/dev/null 2>&1 \
   && [ ! -x /usr/libexec/qdistro-tier1-exec ] \
   && [ ! -x /usr/local/libexec/qdistro-tier1-exec ]; then
    skip "qdistro-tier1-exec not installed"
fi
command -v dbus-send >/dev/null 2>&1 || skip "dbus-send absent"
if ! systemctl is-active --quiet qdistro-admin-broker.service \
        && ! systemctl start qdistro-admin-broker.service 2>/dev/null; then
    skip "qdistro-admin-broker.service not active"
fi

# Tier-1 launch authorization is mandatory. The enforcing workload is
# allowed explicitly so any failure below is about SELinux AVC budget,
# not an absent broker rule.
RULE_DIR=/etc/qdistro/rules.d
mkdir -p "$RULE_DIR"
rm -f "$RULE_DIR"/s55-allow-*.yaml 2>/dev/null || true
cat >"$RULE_DIR/s55-allow-id.yaml" <<'EOF'
- decision: allow
  match:
    action: qdistro.tier1.spawn:/usr/bin/id
  rationale: s55 Tier-1 enforcing workload
EOF
cat >"$RULE_DIR/s55-allow-cat.yaml" <<'EOF'
- decision: allow
  match:
    action: qdistro.tier1.spawn:/usr/bin/cat
  rationale: s55 Tier-1 enforcing workload
EOF
cat >"$RULE_DIR/s55-allow-dbus-send.yaml" <<'EOF'
- decision: allow
  match:
    action: qdistro.tier1.spawn:/usr/bin/dbus-send
  rationale: s55 Tier-1 enforcing workload
EOF
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true

wait_allow_rule() {
    local action="$1"
    local reply=""
    for _ in $(seq 1 25); do
        reply=$(dbus-send --system --print-reply=literal \
            --dest=org.qdistro.AdminBroker1 \
            /org/qdistro/AdminBroker1 \
            org.qdistro.AdminBroker1.CheckPermission \
            "string:$action" \
            "dict:string:string:" 2>/dev/null \
            | tr -d ' \t\n')
        case "$reply" in
            allow|string\"allow\") return 0 ;;
        esac
        sleep 0.2
    done
    fail "broker did not load Tier-1 allow rule for $action; reply='$reply'"
    return 1
}
wait_allow_rule "qdistro.tier1.spawn:/usr/bin/id" || true
wait_allow_rule "qdistro.tier1.spawn:/usr/bin/cat" || true
wait_allow_rule "qdistro.tier1.spawn:/usr/bin/dbus-send" || true

# --- restore-on-exit trap ----------------------------------------------
cleanup() {
    /usr/sbin/setenforce 0 2>/dev/null || setenforce 0 2>/dev/null || true
    rm -f "$RULE_DIR"/s55-allow-*.yaml 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- baseline cursor ---------------------------------------------------
# ausearch wants a checkpoint file; we use --start <epoch> instead, which
# is more robust across audit-userspace versions. The cursor is "now,
# minus a hair" — anything strictly newer than this is a new AVC.
BASELINE_TS=$(date +%s)
# Sub-second clock-skew guard: bump the cursor 1s into the past so we
# don't miss an AVC that auditd timestamps in the same second.
BASELINE_TS=$((BASELINE_TS - 1))

# --- flip to Enforcing -------------------------------------------------
/usr/sbin/setenforce 1 2>/dev/null || setenforce 1 2>/dev/null
SE_MODE=$(/usr/sbin/getenforce 2>/dev/null || getenforce 2>/dev/null || echo Unknown)
if [ "$SE_MODE" = "Enforcing" ]; then
    pass "SELinux mode now Enforcing"
else
    skip "setenforce 1 left mode at $SE_MODE (kernel refused — likely config-pinned permissive)"
fi

# --- run the workload --------------------------------------------------
WORKLOAD_LOG=/tmp/s55-workload.log
: >"$WORKLOAD_LOG"

# Always disable the secctx wrap for this test: the driver runs as
# root under qemu-guest-agent with no XDG_RUNTIME_DIR, so
# qdistro-secctx-exec aborts before reaching qdistro-tier1-exec.
# secctx is covered by s44; s55's job is the AVC budget under
# enforcing.
TIER1_USE_SECCTX_FLAG="QDISTRO_PROFILE=dev TIER1_USE_SECCTX=0"

run_step() {
    local label="$1"; shift
    echo "--- $label ---" >>"$WORKLOAD_LOG"
    env $TIER1_USE_SECCTX_FLAG \
        bash "$SPAWN" s55silo -- "$@" \
        >>"$WORKLOAD_LOG" 2>&1 || true
    if grep -q '^\[tier1\] FAIL:' "$WORKLOAD_LOG"; then
        fail "$label did not enter Tier-1; spawn wrapper failed"
    fi
}

run_step "id"           /usr/bin/id
run_step "cat-passwd"   /usr/bin/cat /etc/passwd
# Session-bus access from a sandboxed domain needs DBUS_SESSION_BUS_ADDRESS
# resolvable. Skip the dbus step if the env can't reach a session bus
# (root-shell context usually can't).
if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    run_step "dbus-list" /usr/bin/dbus-send --session --print-reply \
        --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames
fi

# Give auditd a beat to flush.
sleep 1

# --- collect new AVCs --------------------------------------------------
AUDIT_DUMP=/tmp/s55-avcs.txt
: >"$AUDIT_DUMP"
if command -v ausearch >/dev/null 2>&1; then
    ausearch -m AVC,USER_AVC --start "$(date -d @"$BASELINE_TS" '+%x %T' 2>/dev/null \
        || date -r "$BASELINE_TS" '+%x %T' 2>/dev/null)" 2>/dev/null \
        | grep -E 'scontext=[^ ]*:qdistro_tier1_t' >"$AUDIT_DUMP" || true
else
    # Fallback: tail audit.log directly.
    if [ -r /var/log/audit/audit.log ]; then
        awk -v cutoff="$BASELINE_TS" '
            /type=AVC|type=USER_AVC/ {
                if (match($0, /audit\(([0-9]+)\./, m) && m[1] > cutoff) print
            }' /var/log/audit/audit.log \
            | grep -E 'scontext=[^ ]*:qdistro_tier1_t' >"$AUDIT_DUMP" || true
    fi
fi

NEW_AVCS=$(wc -l <"$AUDIT_DUMP" 2>/dev/null || echo 0)
NEW_AVCS=${NEW_AVCS:-0}

if [ "$NEW_AVCS" -eq 0 ]; then
    pass "0 new denials — qdistro_tier1.te covers the enforcing workload"
else
    echo "FAIL: $NEW_AVCS new qdistro_tier1_t AVCs against baseline" >&2
    echo "--- raw AVCs ---" >&2
    cat "$AUDIT_DUMP" >&2
    if command -v audit2allow >/dev/null 2>&1; then
        echo "--- audit2allow suggestion ---" >&2
        audit2allow -i "$AUDIT_DUMP" 2>/dev/null >&2 || true
    fi
    echo "--- workload log ---" >&2
    cat "$WORKLOAD_LOG" >&2
    fail "$NEW_AVCS new denials — qdistro_tier1.te needs more allow rules"
fi

# --- summary -----------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    echo "[s55] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s55] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
