#!/bin/bash
# s121-perm-rules — headless replacement for permissions-gui 24..29 (the
# declarative-rules scenarios). Runs INSIDE the test VM as root, staged by
# permissions-headless.bats next to s120-perm-lib.sh.
#
#   bash s121-perm-rules.sh pg24|pg25|pg26|pg27|pg28|pg29
#
# Each case asserts the SAME hard checks the GUI scenario asserted (SDK
# verdict, GetPending, audit row, approvals row, signals, ListRules). The GUI
# scenarios' only visual content was a screenshot of an EMPTY admin-app
# pending list; its real oracle was always GetPending, asserted here.

. "$(dirname "$0")/s120-perm-lib.sh"

WORKU=$(ensure_uid_user work 2000)
install_test_permission

# --- pg24: SaveRule allow short-circuits the prompt ---------------------------
pg24() {
    local act="test.action.$TAG" r out rc
    isolate_rules
    r=$(save_rule "24-allow-test-action.yaml" "- name: allow-work-test-action
  decision: allow
  match:
    uid: 2000
    action: $act
  rationale: pg24 rule-allow short-circuit
")
    expect_eq "pg24: SaveRule returns the installed rule path" "$r" \
        "OK $RULES_DIR/24-allow-test-action.yaml"
    check "pg24: rule file on disk is -rw-r--r-- and non-empty" \
        bash -c "[ -s $RULES_DIR/24-allow-test-action.yaml ] && [ \"\$(stat -c %A $RULES_DIR/24-allow-test-action.yaml)\" = -rw-r--r-- ]"
    out=$(run_test_permission "$WORKU" "$act"); rc=$?
    expect_eq "pg24: SDK request as work returns ALLOWED (rc 0)" "$out/$rc" "ALLOWED/0"
    expect_eq "pg24: no pending request was enqueued" "$(pending_ids_for "$act")" ""
    expect_eq "pg24: audit row is uid 2000, decision 1, source rule" \
        "$(sql_audit "SELECT caller_uid||'|'||decision||'|'||source FROM audit WHERE action='$act' ORDER BY id DESC LIMIT 1")" \
        "2000|1|rule"
    expect_eq "pg24: rule decisions write no cache row" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE action='$act'")" 0
}

# --- pg25: SaveRule deny short-circuits -------------------------------------
pg25() {
    local act="test.action.$TAG" r out rc
    isolate_rules
    r=$(save_rule "25-deny-test-action.yaml" "- name: deny-work-test-action
  decision: deny
  match:
    uid: 2000
    action: $act
  rationale: pg25 rule-deny short-circuit
")
    expect_eq "pg25: SaveRule returns the installed rule path" "$r" \
        "OK $RULES_DIR/25-deny-test-action.yaml"
    out=$(run_test_permission "$WORKU" "$act"); rc=$?
    expect_eq "pg25: SDK request as work returns DENIED (rc 1)" "$out/$rc" "DENIED/1"
    expect_eq "pg25: no pending request was enqueued" "$(pending_ids_for "$act")" ""
    expect_eq "pg25: audit row is uid 2000, decision 0, source rule" \
        "$(sql_audit "SELECT caller_uid||'|'||decision||'|'||source FROM audit WHERE action='$act' ORDER BY id DESC LIMIT 1")" \
        "2000|0|rule"
    expect_eq "pg25: rule decisions write no cache row" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE action='$act'")" 0
}

# --- pg26: inotify hot reload (direct file drop, NO SaveRule/ReloadRules) ----
pg26() {
    local act="test.action.$TAG" log="$WORK/26-signals.log" i hit="" out rc lr
    isolate_rules     # verified-empty baseline: every pre-drop emit carries 0
    start_signal_monitor RulesReloaded "$log" \
        || die "pg26: dbus-monitor never proved its RulesReloaded subscription"
    pass "pg26: monitor subscription verified (positive-control probe seen)"
    # Every signal already in the log predates the drop and carries count 0
    # (baseline verified empty above); grade only signals after this mark.
    local mark
    mark=$(grep -c 'member=RulesReloaded' "$log" 2>/dev/null)
    cat >"$RULES_DIR/26-inotify-allow.yaml" <<YAML
- name: allow-work-test-action-via-inotify
  decision: allow
  match:
    uid: 2000
    action: $act
  rationale: pg26 direct file drop, inotify must catch it
YAML
    for ((i = 0; i < 120; i++)); do
        hit=$(awk -v m="$mark" '
            /member=RulesReloaded/ { n++; want = (n > m); next }
            want && /int32/ { if ($NF + 0 >= 1) { print $NF; exit } ; want = 0 }' "$log")
        [ -n "$hit" ] && break
        sleep 0.25
    done
    stop_signal_monitor "$log"
    if [ -n "$hit" ]; then
        pass "pg26: post-drop RulesReloaded signal with count >= 1 (count=$hit), no SaveRule/ReloadRules call"
    else
        fail "pg26: no post-drop RulesReloaded with count >= 1; log: $(tr '\n' ' ' <"$log" | head -c 600)"
    fi
    lr=$(broker_call_as admin ListRules '[]')
    if reply_json "$lr" | python3 -c '
import json,sys
rows=json.load(sys.stdin)
want={"name":"allow-work-test-action-via-inotify","decision":"allow",
      "action":sys.argv[1],"uid":2000,
      "source_path":"/etc/qdistro/rules.d/26-inotify-allow.yaml"}
sys.exit(0 if any(all(r.get(k)==v for k,v in want.items()) for r in rows) else 1)' "$act"; then
        pass "pg26: ListRules reports the dropped rule (name/decision/action/uid/source_path)"
    else
        fail "pg26: ListRules lacks the dropped rule: $lr"
    fi
    out=$(run_test_permission "$WORKU" "$act"); rc=$?
    expect_eq "pg26: rule is live — work SDK request ALLOWED" "$out/$rc" "ALLOWED/0"
    expect_eq "pg26: no pending request" "$(pending_ids_for "$act")" ""
}

# --- pg27: sorted-filename first-match-wins ---------------------------------
pg27() {
    local act="test.action.$TAG" r out rc
    isolate_rules
    cat >"$RULES_DIR/27a-allow.yaml" <<YAML
- name: allow-first
  decision: allow
  match: {uid: 2000, action: $act}
YAML
    cat >"$RULES_DIR/27b-deny.yaml" <<YAML
- name: deny-second
  decision: deny
  match: {uid: 2000, action: $act}
YAML
    r=$(reload_rules)
    expect_eq "pg27: ReloadRules loads 2 rules with no errors" "$r" "OK [2, []]"
    out=$(run_test_permission "$WORKU" "$act"); rc=$?
    expect_eq "pg27: allow-file sorts first -> ALLOWED" "$out/$rc" "ALLOWED/0"
    expect_eq "pg27: audit 1|rule|27a-allow.yaml" \
        "$(sql_audit "SELECT decision||'|'||source||'|'||rule_path FROM audit WHERE action='$act' ORDER BY id DESC LIMIT 1")" \
        "1|rule|$RULES_DIR/27a-allow.yaml"
    mv "$RULES_DIR/27a-allow.yaml" "$RULES_DIR/27c-allow.yaml"
    mv "$RULES_DIR/27b-deny.yaml" "$RULES_DIR/27a-deny.yaml"
    r=$(reload_rules)
    expect_eq "pg27: ReloadRules after swap loads 2 rules, no errors" "$r" "OK [2, []]"
    out=$(run_test_permission "$WORKU" "$act"); rc=$?
    expect_eq "pg27: deny-file now sorts first -> DENIED" "$out/$rc" "DENIED/1"
    expect_eq "pg27: audit 0|rule|27a-deny.yaml" \
        "$(sql_audit "SELECT decision||'|'||source||'|'||rule_path FROM audit WHERE action='$act' ORDER BY id DESC LIMIT 1")" \
        "0|rule|$RULES_DIR/27a-deny.yaml"
}

# --- pg28: exe glob match on the live /proc exe ------------------------------
pg28() {
    local act="test.action.$TAG" r out rc rid row wout
    isolate_rules
    r=$(save_rule "28-allow-python.yaml" "- name: allow-python-test-action
  decision: allow
  match:
    uid: 2000
    action: $act
    exe: /usr/bin/python*
  rationale: pg28 exe glob match
")
    expect_eq "pg28: SaveRule returns the installed rule path" "$r" "OK $RULES_DIR/28-allow-python.yaml"
    out=$(run_test_permission "$WORKU" "$act"); rc=$?
    expect_eq "pg28: python caller matches /usr/bin/python* -> ALLOWED" "$out/$rc" "ALLOWED/0"
    expect_eq "pg28: python call enqueued nothing" "$(pending_ids_for "$act")" ""
    # Alternate exe: /usr/bin/dbus-send as work does NOT match the glob.
    out=$(dsend_as "$WORKU" RequestPermission string:"$act" dict:string:string:"caller","dbus-send-28")
    rid=$(printf '%s\n' "$out" | awk '$1=="int32"{print $2; exit}')
    [ -n "$rid" ] || fail "pg28: RequestPermission via dbus-send returned no id: $out"
    row=$(pending_row_json "$act")
    if printf '%s' "$row" | python3 -c '
import json,sys
r=json.load(sys.stdin)
ok = r["uid"]==2000 and r["exe"]=="/usr/bin/dbus-send" and r["details"].get("caller")=="dbus-send-28" and str(r["id"])==sys.argv[1]
sys.exit(0 if ok else 1)' "$rid" 2>/dev/null; then
        pass "pg28: dbus-send caller falls through to a pending prompt (uid 2000, exe /usr/bin/dbus-send, caller=dbus-send-28)"
    else
        fail "pg28: expected one pending row for the dbus-send caller; got: $row"
    fi
    # The waiter is the same exe/uid; admin denies; the waiter sees false.
    ( dsend_as "$WORKU" WaitForDecision int32:"$rid" >"$WORK/28-wait.out" 2>&1 ) &
    local wpid=$!
    sleep 1
    r=$(broker_call_as admin DecideRequest "[$rid, \"deny\", \"once\"]")
    expect_eq "pg28: admin DecideRequest(deny) accepted" "$r" 'OK "applied"'
    wait "$wpid"
    wout=$(cat "$WORK/28-wait.out")
    if printf '%s' "$wout" | grep -qE 'boolean false|AdminBroker1\.Denied'; then
        pass "pg28: the alt-exe waiter observes the deny (boolean false / .Denied)"
    else
        fail "pg28: WaitForDecision did not report the deny: $wout"
    fi
    expect_eq "pg28: pending list drained" "$(pending_ids_for "$act")" ""
}

# --- pg29: CheckPermission fast path: unknown / rule / cache -----------------
pg29() {
    local act="test.action.$TAG" out
    isolate_rules
    out=$(dsend_as "$WORKU" CheckPermission string:"$act" dict:string:string:"purpose","pg29-fastpath")
    check "pg29: CheckPermission with no rule/cache returns \"unknown\"" \
        grep -q 'string "unknown"' <<<"$out"
    expect_eq "pg29: unknown fast path enqueues no prompt" "$(pending_ids_for "$act")" ""
    expect_eq "pg29: unknown fast path writes no audit row" \
        "$(sql_audit "SELECT count(*) FROM audit WHERE action='$act'")" 0
    expect_eq "pg29: unknown fast path writes no cache row" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE action='$act'")" 0
    local r
    r=$(save_rule "29-allow.yaml" "- name: pg29-allow
  decision: allow
  match: {uid: 2000, action: $act}
")
    expect_eq "pg29: SaveRule allow installed" "$r" "OK $RULES_DIR/29-allow.yaml"
    out=$(dsend_as "$WORKU" CheckPermission string:"$act" dict:string:string:"purpose","pg29-rule")
    check "pg29: with the allow rule CheckPermission returns \"allow\"" \
        grep -q 'string "allow"' <<<"$out"
    rm -f "$RULES_DIR/29-allow.yaml"
    r=$(reload_rules)
    expect_eq "pg29: rule removed, ReloadRules counts 0" "$r" "OK [0, []]"
    python3 - "$act" <<'PY' || fail "pg29: could not seed the deny cache row"
import sys
sys.path.insert(0, "/usr/libexec/qdistro")
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache("/var/lib/qdistro/approvals/approvals.sqlite")
assert c.store(2000, sys.argv[1], "/usr/bin/dbus-send", "forever_exe", False, 1000)
PY
    out=$(dsend_as "$WORKU" CheckPermission string:"$act" dict:string:string:"purpose","pg29-cache")
    check "pg29: with a deny cache row CheckPermission returns \"deny\"" \
        grep -q 'string "deny"' <<<"$out"
    sql_approvals "DELETE FROM approvals WHERE action='$act'"
}

case "${1:-}" in
    pg24|pg25|pg26|pg27|pg28|pg29) "$1" ;;
    *) die "usage: $0 pg24|pg25|pg26|pg27|pg28|pg29" ;;
esac
[ "$_S120_FAILED" = 0 ] && printf 'PASS: s121 %s complete\n' "$1"
