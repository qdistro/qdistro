#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-qsu-real-flow.
#
# Drives the real /usr/local/bin/qsu binary end-to-end through the
# pending → admin allow → cache-hit → re-prompt-on-different-command
# loop. This is the delegated-path counterpart to s57's pure D-Bus
# probe — here every step traverses qsu, the qdistro-root-exec
# socket-activated service, and the broker's RequestPermissionAs path.
#
# Test users:
#   admin  uid 1000 — already present (broker DecideRequest authz)
#   work   uid 1001 — created here if missing (non-admin qsu caller)
#
# PASS strings here MUST match assert_output_contains in the bats
# @test phase7-qsu-real-flow block.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# Track every qsu background pid so cleanup kills any stragglers.
QSU_PIDS=()
register_qsu_pid() { QSU_PIDS+=("$1"); }

cleanup() {
    for pid in "${QSU_PIDS[@]:-}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    # Drain anything left pending so a re-run starts clean.
    runuser -u admin -- python3 -c '
import dbus
try:
    bus = dbus.SystemBus()
    obj = bus.get_object("com.qdistro.AdminBroker1",
                          "/com/qdistro/AdminBroker1")
    iface = dbus.Interface(obj, "com.qdistro.AdminBroker1")
    for r in iface.GetPending():
        try:
            iface.DecideRequest(int(r["id"]), "deny", "once")
        except Exception:
            pass
    iface.RevokeAllForUid(1001)
except Exception:
    pass
' 2>/dev/null || true
}
trap cleanup EXIT

# --- Preflight ---
if [ ! -x /usr/local/bin/qsu ]; then
    fail "/usr/local/bin/qsu absent"
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
pass "qsu installed at /usr/local/bin/qsu"

# Start the broker (bats setup() stops it).
systemctl start qdistro-admin-broker.service 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if dbus-send --system --print-reply \
        --dest=com.qdistro.AdminBroker1 \
        /com/qdistro/AdminBroker1 \
        org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1; then
        break
    fi
    sleep 0.3
done
if ! systemctl is-active qdistro-admin-broker.service >/dev/null 2>&1; then
    fail "qdistro-admin-broker.service not active"
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
pass "broker service active"

# qdistro-root-exec is socket-activated. The socket file must be
# present; the service starts on first connect.
systemctl start qdistro-root-exec.socket 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -S /run/qdistro-root-exec/sock ] && break
    sleep 0.3
done
if [ ! -S /run/qdistro-root-exec/sock ]; then
    fail "/run/qdistro-root-exec/sock not present"
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
pass "qdistro-root-exec socket present"

# Ensure 'work' (uid 1001) exists for the non-admin caller. The bats
# VM convention is admin=1000, regular=1001; some images haven't
# baked uid 1001 in, so create it here if needed.
WORK_USER=work
if ! getent passwd "$WORK_USER" >/dev/null 2>&1; then
    if getent passwd 1001 >/dev/null 2>&1; then
        WORK_USER=$(getent passwd 1001 | cut -d: -f1)
    else
        useradd -m -u 1001 -U -s /bin/bash "$WORK_USER" 2>/dev/null \
            || { fail "could not create non-admin user $WORK_USER (uid 1001)"; \
                 echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1; }
    fi
fi
WORK_UID=$(id -u "$WORK_USER")
pass "non-admin caller present: $WORK_USER (uid=$WORK_UID)"

