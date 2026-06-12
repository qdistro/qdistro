#!/bin/bash
# In-VM driver for S6: session-manager SELinux enforcing harvest.
#
# Flip SELinux Enforcing, restart qdistro-session-manager.service so it runs
# in qdistro_sessmgr_t under the new mode, exercise the representative silo
# lifecycle workload, then fail on any new qdistro_sessmgr_t AVCs since the
# baseline. On failure, dump raw AVCs plus audit2allow output for the next
# policy iteration.
#
# This is the VM/console half of todo/fable-release/02-security-gate S6. It
# is intentionally skipped on hosts where SELinux is disabled, config-pinned
# permissive, the session-manager policy is absent, or qga cannot flip
# enforcing. EXIT restores permissive.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
LIFECYCLE="$SRC/tests/integration/vm/s101-session-lifecycle.sh"

# --- pre-reqs ----------------------------------------------------------
SE_MODE_INITIAL=$(/usr/sbin/getenforce 2>/dev/null || getenforce 2>/dev/null || echo Disabled)
[ "$SE_MODE_INITIAL" = "Disabled" ] && skip "SELinux is Disabled"
if grep -E '^SELINUX=permissive' /etc/selinux/config >/dev/null 2>&1; then
    skip "/etc/selinux/config pins SELINUX=permissive — runtime flip refused"
fi
command -v semodule >/dev/null 2>&1 || skip "semodule absent"
semodule -l 2>/dev/null | grep -q '^qdistro_session_manager\b' \
    || skip "qdistro_session_manager policy module not loaded"
command -v systemctl >/dev/null 2>&1 || skip "systemctl absent"
command -v busctl >/dev/null 2>&1 || skip "busctl absent"
[ -x "$LIFECYCLE" ] || skip "session lifecycle probe missing at $LIFECYCLE"

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

# --- restart session manager under the new mode ------------------------
systemctl restart qdistro-session-manager.service 2>/tmp/s63-restart.log || {
    cat /tmp/s63-restart.log >&2 || true
    fail "session manager restart failed under enforcing"
}
for _ in $(seq 1 20); do
    if busctl --system call org.qdistro.SessionManager1 \
            /org/qdistro/SessionManager1 \
            org.qdistro.SessionManager1 ListSilos >/tmp/s63-listsilos.log 2>&1; then
        break
    fi
    sleep 0.25
done

SM_PID=$(systemctl show -p MainPID --value qdistro-session-manager.service 2>/dev/null)
SM_PID=${SM_PID:-0}
if [ "$SM_PID" -gt 1 ] 2>/dev/null; then
    pass "session manager running pid=$SM_PID"
else
    journalctl -u qdistro-session-manager.service --since "1 minute ago" --no-pager >&2 || true
    fail "session manager did not run under enforcing (MainPID=$SM_PID)"
fi

# --- exercise representative lifecycle workload ------------------------
if bash "$LIFECYCLE" >/tmp/s63-lifecycle.log 2>&1; then
    pass "session lifecycle probe succeeded under enforcing"
else
    cat /tmp/s63-lifecycle.log >&2 || true
    fail "session lifecycle probe failed under enforcing"
fi

# --- collect new AVCs against qdistro_sessmgr_t ------------------------
sleep 1
AUDIT_DUMP=/tmp/s63-avcs.txt
: >"$AUDIT_DUMP"
if command -v ausearch >/dev/null 2>&1; then
    ausearch -m AVC,USER_AVC --start "$(date -d @"$BASELINE_TS" '+%x %T' 2>/dev/null \
        || date -r "$BASELINE_TS" '+%x %T' 2>/dev/null)" 2>/dev/null \
        | grep -E 'scontext=[^ ]*:qdistro_sessmgr_t' >"$AUDIT_DUMP" || true
else
    if [ -r /var/log/audit/audit.log ]; then
        awk -v cutoff="$BASELINE_TS" '
            /type=AVC|type=USER_AVC/ {
                if (match($0, /audit\(([0-9]+)\./, m) && m[1] > cutoff) print
            }' /var/log/audit/audit.log \
            | grep -E 'scontext=[^ ]*:qdistro_sessmgr_t' >"$AUDIT_DUMP" || true
    fi
fi

NEW_AVCS=$(wc -l <"$AUDIT_DUMP" 2>/dev/null || echo 0)
NEW_AVCS=${NEW_AVCS:-0}

if [ "$NEW_AVCS" -eq 0 ]; then
    pass "0 new denials — qdistro_session_manager policy covers the lifecycle workload"
else
    echo "FAIL: $NEW_AVCS new qdistro_sessmgr_t AVCs against baseline" >&2
    echo "--- raw AVCs ---" >&2
    cat "$AUDIT_DUMP" >&2
    if command -v audit2allow >/dev/null 2>&1; then
        echo "--- audit2allow suggestion ---" >&2
        audit2allow -i "$AUDIT_DUMP" 2>/dev/null >&2 || true
    fi
    fail "$NEW_AVCS new denials — qdistro_session_manager.te needs more allow rules"
fi

# --- summary -----------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ]; then
    echo "[s63] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s63] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
