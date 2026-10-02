#!/bin/bash
# s125-sendto-xuser — headless replacement for permissions-gui 11, 15 and 17:
# CROSS-UID send-to through the broker and the per-uid user relays.
# Runs INSIDE the test VM as root, staged by permissions-headless.bats.
#
#   bash s125-sendto-xuser.sh pg11|pg15|pg17
#
#   pg11  work(2000) -> work2(3000) qstub-notepad, admin approves `once`:
#         delivery into the stub's document, audit row with both uids, no
#         cache row (one-shot action).
#   pg15  two REAL qnotebook instances (offscreen), one per uid, each with its
#         built-in qdistro send-to receiver: both discoverable via
#         ListReceivers, delivery both directions, audit rows, no cache.
#   pg17  the deny leg on the real qnotebooks (the GUI scenario clicked Deny;
#         here admin calls DecideRequest(deny)): sender gets .Denied, the
#         receiver never sees the payload (after a positive-control delivery),
#         this request's audit row is caller 2000 / decision 0.
#
# work/work2 are created as REAL silos through SessionManager1 (CreateSilo +
# StartSilo), the same product path the GUI harness uses, so their relay
# grants exist; the relays are the system qdistro-user-relay@<uid> units.

. "$(dirname "$0")/s120-perm-lib.sh"

SRC=/root/qdistro-src

sm() { runuser -u admin -- busctl --system call org.qdistro.SessionManager1 \
         /org/qdistro/SessionManager1 org.qdistro.SessionManager1 "$@" 2>&1; }

ensure_silo() {  # <name> <uid>
    local n=$1 u=$2 out i
    if ! id "$n" >/dev/null 2>&1; then
        out=$(sm CreateSilo si "$n" "$u") || die "CreateSilo $n $u failed: $out"
    fi
    [ "$(id -u "$n" 2>/dev/null)" = "$u" ] || die "silo user $n is not uid $u"
    out=$(sm StartSilo s "$n") || grep -qiE 'already|active' <<<"$out" \
        || die "StartSilo $n failed: $out"
    loginctl enable-linger "$n" >/dev/null 2>&1 || true
    systemctl start "user@$u.service" >/dev/null 2>&1 || true
    for ((i = 0; i < 100; i++)); do [ -S "/run/user/$u/bus" ] && break; sleep 0.2; done
    [ -S "/run/user/$u/bus" ] || die "session bus /run/user/$u/bus never appeared"
    systemctl is-active --quiet "qdistro-user-relay@$u.service" \
        || systemctl start "qdistro-user-relay@$u.service" \
        || die "qdistro-user-relay@$u.service will not start"
}

as_session() {  # <user> <uid> <cmd...> — run with that uid's session bus
    local u=$1 id=$2; shift 2
    runuser -u "$u" -- env XDG_RUNTIME_DIR="/run/user/$id" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$id/bus" "$@"
}

# wait_receiver <uid> <service> — ListReceivers (a live fan-out to each uid's
# relay) shows the pair.
wait_receiver() {
    local i
    for ((i = 0; i < 120; i++)); do
        has_receiver "$1" "$2" && return 0
        sleep 0.5
    done
    return 1
}

receivers_json() { reply_json "$(broker_call_as root ListReceivers '[]')"; }
has_receiver() {  # <uid> <service> [friendly]
    receivers_json | python3 -c '
import json, sys
uid, svc = int(sys.argv[1]), sys.argv[2]
fr = sys.argv[3] if len(sys.argv) > 3 else None
rows = json.load(sys.stdin)
ok = any(int(r[0]) == uid and r[1] == svc and (fr is None or fr in [str(x) for x in r])
         for r in rows)
sys.exit(0 if ok else 1)' "$@"
}

# relay_and_decide <from_user> <to_uid> <service> <payload> <decision>
# Runs RelayMessage as <from_user> in the background (its own child of THIS
# shell), waits for the request to reach GetPending, decides as admin, then
# waits for the sender. Leaves the sender output in $WORK/relay.out and the
# request id in RELAY_RID.
RELAY_RID=""
relay_and_decide() {
    local from=$1 to=$2 svc=$3 payload=$4 decision=$5 action rid r
    action="app.send-to:$to:$svc"
    ( runuser -u "$from" -- dbus-send --system --print-reply --reply-timeout=120000 \
          --dest="$BUS" "$OBJ" "$BUS.RelayMessage" int32:"$to" string:"$svc" \
          string:text/plain string:"$payload" >"$WORK/relay.out" 2>&1 ) &
    local spid=$!
    wait_pending "$action" 160 || { kill "$spid" 2>/dev/null; die "send-to request $action never reached GetPending"; }
    rid=$(pending_ids_for "$action" | head -1)
    RELAY_RID=$rid
    r=$(broker_call_as admin DecideRequest "[$rid, \"$decision\", \"once\"]")
    [ "$r" = "OK null" ] || fail "DecideRequest($rid, $decision, once) failed: $r"
    wait "$spid"
}

