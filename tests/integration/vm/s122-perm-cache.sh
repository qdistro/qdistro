#!/bin/bash
# s122-perm-cache — headless replacement for permissions-gui 07, 23, 30, 31,
# 32, 33 (approval cache, revoke, rate limit, fire-and-forget, expiry/GC).
# Runs INSIDE the test VM as root, staged by permissions-headless.bats.
#
#   bash s122-perm-cache.sh pg07|pg23|pg30|pg31|pg32|pg33
#
# Where a GUI scenario's only GUI step was "admin presses Approve/Deny in the
# admin app", the same decision is made here with DecideRequest as the admin
# uid — the identical broker call the app makes.

. "$(dirname "$0")/s120-perm-lib.sh"

WORKU=$(ensure_uid_user work 2000)

# --- pg07: qdistro-approvals CLI round trip ---------------------------------
# The CLI is a root admin-control helper (the broker trusts its exe path); it
# is not in the shipped image, but RevokeApproval/RunAuditGc behind it are.
pg07() {
    local cli=/usr/local/sbin/qdistro-approvals a1="test.action.$TAG" \
          a2="other.action.$TAG" a3="net.restart.$TAG" out rc tid
    [ -x "$cli" ] || die "pg07: $cli not installed in this VM"
    python3 - "$a1" "$a2" "$a3" <<'PY' || die "pg07: seeding failed"
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
from qdistro_admin_audit import AuditLog
a1, a2, a3 = sys.argv[1:4]
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
assert c.store(2000, a1, "/usr/bin/python3.13", "1h", True, 1000)
assert c.store(2000, a2, "/usr/bin/curl", "24h", True, 1000)
assert c.store(3000, a3, "", "forever", True, 1000)
a = AuditLog("/var/lib/qdistro/audit/audit.sqlite")
a.log(caller_uid=2000, caller_pid=111, caller_exe="/usr/bin/python3.13",
      action=a1, decision=True, scope="1h", source="prompt", approver_uid=1000)
a.log(caller_uid=3000, caller_pid=222, caller_exe="/usr/bin/sudo",
      action=a3, decision=False, scope=None, source="prompt", approver_uid=1000)
PY
    out=$("$cli" list 2>&1); rc=$?
    expect_eq "pg07: 'list' exits 0" "$rc" 0
    check "pg07: 'list' shows all three seeded actions" \
        bash -c 'grep -q "action=$1\$" <<<"$4" && grep -q "action=$2\$" <<<"$4" && grep -q "action=$3\$" <<<"$4"' _ "$a1" "$a2" "$a3" "$out"
    check "pg07: 'list' shows the exe_only and always match labels" \
        bash -c 'grep -q "exe_only" <<<"$1" && grep -q "always" <<<"$1"' _ "$out"
    out=$("$cli" audit --limit 50 2>&1); rc=$?
    expect_eq "pg07: 'audit --limit 50' exits 0" "$rc" 0
    check "pg07: 'audit' shows the allow row for $a1 and the deny row for $a3" \
        bash -c 'grep -E "allow .* $1\$" <<<"$3" >/dev/null && grep -E "deny .* $2\$" <<<"$3" >/dev/null' _ "$a1" "$a3" "$out"
    tid=$(sql_approvals "SELECT id FROM approvals WHERE action='$a1'")
    out=$("$cli" revoke "$tid" 2>&1); rc=$?
    expect_eq "pg07: 'revoke <id>' exits 0 and reports it" "$rc|$out" "0|revoked approval id=$tid"
    expect_eq "pg07: only the revoked row is gone" \
        "$(sql_approvals "SELECT group_concat(action, ',') FROM (SELECT action FROM approvals WHERE action IN ('$a1','$a2','$a3') ORDER BY id)")" \
        "$a2,$a3"
    expect_eq "pg07: revoke audited as source=revoke, approver_uid=0 (root CLI)" \
        "$(sql_audit "SELECT source||'|'||approver_uid FROM audit WHERE action='$a1' ORDER BY id DESC LIMIT 1")" \
        "revoke|0"
    out=$("$cli" revoke 99999999 2>&1); rc=$?
    expect_eq "pg07: revoke of a missing id exits 1" "$rc" 1
    check "pg07: missing-id revoke says 'no cached approval with id=99999999'" \
        grep -q "no cached approval with id=99999999" <<<"$out"
    # Age ONLY this case's rows (the original aged the whole table on a VM of
    # its own); a fresh control row must survive the GC.
    local ctl="control.action.$TAG"
    python3 - "$ctl" <<'PY'
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_audit import AuditLog
AuditLog("/var/lib/qdistro/audit/audit.sqlite").log(
    caller_uid=2000, caller_pid=1, caller_exe="/x", action=sys.argv[1],
    decision=True, scope=None, source="prompt", approver_uid=1000)
PY
    sql_audit "UPDATE audit SET ts = ts - 172800 WHERE action IN ('$a1','$a2','$a3')"
    out=$("$cli" audit-gc --retention-days 1 2>&1); rc=$?
    expect_eq "pg07: 'audit-gc --retention-days 1' exits 0" "$rc" 0
    local n
    n=$(sed -n 's/^audit-gc: deleted \([0-9]*\) row(s) older than 1d$/\1/p' <<<"$out")
    if [ -n "$n" ] && [ "$n" -ge 3 ]; then
        pass "pg07: audit-gc reports deleted N>=3 rows (N=$n)"
    else
        fail "pg07: audit-gc output unexpected: $out"
    fi
    expect_eq "pg07: the aged rows are gone" \
        "$(sql_audit "SELECT count(*) FROM audit WHERE action IN ('$a1','$a2','$a3')")" 0
    expect_eq "pg07: a fresh row survives the GC" \
        "$(sql_audit "SELECT count(*) FROM audit WHERE action='$ctl'")" 1
    sql_approvals "DELETE FROM approvals WHERE action IN ('$a2','$a3')"
}

