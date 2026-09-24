# 17 — cross-user send-to between real qnotebook instances, admin denies

<!-- qci:visual: required -->

**What**: same flow as 16 (work's qnotebook → work2's qnotebook
via broker, admin app in admin's compositor session), but admin clicks
**Deny**. Assert that work2's qnotebook receiver was NOT called
(its `GetLastReceived` reflects its pre-deny state), the sender
observes `Denied`, and the audit row records `decision=0`.

**Why**: 16 covers Approve. A silent downgrade to allow, or a
failure to suppress delivery on deny, would look identical on the
surface. Only the receiver state + audit distinguish genuine deny
from a false positive.

**Caveat (loud)**: shared-XWayland expedient. See
.

** scope note**: qterminator side deferred per
.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1957}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl --machine=work@.host --user stop qstub-notepad.service 2>/dev/null || true'
$VMEXEC "$VM" 'systemctl --machine=work2@.host --user stop qstub-notepad.service 2>/dev/null || true'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action LIKE 'app.send-to:%';
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 2>/dev/null; true"

# Launch qnotebook instances first so the admin app ends up in front.
$VMEXEC "$VM" '
 pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 sleep 1
 rm -f /home/work/testnb/.zim-qt/lock /home/work2/testnb/.zim-qt/lock
 /usr/local/bin/qdistro-start-user-app work /usr/local/bin/qnotebook /home/work/testnb
 /usr/local/bin/qdistro-start-user-app work2 /usr/local/bin/qnotebook /home/work2/testnb
'
sleep 6

# Start admin app after qnotebooks (matches scenario 16's stable
# ordering) and wait for the window to become visible under
# XWayland before proceeding.
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 2
$VMEXEC "$VM" 'runuser -u admin -- env DISPLAY=:0 sh -c '"'"'
  for retry in 1 2 3 4 5 6; do
    w=$(xdotool search --onlyvisible --name ".*admin approvals.*" 2>/dev/null | head -1 || true)
    if [ -n "$w" ]; then
      xdotool windowactivate --sync "$w" windowraise "$w"
      break
    fi
    sleep 1
  done
'"'"''

# Snapshot pre-deny state of work2's qnotebook — anything already
# in GetLastReceived (from prior scenarios on this VM) is the
# baseline we assert doesn't change.
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.Qnotebook.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetLastReceived' > "${QCI_SCENARIO_TMPDIR:-/tmp}/17-baseline.out" 2>&1
echo "=== pre-deny baseline ==="
cat "${QCI_SCENARIO_TMPDIR:-/tmp}/17-baseline.out"
```

## Steps

### S1 — trigger, confirm pending visible

```bash
B64=$(base64 -w0 <<'EOF'
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
  | QCI_ACTION='app.send-to:3000:org.qdistro.Qnotebook.uid3000' QCI_UID=2000 python3 -c '
import json, os, sys
def unwrap(x):
    return x["data"] if isinstance(x, dict) and "data" in x else x
try:
    rows = json.load(sys.stdin)["data"][0]
except Exception:
    print("none"); raise SystemExit(0)
ids = [str(unwrap(r.get("id"))) for r in rows
       if str(unwrap(r.get("action", ""))) == os.environ["QCI_ACTION"]
       and str(unwrap(r.get("uid", ""))) == os.environ["QCI_UID"]]
print(ids[0] if len(ids) == 1 else ("ambiguous" if ids else "none"))
' 2>/dev/null || true)
 case "$relay_reqid" in ''|*[!0-9]*) ;; *) break ;; esac
 sleep 1
done
echo "${relay_reqid:-none}" >/tmp/17-relay.reqid
echo "broker request_id=${relay_reqid:-none}"
sleep 1
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
$VMGUI "$VM" screenshot /tmp/17-s1-pending.png
```

**Assert (OCR /tmp/17-s1-pending.png)**:
- `uid=2000` visible.
- `payload=please_deny_me` on the details line.
- `Approve` and `Deny` buttons present.

### S2 — click Deny via OCR targeting

```bash
# Runner:
# 1. OCR /tmp/17-s1-pending.png.
# 2. Find bounding box of "Deny" (scope picker has no "Deny"
# label — only Approve/Deny buttons fit that width).
# 3. Click its center.
```

Then, as a separate command, the title wait:

```bash
# Settle before grading. The title is computed from the Pending model's row
# count, so "admin approvals" with no "(N pending)" means the model is empty.
# The client surface can lag the title: a fixed `sleep 2` once captured the
# emptied title over the stale, still-selected row (2026-09-24, scenario 13).
$VMEXEC "$VM" 'for _ in $(seq 1 60); do
  t=$(runuser -u admin -- env DISPLAY=:0 xdotool search --name "^admin approvals" getwindowname 2>/dev/null | head -1)
  [ "$t" = "admin approvals" ] && exit 0
  sleep 0.5