start_stub() {  # <user> <uid>
    local u=$1 id=$2
    install -m 0644 "$SRC/stubs/qstub_notepad.py" "$WORK/qstub_notepad.py"
    as_session "$u" "$id" env QT_QPA_PLATFORM=offscreen \
        setsid python3 "$WORK/qstub_notepad.py" >"$WORK/stub-$u.log" 2>&1 </dev/null &
}

QNB_PIDS=()
start_qnotebook() {  # <user> <uid>
    local u=$1 id=$2 home dir
    home=$(getent passwd "$u" | cut -d: -f6)
    dir="$home/s125nb-$TAG"
    install -d -o "$u" -m 0700 "$dir"
    as_session "$u" "$id" env QT_QPA_PLATFORM=offscreen PYTHONUNBUFFERED=1 \
        setsid /usr/local/bin/qnotebook "$dir" >"$WORK/qnb-$u.log" 2>&1 </dev/null &
    QNB_PIDS+=("$!")
}

cleanup_apps() {
    local p
    for p in "${QNB_PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
    pkill -u work -f '^python3 /tmp/s120/qstub_notepad.py' 2>/dev/null
    pkill -u work2 -f '^python3 /tmp/s120/qstub_notepad.py' 2>/dev/null
    return 0
}
trap 'cleanup_apps; _s120_restore' EXIT

setup_silos() {
    systemctl is-active --quiet qdistro-session-manager.service \
        || systemctl start qdistro-session-manager.service \
        || die "qdistro-session-manager.service will not start"
    [ -x /usr/local/bin/qnotebook ] || [ "$1" = stub ] || die "qnotebook not installed in this VM"
    ensure_silo work 2000
    ensure_silo work2 3000
    pass "setup: work(2000) and work2(3000) are Active silos with session buses + relays"
}

# --- pg11: stub notepad, approve once ------------------------------------------
pg11() {
    local s2=org.qdistro.StubNotepad.uid2000 s3=org.qdistro.StubNotepad.uid3000 \
          payload="hello_from_work_headless_$TAG" out
    setup_silos stub
    start_stub work 2000
    start_stub work2 3000
    wait_receiver 2000 "$s2" && wait_receiver 3000 "$s3" \
        || die "pg11: stub notepads never appeared in ListReceivers"
    check "pg11: ListReceivers has (2000, $s2)" has_receiver 2000 "$s2"
    check "pg11: ListReceivers has (3000, $s3)" has_receiver 3000 "$s3"
    relay_and_decide work 3000 "$s3" "$payload" allow
    check "pg11: RelayMessage as work returned 'method return' after admin allow once" \
        bash -c 'grep -q "^method return" "$1" && ! grep -q "^Error" "$1"' _ "$WORK/relay.out"
    out=$(as_session work2 3000 dbus-send --session --print-reply --dest="$s3" \
          /org/qdistro/App1 org.qdistro.App1.GetDocument 2>&1)
    check "pg11: work2's notepad document contains '[text/plain] $payload'" \
        grep -qF "[text/plain] $payload" <<<"$out"
    expect_eq "pg11: audit row 2000|app.send-to:3000:...|1|once|prompt|1000" \
        "$(sql_audit "SELECT caller_uid||'|'||action||'|'||decision||'|'||scope||'|'||source||'|'||approver_uid FROM audit WHERE request_id=$RELAY_RID ORDER BY id DESC LIMIT 1")" \
        "2000|app.send-to:3000:$s3|1|once|prompt|1000"
    expect_eq "pg11: one-shot send-to persisted no cache row" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE action LIKE 'app.send-to:%'")" 0
}

