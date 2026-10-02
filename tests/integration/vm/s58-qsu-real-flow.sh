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
# Since 2026-10-02 it also carries the headless ports of
# permissions-gui 44/45/46 (forever_argv/basename/prefix hit+miss),
# 49 (ListHistory argv shape), 50 (argv_prefix rule), 51 (qsu -u target in
# the action key), 52 (invalid target_user refused before the broker),
# 53 (in-flight cap) and 54 (sanitized privileged env) — see the
# "Extension" block below.
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
    obj = bus.get_object("org.qdistro.AdminBroker1",
                          "/org/qdistro/AdminBroker1")
    iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
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
        --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
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
obj = bus.get_object('org.qdistro.AdminBroker1',
                     '/org/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'org.qdistro.AdminBroker1')
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
obj = bus.get_object('org.qdistro.AdminBroker1',
                     '/org/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'org.qdistro.AdminBroker1')
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
obj = bus.get_object('org.qdistro.AdminBroker1',
                     '/org/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'org.qdistro.AdminBroker1')
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
obj = bus.get_object('org.qdistro.AdminBroker1',
                     '/org/qdistro/AdminBroker1')
iface = dbus.Interface(obj, 'org.qdistro.AdminBroker1')
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


# ===========================================================================
# Extension (2026-10-02, test-audit headless port): the real-qsu halves of
# permissions-gui 44, 45, 46, 49, 50, 51, 52, 53, 54. Those scenarios drove
# the decision through the labwc/XWayland admin app; every load-bearing
# assertion was a qsu exit/stdout, a broker GetPending/ListHistory reply, or
# a cache/audit sqlite row. Here the admin decision is a DecideRequest as
# the admin uid (the call the admin app's Ctrl+Y makes), everything else is
# the real /usr/local/bin/qsu -> qdistro-root-exec -> broker path.
# ===========================================================================

QSU=/usr/local/bin/qsu
APPROVALS_DB=/var/lib/qdistro/approvals/approvals.sqlite
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
ADMIN_UID_S58=$(id -u admin)

# sql <db> <query> — read-only query, rows printed '|'-joined (NULL -> '').
sql() {
    python3 - "$1" "$2" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=10)
for row in con.execute(sys.argv[2]):
    print("|".join("" if v is None else str(v) for v in row))
PYEOF
}

# Audit cursor: the max audit id now; later queries look only at id > cursor.
audit_cursor() { sql "$AUDIT_DB" "SELECT COALESCE(MAX(id),0) FROM audit;"; }

# Drop every qsu.exec cache row + deny every pending qsu.exec request, so a
# section starts from "nothing cached, nothing pending". Writes the cache DB
# directly (the GUI scenarios' drain did the same with sqlite3).
qsu_reset() {
    python3 - "$APPROVALS_DB" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
con.execute("DELETE FROM approvals WHERE action LIKE 'qsu.exec:%'")
con.commit()
PYEOF
    runuser -u admin -- python3 -c '
import dbus
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
for r in iface.GetPending():
    if str(r.get("action", "")).startswith("qsu.exec:"):
        try: iface.DecideRequest(int(r["id"]), "deny", "once")
        except Exception: pass
' >/dev/null 2>&1 || true
}

# qsu_start <tag> <user> <argv...> — background `qsu <argv>` as <user>;
# stdout/stderr/rc land in /tmp/s58-<tag>.{out,err,rc}. QSU_ENV (array of
# NAME=VALUE) is applied with env(1) right before exec'ing qsu.
QSU_ENV=()
qsu_start() {
    local tag=$1 user=$2; shift 2
    rm -f "/tmp/s58-$tag.out" "/tmp/s58-$tag.err" "/tmp/s58-$tag.rc"
    ( runuser -u "$user" -- env "${QSU_ENV[@]}" "$QSU" "$@" \
          >"/tmp/s58-$tag.out" 2>"/tmp/s58-$tag.err" </dev/null
      echo $? >"/tmp/s58-$tag.rc.part" && mv "/tmp/s58-$tag.rc.part" "/tmp/s58-$tag.rc" ) &
    register_qsu_pid "$!"
}
qsu_done() { [ -s "/tmp/s58-$1.rc" ]; }
qsu_wait() {  # <tag> [timeout_s]
    local i n=$(( ${2:-30} * 5 ))
    for ((i = 0; i < n; i++)); do qsu_done "$1" && return 0; sleep 0.2; done
    return 1
}
qsu_rc()  { cat "/tmp/s58-$1.rc" 2>/dev/null || echo missing; }
qsu_out() { cat "/tmp/s58-$1.out" 2>/dev/null; }
qsu_err() { cat "/tmp/s58-$1.err" 2>/dev/null; }

# pending_find <uid> <argv-json> [timeout_s] — wait for a pending request
# from <uid> whose details argv is EXACTLY shlex.join(argv). Prints
# "rid|action|target_user", or nothing on timeout.
pending_find() {
    runuser -u admin -- python3 - "$1" "$2" "${3:-15}" <<'PYEOF'
import dbus, json, shlex, sys, time
uid, want, timeout = int(sys.argv[1]), shlex.join(json.loads(sys.argv[2])), float(sys.argv[3])
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    for r in iface.GetPending():
        d = r.get("details", {})
        if int(r["uid"]) == uid and str(d.get("argv", "")) == want:
            print(f'{int(r["id"])}|{r["action"]}|{d.get("target_user", "")}')
            sys.exit(0)
    time.sleep(0.1)
PYEOF
}

# expect_cache_hit <label> <tag> <user> <argv...> — the call must finish
# (rc recorded) without a pending request for its argv ever appearing.
expect_cache_hit() {
    local label=$1 tag=$2 user=$3; shift 3
    local uid argv_json seen="" i
    uid=$(id -u "$user")
    argv_json=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")
    qsu_start "$tag" "$user" "$@"
    for ((i = 0; i < 100; i++)); do
        qsu_done "$tag" && break
        if [ -n "$(pending_find "$uid" "$argv_json" 0.2)" ]; then seen=yes; break; fi
    done
    if [ -n "$seen" ]; then
        fail "$label: re-prompted (pending row for $argv_json) instead of a cache hit"
        return 1
    fi
    if ! qsu_wait "$tag" 20; then
        fail "$label: qsu never finished (no pending row seen either)"
        return 1
    fi
    return 0
}

# expect_prompt <label> <tag> <user> <argv...> — the call must produce a
# pending request with exactly this argv. Leaves the request pending and
# sets PROMPT_RID / PROMPT_ACTION / PROMPT_TARGET.
expect_prompt() {
    local label=$1 tag=$2 user=$3; shift 3
    local uid argv_json row
    uid=$(id -u "$user")
    argv_json=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")
    qsu_start "$tag" "$user" "$@"
    row=$(pending_find "$uid" "$argv_json" 15)
    PROMPT_RID=${row%%|*}; row=${row#*|}
    PROMPT_ACTION=${row%%|*}; PROMPT_TARGET=${row#*|}
    if [ -z "$PROMPT_RID" ]; then
        fail "$label: no pending request for $argv_json (rc=$(qsu_rc "$tag") err=$(qsu_err "$tag"))"
        return 1
    fi
    return 0
}

# deny_prompt <label> <tag> — deny PROMPT_RID; qsu must say "request denied"
# and exit nonzero.
deny_prompt() {
    local label=$1 tag=$2
    decide_as_admin "$PROMPT_RID" deny once 2>/dev/null \
        || { fail "$label: DecideRequest(deny) failed"; return 1; }
    if ! qsu_wait "$tag" 20; then fail "$label: qsu did not exit after deny"; return 1; fi
    if [ "$(qsu_rc "$tag")" != 0 ] && qsu_err "$tag" | grep -q 'request denied'; then
        return 0
    fi
    fail "$label: denied qsu rc=$(qsu_rc "$tag") err=$(qsu_err "$tag")"
    return 1
}

# ---------------------------------------------------------------------------
# pg/44 — forever_argv: exact tuple hits; different / longer argv re-prompts
# ---------------------------------------------------------------------------
qsu_reset
if expect_prompt "pg44 S1" p44a "$WORK_USER" /bin/echo hello \
   && decide_as_admin "$PROMPT_RID" allow forever_argv 2>/dev/null \
   && qsu_wait p44a 20; then
    if [ "$(qsu_rc p44a)" = 0 ] && [ "$(qsu_out p44a)" = hello ]; then
        pass "pg44: qsu /bin/echo hello ran after admin forever_argv"
    else
        fail "pg44: approved echo rc=$(qsu_rc p44a) out=$(qsu_out p44a)"
    fi
fi
ROW=$(sql "$APPROVALS_DB" "SELECT match_kind, scope, argv FROM approvals WHERE action='qsu.exec:root' AND caller_uid=$WORK_UID;")
if [ "$ROW" = 'argv_exact|forever_argv|["/bin/echo", "hello"]' ]; then
    pass "pg44: cache row argv_exact|forever_argv for [/bin/echo, hello]"
else
    fail "pg44: cache row expected argv_exact|forever_argv|[\"/bin/echo\", \"hello\"], got [$ROW]"
fi
if expect_cache_hit "pg44 S3" p44b "$WORK_USER" /bin/echo hello; then
    if [ "$(qsu_rc p44b)" = 0 ] && [ "$(qsu_out p44b)" = hello ]; then
        pass "pg44: same argv cache-hit, no prompt"
    else
        fail "pg44: cache-hit echo rc=$(qsu_rc p44b) out=$(qsu_out p44b)"
    fi
fi
expect_prompt "pg44 S4" p44c "$WORK_USER" /bin/echo hi \
    && deny_prompt "pg44 S4" p44c \
    && pass "pg44: different argv [echo, hi] re-prompted (denied: request denied)"
expect_prompt "pg44 S5" p44d "$WORK_USER" /bin/echo hello world \
    && deny_prompt "pg44 S5" p44d \
    && pass "pg44: longer argv [echo, hello, world] re-prompted (denied: request denied)"

# ---------------------------------------------------------------------------
# pg/45 — forever_basename: same basename hits across paths; perl misses
# ---------------------------------------------------------------------------
qsu_reset
MADE_LOCAL_PY=""
if [ ! -e /usr/local/bin/python3 ]; then
    ln -s /usr/bin/python3 /usr/local/bin/python3 && MADE_LOCAL_PY=1
fi
if expect_prompt "pg45 S1" p45a "$WORK_USER" /usr/bin/python3 -c 'print("py1")' \
   && decide_as_admin "$PROMPT_RID" allow forever_basename 2>/dev/null \
   && qsu_wait p45a 20 && [ "$(qsu_out p45a)" = py1 ]; then
    pass "pg45: python3 ran after admin forever_basename"
else
    fail "pg45: first python3 call rc=$(qsu_rc p45a) out=$(qsu_out p45a)"
fi
ROW=$(sql "$APPROVALS_DB" "SELECT match_kind, match_value, argv, scope FROM approvals WHERE action='qsu.exec:root' AND caller_uid=$WORK_UID;")
if [ "$ROW" = 'basename||python3|forever_basename' ]; then
    pass "pg45: cache row basename||python3|forever_basename"
else
    fail "pg45: cache row expected basename||python3|forever_basename, got [$ROW]"
fi
expect_cache_hit "pg45 S3" p45b "$WORK_USER" /usr/bin/python3 -c 'print("py2")' \
    && [ "$(qsu_out p45b)" = py2 ] && [ "$(qsu_rc p45b)" = 0 ] \
    && pass "pg45: same argv[0], different payload cache-hit"
if [ -x /usr/local/bin/python3 ]; then
    expect_cache_hit "pg45 S4" p45c "$WORK_USER" /usr/local/bin/python3 -c 'print("py3")' \
        && [ "$(qsu_out p45c)" = py3 ] && [ "$(qsu_rc p45c)" = 0 ] \
        && pass "pg45: different path, same basename python3 cache-hit"
else
    fail "pg45: /usr/local/bin/python3 not executable; cannot test the cross-path hit"
fi
expect_prompt "pg45 S5" p45d "$WORK_USER" /usr/bin/perl -e 'print qq(perl1\n)' \
    && deny_prompt "pg45 S5" p45d \
    && pass "pg45: different basename perl re-prompted"
[ -n "$MADE_LOCAL_PY" ] && rm -f /usr/local/bin/python3

# ---------------------------------------------------------------------------
# pg/46 — forever_prefix: list-equality on argv[:2]; trailers hit, restart misses
# ---------------------------------------------------------------------------
qsu_reset
if expect_prompt "pg46 S1" p46a "$WORK_USER" /usr/bin/systemctl status \
   && decide_as_admin "$PROMPT_RID" allow forever_prefix 2>/dev/null \
   && qsu_wait p46a 30 && [ -n "$(qsu_out p46a)$(qsu_err p46a)" ]; then
    pass "pg46: systemctl status ran after admin forever_prefix"
else
    fail "pg46: first systemctl status call rc=$(qsu_rc p46a)"
fi
ROW=$(sql "$APPROVALS_DB" "SELECT match_kind, scope FROM approvals WHERE action='qsu.exec:root' AND caller_uid=$WORK_UID;")
if [ "$ROW" = 'prefix|forever_prefix' ]; then
    pass "pg46: cache row prefix|forever_prefix"
else
    fail "pg46: cache row expected prefix|forever_prefix, got [$ROW]"
fi
expect_cache_hit "pg46 S3" p46b "$WORK_USER" /usr/bin/systemctl status dbus.service \
    && qsu_out p46b | grep -q 'dbus' \
    && pass "pg46: prefix + one trailing arg cache-hit (status output streamed)"
expect_cache_hit "pg46 S4" p46c "$WORK_USER" /usr/bin/systemctl status dbus.service qdistro-admin-broker.service \
    && qsu_out p46c | grep -q 'qdistro-admin-broker' \
    && pass "pg46: prefix + two trailing args cache-hit"
expect_prompt "pg46 S5" p46d "$WORK_USER" /usr/bin/systemctl restart sshd \
    && deny_prompt "pg46 S5" p46d \
    && pass "pg46: different verb [systemctl, restart] re-prompted"

# ---------------------------------------------------------------------------
# pg/49 — ListHistory carries argv losslessly as `as`; cache hit audited
# ---------------------------------------------------------------------------
qsu_reset
if expect_prompt "pg49 S2" p49a "$WORK_USER" /usr/bin/echo "hello world" \
   && decide_as_admin "$PROMPT_RID" allow forever_argv 2>/dev/null \
   && qsu_wait p49a 20 && [ "$(qsu_out p49a)" = "hello world" ]; then
    pass "pg49: qsu echo 'hello world' streamed after forever_argv"
else
    fail "pg49: echo 'hello world' rc=$(qsu_rc p49a) out=$(qsu_out p49a)"
fi
ROW=$(sql "$APPROVALS_DB" "SELECT caller_uid, action, match_kind, scope FROM approvals WHERE action='qsu.exec:root' AND caller_uid=$WORK_UID;")
[ "$ROW" = "$WORK_UID|qsu.exec:root|argv_exact|forever_argv" ] \
    && pass "pg49: cache row $WORK_UID|qsu.exec:root|argv_exact|forever_argv" \
    || fail "pg49: cache row got [$ROW]"
HIST=$(runuser -u admin -- python3 -c '
import dbus, json
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
for r in iface.ListHistory(20):
    if str(r.get("action", "")).startswith("qsu.exec:"):
        print(json.dumps({"action": str(r["action"]), "caller_uid": int(r["caller_uid"]),
              "caller_exe": str(r["caller_exe"]), "decision": bool(r["decision"]),
              "scope": str(r["scope"]), "source": str(r["source"]),
              "argv": [str(a) for a in r["argv"]]}, sort_keys=True))
        break
' 2>&1)
WANT49=$(python3 -c 'import json,sys; print(json.dumps({"action":"qsu.exec:root","caller_uid":int(sys.argv[1]),"caller_exe":"/usr/local/bin/qsu","decision":True,"scope":"forever_argv","source":"prompt","argv":["/usr/bin/echo","hello world"]}, sort_keys=True))' "$WORK_UID")
if [ "$HIST" = "$WANT49" ]; then
    pass "pg49: ListHistory argv is the lossless list [/usr/bin/echo, 'hello world'], caller_exe=/usr/local/bin/qsu, source=prompt"
else
    fail "pg49: ListHistory newest qsu row expected $WANT49, got $HIST"
fi
expect_cache_hit "pg49 S5" p49b "$WORK_USER" /usr/bin/echo "hello world" \
    && [ "$(qsu_out p49b)" = "hello world" ] && pass "pg49: second call cache-hit"
SOURCES=$(runuser -u admin -- python3 -c '
import dbus
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
print(",".join(str(r["source"]) for r in iface.ListHistory(20)
               if str(r.get("action", "")).startswith("qsu.exec:"))[:2])
' 2>&1)
case "$SOURCES" in
    cache,prompt*) pass "pg49: ListHistory newest qsu sources = cache,prompt" ;;
    *) fail "pg49: ListHistory qsu sources expected cache,prompt..., got [$SOURCES]" ;;
esac

# ---------------------------------------------------------------------------
# pg/50 — rule argv_prefix pre-approves real qsu (source=rule + rule_path)
# ---------------------------------------------------------------------------
qsu_reset
RULE50=s58-allow-systemctl-status.yaml
SAVED=$(runuser -u admin -- python3 -c '
import dbus, sys
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
body = """- name: s58-allow-work-systemctl-status
  decision: allow
  match:
    uid: %s
    action: qsu.exec:root
    argv_prefix: ["/usr/bin/systemctl", "status"]
  rationale: s58 pg/50 argv_prefix pre-approval
""" % sys.argv[2]
print(str(iface.SaveRule(sys.argv[1], body)))
' "$RULE50" "$WORK_UID" 2>&1)
if [ "$SAVED" = "/etc/qdistro/rules.d/$RULE50" ] && [ -f "/etc/qdistro/rules.d/$RULE50" ]; then
    pass "pg50: SaveRule wrote /etc/qdistro/rules.d/$RULE50"
else
    fail "pg50: SaveRule returned [$SAVED]"
fi
sleep 1   # rules.d inotify reload is debounced 200 ms
CUR=$(audit_cursor)
expect_cache_hit "pg50 S3" p50a "$WORK_USER" /usr/bin/systemctl status dbus.service \
    && qsu_out p50a | grep -q 'dbus' \
    && pass "pg50: argv_prefix rule allowed systemctl status with no prompt"
ROW=$(sql "$AUDIT_DB" "SELECT decision, COALESCE(scope,''), source, rule_path FROM audit WHERE id>$CUR AND action='qsu.exec:root' ORDER BY id DESC LIMIT 1;")
[ "$ROW" = "1||rule|/etc/qdistro/rules.d/$RULE50" ] \
    && pass "pg50: audit row 1||rule|/etc/qdistro/rules.d/$RULE50" \
    || fail "pg50: audit row expected 1||rule|/etc/qdistro/rules.d/$RULE50, got [$ROW]"
expect_prompt "pg50 S5" p50b "$WORK_USER" /usr/bin/systemctl restart sshd \
    && deny_prompt "pg50 S5" p50b \
    && pass "pg50: systemctl restart not matched by the rule (prompted, denied)"
ROW=$(sql "$AUDIT_DB" "SELECT decision, source FROM audit WHERE id>$CUR AND action='qsu.exec:root' ORDER BY id DESC LIMIT 2;" | paste -sd, -)
[ "$ROW" = "0|prompt,1|rule" ] \
    && pass "pg50: newest audit rows 0|prompt,1|rule" \
    || fail "pg50: newest audit rows expected 0|prompt,1|rule, got [$ROW]"
runuser -u admin -- python3 -c '
import dbus, sys
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
iface.DeleteRule(sys.argv[1], "s58-allow-work-systemctl-status")
' "/etc/qdistro/rules.d/$RULE50" >/dev/null 2>&1 || rm -f "/etc/qdistro/rules.d/$RULE50"

# ---------------------------------------------------------------------------
# pg/51 — `qsu -u <user>`: target_user is part of the action key
# ---------------------------------------------------------------------------
qsu_reset
CUR=$(audit_cursor)
if expect_prompt "pg51 S1" p51a admin -u "$WORK_USER" /usr/bin/id; then
    if [ "$PROMPT_ACTION" = "qsu.exec:$WORK_USER" ] && [ "$PROMPT_TARGET" = "$WORK_USER" ]; then
        pass "pg51: qsu -u $WORK_USER pends as action qsu.exec:$WORK_USER"
    else
        fail "pg51: expected action qsu.exec:$WORK_USER target $WORK_USER, got $PROMPT_ACTION / $PROMPT_TARGET"
    fi
    decide_as_admin "$PROMPT_RID" allow forever_argv 2>/dev/null || fail "pg51: DecideRequest(allow, forever_argv) failed"
    if qsu_wait p51a 20 && qsu_out p51a | grep -q "^uid=$WORK_UID($WORK_USER) gid=$(id -g "$WORK_USER")("; then
        pass "pg51: id ran as $WORK_USER (uid=$WORK_UID), not root"
    else
        fail "pg51: id output [$(qsu_out p51a)] rc=$(qsu_rc p51a)"
    fi
fi
ROW=$(sql "$APPROVALS_DB" "SELECT caller_uid, action, scope FROM approvals WHERE action LIKE 'qsu.exec:%';")
[ "$ROW" = "$ADMIN_UID_S58|qsu.exec:$WORK_USER|forever_argv" ] \
    && pass "pg51: cache row $ADMIN_UID_S58|qsu.exec:$WORK_USER|forever_argv" \
    || fail "pg51: cache rows expected $ADMIN_UID_S58|qsu.exec:$WORK_USER|forever_argv, got [$ROW]"
if expect_prompt "pg51 S3" p51b admin -u root /usr/bin/id; then
    if [ "$PROMPT_ACTION" = qsu.exec:root ] && [ "$PROMPT_TARGET" = root ]; then
        pass "pg51: same argv as root re-prompted as qsu.exec:root (no cross-target cache hit)"
    else
        fail "pg51: root request pended as $PROMPT_ACTION / $PROMPT_TARGET"
    fi
    deny_prompt "pg51 S4" p51b && pass "pg51: root request denied (request denied)"
else
    qsu_wait p51b 5
    fail "pg51: qsu -u root /usr/bin/id did NOT prompt (out=$(qsu_out p51b)) — cross-target cache hit?"
fi
ROW=$(sql "$AUDIT_DB" "SELECT action, decision FROM audit WHERE id>$CUR AND action LIKE 'qsu.exec:%' ORDER BY id DESC LIMIT 2;" | paste -sd, -)
[ "$ROW" = "qsu.exec:root|0,qsu.exec:$WORK_USER|1" ] \
    && pass "pg51: audit rows keyed by distinct actions (root|0, $WORK_USER|1)" \
    || fail "pg51: audit rows expected qsu.exec:root|0,qsu.exec:$WORK_USER|1, got [$ROW]"

# ---------------------------------------------------------------------------
# pg/54 — the privileged child gets the fixed sanitized env
# ---------------------------------------------------------------------------
qsu_reset
QSU_ENV=(LD_PRELOAD=/tmp/evil.so PYTHONPATH=/tmp/poison LD_LIBRARY_PATH=/tmp/lib-evil PATH=/tmp/evilbin:/bin)
expect_prompt "pg54 S1" p54 "$WORK_USER" /usr/bin/env \
    && decide_as_admin "$PROMPT_RID" allow once 2>/dev/null
QSU_ENV=()
if qsu_wait p54 20 && [ "$(qsu_rc p54)" = 0 ]; then
    ENV_OUT=$(qsu_out p54)
    missing=""
    for want in HOME=/root LOGNAME=root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin TERM=xterm USER=root; do
        printf '%s\n' "$ENV_OUT" | grep -qxF "$want" || missing="$missing $want"
    done
    leaked=$(printf '%s\n' "$ENV_OUT" | grep -E '^(LD_PRELOAD|LD_LIBRARY_PATH|PYTHONPATH)=|^PATH=/tmp/' || true)
    if [ -z "$missing" ] && [ -z "$leaked" ]; then
        pass "pg54: privileged env is the fixed baseline (no LD_PRELOAD/LD_LIBRARY_PATH/PYTHONPATH, PATH reset, USER/LOGNAME/HOME root)"
    else
        fail "pg54: env missing=[$missing] leaked=[$leaked] full=[$ENV_OUT]"
    fi
else
    fail "pg54: qsu /usr/bin/env rc=$(qsu_rc p54) err=$(qsu_err p54)"
fi

# ---------------------------------------------------------------------------
# pg/52 — control-char target_user rejected by root-exec before the broker
# ---------------------------------------------------------------------------
qsu_reset
CUR=$(audit_cursor)
MON_LOG=/tmp/s58-52-dbusmon.log
rm -f "$MON_LOG"
dbus-monitor --system "interface='org.qdistro.AdminBroker1'" \
    "type='signal',interface='org.qdistro.QciProbe',member='Ready'" >"$MON_LOG" 2>&1 </dev/null &
MON_PID=$!
TOK=ready-$$-$RANDOM
for _ in $(seq 1 100); do
    dbus-send --system --type=signal /org/qdistro/QciProbe org.qdistro.QciProbe.Ready "string:$TOK" 2>/dev/null
    grep -q "$TOK" "$MON_LOG" 2>/dev/null && break
    sleep 0.1
done
dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.GetPending >/dev/null 2>&1
for _ in $(seq 1 50); do grep -q 'member=GetPending' "$MON_LOG" 2>/dev/null && break; sleep 0.1; done
if grep -q "$TOK" "$MON_LOG" && grep -q 'member=GetPending' "$MON_LOG"; then
    MON_OK=1
    pass "pg52: root dbus-monitor live and sees unicast broker calls (positive control)"
else
    MON_OK=0
    fail "pg52: dbus-monitor positive control failed — the zero count below would be vacuous"
fi
EVIL=$(runuser -u "$WORK_USER" -- python3 -c '
import json, socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/run/qdistro-root-exec/sock")
s.sendall((json.dumps({"target_user": "root\n[OK] audit row trailer\x1b[2J",
                       "argv": ["/bin/echo", "s58-52-must-not-run"]}) + "\n").encode())
s.settimeout(5.0)
buf = b""
try:
    while True:
        c = s.recv(4096)
        if not c: break
        buf += c
except socket.timeout:
    pass
print(buf.decode(errors="replace"))
' 2>&1)
sleep 1
kill "$MON_PID" 2>/dev/null; wait "$MON_PID" 2>/dev/null
FRAMES=$(printf '%s\n' "$EVIL" | python3 -c '
import json, sys
types = []
ok_err = False
code = None
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    f = json.loads(line)
    types.append(f.get("type"))
    if f.get("type") == "error" and "invalid target_user" in f.get("message", ""): ok_err = True
    if f.get("type") == "exit": code = f.get("code")
print("error_ok" if ok_err else "error_missing", "exit=%s" % code, "types=" + ",".join(types))
' 2>&1)
if [ "$FRAMES" = "error_ok exit=1 types=error,exit" ]; then
    pass "pg52: root-exec answered error 'invalid target_user' + exit 1, no stdout/stderr frames"
else
    fail "pg52: unexpected reply frames [$FRAMES] raw=[$EVIL]"
fi
RPA=$(grep -c 'RequestPermissionAs' "$MON_LOG"); QX=$(grep -c 'qsu.exec:' "$MON_LOG")
if [ "$MON_OK" = 1 ] && [ "${RPA:-0}" = 0 ] && [ "${QX:-0}" = 0 ]; then
    pass "pg52: broker saw zero RequestPermissionAs / qsu.exec traffic"
else
    fail "pg52: broker traffic RequestPermissionAs=$RPA qsu.exec=$QX (monitor_ok=$MON_OK)"
fi
N=$(sql "$AUDIT_DB" "SELECT COUNT(*) FROM audit WHERE id>$CUR AND action LIKE 'qsu.exec:%';")
[ "$N" = 0 ] && pass "pg52: no qsu.exec audit row written" \
    || fail "pg52: $N qsu.exec audit row(s) written for the malicious target"

# ---------------------------------------------------------------------------
# pg/53 — per-uid in-flight cap: exactly one of 5 concurrent calls rejected
# (last: it leaves 4 handlers blocked until the drain below).
# ---------------------------------------------------------------------------
qsu_reset
for i in 1 2 3 4 5; do qsu_start "p53-$i" "$WORK_USER" /bin/sleep 60 "$i"; done
sleep 5
REJ=""; REJ_N=0
for i in 1 2 3 4 5; do
    if grep -q 'too many in-flight qsu requests for uid='"$WORK_UID" "/tmp/s58-p53-$i.err" 2>/dev/null; then
        REJ="$REJ $i"; REJ_N=$((REJ_N + 1))
    fi
done
if [ "$REJ_N" = 1 ]; then
    pass "pg53: exactly one of 5 concurrent qsu calls rejected (#${REJ# }) with the in-flight error"
else
    fail "pg53: expected exactly one in-flight rejection, got $REJ_N [$REJ]"
fi
if [ "$REJ_N" = 1 ]; then
    r=${REJ# }
    if qsu_done "p53-$r" && [ "$(qsu_rc "p53-$r")" != 0 ]; then
        pass "pg53: rejected qsu exited promptly with rc=$(qsu_rc "p53-$r")"
    else
        fail "pg53: rejected qsu still running or rc=$(qsu_rc "p53-$r") 5 s after launch"
    fi
    PEND=$(runuser -u admin -- python3 -c '
import dbus, sys
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
rows = [str(r.get("details", {}).get("argv", "")) for r in iface.GetPending()
        if int(r["uid"]) == int(sys.argv[1]) and str(r.get("action", "")).startswith("qsu.exec:")]
print(len(rows), "rejected_pending" if ("/bin/sleep 60 " + sys.argv[2]) in rows else "rejected_absent")
' "$WORK_UID" "$r" 2>&1)
    case "$PEND" in
        [1-4]" rejected_absent") pass "pg53: $PEND — 1..4 pending, the rejected argv never reached the broker" ;;
        *) fail "pg53: pending snapshot [$PEND]" ;;
    esac
fi
# Drain: deny every qsu request until none is pending for 3 s, then make
# sure every client has exited (their handlers release the in-flight slot).
quiet=0
for _ in $(seq 1 60); do
    n=$(runuser -u admin -- python3 -c '
import dbus
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
n = 0
for r in iface.GetPending():
    if str(r.get("action", "")).startswith("qsu.exec:"):
        n += 1
        try: iface.DecideRequest(int(r["id"]), "deny", "once")
        except Exception: pass
print(n)
' 2>/dev/null)
    if [ "${n:-1}" = 0 ]; then quiet=$((quiet + 1)); else quiet=0; fi
    [ "$quiet" -ge 6 ] && break
    sleep 0.5
done
for i in 1 2 3 4 5; do qsu_wait "p53-$i" 20 || fail "pg53 drain: client $i still running"; done

qsu_reset

# Final cleanup is in trap; emit summary.
if [ "$FAILCOUNT" -eq 0 ]; then
    pass "s58 — qsu real-flow argv-aware cache + re-prompt end-to-end"
    echo "[s58] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s58] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