done
echo "title never settled: $t" >&2; exit 1' 2>"${QCI_SCENARIO_TMPDIR:-/tmp}/17-s2-title.err"
echo "title-wait rc=$?"
cat "${QCI_SCENARIO_TMPDIR:-/tmp}/17-s2-title.err"
```

**Readiness, step 1 — title wait** (up to 60 polls, ~30 s). Run the
block above as its own command and record the printed `title-wait rc=`
line together with the stderr shown after it. Any rc other than 0 is a
failed step: S2 FAILS on that ground regardless of what the frames below
show. Still capture and grade the frames as evidence; a later good frame
does not erase the timeout.

**Readiness, step 2 — bounded frame capture** (at most 5 frames, 2 s
apart). Start with N=1. Each iteration is a separate runner action, not
a shell loop:

1. Capture frame N (substitute the number for `N`):

   ```bash
   $VMGUI "$VM" screenshot /tmp/17-s2-denied-N.png
   ```

2. Open `/tmp/17-s2-denied-N.png` and grade it by looking at the image
   (vision; no OCR helper). The empty state is: `(no selection)` in the
   details pane and no request row in the Pending list.
3. If frame N shows the empty state, copy it to the canonical path and
   stop capturing:

   ```bash
   cp /tmp/17-s2-denied-N.png /tmp/17-s2-denied.png
   ```

4. Otherwise, if N < 5: `sleep 2`, increment N, and go back to 1.
5. If frame 5 still does not show the empty state, the surface stayed
   stale for ~10 s after the model emptied: S2 FAILS. Copy the last
   frame to the canonical path and grade that below:

   ```bash
   cp /tmp/17-s2-denied-5.png /tmp/17-s2-denied.png
   ```

Keep every numbered frame; do not delete or overwrite them.

**Assert (open and grade /tmp/17-s2-denied.png by vision)**:
- `(no selection)` visible.
- No `uid=2000` / `app.send-to:` text.

### S3 — work2's qnotebook was NOT delivered to

```bash
$VMEXEC "$VM" 'runuser -u work2 -- env \
 XDG_RUNTIME_DIR=/run/user/3000 \
 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/3000/bus \
 dbus-send --session --print-reply \
 --dest=org.qdistro.Qnotebook.uid3000 \
 /org/qdistro/App1 \
 org.qdistro.App1.GetLastReceived' > "${QCI_SCENARIO_TMPDIR:-/tmp}/17-after.out" 2>&1
cat "${QCI_SCENARIO_TMPDIR:-/tmp}/17-after.out"

$VMEXEC "$VM" 'cat /tmp/17-relay.out'

SQL_B64=$(base64 -w0 <<'SQL_EOF'
SELECT caller_uid, action, decision, scope, source FROM audit
 WHERE action LIKE 'app.send-to:%Qnotebook%'
 ORDER BY id DESC LIMIT 1;
SQL_EOF
)
$VMEXEC "$VM" "echo $SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"

# Product-FAIL vs harness-ERROR, decided mechanically. Three facts, all
# read in the guest, on the guest's own clock:
#   sender  = /tmp/17-relay.pid + .pidstart. The broker commits the audit
#             row BEFORE it invokes relay_reply, so there is ALWAYS a
#             window where the row is visible and relay.out is still
#             empty. Classifying inside that window turns a healthy
#             delayed reply into a product FAIL, so the block below first
#             WAITS for the sender to terminate, bounded by the sender's
#             own --reply-timeout=180000 plus 30s of grace. A sender that
#             outlives that bound is ERROR (wedged), never FAIL.
#   row     = the audit row for THIS request: id > the pre-send baseline,
#             the exact `action` string, caller_uid 2000, decision 0.
#             `.Denied` alone is NOT a pass -- the broker's AUDIT_REQUIRED
#             failure path releases the waiter with `.Denied` and writes
#             no row at all (qdistro_admin_broker.py, DecideRequest).
#   budget  = whether that row's `ts` is PROVABLY inside the sender's
#             [start, start+180] window. Both `ts` (int(time.time()) in
#             qdistro_admin_audit.py) and `date +%s` FLOOR to whole
#             seconds, and the audit schema has no finer column, so a
#             decision a fraction of a second past the deadline can still
#             carry ts == deadline. BA17D=2 keeps the product-FAIL branch
#             strictly inside the provable region; ts in the last 2s of
#             the window (and up to 2s past it) is AMBIGUOUS and resolves
#             to ERROR -- a re-run -- never to FAIL.
VERDICT_B64=$(base64 -w0 <<'EOF'
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
EOF
)
$VMEXEC "$VM" "echo $VERDICT_B64 | base64 -d | bash"

