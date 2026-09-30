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

# S1's send + request-id capture and S3's verdict are ONE shipped guest
# script, 17-relay.sh. Push it once and call it; do not re-type its logic
# into a driver of your own (a re-typed GetPending parser lost the
# 2026-09-30 full run as BROKER_REQUEST_ID=none).
RELAY_B64=$(base64 -w0 < "${QDISTRO_REPO}/tests/integration/permissions-gui/17-relay.sh")
$VMEXEC "$VM" "echo $RELAY_B64 | base64 -d > /tmp/17-relay.sh"

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
      timeout 10 xdotool windowactivate --sync "$w" windowraise "$w"
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
# Baselines the audit table, stamps the start, sends the relay in the
# background with --reply-timeout=180000, records the sender pid + /proc
# starttime, and captures the broker's own request id from GetPending
# (exactly one pending row for this action and caller uid). It prints
# `broker request_id=<n>`; anything other than a number there makes S3 an
# ERROR, never a pass. Run it exactly as written, from a claimed driver or
# directly through vm-exec.
$VMEXEC "$VM" 'bash /tmp/17-relay.sh send'
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
title_rc=0
$VMEXEC "$VM" 'for _ in $(seq 1 60); do
  t=$(runuser -u admin -- env DISPLAY=:0 xdotool search --name "^admin approvals" getwindowname 2>/dev/null | head -1)
  [ "$t" = "admin approvals" ] && exit 0
  sleep 0.5
done
echo "title never settled: $t" >&2; exit 1' 2>"${QCI_SCENARIO_TMPDIR:-/tmp}/17-s2-title.err" || title_rc=$?
echo "$title_rc" >"${QCI_SCENARIO_TMPDIR:-/tmp}/17-s2-title.rc"
echo "title-wait rc=$title_rc"
cat "${QCI_SCENARIO_TMPDIR:-/tmp}/17-s2-title.err"
```

**Readiness, step 1 — title wait** (up to 60 polls, ~30 s). Run the
block above as its own command and record the printed `title-wait rc=`
line together with the stderr shown after it (both are also kept in
`${QCI_SCENARIO_TMPDIR:-/tmp}/17-s2-title.rc` and `.err`; the `|| title_rc=$?` form keeps them
even in a shell with errexit on). Any rc other than 0 is a
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
   $VMGUI "$VM" view-copy /tmp/17-s2-denied-N.png --out /tmp/17-s2-denied.png
   ```

4. Otherwise, if N < 5: `sleep 2`, increment N, and go back to 1.
5. If frame 5 still does not show the empty state, the surface stayed
   stale across five frames with 2 s pauses between them (8 s of sleeps
   plus capture and grading time) after the model emptied: S2 FAILS. Copy the last
   frame to the canonical path and grade that below:

   ```bash
   $VMGUI "$VM" view-copy /tmp/17-s2-denied-5.png --out /tmp/17-s2-denied.png
   ```

Keep every numbered frame; do not delete or overwrite them.
The canonical copy is made with `view-copy`, not `cp`: it carries the frame's
raw identity (`.raw` sidecar, lineage to frame N) and a size of its own. You
have already looked at frame N, and a same-size twin of a frame you have seen
is read as black where it repeats.

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
# The whole classification is `bash /tmp/17-relay.sh verdict`; its
# RELAY_VERDICT= line IS the scenario verdict. Do not rebuild it.
$VMEXEC "$VM" 'bash /tmp/17-relay.sh verdict'

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
 rm -f /tmp/17-*.out /tmp/17-relay.pid /tmp/17-relay.pidstart /tmp/17-relay.start /tmp/17-relay.baseid /tmp/17-relay.reqid /tmp/17-relay.sh
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