# Clean slate: revoke any cache rows for the work uid from a prior run.
runuser -u admin -- python3 -c "
import dbus
bus = dbus.SystemBus()
obj = bus.get_object('com.qdistro.AdminBroker1',
                     '/com/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'com.qdistro.AdminBroker1')
try: iface.RevokeAllForUid(${WORK_UID})
except Exception: pass
for r in iface.GetPending():
    try: iface.DecideRequest(int(r['id']), 'deny', 'once')
    except Exception: pass
" >/dev/null 2>&1 || true

# Helper: in a subshell, wait for a pending request whose claim_uid
# matches our work uid AND whose argv matches a given argv list.
# Returns the rid on stdout, blank if not found within timeout.
wait_for_pending_rid() {
    local want_argv="$1"  # space-joined argv string for matching
    local timeout_s="${2:-10}"
    runuser -u admin -- python3 - "$WORK_UID" "$want_argv" "$timeout_s" <<'PYEOF'
import dbus, sys, time, json
want_uid = int(sys.argv[1])
want_argv = sys.argv[2]
timeout = float(sys.argv[3])
deadline = time.monotonic() + timeout
bus = dbus.SystemBus()
obj = bus.get_object('com.qdistro.AdminBroker1',
                     '/com/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'com.qdistro.AdminBroker1')
found_rid = None
while time.monotonic() < deadline:
    for r in iface.GetPending():
        if int(r['uid']) != want_uid:
            continue
        details = r.get('details', {})
        # qsu/qdistro_root_exec sets details['argv'] = shlex.join(argv);
        # exact substring match is fine since argv is short and we
        # control the test commands.
        argv_str = str(details.get('argv', ''))
        if want_argv in argv_str:
            found_rid = int(r['id'])
            break
    if found_rid is not None:
        break
    time.sleep(0.1)
print(found_rid if found_rid is not None else "")
PYEOF
}

decide_as_admin() {
    local rid="$1" decision="$2" scope="$3"
    runuser -u admin -- python3 -c "
import dbus, sys
bus = dbus.SystemBus()
obj = bus.get_object('com.qdistro.AdminBroker1',
                     '/com/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'com.qdistro.AdminBroker1')
iface.DecideRequest(${rid}, '${decision}', '${scope}')
"
}

# --- Step 1: pending appears after qsu invocation ---
TRUE_OUT=/tmp/s58-true-1.out
TRUE_RC=/tmp/s58-true-1.rc
: >"$TRUE_OUT"; : >"$TRUE_RC"
( runuser -u "$WORK_USER" -- /usr/local/bin/qsu /bin/true >"$TRUE_OUT" 2>&1
  echo $? >"$TRUE_RC" ) &
QSU1_PID=$!
register_qsu_pid "$QSU1_PID"

RID1=$(wait_for_pending_rid "/bin/true" 10)
if [ -n "$RID1" ]; then
    pass "pending rid=$RID1"
else
    fail "no pending request appeared for qsu /bin/true within 10s"
    wait "$QSU1_PID" 2>/dev/null || true
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

# --- Step 2: admin allows forever_argv → qsu unblocks rc=0 ---
if decide_as_admin "$RID1" "allow" "forever_argv" 2>/tmp/s58-decide1.err; then
    :
else
    fail "DecideRequest(rid=$RID1, allow, forever_argv) failed: $(cat /tmp/s58-decide1.err 2>/dev/null)"
    wait "$QSU1_PID" 2>/dev/null || true
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

# Wait for the qsu invocation to finish (it should now run /bin/true
# and exit 0 quickly).
for _ in $(seq 1 50); do
    if ! kill -0 "$QSU1_PID" 2>/dev/null; then break; fi
    sleep 0.2
done
wait "$QSU1_PID" 2>/dev/null || true
RC1=$(cat "$TRUE_RC" 2>/dev/null || echo "missing")
if [ "$RC1" = "0" ]; then
    pass "qsu /bin/true rc=0 after admin allow forever_argv"
else
    fail "qsu /bin/true did not exit rc=0 (got rc=$RC1, output=$(cat "$TRUE_OUT"))"
fi

# --- Step 3: second qsu /bin/true should cache-hit, no pending ---
TRUE2_OUT=/tmp/s58-true-2.out
TRUE2_RC=/tmp/s58-true-2.rc
: >"$TRUE2_OUT"; : >"$TRUE2_RC"
( runuser -u "$WORK_USER" -- /usr/local/bin/qsu /bin/true >"$TRUE2_OUT" 2>&1
  echo $? >"$TRUE2_RC" ) &
QSU2_PID=$!
register_qsu_pid "$QSU2_PID"

# Cache-hit path: broker decides synchronously, qsu should exit fast.
# If it lingers we'd see a pending row appear — sanity-check that
# DIDN'T happen.
SAW_PENDING=""
for _ in 1 2 3 4 5; do
    if ! kill -0 "$QSU2_PID" 2>/dev/null; then break; fi
    sleep 0.2
done
# Snapshot pending immediately:
PENDING_AT_CACHE_HIT=$(runuser -u admin -- python3 -c "
import dbus, json
bus = dbus.SystemBus()
obj = bus.get_object('com.qdistro.AdminBroker1',
                     '/com/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'com.qdistro.AdminBroker1')
rows = iface.GetPending()
print(json.dumps([
    {'id': int(r['id']), 'uid': int(r['uid']),
     'argv': str(r.get('details', {}).get('argv', ''))}
    for r in rows
]))
" 2>/dev/null || echo "[]")
if printf '%s' "$PENDING_AT_CACHE_HIT" \
    | python3 -c "
import json, sys
rows = json.loads(sys.stdin.read() or '[]')
for r in rows:
    if r['uid'] == ${WORK_UID} and '/bin/true' in r['argv']:
        print('PENDING')
        break
" 2>/dev/null | grep -q PENDING; then
    SAW_PENDING="yes"
fi
wait "$QSU2_PID" 2>/dev/null || true
RC2=$(cat "$TRUE2_RC" 2>/dev/null || echo "missing")

if [ -z "$SAW_PENDING" ] && [ "$RC2" = "0" ]; then
    pass "second qsu /bin/true cache-hit"
else
    fail "second qsu /bin/true did NOT cache-hit (saw_pending=$SAW_PENDING rc=$RC2)"
fi

# --- Step 4: different command (echo) re-prompts ---
ECHO_OUT=/tmp/s58-echo.out
ECHO_RC=/tmp/s58-echo.rc
: >"$ECHO_OUT"; : >"$ECHO_RC"
( runuser -u "$WORK_USER" -- /usr/local/bin/qsu /bin/echo hello-from-s58 \
    >"$ECHO_OUT" 2>&1
  echo $? >"$ECHO_RC" ) &
QSU3_PID=$!
register_qsu_pid "$QSU3_PID"

RID3=$(wait_for_pending_rid "/bin/echo" 10)
if [ -n "$RID3" ]; then
    pass "qsu /bin/echo re-prompted"
else
    fail "qsu /bin/echo did not re-prompt within 10s — argv-pinning broken?"
    wait "$QSU3_PID" 2>/dev/null || true
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

# --- Step 5: admin one-shot allow → echo runs, captures stdout ---
if decide_as_admin "$RID3" "allow" "once" 2>/tmp/s58-decide3.err; then
    :
else
    fail "DecideRequest(rid=$RID3, allow, once) failed: $(cat /tmp/s58-decide3.err 2>/dev/null)"
    wait "$QSU3_PID" 2>/dev/null || true
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

for _ in $(seq 1 50); do
    if ! kill -0 "$QSU3_PID" 2>/dev/null; then break; fi
    sleep 0.2
done
wait "$QSU3_PID" 2>/dev/null || true
RC3=$(cat "$ECHO_RC" 2>/dev/null || echo "missing")
ECHO_STDOUT=$(cat "$ECHO_OUT" 2>/dev/null || echo "")
# qsu streams the target's stdout verbatim; echo appends a newline so
# strip trailing whitespace before comparing.
ECHO_STDOUT_TRIMMED=$(printf '%s' "$ECHO_STDOUT" | tr -d '\r\n ')
if [ "$RC3" = "0" ] && [ "$ECHO_STDOUT_TRIMMED" = "hello-from-s58" ]; then
    pass "qsu /bin/echo rc=0 stdout='hello-from-s58' after admin allow once"
else
    fail "qsu /bin/echo did not match expected (rc=$RC3 stdout=$ECHO_STDOUT)"
fi

# Final cleanup is in trap; emit summary.
if [ "$FAILCOUNT" -eq 0 ]; then
    pass "s58 — qsu real-flow argv-aware cache + re-prompt end-to-end"
    echo "[s58] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