# --- pg23: RevokeAllForUid: one signal per row, scoped to the uid -----------
pg23() {
    # Dedicated uids (no account needed: cache rows are keyed by uid) so other
    # cases' rows for 2000/3000 cannot change the counts.
    local u1=2923 u2=3923 log="$WORK/23-signals.log" out
    sql_approvals "DELETE FROM approvals WHERE caller_uid IN ($u1,$u2)"
    python3 - "$u1" "$u2" "$TAG" <<'PY' || die "pg23: seeding failed"
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
u1, u2, t = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
assert c.store(u1, f"act.a.{t}", "/usr/bin/python3.13", "forever_exe", True, 1000)
assert c.store(u1, f"act.b.{t}", "/usr/bin/curl", "24h", True, 1000)
assert c.store(u1, f"act.c.{t}", "", "forever", True, 1000)
assert c.store(u2, f"act.x.{t}", "/usr/bin/vim", "forever_exe", True, 1000)
assert c.store(u2, f"act.y.{t}", "", "forever", True, 1000)
PY
    expect_eq "pg23: five rows seeded across two uids" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE caller_uid IN ($u1,$u2)")" 5
    start_signal_monitor ApprovalRevoked "$log" \
        || die "pg23: dbus-monitor never proved its ApprovalRevoked subscription"
    out=$(dsend_as admin RevokeAllForUid int32:$u1)
    check "pg23: RevokeAllForUid($u1) as admin returns int32 3" \
        grep -q "int32 3$" <<<"$out"
    sleep 1
    stop_signal_monitor "$log"
    local sig
    sig=$(python3 - "$log" <<'PY'
import sys, re
blocks = re.split(r"\n(?=signal )", open(sys.argv[1]).read())
out = []
for b in blocks:
    if "member=ApprovalRevoked" not in b:
        continue
    ints = re.findall(r"int32 (-?\d+)", b)
    strs = re.findall(r'string "([^"]*)"', b)
    out.append(f"{ints[0] if ints else '?'}:{strs[0] if strs else '?'}")
print(" ".join(sorted(out)))
PY
)
    expect_eq "pg23: exactly three ApprovalRevoked signals, all uid $u1, actions a/b/c" \
        "$sig" "$u1:act.a.$TAG $u1:act.b.$TAG $u1:act.c.$TAG"
    expect_eq "pg23: uid $u2 rows survive, no uid $u1 row left" \
        "$(sql_approvals "SELECT group_concat(caller_uid||'|'||action, ' ') FROM (SELECT caller_uid, action FROM approvals WHERE caller_uid IN ($u1,$u2) ORDER BY caller_uid, action)")" \
        "$u2|act.x.$TAG $u2|act.y.$TAG"
    expect_eq "pg23: three source=revoke audit rows, uid $u1, approver 1000" \
        "$(sql_audit "SELECT group_concat(r, ' ') FROM (SELECT caller_uid||'|'||action||'|'||approver_uid AS r FROM audit WHERE source='revoke' AND action LIKE 'act._.$TAG' ORDER BY action)")" \
        "$u1|act.a.$TAG|1000 $u1|act.b.$TAG|1000 $u1|act.c.$TAG|1000"
    sql_approvals "DELETE FROM approvals WHERE caller_uid IN ($u1,$u2)"
}

