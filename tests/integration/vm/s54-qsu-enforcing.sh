#!/bin/bash
# In-VM driver for qsu-enforcing.bats (`# qci:enforcing`: the bats gate runs
# it on a clone of the per-run golden booted SELinux=enforcing, over SSH).
#
# Headless replacement for the retired GUI scenario
# tests/integration/permissions-gui/55-qsu-selinux-enforcing.md (and its host
# runner run-55). It never needed the GUI. Under Enforcing it runs the real
# `qsu /usr/bin/id` as the non-admin user twice -- approved `once`, then
# approved `forever_argv` -- each found over D-Bus by the admin uid (read-only
# GetPending) and approved by root (the broker refuses DecideRequest from
# admin-uid inline Python), and asserts
#   (A) both invocations completed with rc 0 and ran the command as root
#       (qsu targets root by default; the requester is the work user):
#       `uid=0(root)`;
#   (B) the `forever_argv` decision was APPLIED (not `applied-uncached`) and
#       wrote the matching approval row: caller uid = work, action
#       qsu.exec:root, scope forever_argv, an argv-exact match whose captured
#       argv is exactly the one `id` command, approver uid 0;
#   (C) zero new AVC / USER_AVC denials with a qdistro source context
#       (qdistro_root_exec_t / qdistro_tier1_t / qdistro_broker_t /
#       qdistro_pwd_t / qsu_child_t) since a baseline taken before the first
#       request -- where a failed audit query is a FAILURE, never "zero";
#   (D) the SELinux mode is restored to what it was at start (Permissive when
#       the driver flipped a permissive VM; Enforcing on an enforcing boot),
#       and the added approval row is removed.
#
# TRANSPORT: qemu-guest-agent's domain cannot setenforce, so this needs SSH
# (the `# qci:enforcing` routing, or run-bats-enforcing.sh). It prints
# "SKIP:" only when SELinux is Disabled or the runtime flip to Enforcing did
# not take; the enforcing bats file treats any SKIP as a failure.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SE_MODE_INITIAL=$(/usr/sbin/getenforce 2>/dev/null || echo Disabled)
[ "$SE_MODE_INITIAL" = Disabled ] && skip "SELinux is Disabled"
[ -x /usr/local/bin/qsu ] || { fail "/usr/local/bin/qsu absent"; exit 1; }

WORK_USER=work
if ! getent passwd "$WORK_USER" >/dev/null 2>&1; then
    useradd -m -U -s /bin/bash "$WORK_USER" \
        || { fail "could not create $WORK_USER"; exit 1; }
fi
WORK_UID=$(id -u "$WORK_USER")
APPROVALS=/var/lib/qdistro/approvals/approvals.sqlite
QSU_PGID=""

drain_qsu_cache() {
    python3 - "$APPROVALS" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
con.execute("DELETE FROM approvals WHERE action LIKE 'qsu.exec:%'")
con.commit()
PYEOF
}

restore() {
    [ -n "$QSU_PGID" ] && kill -KILL -- "-$QSU_PGID" 2>/dev/null
    drain_qsu_cache 2>/dev/null
    case "$SE_MODE_INITIAL" in
        Permissive) /usr/sbin/setenforce 0 2>/dev/null ;;
        Enforcing) /usr/sbin/setenforce 1 2>/dev/null ;;
    esac
}
trap restore EXIT INT TERM

# Drain qsu state so the requests below are the only ones.
drain_qsu_cache || { fail "could not drain qsu approvals in $APPROVALS"; exit 1; }

BASELINE_TS=$(( $(date +%s) - 1 ))
/usr/sbin/setenforce 1 2>/dev/null
SE_MODE=$(/usr/sbin/getenforce 2>/dev/null || echo Unknown)
if [ "$SE_MODE" = Enforcing ]; then
    pass "SELinux mode now Enforcing (was $SE_MODE_INITIAL)"
else
    skip "setenforce 1 left mode at $SE_MODE (the runtime flip did not take; /etc/selinux/config: $(grep -E '^SELINUX=' /etc/selinux/config 2>/dev/null))"
fi

systemctl restart qdistro-admin-broker.service 2>/dev/null || true
systemctl restart qdistro-root-exec.socket 2>/dev/null || true
for _ in $(seq 1 40); do
    dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 && break
    sleep 0.25
done