# Evidence for whichever branch fired. Bounded by -n, deliberately NOT by
# --since: a time window here would be the same guessing game the
# timestamps above exist to replace.
$VMEXEC "$VM" 'journalctl -u qdistro-admin-broker.service -n 200 --no-pager'
```

**Assert**:
- `${QCI_SCENARIO_TMPDIR:-/tmp}/17-after.out` does NOT contain `please_deny_me`.
 (It may equal the baseline captured in Setup, or be empty if
 work2's qnotebook had no prior Receive this session.)
- `/tmp/17-relay.out` contains `org.qdistro.AdminBroker1.Denied` in
 its error-name line. A bare `NoReply` is NO LONGER ACCEPTED: with
 the explicit 180s bound in S1 the sender-visible denial contract
 — the thing this scenario exists to test — is required, not
 optional. (Non-delivery + `decision=0` prove suppression; they say
 nothing about what the caller was told.) A `method return` with no
 error is the worst outcome: success was reported to the caller.
 `.Denied` is NECESSARY but NOT SUFFICIENT: the verdict block below
 also requires the exact decision=0 audit row for this request, since
 the broker's `AUDIT_REQUIRED` failure path releases the caller with
 `.Denied` and writes no row at all.
- **The scenario verdict is the `RELAY_VERDICT=` line printed by S3;
 do not re-derive it by eye.** S3 first WAITS for the recorded sender
 process (`/tmp/17-relay.pid`, identity-checked against the `/proc`
 starttime recorded alongside it) to terminate, bounded by the sender's
 own 180s reply timeout plus 30s of grace, and classifies only after
 that. Both deny scenarios (13 and 17) use the identical contract; the
 only per-scenario difference is the `action` string and the `/tmp`
 prefix. The full branch table, in evaluation order:

 | condition (evaluated in this order) | verdict |
 |---|---|
 | `method return` on the wire AND this request's row records decision=1 | `FAIL reason=relay-succeeded-and-audit-records-allow` |
 | `method return` on the wire, any other case | `FAIL reason=deny-reported-to-caller-as-success` |
 | `sqlite3` missing in the guest | `ERROR reason=sqlite3-unavailable-in-guest` |
 | S1 recorded no audit baseline id | `ERROR reason=audit-baseline-id-missing-from-S1` |
 | S1 recorded no start stamp | `ERROR reason=relay-start-stamp-missing-from-S1` |
 | S1 recorded no sender pid | `ERROR reason=sender-pid-missing-from-S1` |
 | S1 recorded no sender `/proc` starttime | `ERROR reason=sender-pid-starttime-missing-from-S1` |
 | S1 captured no unambiguous broker request id | `ERROR reason=broker-request-id-not-captured-by-S1` |
 | the sender was still present at the end of its own timeout + 30s grace | `ERROR reason=sender-did-not-exit-within-its-own-timeout` |
 | no row carries this `request_id`, but rows for this action exist since the baseline | `ERROR reason=audit-rows-for-this-action-belong-to-another-request` |
 | `.Denied` AND no row for this `request_id` (and none for the action at all) | `FAIL reason=Denied-reply-with-no-audit-row` |
 | `.Denied` AND this request's row is not caller_uid 2000 / decision=0 | `FAIL reason=Denied-reply-but-audit-row-is-not-caller-decision0` |
 | `.Denied` AND this request's row is caller_uid 2000 / decision=0, `ts` `late` or unusable | `ERROR reason=Denied-reply-but-audit-ts-inconsistent-budget=late` (or `...=none`) |
 | `.Denied` AND this request's row is caller_uid 2000 / decision=0, `ts` consistent | `PASS` |
 | no reply on the wire, no row for this request | `ERROR reason=no-audit-row-for-this-request-admin-never-decided` |
 | no reply, this request's row is not caller_uid 2000 / decision=0 | `ERROR reason=audit-row-does-not-match-the-denied-request` |
 | no reply, exact row, `ts` not provably inside `[start, start+178]` | `ERROR reason=decision-not-provably-inside-sender-budget-budget=late` or `...budget=ambiguous` |
 | no reply, exact row provably in budget | `FAIL reason=decision-in-budget-but-no-Denied-reply` |

 Reading the table:
 - The `method return` branches are FIRST and deliberately do not wait on
 any precondition. `$PREFIX.out` is THIS sender's own stdout, so a
 success reply in it belongs to this request by construction and needs
 no correlation; a denied relay answered with success is a broker
 defect whatever else is missing, and must never be downgraded to a
 harness ERROR by an unrelated missing input.
 - Every other branch, `PASS` included, sits BEHIND the precondition
 block. Missing sender identity (pid or `/proc` starttime), a sender
 that was still present at the end of its own timeout, and a missing or
 ambiguous broker request id are all rejected there, before any
 classification runs. Without the starttime the waiter cannot tell
 "exited" from "pid recycled"; while the sender is alive `$PREFIX.out`
 may simply be unfinished; without the request id the audit row cannot
 be proved to belong to this request.
 - Every `FAIL` is a broker defect: a denied relay reported to the
 caller as success, a `.Denied` released without the durable
 decision=0 record (the broker's `AUDIT_REQUIRED` downgrade path does
 exactly that -- it calls the waiter with `False` and writes no row),
 or an admin decision that was recorded and then never reached the
 caller. Report the broker journal printed above.
 - Every `ERROR` is a HARNESS outcome. Record the scenario as ERROR and
 re-run; do NOT file it against the broker, and do NOT record it as a
 pass either.
 - `PASS` requires BOTH halves of the documented contract: the
 sender-visible `.Denied` AND the decision=0 audit row CARRYING THIS
 REQUEST'S `request_id`. Neither one alone is a pass, and both the
 preconditions and the audit half are checked before PASS is emitted,
 not after.
 - Correlation is by `request_id`, captured from the broker's own
 `GetPending` in S1 and matched exactly in S3. The older
 `id > baseline` + action + caller_uid + decision tuple is not unique:
 on a shared or manually re-driven VM a second request with the same
 action and uid can write a decision=0 row inside this run's window,
 and this request would have consumed it -- turning its own
 `AUDIT_REQUIRED` failure into a PASS. When rows for this action exist
 since the baseline but none carries this request's id, the run is
 contaminated and is reported ERROR; the row is never borrowed in
 either direction.
 - Residual ambiguity, stated explicitly: the audit `ts` column is
 `int(time.time())` -- whole seconds, floored -- and no
 finer-resolution source exists. The schema has no sub-second column,
 and the broker emits no journal line at all on a healthy deny
 decision (only the allow-path cache write and audit *failures*
 print), so sub-second correlation is unavailable. `BAND=2` is
 therefore applied on the no-reply branch: a decision whose audit `ts`
 lands in `[start+179, start+182]` is reported ERROR even when it was
 in fact in budget. That is a ~4s window at the very end of a 180s
 budget and costs at most a re-run; the product-FAIL branch never
 fires inside it. On the `.Denied` branch the `ts` check is only an
 internal-consistency guard -- the `.Denied` reached the sender inside
 its own 180s reply timeout, so in-budget delivery is already proved
 by the wire, and only a `late`/unusable `ts` (which contradicts that
 evidence) blocks the PASS. Staleness is not handled by the clock at
 all: the row must carry this request's `request_id` and an `id >`
 the baseline captured in S1 before the send.
 - If the diagnostics line shows `AUDIT_DECISION=1`, check the S2
 screenshot before filing anything: a driver that actuated
 **Approve** instead of **Deny** invalidates the run and must be
 recorded as ERROR, not as a product FAIL.
- Audit row: `2000|app.send-to:3000:org.qdistro.Qnotebook.uid3000|0|once|prompt`.

## Teardown

```bash
$VMEXEC "$VM" '
 pkill -u admin -f qdistro_admin_app 2>/dev/null || true
 pkill -u work -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 pkill -u work2 -f "python3 -m (zim_qt|qnotebook)" 2>/dev/null || true
 rm -f /tmp/17-*.out /tmp/17-relay.pid /tmp/17-relay.pidstart /tmp/17-relay.start /tmp/17-relay.baseid /tmp/17-relay.reqid
'
```

## Notes for the runner

- If S3 sees `please_deny_me` in `GetLastReceived`, that's a real
 regression — the broker relayed a denied message. Do NOT mask.
 Report the bug with the audit row, relay.out, and screenshot.
- If `/tmp/17-relay.out` does not contain `Denied` but
 GetLastReceived is clean, read `RELAY_VERDICT` rather than
 guessing. S3 has already waited for the sender process to exit, so
 the reply is not merely "still in flight": an `ERROR` means the
 decision was never provably recorded inside the sender's 180s budget
 (re-run, click sooner) or the sender itself wedged; a `FAIL` means an
 in-budget admin decision produced no caller reply and is a broker
 defect to file with the journal already captured in S3.
- A `Denied` in relay.out is NOT on its own a pass. The verdict block
 also requires the exact decision=0 audit row for this request, because
 the broker's `AUDIT_REQUIRED` failure path releases the caller with
 `.Denied` and writes no row.