# --- pg30: rate limit: 50 ok, 51st .RateLimited, per action, recovers --------
pg30() {
    local act="test.action.$TAG" out
    out=$(runuser -u "$WORKU" -- python3 - "$act" <<'PY'
import sys, dbus, time
act = sys.argv[1]
p = dbus.SystemBus().get_object("org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1")
def chk(a, i):
    return str(p.CheckPermission(a, {"i": str(i)}, dbus_interface="org.qdistro.AdminBroker1"))
ok = 0; name = msg = None
for i in range(60):
    try:
        v = chk(act, i)
        if v != "unknown":
            print(f"unexpected={v}"); break
        ok += 1
    except dbus.DBusException as e:
        name, msg = e.get_dbus_name(), e.get_dbus_message(); break
print(f"ok_before_raise={ok}")
print(f"err_name={name}")
print(f"err_msg={msg}")
print("other=" + chk(act + ".other", 0))
time.sleep(2)
try:
    print("postwindow=" + chk(act, "post"))
except dbus.DBusException as e:
    print("postwindow=ERR " + e.get_dbus_name())
PY
)
    note "$out"
    check "pg30: exactly 50 CheckPermission calls answered 'unknown' before the limiter fired" \
        grep -qx "ok_before_raise=50" <<<"$out"
    check "pg30: the 51st call raised org.qdistro.AdminBroker1.RateLimited" \
        grep -qx "err_name=org.qdistro.AdminBroker1.RateLimited" <<<"$out"
    check "pg30: the error names uid=2000, the action and >50/1.0s" \
        bash -c 'l=$(grep "^err_msg=" <<<"$2"); [[ $l == *"uid=2000"* && $l == *"'"'"'$1'"'"'"* && $l == *">50/1.0s"* ]]' _ "$act" "$out"
    check "pg30: a different action under the same uid is not limited" \
        grep -qx "other=unknown" <<<"$out"
    check "pg30: after the 1s window the original action answers again" \
        grep -qx "postwindow=unknown" <<<"$out"
}

