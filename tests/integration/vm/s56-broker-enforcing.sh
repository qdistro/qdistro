#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-broker-enforcing.
#
# task(068) — qdistro_broker.te 0.3.0 dropped permissive. This driver
# confirms the broker daemon stays clean under SELinux Enforcing when
# its standard D-Bus surface is exercised:
#
#   CheckClipboardTransfer  source_silo:user1 → admin (deny by default)
#   ListRules               admin/root-gated read of /etc/qdistro/rules.d
#   GetPending              the broker's pending-requests snapshot
#                           (the bats prose calls this 'ListPending';
#                           the actual broker method is GetPending —
#                           same load-bearing path)
#   ListHistory             admin/root-gated audit-row read
#
# Any new qdistro_broker_t AVC against the audit baseline counts as
# DIRTY; we dump audit2allow output for the next .te iteration.
#
# SKIP semantics match s55: Disabled, config-pinned permissive, or
# qdistro_broker policy not loaded. EXIT trap restores Permissive on
# natural exit or signal.
#
# Paired bats @test: phase7-broker-enforcing.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# --- pre-reqs ----------------------------------------------------------
SE_MODE_INITIAL=$(/usr/sbin/getenforce 2>/dev/null || getenforce 2>/dev/null || echo Disabled)
[ "$SE_MODE_INITIAL" = "Disabled" ] && skip "SELinux is Disabled"
if grep -E '^SELINUX=permissive' /etc/selinux/config >/dev/null 2>&1; then
    skip "/etc/selinux/config pins SELINUX=permissive — runtime flip refused"
fi
command -v semodule >/dev/null 2>&1 || skip "semodule absent"
semodule -l 2>/dev/null | grep -q '^qdistro_broker\b' \
    || skip "qdistro_broker policy module not loaded"
command -v dbus-send >/dev/null 2>&1 || skip "dbus-send absent"
command -v systemctl >/dev/null 2>&1 || skip "systemctl absent"

# --- restore-on-exit trap ----------------------------------------------
restore_permissive() {
    /usr/sbin/setenforce 0 2>/dev/null || setenforce 0 2>/dev/null || true
}
trap restore_permissive EXIT INT TERM

# --- flip + baseline ---------------------------------------------------
BASELINE_TS=$(date +%s)
BASELINE_TS=$((BASELINE_TS - 1))

/usr/sbin/setenforce 1 2>/dev/null || setenforce 1 2>/dev/null
SE_MODE=$(/usr/sbin/getenforce 2>/dev/null || getenforce 2>/dev/null || echo Unknown)
if [ "$SE_MODE" = "Enforcing" ]; then
    pass "SELinux mode now Enforcing"
else
    skip "setenforce 1 left mode at $SE_MODE"
fi

# --- restart broker under the new mode ---------------------------------
systemctl restart qdistro-admin-broker.service 2>/dev/null || true
# Give the unit a moment to bind its bus name.
for _ in $(seq 1 20); do
    if dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
            / org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done

BROKER_PID=$(systemctl show -p MainPID --value qdistro-admin-broker.service 2>/dev/null)
BROKER_PID=${BROKER_PID:-0}
if [ "$BROKER_PID" -gt 1 ] 2>/dev/null; then
    pass "broker running pid=$BROKER_PID"
else
    journalctl -u qdistro-admin-broker.service --since "1 minute ago" --no-pager >&2 || true
    fail "broker did not restart under enforcing (MainPID=$BROKER_PID)"
fi

# --- exercise the D-Bus surface ----------------------------------------
probe_ok=1

# CheckClipboardTransfer signature: ss as(...)ssss → s
# Use a cross-silo synthetic pair so it hits the rules engine.
if ! dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckClipboardTransfer \
        string:user1 string:admin \
        array:string:"text/plain" \
        string: string: string: \
        >/tmp/s56-cct.log 2>&1; then
    cat /tmp/s56-cct.log >&2
    probe_ok=0
    echo "FAIL: CheckClipboardTransfer D-Bus probe failed" >&2
fi

# ListRules — admin/root-only; root caller here so it should succeed.
if ! dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.ListRules \
        >/tmp/s56-lr.log 2>&1; then
    cat /tmp/s56-lr.log >&2
    probe_ok=0
    echo "FAIL: ListRules D-Bus probe failed" >&2
fi

# GetPending — what the bats prose calls 'ListPending'. No args.
if ! dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.GetPending \
        >/tmp/s56-gp.log 2>&1; then
    cat /tmp/s56-gp.log >&2
    probe_ok=0
    echo "FAIL: GetPending D-Bus probe failed" >&2
fi

# ListHistory takes one int32 (limit).
if ! dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.ListHistory \
        int32:25 >/tmp/s56-lh.log 2>&1; then
    cat /tmp/s56-lh.log >&2
    probe_ok=0
    echo "FAIL: ListHistory D-Bus probe failed" >&2
fi

if [ "$probe_ok" = "1" ]; then
    pass "D-Bus probe succeeded under enforcing"
else
    fail "one or more D-Bus probes failed (see above)"
fi

# --- collect new AVCs against qdistro_broker_t -------------------------
sleep 1
AUDIT_DUMP=/tmp/s56-avcs.txt
: >"$AUDIT_DUMP"
if command -v ausearch >/dev/null 2>&1; then
    ausearch -m AVC,USER_AVC --start "$(date -d @"$BASELINE_TS" '+%x %T' 2>/dev/null \
        || date -r "$BASELINE_TS" '+%x %T' 2>/dev/null)" 2>/dev/null \
        | grep -E 'scontext=[^ ]*:qdistro_broker_t' >"$AUDIT_DUMP" || true
else
    if [ -r /var/log/audit/audit.log ]; then
        awk -v cutoff="$BASELINE_TS" '
            /type=AVC|type=USER_AVC/ {
                if (match($0, /audit\(([0-9]+)\./, m) && m[1] > cutoff) print
            }' /var/log/audit/audit.log \
            | grep -E 'scontext=[^ ]*:qdistro_broker_t' >"$AUDIT_DUMP" || true
    fi
fi

NEW_AVCS=$(wc -l <"$AUDIT_DUMP" 2>/dev/null || echo 0)
NEW_AVCS=${NEW_AVCS:-0}

if [ "$NEW_AVCS" -eq 0 ]; then
    pass "0 new denials — qdistro_broker.te 0.3.0 covers the enforcing workload"
else
    echo "FAIL: $NEW_AVCS new qdistro_broker_t AVCs against baseline" >&2
    echo "--- raw AVCs ---" >&2
    cat "$AUDIT_DUMP" >&2
    if command -v audit2allow >/dev/null 2>&1; then
        echo "--- audit2allow suggestion ---" >&2
        audit2allow -i "$AUDIT_DUMP" 2>/dev/null >&2 || true
    fi
    fail "$NEW_AVCS new denials — qdistro_broker.te needs more allow rules"
fi

# --- summary -----------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    echo "[s56] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s56] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
