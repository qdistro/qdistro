#!/bin/bash
# s124-perm-lineage — headless replacement for permissions-gui 58 and 59
# (finding P0-1 / P1-1: launcher-attested lineage under lineage_enforce).
# Both scenarios were `qci:visual: none`. Runs INSIDE the test VM as root.
#
#   bash s124-perm-lineage.sh pg58|pg59
#
# The broker's posture is changed through /etc/qdistro/broker.conf and is
# restored (with a broker restart) on EXIT by s120-perm-lib.sh.

. "$(dirname "$0")/s120-perm-lib.sh"

WORKU=$(ensure_uid_user work 2000)

starttime_of() { sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f20; }

# register_launch <silo> <engine> <app_id> <pid> — RegisterLaunch as ROOT via
# dbus-send (a trusted root launcher helper); prints the record id.
register_launch() {
    local pid=$4 st exe
    st=$(starttime_of "$pid"); exe=$(readlink "/proc/$pid/exe")
    dbus-send --system --print-reply=literal --dest="$BUS" "$OBJ" "$BUS.RegisterLaunch" \
        string:"$1" string:"$2" string:"$3" string:"i1" string:"$exe" \
        uint64:"$pid" string:"" uint64:"$st" 2>&1
}

_HELPER_PID=""
_kill_helper() {  # by (pid, starttime) identity, never by pattern
    local p=$_HELPER_PID want=${_HELPER_ST:-}
    [ -n "$p" ] || return 0
    [ "$(starttime_of "$p")" = "$want" ] && kill "$p" 2>/dev/null
    return 0
}