# --- pg31: fire-and-forget RequestPermission (no waiter) ---------------------
pg31() {
    local act="test.action.$TAG" log="$WORK/31-decided.log" out rid r
    out=$(dsend_as "$WORKU" RequestPermission string:"$act" dict:string:string:"purpose","fire-and-forget-31")
    rid=$(awk '$1=="int32"{print $2; exit}' <<<"$out")
    if [ -n "$rid" ] && [ "$rid" -gt 0 ] && grep -q "^method return" <<<"$out"; then
        pass "pg31: RequestPermission returned immediately with id $rid"
    else
        die "pg31: RequestPermission did not return an id: $out"
    fi
    local row
    row=$(pending_row_json "$act")
    check "pg31: GetPending shows id $rid, uid 2000" \
        python3 -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if str(r["id"])==sys.argv[2] and r["uid"]==2000 else 1)' "$row" "$rid"
    sleep 10   # the behaviour under test: no waiter, no GC for >= 10 s
    expect_eq "pg31: still pending after 10 s with no waiter" "$(pending_ids_for "$act")" "$rid"
    start_signal_monitor RequestDecided "$log" \
        || die "pg31: dbus-monitor never proved its RequestDecided subscription"
    expect_eq "pg31: monitor up BEFORE the approval (request still pending)" \
        "$(pending_ids_for "$act")" "$rid"
    r=$(broker_call_as admin DecideRequest "[$rid, \"allow\", \"1h\"]")
    expect_eq "pg31: admin DecideRequest(allow, 1h)" "$r" 'OK "applied"'
    sleep 1
    stop_signal_monitor "$log"
    check "pg31: one RequestDecided signal carrying (int32 $rid, \"allow\")" \
        python3 - "$log" "$rid" <<'PY'
import sys, re
blocks = [b for b in re.split(r"\n(?=signal )", open(sys.argv[1]).read())
          if "member=RequestDecided" in b]
hits = [b for b in blocks if re.search(r"int32 %s\b" % sys.argv[2], b)
        and 'string "allow"' in b]
sys.exit(0 if len(hits) == 1 else 1)
PY
    expect_eq "pg31: the cache row was written with no waiter (1h scope)" \
        "$(sql_approvals "SELECT count(*)||'|'||max(scope) FROM approvals WHERE action='$act' AND caller_uid=2000")" "1|1h"
    expect_eq "pg31: pending list drained" "$(pending_ids_for "$act")" ""
    sql_approvals "DELETE FROM approvals WHERE action='$act'"
}

# --- pg32: forever_exe scope is exe-bound ------------------------------------
pg32() {
    local act="test.action.$TAG" id r out rc pyexe
    install_test_permission
    ( run_test_permission "$WORKU" "$act" >"$WORK/32-py1.out" 2>&1; echo $? >"$WORK/32-py1.rc" ) &
    local p1=$!
    wait_pending "$act" || die "pg32: python request never reached GetPending"
    id=$(pending_ids_for "$act")
    pyexe=$(pending_row_json "$act" | python3 -c 'import json,sys; print(json.load(sys.stdin)["exe"])')
    r=$(broker_call_as admin DecideRequest "[$id, \"allow\", \"forever_exe\"]")
    expect_eq "pg32: admin DecideRequest(allow, forever_exe)" "$r" 'OK "applied"'
    wait "$p1"
    expect_eq "pg32: first python request ALLOWED" "$(cat "$WORK/32-py1.out")/$(cat "$WORK/32-py1.rc")" "ALLOWED/0"
    case $pyexe in /usr/bin/python*) pass "pg32: caller exe is the python interpreter ($pyexe)" ;;
                   *) fail "pg32: unexpected caller exe $pyexe" ;; esac
    expect_eq "pg32: cache row 2000|exe_only|<python exe>|forever_exe" \
        "$(sql_approvals "SELECT caller_uid||'|'||match_kind||'|'||match_value||'|'||scope FROM approvals WHERE action='$act'")" \
        "2000|exe_only|$pyexe|forever_exe"
    out=$(run_test_permission "$WORKU" "$act"); rc=$?
    expect_eq "pg32: second python request is a cache hit: ALLOWED" "$out/$rc" "ALLOWED/0"
    expect_eq "pg32: cache hit enqueued no prompt" "$(pending_ids_for "$act")" ""
    # A different exe under the same uid (the GUI scenario used perl; the
    # bats golden has no Net::DBus, so /usr/bin/dbus-send is the other exe).
    out=$(dsend_as "$WORKU" RequestPermission string:"$act" dict:string:string:"caller","other-exe-32")
    id=$(awk '$1=="int32"{print $2; exit}' <<<"$out")
    check "pg32: a different exe re-prompts (pending row, exe /usr/bin/dbus-send, caller=other-exe-32)" \
        python3 -c 'import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if r["exe"]=="/usr/bin/dbus-send" and r["uid"]==2000 and r["details"].get("caller")=="other-exe-32" else 1)' \
        "$(pending_row_json "$act")"
    ( dsend_as "$WORKU" WaitForDecision int32:"$id" >"$WORK/32-wait.out" 2>&1 ) &
    local p2=$!
    sleep 1
    r=$(broker_call_as admin DecideRequest "[$id, \"deny\", \"once\"]")
    expect_eq "pg32: admin denies the other-exe request" "$r" 'OK "applied"'
    wait "$p2"
    check "pg32: the other-exe caller sees the deny" \
        grep -qE 'boolean false|AdminBroker1\.Denied' "$WORK/32-wait.out"
    sql_approvals "DELETE FROM approvals WHERE action='$act'"
}