# run_leg <label> <scope> — one qsu request as the work user, found by the
# admin uid, decided by root with <scope>. Bounded: the request must reach
# GetPending within 20 s and qsu must finish within 60 s of the decision; on a
# timeout its whole process group is killed and the leg fails.
run_leg() {
    local label=$1 scope=$2 out=/tmp/s54-$1.out rc_file=/tmp/s54-$1.rc rid decide
    rm -f "$out" "$rc_file"
    setsid bash -c 'runuser -u "$1" -- /usr/local/bin/qsu /usr/bin/id >"$2" 2>&1 </dev/null; echo $? >"$3"' \
        _ "$WORK_USER" "$out" "$rc_file" &
    QSU_PGID=$!
    rid=$(runuser -u admin -- python3 -c '
import dbus, sys, time
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
uid = int(sys.argv[1])
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    for r in iface.GetPending():
        if int(r["uid"]) == uid and str(r.get("action", "")) == "qsu.exec:root":
            print(int(r["id"]))
            sys.exit(0)
    time.sleep(0.2)
print("no qsu pending row")
' "$WORK_UID" 2>&1)
    case "$rid" in
        ''|*[!0-9]*) decide="admin GetPending: $rid" ;;
        *) decide=$(python3 -c '
import dbus, sys
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
r = iface.DecideRequest(int(sys.argv[1]), "allow", sys.argv[2])
print("decided rid=%d scope=%s result=%s" % (int(sys.argv[1]), sys.argv[2], r))
' "$rid" "$scope" 2>&1) ;;
    esac
    case "$decide" in
        "decided rid="*" result=applied")
            pass "$label: qsu request found by admin and approved by root over D-Bus under Enforcing ($decide)" ;;
        *) fail "$label: D-Bus approve (admin GetPending, root DecideRequest $scope) did not apply under Enforcing: $decide" ;;
    esac
    for _ in $(seq 1 300); do [ -s "$rc_file" ] && break; sleep 0.2; done
    if [ ! -s "$rc_file" ]; then
        kill -KILL -- "-$QSU_PGID" 2>/dev/null
        QSU_PGID=""
        fail "$label: qsu /usr/bin/id did not finish within 60 s (process group killed); out=$(cat "$out" 2>/dev/null)"
        return
    fi
    QSU_PGID=""
    if [ "$(cat "$rc_file")" = 0 ] && grep -q "uid=0(root)" "$out"; then
        pass "$label: qsu /usr/bin/id ran under Enforcing (rc=0, uid=0(root))"
    else
        fail "$label: qsu /usr/bin/id under Enforcing: rc=$(cat "$rc_file") out=$(cat "$out" 2>/dev/null)"
    fi
}

run_leg once once
run_leg forever_argv forever_argv

ROW=$(python3 - "$APPROVALS" "$WORK_UID" <<'PYEOF'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
rows = con.execute(
    "SELECT caller_uid, action, scope, match_kind, argv, decision, approver_uid "
    "FROM approvals WHERE action='qsu.exec:root' AND scope='forever_argv'").fetchall()
if len(rows) != 1:
    print("rows=%r" % (rows,)); sys.exit(1)
uid, action, scope, kind, argv, decision, approver = rows[0]
try:
    argv_list = json.loads(argv)
except ValueError:
    argv_list = None
# The captured argv is the one command run: [<path to id>] (qsu may record
# the resolved path), nothing more.
ok = (uid == int(sys.argv[2]) and kind == "argv_exact" and decision == 1
      and approver == 0 and isinstance(argv_list, list) and len(argv_list) == 1
      and argv_list[0].rsplit("/", 1)[-1] == "id")
print("caller_uid=%s action=%s scope=%s match_kind=%s argv=%s decision=%s approver_uid=%s"
      % (uid, action, scope, kind, argv, decision, approver))
sys.exit(0 if ok else 1)
PYEOF
)
if [ $? -eq 0 ]; then
    pass "forever_argv approval row written ($ROW)"
else
    fail "forever_argv approval row missing or wrong: $ROW"
fi

sleep 1   # let auditd flush
AVCS=/tmp/s54-avcs.txt
AU_ERR=/tmp/s54-ausearch.err
: >"$AVCS"
AU_RC=0
ausearch -m AVC,USER_AVC --start "$(date -d @"$BASELINE_TS" '+%x %T')" \
    >/tmp/s54-ausearch.out 2>"$AU_ERR" || AU_RC=$?
# ausearch exits 1 with "<no matches>" when there is nothing; any other
# failure (no audit log, no permission, bad time) proves nothing.
if [ "$AU_RC" -eq 0 ] || { [ "$AU_RC" -eq 1 ] && grep -q '<no matches>' "$AU_ERR"; }; then
    grep -E 'scontext=[^ ]*:(qdistro_root_exec_t|qdistro_tier1_t|qdistro_broker_t|qdistro_pwd_t|qsu_child_t)' \
        /tmp/s54-ausearch.out >"$AVCS" || true
    N=$(wc -l <"$AVCS")
    if [ "$N" -eq 0 ]; then
        pass "0 new denials — qsu policy covers the enforcing qsu flow"
    else
        cat "$AVCS"
        command -v audit2allow >/dev/null 2>&1 && audit2allow -i "$AVCS"
        fail "$N new qdistro-domain AVCs for the qsu flow under Enforcing"
    fi
else
    fail "audit query failed (ausearch rc=$AU_RC: $(head -c 300 "$AU_ERR")); zero denials NOT established"
fi

trap - EXIT INT TERM
restore
SE_MODE_FINAL=$(/usr/sbin/getenforce 2>/dev/null || echo Unknown)
if [ "$SE_MODE_FINAL" = "$SE_MODE_INITIAL" ]; then
    pass "SELinux mode restored to $SE_MODE_INITIAL"
else
    fail "SELinux mode is $SE_MODE_FINAL after the run, not the initial $SE_MODE_INITIAL"
fi
LEFT=$(python3 - "$APPROVALS" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], timeout=10)
print(con.execute("SELECT count(*) FROM approvals WHERE action LIKE 'qsu.exec:%'").fetchone()[0])
PYEOF
)
[ "$LEFT" = 0 ] && pass "added qsu approval rows removed" \
    || fail "$LEFT qsu approval rows left behind"

if [ "$FAILCOUNT" -eq 0 ]; then
    echo "[s54] $PASSCOUNT passes, 0 failures"; exit 0
fi
echo "[s54] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1