# --- pg58: forged sandbox_engine claim ---------------------------------------
pg58() {
    local act="org.qdistro.lineage.test.$TAG" out r rid m
    isolate_rules
    set_lineage_enforce unset || fail "pg58: broker never logged lineage_enforce=False (shadow) after restart"
    r=$(save_rule "58-tier1.yaml" "- name: pg58-tier1
  decision: allow
  match:
    action: $act
    sandbox_engine: qdistro.tier1
")
    expect_eq "pg58: tier-1 allow rule installed" "$r" "OK $RULES_DIR/58-tier1.yaml"
    out=$(dsend_as "$WORKU" CheckPermission string:"$act" dict:string:string:"sandbox_engine","qdistro.tier1")
    check "pg58: shadow mode trusts the forged sandbox_engine claim: \"allow\"" \
        grep -q 'string "allow"' <<<"$out"
    if set_lineage_enforce true; then
        pass "pg58: broker restarted with lineage_enforce=True (startup line after cursor)"
    else
        fail "pg58: broker never logged lineage_enforce=True after restart"
    fi
    out=$(dsend_as "$WORKU" CheckPermission string:"$act" dict:string:string:"sandbox_engine","qdistro.tier1")
    check "pg58: enforce mode drops the forged claim of an unregistered caller: \"unknown\"" \
        grep -q 'string "unknown"' <<<"$out"
    out=$(dsend_as "$WORKU" RegisterLaunch string:work string:qdistro.tier1 string:qdistro.tier1.work \
          string:i1 string:/usr/bin/sleep uint64:1 string:"" uint64:0)
    if grep -qE 'org\.freedesktop\.DBus\.Error\.AccessDenied|org\.qdistro\.AdminBroker1\.AccessDenied' <<<"$out"; then
        pass "pg58: RegisterLaunch from the work uid is refused (AccessDenied)"
    else
        fail "pg58: work RegisterLaunch was not refused: $out"
    fi
    # A live work helper that calls CheckPermission with NO claim once released.
    rm -f "$WORK/58-go" "$WORK/58-out" "$WORK/58-pid"
    cat >"$WORK/58-helper.py" <<PY
import dbus, os, time
iface = dbus.Interface(dbus.SystemBus().get_object(
    "org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
open("$WORK/58-pid", "w").write(str(os.getpid()))
for _ in range(600):
    if os.path.exists("$WORK/58-go"):
        break
    time.sleep(0.1)
open("$WORK/58-out", "w").write(str(iface.CheckPermission("$act", {})))
PY
    chmod 0644 "$WORK/58-helper.py"
    runuser -u "$WORKU" -- setsid python3 "$WORK/58-helper.py" >"$WORK/58-helper.log" 2>&1 &
    local i
    for ((i = 0; i < 100; i++)); do [ -s "$WORK/58-pid" ] && break; sleep 0.1; done
    _HELPER_PID=$(cat "$WORK/58-pid" 2>/dev/null); _HELPER_ST=$(starttime_of "$_HELPER_PID")
    trap '_kill_helper; _s120_restore' EXIT
    m=$(sql_audit "SELECT coalesce(max(id),0) FROM audit")
    rid=$(register_launch work qdistro.tier1 qdistro.tier1.work "$_HELPER_PID")
    if [[ $rid =~ ^[[:space:]]*[0-9a-f]+[[:space:]]*$ ]]; then
        pass "pg58: root RegisterLaunch bound the live helper pid $_HELPER_PID (record $(echo $rid))"
    else
        fail "pg58: root RegisterLaunch failed: $rid"
    fi
    touch "$WORK/58-go"
    for ((i = 0; i < 100; i++)); do [ -s "$WORK/58-out" ] && break; sleep 0.1; done
    expect_eq "pg58: registered caller with NO claim gets the attested engine: allow" \
        "$(cat "$WORK/58-out" 2>/dev/null)" allow
    r=$(sql_audit "SELECT count(*) FROM audit WHERE id > $m AND action LIKE 'qdistro.lineage.register:%'")
    if [ "${r:-0}" -ge 1 ]; then
        pass "pg58: RegisterLaunch wrote a qdistro.lineage.register:* audit row"
    else
        fail "pg58: no qdistro.lineage.register audit row"
    fi
}

# --- pg59: cross-silo clipboard source pid attestation --------------------------
pg59() {
    local out r m st i
    isolate_rules
    set_lineage_enforce unset || fail "pg59: broker never logged lineage_enforce=False (shadow) after restart"
    r=$(save_rule "59-work-to-admin.yaml" "- name: pg59-work-to-admin
  decision: allow
  match:
    action: qdistro.clipboard.transfer:work:admin
")
    expect_eq "pg59: work->admin clipboard allow rule installed" "$r" "OK $RULES_DIR/59-work-to-admin.yaml"
    _HELPER_PID=$(runuser -u "$WORKU" -- bash -c 'setsid sleep 600 >/dev/null 2>&1 </dev/null & echo $!')
    _HELPER_ST=$(starttime_of "$_HELPER_PID")
    [ -n "$_HELPER_ST" ] || die "pg59: work source helper did not start"
    trap '_kill_helper; _s120_restore' EXIT
    xfer() { bcall_as admin CheckClipboardTransfer ssassssbut work admin 1 text/plain "" "" "" false "$1" "$2"; }
    expect_eq "pg59: shadow: claimed source silo drives the decision (no pid): allow" "$(xfer 0 0)" 's "allow"'
    if set_lineage_enforce true; then
        pass "pg59: broker restarted with lineage_enforce=True (startup line after cursor)"
    else
        fail "pg59: broker never logged lineage_enforce=True after restart"
    fi
    m=$(sql_audit "SELECT coalesce(max(id),0) FROM audit")
    expect_eq "pg59: enforce: no source pid -> deny" "$(xfer 0 0)" 's "deny"'
    expect_eq "pg59: enforce: unregistered source pid -> deny" "$(xfer "$_HELPER_PID" "$_HELPER_ST")" 's "deny"'
    r=$(register_launch work qdistro.tier3 qdistro.tier3.work "$_HELPER_PID")
    [[ $r =~ ^[[:space:]]*[0-9a-f]+[[:space:]]*$ ]] && pass "pg59: root registered the source pid as silo work" \
        || fail "pg59: RegisterLaunch work failed: $r"
    expect_eq "pg59: enforce: registered source (attested work) -> allow" "$(xfer "$_HELPER_PID" "$_HELPER_ST")" 's "allow"'
    r=$(register_launch scratch qdistro.tier3 qdistro.tier3.scratch "$_HELPER_PID")
    [[ $r =~ ^[[:space:]]*[0-9a-f]+[[:space:]]*$ ]] && pass "pg59: root re-registered the same pid as silo scratch" \
        || fail "pg59: RegisterLaunch scratch failed: $r"
    expect_eq "pg59: enforce: forged source claim 'work' overridden by attested 'scratch' -> deny" \
        "$(xfer "$_HELPER_PID" "$_HELPER_ST")" 's "deny"'
    r=$(sql_audit "SELECT count(*) FROM audit WHERE id > $m AND action LIKE 'qdistro.lineage.source_deny:%'")
    if [ "${r:-0}" -ge 1 ]; then
        pass "pg59: unattested-source denials left qdistro.lineage.source_deny:* audit rows ($r)"
    else
        fail "pg59: no qdistro.lineage.source_deny audit row"
    fi
}

case "${1:-}" in
    pg58|pg59) "$1" ;;
    *) die "usage: $0 pg58|pg59" ;;
esac
[ "$_S120_FAILED" = 0 ] && printf 'PASS: s124 %s complete\n' "$1"
