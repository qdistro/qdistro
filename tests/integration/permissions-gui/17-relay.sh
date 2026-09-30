#!/bin/bash
# Guest-side driver for permissions-gui/17-realapp-sendto-deny.md.
#
# The scenario pushes this file to /tmp/17-relay.sh and calls it; the runner
# must NOT re-type these blocks into its own driver. In the 2026-09-30 full
# run (full-20260930T051422Z-65193) the runner re-implemented S1's GetPending
# parser with the wrong JSON shape, recorded BROKER_REQUEST_ID=none, and the
# run was lost as a harness ERROR. Shipping the code makes the invocation one
# line that cannot drift from the reviewed contract.
#
#   bash /tmp/17-relay.sh send       S1: baseline, send the relay, capture ids
#   bash /tmp/17-relay.sh verdict    S3: wait for the sender, print RELAY_VERDICT=
#   bash /tmp/17-relay.sh pending-id <action> <uid>
#        read `busctl --json=short call ... GetPending` on stdin and print the
#        request id of EXACTLY one pending row matching action and caller uid;
#        prints `none` or `ambiguous` (exit 1) otherwise.

# busctl --json=short renders aa{sv} as {"type":"aa{sv}","data":[[{key:
# {"type":..,"data":..}}, ...]]}: one list of dict rows whose values are
# type/data wrappers.
pending_request_id() {
    QCI_ACTION=$1 QCI_UID=$2 python3 -c '
import json, os, sys
def unwrap(x):
    return x["data"] if isinstance(x, dict) and "data" in x else x
try:
    rows = json.load(sys.stdin)["data"][0]
    ids = [str(unwrap(r.get("id"))) for r in rows
           if str(unwrap(r.get("action", ""))) == os.environ["QCI_ACTION"]
           and str(unwrap(r.get("uid", ""))) == os.environ["QCI_UID"]]
except Exception:
    ids = []
out = ids[0] if len(ids) == 1 else ("ambiguous" if ids else "none")
print(out)
sys.exit(0 if out.isdigit() else 1)
'
}

cmd_send() (
    set -e
    # --reply-timeout is MANDATORY and must stay well above the wall-clock
    # cost of the S2 deny interaction. dbus-send's default is 25s, which an
    # OCR-driven click routinely exceeds with three Qt apps up; the sender
    # then prints NoReply even though the broker delivered a perfectly good
    # `.Denied` a moment later. This scenario used to ACCEPT that NoReply,
    # which meant it never tested its own headline claim -- the
    # sender-visible `.Denied` contract. With an explicit 180s bound the
    # `.Denied` is required (see the S3 verdict block).
    #
    # The stamp is written by the guest itself, immediately before the send,
    # so the 180s window is measured on the same clock as the audit row's
    # `ts` column -- no host/guest clock comparison.
    # Baseline the audit table BEFORE the send. S3 requires a row whose id is
    # strictly greater than this, which correlates the row to THIS run exactly
    # and does not depend on the audit clock's one-second resolution. Without
    # it, "the newest matching row" could be a previous run's.
    sqlite3 /var/lib/qdistro/audit/audit.sqlite \
     'SELECT COALESCE(MAX(id),0) FROM audit;' >/tmp/17-relay.baseid 2>/dev/null || true
    date +%s >/tmp/17-relay.start
    runuser -u work -- dbus-send --system --print-reply --reply-timeout=180000 \
     --dest=org.qdistro.AdminBroker1 \
     /org/qdistro/AdminBroker1 \
     org.qdistro.AdminBroker1.RelayMessage \
     int32:3000 \
     string:org.qdistro.Qnotebook.uid3000 \
     string:text/plain \
     string:please_deny_me \
     >/tmp/17-relay.out 2>&1 &
    relay_pid=$!
    echo "$relay_pid" >/tmp/17-relay.pid
    # S3 WAITS for this process to terminate before it reads relay.out, so a
    # bare pid is not enough: it can be recycled while S3 waits. Record
    # /proc field 22 (starttime) as well. The field index is counted after
    # the LAST ')' because comm is parenthesised and may contain spaces.
    relay_stat=$(cat "/proc/$relay_pid/stat" 2>/dev/null || true)
    relay_rest=${relay_stat##*') '}
    # shellcheck disable=SC2086
    set -- $relay_rest
    echo "${20:-0}" >/tmp/17-relay.pidstart
    # Correlate the audit row to THIS request by the broker's OWN request id.
    # `id > baseline` + action + caller_uid + decision is NOT a unique key: a
    # concurrent request B with the same action and uid can write a decision=0
    # row that this request A would then borrow, turning A's AUDIT_REQUIRED
    # failure -- `.Denied` released with NO row of its own -- into a PASS, and
    # hiding exactly the broker defect the audit assertion exists to catch.
    # GetPending is the broker's own list of undecided requests; the id it
    # returns is the same rid DecideRequest later stores in audit.request_id
    # (qdistro_admin_broker.py -> self.audit.log(request_id=int(request_id))).
    # busctl running as root is a trusted admin-control D-Bus client
    # (_ROOT_DBUS_CLIENT_EXES), so no extra privilege is needed here. Poll
    # until EXACTLY ONE pending request matches this action and caller uid.
    # Zero matches, several, or an unparsable reply all leave a non-numeric
    # value in .reqid, and S3 turns that into a harness ERROR -- never a pass.
    relay_reqid=none
    relay_try=0
    while [ "$relay_try" -lt 30 ]; do
     relay_try=$((relay_try + 1))
     relay_reqid=$(busctl --system --json=short call \
      org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
      org.qdistro.AdminBroker1 GetPending 2>/dev/null \
      | pending_request_id 'app.send-to:3000:org.qdistro.Qnotebook.uid3000' 2000 \
      || true)
     case "$relay_reqid" in ''|*[!0-9]*) ;; *) break ;; esac
     sleep 1
    done
    echo "${relay_reqid:-none}" >/tmp/17-relay.reqid
    echo "broker request_id=${relay_reqid:-none}"
    sleep 1
)