# --- pg15: two real qnotebooks, both directions --------------------------------
pg15() {
    local q2=org.qdistro.Qnotebook.uid2000 q3=org.qdistro.Qnotebook.uid3000 out r1 r2
    setup_silos qnb
    start_qnotebook work 2000
    start_qnotebook work2 3000
    wait_receiver 2000 "$q2" && wait_receiver 3000 "$q3" \
        || die "pg15: qnotebook receivers never appeared (logs: $(tail -3 "$WORK"/qnb-*.log | tr '\n' ' '))"
    check "pg15: ListReceivers has (2000, $q2, Qnotebook)" has_receiver 2000 "$q2" Qnotebook
    check "pg15: ListReceivers has (3000, $q3, Qnotebook)" has_receiver 3000 "$q3" Qnotebook
    relay_and_decide work 3000 "$q3" "phase4_real_to_real_$TAG" allow; r1=$RELAY_RID
    check "pg15: work->work2 RelayMessage returned 'method return'" \
        bash -c 'grep -q "^method return" "$1" && ! grep -q "^Error" "$1"' _ "$WORK/relay.out"
    out=$(as_session work2 3000 dbus-send --session --print-reply --dest="$q3" \
          /org/qdistro/App1 org.qdistro.App1.GetLastReceived 2>&1)
    check "pg15: work2's qnotebook GetLastReceived = [text/plain] phase4_real_to_real_$TAG" \
        grep -qF "string \"[text/plain] phase4_real_to_real_$TAG\"" <<<"$out"
    relay_and_decide work2 2000 "$q2" "echo_reverse_$TAG" allow; r2=$RELAY_RID
    check "pg15: work2->work RelayMessage returned 'method return'" \
        bash -c 'grep -q "^method return" "$1" && ! grep -q "^Error" "$1"' _ "$WORK/relay.out"
    out=$(as_session work 2000 dbus-send --session --print-reply --dest="$q2" \
          /org/qdistro/App1 org.qdistro.App1.GetLastReceived 2>&1)
    check "pg15: work's qnotebook GetLastReceived = [text/plain] echo_reverse_$TAG" \
        grep -qF "string \"[text/plain] echo_reverse_$TAG\"" <<<"$out"
    expect_eq "pg15: audit rows for both directions (uid|action|1|once|prompt|1000)" \
        "$(sql_audit "SELECT group_concat(x, ' ') FROM (SELECT caller_uid||'|'||action||'|'||decision||'|'||scope||'|'||source||'|'||approver_uid AS x FROM audit WHERE request_id IN ($r1,$r2) AND action LIKE 'app.send-to:%' ORDER BY id DESC)")" \
        "3000|app.send-to:2000:$q2|1|once|prompt|1000 2000|app.send-to:3000:$q3|1|once|prompt|1000"
    expect_eq "pg15: no send-to cache row was ever persisted" \
        "$(sql_approvals "SELECT count(*) FROM approvals WHERE action LIKE 'app.send-to:%'")" 0
}

# --- pg17: deny on the real qnotebooks -----------------------------------------
pg17() {
    local q3=org.qdistro.Qnotebook.uid3000 out mark
    setup_silos qnb
    start_qnotebook work 2000
    start_qnotebook work2 3000
    wait_receiver 3000 "$q3" || die "pg17: work2's qnotebook receiver never appeared"
    # Positive control: an approved delivery on the same path lands, so the
    # deny's non-delivery below cannot be a dead path.
    relay_and_decide work 3000 "$q3" "sentinel_allow_$TAG" allow
    out=$(as_session work2 3000 dbus-send --session --print-reply --dest="$q3" \
          /org/qdistro/App1 org.qdistro.App1.GetLastReceived 2>&1)
    check "pg17: positive control — approved sentinel delivered" \
        grep -qF "[text/plain] sentinel_allow_$TAG" <<<"$out"
    mark=$(sql_audit "SELECT coalesce(max(id),0) FROM audit")
    relay_and_decide work 3000 "$q3" "please_deny_me_$TAG" deny
    if grep -q "org.qdistro.AdminBroker1.Denied" "$WORK/relay.out" \
       && ! grep -q "^method return" "$WORK/relay.out"; then
        pass "pg17: the sender observes org.qdistro.AdminBroker1.Denied (no success reply)"
    else
        fail "pg17: sender reply was not .Denied: $(cat "$WORK/relay.out")"
    fi
    out=$(as_session work2 3000 dbus-send --session --print-reply --dest="$q3" \
          /org/qdistro/App1 org.qdistro.App1.GetLastReceived 2>&1)
    if ! grep -qF "please_deny_me_$TAG" <<<"$out" && grep -qF "sentinel_allow_$TAG" <<<"$out"; then
        pass "pg17: work2's qnotebook never received the denied payload (still the sentinel)"
    else
        fail "pg17: receiver state after deny unexpected: $out"
    fi
    expect_eq "pg17: this request's audit row is caller 2000, decision 0, source prompt" \
        "$(sql_audit "SELECT caller_uid||'|'||decision||'|'||source FROM audit WHERE id > $mark AND request_id=$RELAY_RID AND action='app.send-to:3000:$q3'")" \
        "2000|0|prompt"
}

case "${1:-}" in
    pg11|pg15|pg17) "$1" ;;
    *) die "usage: $0 pg11|pg15|pg17" ;;
esac
[ "$_S120_FAILED" = 0 ] && printf 'PASS: s125 %s complete\n' "$1"