# --- pg33: expired cache row is not served; RunCacheGc deletes it ------------
pg33() {
    local act="test.action.$TAG" ctl="test.control.$TAG" out n attempt
    for attempt in 1 2; do
        python3 - "$act" "$ctl" <<'PY' || die "pg33: seeding failed"
import sqlite3, sys, time
act, ctl = sys.argv[1], sys.argv[2]
now = int(time.time())
c = sqlite3.connect("/var/lib/qdistro/approvals/approvals.sqlite")
q = ("INSERT INTO approvals (caller_uid, action, match_kind, match_value, decision,"
     " scope, expires_at, created_at, approver_uid) VALUES (2000, ?, 'exe_only',"
     " '/usr/bin/dbus-send', 1, '1h', ?, ?, 1000)")
c.execute("DELETE FROM approvals WHERE action IN (?, ?)", (act, ctl))
c.execute(q, (act, now - 3600, now - 7200))   # expired one hour ago
c.execute(q, (ctl, now + 3600, now))          # positive control: live row
c.commit()
PY
        out=$(dsend_as "$WORKU" CheckPermission string:"$ctl" dict:string:string:"purpose","33-control")
        check "pg33: control — an UNexpired row for the same exe is served: \"allow\"" \
            grep -q 'string "allow"' <<<"$out"
        out=$(dsend_as "$WORKU" CheckPermission string:"$act" dict:string:string:"purpose","33-expired")
        check "pg33: the expired row is NOT served: \"unknown\"" \
            grep -q 'string "unknown"' <<<"$out"
        out=$(dsend_as admin RunCacheGc)
        n=$(awk '$1=="int32"{print $2; exit}' <<<"$out")
        # The broker's own minute GC tick may have reaped the row first; that
        # race is the only reason a retry is allowed.
        if [ "${n:-0}" -ge 1 ] 2>/dev/null; then break; fi
        note "pg33: RunCacheGc returned '${n:-?}' (periodic GC raced?); retrying once"
    done
    if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
        pass "pg33: RunCacheGc (admin) deleted the expired row (int32 $n)"
    else
        fail "pg33: RunCacheGc did not report a deletion: $out"
    fi
    expect_eq "pg33: expired row gone from approvals" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE action='$act'")" 0
    expect_eq "pg33: the live control row survived the GC" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE action='$ctl'")" 1
    sql_approvals "DELETE FROM approvals WHERE action='$ctl'"
}

case "${1:-}" in
    pg07|pg23|pg30|pg31|pg32|pg33) "$1" ;;
    *) die "usage: $0 pg07|pg23|pg30|pg31|pg32|pg33" ;;
esac
[ "$_S120_FAILED" = 0 ] && printf 'PASS: s122 %s complete\n' "$1"