cmd_verdict() (
    set -u
    # --- parameters (the ONLY per-scenario difference; contract is shared) -
    DB=/var/lib/qdistro/audit/audit.sqlite
    ACTION='app.send-to:3000:org.qdistro.Qnotebook.uid3000'
    CALLER_UID=2000
    PREFIX=/tmp/17-relay
    # Whole-second audit clock (qdistro_admin_audit.py writes int(time.time()),
    # i.e. FLOOR) vs a whole-second `date +%s` start stamp: a decision whose
    # real time is a fraction of a second past the deadline can still land on
    # ts == deadline. BAND is the guard band, in seconds, that keeps the
    # product-FAIL branch strictly inside the provable region; anything inside
    # the band resolves to ERROR (re-run), never to FAIL.
    BAND=2
    # The sender's own bound is --reply-timeout=180000. Give it this much
    # extra wall clock to unwind (runuser teardown, loaded parallel guest)
    # before we call it wedged.
    SENDER_GRACE=30
    BUDGET_S=180

    verdict() { echo "RELAY_VERDICT=$*"; exit 0; }

    num_or() { case "$1" in ''|*[!0-9]*) echo "$2" ;; *) echo "$1" ;; esac; }

    # --- 0. inputs recorded by S1 -----------------------------------------
    # Every one of these is a PRECONDITION, checked in section 5a before any
    # verdict branch can run. A missing or unparsable value degrades what the
    # rest of this script can prove, so it must produce a harness ERROR --
    # never a silently weaker PASS.
    start=$(num_or "$(cat "$PREFIX.start" 2>/dev/null || true)" 0)
    baseid=$(num_or "$(cat "$PREFIX.baseid" 2>/dev/null || true)" '')
    pid=$(num_or "$(cat "$PREFIX.pid" 2>/dev/null || true)" 0)
    pidstart=$(num_or "$(cat "$PREFIX.pidstart" 2>/dev/null || true)" 0)
    # The broker's own request id, captured from GetPending in S1. Non-numeric
    # ("none" / "ambiguous" / unwritten) becomes 0 and is rejected below.
    reqid=$(num_or "$(cat "$PREFIX.reqid" 2>/dev/null || true)" 0)
    deadline=$((start + BUDGET_S))

    # --- 1. WAIT FOR THE SENDER before reading anything it writes ---------
    # The broker commits the audit row BEFORE it invokes relay_reply, so
    # there is always a window in which the row is visible and the sender has
    # not yet written its error to $PREFIX.out. Classifying inside that
    # window is exactly how a healthy delayed reply becomes a product FAIL.
    # So: wait for the recorded process to be gone, bounded by its own
    # 180s reply timeout plus SENDER_GRACE. A bare pid can be recycled while
    # we wait, so the /proc starttime recorded in S1 is re-checked -- and
    # without that starttime the wait cannot tell exit from reuse, so we do
    # not even start it: sender_gone stays `unknown` and 5a errors out.
    sender_gone=unknown
    if [ "$pid" -gt 0 ] && [ "$pidstart" -gt 0 ]; then
      sender_gone=no
      now=$(date +%s)
      if [ "$start" -gt 0 ]; then
        wait_until=$((deadline + SENDER_GRACE))
      else
        wait_until=$((now + BUDGET_S + SENDER_GRACE))
      fi
      if [ "$wait_until" -lt $((now + 15)) ]; then wait_until=$((now + 15)); fi
      while : ; do
        st=$(cat "/proc/$pid/stat" 2>/dev/null || true)
        if [ -z "$st" ]; then sender_gone=yes; break; fi
        rest=${st##*') '}
        # shellcheck disable=SC2086
        set -- $rest
        live_start=$(num_or "${20:-0}" 0)
        if [ "$live_start" -gt 0 ] && [ "$live_start" -ne "$pidstart" ]; then
          sender_gone=yes; break
        fi
        now=$(date +%s)
        if [ "$now" -ge "$wait_until" ]; then break; fi
        sleep 1
      done
    fi

    # --- 2. what the sender saw on the wire -------------------------------
    out=$(cat "$PREFIX.out" 2>/dev/null || true)
    denied=no
    success=no
    if printf '%s' "$out" | grep -q 'org.qdistro.AdminBroker1.Denied'; then denied=yes; fi
    if printf '%s' "$out" | grep -q 'method return'; then success=yes; fi

    # --- 3. THE EXACT audit row for THIS request --------------------------
    # Correlation is by the broker's OWN request id, not by a tuple. The
    # prompt path writes request_id=<rid> on the same rid GetPending handed
    # S1 (qdistro_admin_broker.py DecideRequest -> audit.log(request_id=...)),
    # so `id > baseline AND action AND request_id` names exactly one request.
    # The old tuple (id > baseline + action + uid + decision) is NOT unique:
    # a concurrent request B with the same action and uid can write a
    # decision=0 row that this request A would borrow, turning A's
    # AUDIT_REQUIRED failure -- `.Denied` with no row of its own -- into a
    # PASS. `row_any` counts rows for this action since the baseline so that
    # "no row for MY request, but rows for this action exist" is reported as
    # a contaminated-run ERROR rather than being scored either way.
    have_sqlite=no
    if command -v sqlite3 >/dev/null 2>&1; then have_sqlite=yes; fi
    row=''
    row_any=0
    if [ "$have_sqlite" = yes ] && [ -n "$baseid" ]; then
      if [ "$reqid" -gt 0 ]; then
        row=$(sqlite3 -separator '|' "$DB" \
          "SELECT id, ts, caller_uid, decision, COALESCE(scope,''), source
             FROM audit
            WHERE id > $baseid AND action = '$ACTION' AND request_id = $reqid
            ORDER BY id DESC LIMIT 1;" 2>/dev/null || true)
      fi
      row_any=$(num_or "$(sqlite3 "$DB" \
        "SELECT COUNT(*) FROM audit
          WHERE id > $baseid AND action = '$ACTION';" 2>/dev/null || true)" 0)
    fi
    row_new=no; row_ok=no
    rid=0; ts=0; ruid=-1; rdec=-1; rscope=''; rsrc=''
    if [ -n "$row" ]; then
      row_new=yes
      rid=${row%%|*};    r=${row#*|}
      ts=${r%%|*};       r=${r#*|}
      ruid=${r%%|*};     r=${r#*|}
      rdec=${r%%|*};     r=${r#*|}
      rscope=${r%%|*};   rsrc=${r#*|}
      rid=$(num_or "$rid" 0)
      ts=$(num_or "$ts" 0)
      if [ "$ruid" = "$CALLER_UID" ] && [ "$rdec" = "0" ]; then row_ok=yes; fi
    fi

    # --- 4. was the decision PROVABLY inside the sender's budget? ---------
    budget=none
    if [ "$row_new" = yes ] && [ "$start" -gt 0 ] && [ "$ts" -gt 0 ]; then
      if [ "$ts" -lt "$start" ]; then budget=ambiguous
      elif [ "$ts" -le $((deadline - BAND)) ]; then budget=in
      elif [ "$ts" -le $((deadline + BAND)) ]; then budget=ambiguous
      else budget=late
      fi
    fi

    echo "RELAY_START=$start DEADLINE=$deadline BAND=$BAND SENDER_PID=$pid SENDER_PIDSTART=$pidstart SENDER_GONE=$sender_gone BROKER_REQUEST_ID=$reqid AUDIT_BASE_ID=${baseid:-unknown} AUDIT_ID=$rid AUDIT_TS=$ts AUDIT_UID=$ruid AUDIT_DECISION=$rdec AUDIT_SCOPE=$rscope AUDIT_SOURCE=$rsrc ROW_NEW=$row_new ROW_ANY=$row_any ROW_OK=$row_ok BUDGET=$budget DENIED=$denied SUCCESS=$success"

    # --- 5. classify ------------------------------------------------------
    #
    # 5a. One conclusive observation is taken first, because it needs no
    # correlation at all: $PREFIX.out is THIS sender's own stdout, so a
    # `method return` in it is this request's success reply by construction.
    # A relay the admin denied that answers the caller with success is a
    # broker defect whatever else is missing, and must not be downgraded to
    # a harness ERROR by a failed precondition. (It is also the branch that
    # catches a stale cached approval silently allowing the relay, which is
    # precisely the downgrade this scenario exists to detect.)
    if [ "$success" = yes ]; then
      if [ "$row_new" = yes ] && [ "$rdec" = "1" ]; then
        verdict "FAIL reason=relay-succeeded-and-audit-records-allow"
      fi
      verdict "FAIL reason=deny-reported-to-caller-as-success"
    fi

    # 5b. PRECONDITIONS. Every remaining branch -- PASS included -- depends
    # on all of these. They are checked here, before any of them, so that no
    # verdict below can be reached on degraded evidence. In particular:
    #   * no sender starttime  => the wait in section 1 cannot tell "exited"
    #                             from "pid recycled", so `sender_gone` is
    #                             not trustworthy;
    #   * sender still present => $PREFIX.out may simply be unfinished, so
    #                             `denied`/`success` are not yet final;
    #   * no broker request id => the audit row cannot be proved to be this
    #                             request's row.
    [ "$have_sqlite" = yes ] || verdict "ERROR reason=sqlite3-unavailable-in-guest"
    [ -n "$baseid" ]         || verdict "ERROR reason=audit-baseline-id-missing-from-S1"
    [ "$start" -gt 0 ]       || verdict "ERROR reason=relay-start-stamp-missing-from-S1"
    [ "$pid" -gt 0 ]         || verdict "ERROR reason=sender-pid-missing-from-S1"
    [ "$pidstart" -gt 0 ]    || verdict "ERROR reason=sender-pid-starttime-missing-from-S1"
    [ "$reqid" -gt 0 ]       || verdict "ERROR reason=broker-request-id-not-captured-by-S1"
    [ "$sender_gone" = yes ] || verdict "ERROR reason=sender-did-not-exit-within-its-own-timeout"

    # 5c. Contamination guard. Rows for this action exist since the baseline
    # but none of them carries this request's id: another request's decision
    # is in the window. Never borrow it -- in either direction.
    if [ "$row_new" = no ] && [ "$row_any" -gt 0 ]; then
      verdict "ERROR reason=audit-rows-for-this-action-belong-to-another-request"
    fi

    # 5d. The sender-visible contract held only if BOTH halves held: the
    # `.Denied` error AND the durable decision=0 row carrying this request's
    # id. The broker's AUDIT_REQUIRED path releases the waiter with `.Denied`
    # and writes NO row; that is a broker defect, not a pass.
    if [ "$denied" = yes ]; then
      [ "$row_new" = yes ] || verdict "FAIL reason=Denied-reply-with-no-audit-row"
      [ "$row_ok"  = yes ] || verdict "FAIL reason=Denied-reply-but-audit-row-is-not-caller-decision0"
      # The `.Denied` reached the sender inside its own 180s reply timeout,
      # so in-budget delivery is already proved by the wire; `budget` is only
      # re-checked here for INTERNAL CONSISTENCY. `late` (ts more than BAND
      # past the deadline) or `none` (unusable ts) contradicts that wire
      # evidence and is a clock/row anomaly -- ERROR, never a blessed PASS.
      case "$budget" in
        in|ambiguous) verdict "PASS" ;;
        *) verdict "ERROR reason=Denied-reply-but-audit-ts-inconsistent-budget=$budget" ;;
      esac
    fi

    # 5e. Neither `.Denied` nor a success reply on the wire, and the sender
    # has provably exited, so this is its final output.
    [ "$row_new" = yes ] || verdict "ERROR reason=no-audit-row-for-this-request-admin-never-decided"
    [ "$row_ok"  = yes ] || verdict "ERROR reason=audit-row-does-not-match-the-denied-request"
    [ "$budget"  = in  ] || verdict "ERROR reason=decision-not-provably-inside-sender-budget-budget=$budget"
    verdict "FAIL reason=decision-in-budget-but-no-Denied-reply"
)

case "${1:-}" in
    send) cmd_send ;;
    verdict) cmd_verdict ;;
    pending-id) shift; pending_request_id "$@" ;;
    *) echo "usage: $0 send|verdict|pending-id <action> <uid>" >&2; exit 2 ;;
esac
