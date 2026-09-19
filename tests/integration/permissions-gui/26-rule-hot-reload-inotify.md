# 26 — Rules reload automatically via inotify on file drop

<!-- qci:visual: none -->

**What**: with no rule installed, write a new allow-rule YAML
*directly* into `/etc/qdistro/rules.d/` (bypassing `SaveRule`), wait
for the inotify watcher's debounce, verify the `RulesReloaded`
signal fired and the rule is live without any explicit reload call.

**Why**: `SaveRule` calls `reload_rules_from_disk` itself, so its
hot-reload path is trivially exercised by scenarios 24 / 25. The
*inotify* watcher is the failsafe for admins (or future tooling)
that drop files into the directory the old-fashioned way — e.g. a
config-management run, a git pull on `/etc/qdistro/rules.d/`, an
admin editing in vim. Without inotify the broker would silently
serve stale rules until restart. The 200ms debounce coalesces
batch drops; we test by polling, for roughly 30s (60 half-second
iterations plus per-iteration overhead, not a hard elapsed deadline),
for a `RulesReloaded` signal that is stamped AFTER the drop and
counts at least one rule.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

$VMEXEC "$VM" 'pkill -f "[d]bus-monitor.*Rules" 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
# Remove the rule files, not just the NN-prefixed ones. The scenario's premise
# is "with no rule installed", and a surviving hand-authored .yaml/.yml would
# make the broker's pre-drop emits carry a POSITIVE count -- which is exactly
# what S2's `count >= 1` predicate keys on, so a late-stamped pre-drop signal
# could then satisfy it.
# NOT exhaustive, precisely: a shell glob skips DOTFILES, while the loader's
# `os.listdir` + suffix filter does not (broker/qdistro_admin_rules.py:379-380),
# so a `.hidden.yaml` survives this line. It does not silently corrupt the
# scenario -- if it loads, S1's verified-empty baseline fails; if it is invalid,
# S1's error-array check fails -- but this cleanup is not the guarantee; S1 is.
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/*.yaml /etc/qdistro/rules.d/*.yml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
# Liveness only. NOTE what this does NOT buy: the broker is `Type=dbus` with
# `BusName=org.qdistro.AdminBroker1`, so `systemctl restart` has ALREADY waited
# for the name to be acquired -- a bus-name gate here would return instantly and
# prove nothing. The startup `RulesReloaded` fires from a 50ms GLib timeout
# registered in __init__ and serviced once the mainloop runs; systemd completes
# the restart job on name acquisition, which falls between those two points.
# Neither event orders the other, so the startup emit CAN land after a monitor
# that attached moments after restart returned. Nothing in setup can close that; the
# drop-scoped assertion in S2/S3 is what actually makes this scenario sound.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && \
  await_system_unit_active qdistro-admin-broker.service 30 1'

APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
```

## Steps

### S1 — attach dbus-monitor on `RulesReloaded`

```bash
$VMEXEC "$VM" 'rm -f /tmp/26-signals.log; \
  setsid dbus-monitor --system \
    "type=signal,interface=org.qdistro.AdminBroker1,member=RulesReloaded" \
    >/tmp/26-signals.log 2>&1 </dev/null &
  echo $! >/tmp/26-monitor.pid'

# The shell creates /tmp/26-signals.log the instant it opens the redirect --
# BEFORE dbus-monitor has connected or installed its match rule. Testing for
# the file (or for the pid) therefore proves nothing, and a reload landing in
# that gap is simply never recorded, which would make S2 time out on perfectly
# correct product behaviour. Prove the SUBSCRIPTION delivers instead: wait for
# the monitor's own `NameAcquired`, then trigger a harmless `ReloadRules` and
# require the signal count to grow. Neither a PRE-DROP startup emit nor the
# handshake reload can be mistaken for the signal S3 grades -- not because of
# when they land, but because the baseline is verified empty, so any emit made
# before the drop necessarily carries a count of 0 while S3 requires >= 1.
# (Stated as pre-drop deliberately: the startup callback reads whatever count is
# loaded WHEN IT RUNS, so a startup emit serviced after a post-drop reload could
# carry a positive count. That cannot pass an unarmed watcher -- something must
# have loaded the new rule first -- but the guarantee we rely on is the narrow
# one about emits that predate the drop.)
HANDSHAKE_B64=$(base64 -w0 <<'EOF'
for _i in $(seq 1 60); do
  grep -q 'member=NameAcquired' /tmp/26-signals.log && break
  sleep 0.5
done
grep -q 'member=NameAcquired' /tmp/26-signals.log || {
  echo "dbus-monitor never connected" >&2; exit 1; }
# Count BEFORE, and require the count to GROW. Grepping for "any
# RulesReloaded" would be satisfied by a signal already in the log, so S1 could
# report success having proved nothing -- and with no exit-status check it did
# exactly that even when the call failed.
#
# What growth DOES prove: at least one more broker signal reached this monitor
# after the count SNAPSHOT -- which may be a signal that landed between the
# snapshot and our method call. That is enough for the only claim made here:
# the subscription delivers.
# What it does NOT prove, stated plainly because two earlier versions of this
# comment claimed otherwise: it does not prove the observed signal is OURS, and
# it orders nothing against DROP_T. A startup emit still queued at snapshot
# time can be the one that lands while ours stays queued, to be written later
# with a RECEIVE stamp after DROP_T (dbus-monitor stamps on receipt, not on
# emission).
#
# The real guarantee is not chronological at all; it is EXCLUSION BY CONTENT,
# from the VERIFIED EMPTY BASELINE below: every emit that predates the drop --
# startup or handshake -- necessarily carries `int32 0`, and S2 grades on
# `count >= 1`. A late receive stamp on such a signal is rejected on its COUNT,
# whatever its timestamp.
before=$(grep -c 'member=RulesReloaded' /tmp/26-signals.log 2>/dev/null || true)
: "${before:=0}"
# And FAIL on a failed call: without this a broken/denied ReloadRules is
# invisible, and the timeout below then blames the monitor for it.
reply=$(dbus-send --system --print-reply --reply-timeout=5000 \
  --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ReloadRules 2>&1) || {
  echo "ReloadRules call FAILED -- not a monitor problem: $reply" >&2
  exit 1; }
# ReloadRules returns (count, errors). VERIFY THE BASELINE IS EMPTY here: the
# whole post-drop predicate is "count >= 1", so it is only meaningful if every
# emit that predates the drop necessarily carried 0.
baseline=$(printf '%s\n' "$reply" | awk '$1 == "int32" { print $2; exit }')
if [ "${baseline:-x}" != 0 ]; then
  echo "baseline is not empty (ReloadRules counted '${baseline:-?}' rules); a" >&2
  echo "pre-drop signal could then satisfy the post-drop count predicate" >&2
  printf '%s\n' "$reply" >&2
  exit 1
fi
# A count of 0 means zero rules LOADED, which is not the same as a clean load:
# a missing PyYAML, or a .yaml the rm should have removed but which failed to
# parse, also yields 0 with a POPULATED error array. Accepting that would let
# S2 blame inotify for a broken loader or an unclean setup.
if printf '%s\n' "$reply" | sed -n '/array \[/,$p' | grep -q '^ *string "'; then
  echo "ReloadRules reported load errors; setup is not clean" >&2
  printf '%s\n' "$reply" >&2
  exit 1
fi
echo "baseline verified empty: ReloadRules counted 0 rules"
for _i in $(seq 1 60); do
  now=$(grep -c 'member=RulesReloaded' /tmp/26-signals.log 2>/dev/null || true)
  : "${now:=0}"
  [ "$now" -gt "$before" ] && {
    echo "monitor subscription verified: ReloadRules signal observed"; exit 0; }
  sleep 0.5
done
echo "monitor attached but never received RulesReloaded from an explicit ReloadRules" >&2
cat /tmp/26-signals.log >&2
exit 1
EOF
)
$VMEXEC "$VM" "echo $HANDSHAKE_B64 | base64 -d | bash"
```

**Assert**: the handshake above exits 0, printing
`monitor subscription verified`. Quote that line.

Do NOT require the log to be header-only, and do NOT grade any signal
present at this point as the inotify reload. Two emits legitimately
predate the drop: the broker's startup `RulesReloaded` (in
full-20260911T070416Z it arrived 12ms after `NameAcquired` carrying
`int32 0`, and grading it as the inotify one failed the scenario) and
the handshake's own `ReloadRules`. S2/S3 are immune to both because
they require a count of `>= 1` AND a `time=` after the drop — and the
count is the load-bearing half, since S1 verified the baseline empty so
every pre-drop emit carries `int32 0` no matter when it is stamped.

### S2 — drop the rule file directly (NO `SaveRule`)

```bash
B64=$(base64 -w0 <<'EOF'
cat >/etc/qdistro/rules.d/26-inotify-allow.yaml <<'YAML'
- name: allow-work-test-action-via-inotify
  decision: allow
  match:
    uid: 2000
    action: test.action
  rationale: scenario 26 — direct file drop, inotify must catch it
YAML
EOF
)
# Record the guest's clock immediately BEFORE the drop. dbus-monitor stamps
# every signal with its own RECEIVE time, so this instant is a receive-time
# threshold -- NOT a separator between emissions: the monitor can stamp an
# earlier emit after DROP_T. What excludes the earlier emits (the broker's
# startup one and S1's handshake) is the verified EMPTY baseline plus the
# count check: both of those carry count 0 and cannot satisfy a positive
# count, however late they are stamped. Keep it: S3 grades against it.
DROP_T=$($VMEXEC "$VM" 'date +%s.%N' | tr -d '\r')
# FAIL CLOSED on an unreadable clock. The pipe above masks vm-exec's exit
# status, and an empty DROP_T would make the awk filter below compare against
# t=0, so every signal in the log would satisfy "after the drop". That
# INVALIDATES THE TIME PREDICATE -- it does not by itself pass the earlier
# signals, which still fail the count comparison against a verified empty
# baseline. Keep the guard: a scenario with one of its two predicates
# silently disabled is not the scenario we think we are running.
# ANCHORED regex, not a glob: `[0-9]*.[0-9]*` accepts `0junk.1` (awk reads it
# as 0, so every stale signal passes the time filter again) and accepts
# embedded whitespace, which would also break the generated awk command line.
if ! [[ $DROP_T =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: could not read the guest clock (DROP_T='$DROP_T')" >&2; exit 2
fi
echo "DROP_T=$DROP_T"   # evidence: S3 grades signals against this instant
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# inotify-debounce is 200ms, and the broker coalesces on top of that. WAIT for
# the signal rather than sleeping a fixed interval: under an 8-worker run the
# reload can land later, and a fixed sleep is also the step an agent is most
# likely to skip -- in full-20260911T070416Z only 0.45s of wall clock elapsed
# between the monitor attaching and S4, against the >=4s of fixed sleeps the
# scenario documented AT THE TIME. Bounded by 60 half-second iterations (so
# roughly 30s plus per-iteration overhead, not a hard wall-clock deadline) and
# fails LOUD; it never accepts a missing reload.
POLL_B64=$(base64 -w0 <<EOF
for _i in \$(seq 1 60); do
  if awk -v t="$DROP_T" '
       /member=RulesReloaded/ {
         split(\$0, f, "time=");    split(f[2], g, " ");
         if (g[1] + 0 > t + 0) { want = 1; hdr = \$0 } else { want = 0 }; next
       }
       want && /int32/ { if (\$NF + 0 >= 1) { found = 1; hit = hdr }; want = 0 }
       END { if (found) print "matched: " hit; exit !found }' /tmp/26-signals.log; then
    echo "post-drop RulesReloaded with count >= 1 observed"; exit 0
  fi
  sleep 0.5
done
echo "TIMEOUT: no post-drop RulesReloaded with count >= 1 after ~30s of polling (DROP_T=$DROP_T)" >&2
echo "--- signals log ---" >&2; cat /tmp/26-signals.log >&2
exit 1
EOF
)
$VMEXEC "$VM" "echo $POLL_B64 | base64 -d | bash"
```

### S3 — verify `RulesReloaded` fired

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/26-monitor.pid) 2>/dev/null; sleep 0.3; cat /tmp/26-signals.log'
```

**Assert** (textual analysis of `/tmp/26-signals.log`):
- At least one `member=RulesReloaded` signal block carries a
  `time=` GREATER than the `$DROP_T` recorded in S2. A signal that
  predates the drop cannot be the inotify reload — it is the startup
  emit or the S1 handshake reload (expect BOTH; the passing run
  gui-20260911T105937Z logged two pre-drop `int32 0` signals) — and
  must NOT be graded as one.
- That post-drop signal's body is `int32 N` with `N ≥ 1` (the broker
  counted at least the newly-dropped rule).

The S2 poll already enforces exactly this and fails loud on timeout,
so quote its output plus the matching signal block as justification.

### S4 — `ListRules` reports the new rule

```bash
$VMEXEC "$VM" 'runuser -u admin -- dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.ListRules'
```

**Assert**: output contains a dict entry with
`name = "allow-work-test-action-via-inotify"`, `decision = "allow"`,
`action = "test.action"`, `uid = 2000`,
`source_path = "/etc/qdistro/rules.d/26-inotify-allow.yaml"`.

### S5 — rule is live: `work` action gets ALLOWED, no prompt

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 26-work work 'python3 /usr/local/bin/qdistro-test-permission'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2
# bg_wait, never `wait $(cat X.pid)` — that does not wait in a separate guest
# shell (AGENTS.md, "A backgrounded job"). A TIMEOUT here IS this step's failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 26-work 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 26-work; echo "rc=$(bg_rc 26-work)"'
$VMEXEC "$VM" 'dbus-send --system --print-reply \
  --dest=org.qdistro.AdminBroker1 \
  /org/qdistro/AdminBroker1 \
  org.qdistro.AdminBroker1.GetPending'
```

**Assert**:
- `/tmp/26-work.log` contains `ALLOWED`.
- `GetPending` output is `array []`.

## Teardown

```bash
$VMEXEC "$VM" 'kill $(cat /tmp/26-monitor.pid) 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/26-signals.log /tmp/26-monitor.pid'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/*.yaml /etc/qdistro/rules.d/*.yml'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
APPROVALS_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM approvals WHERE action='test.action';
SQL_EOF
)
AUDIT_SQL_B64=$(base64 -w0 <<'SQL_EOF'
DELETE FROM audit WHERE action='test.action';
SQL_EOF
)
$VMEXEC "$VM" "echo $APPROVALS_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite"
$VMEXEC "$VM" "echo $AUDIT_SQL_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite"
$VMEXEC "$VM" 'rm -f /tmp/26-work.log /tmp/26-work.pid'
```

## Notes for the runner

- If S3 shows no POST-DROP `RulesReloaded` *after S1 verified the
  subscription*, the inotify path is the leading suspect — but do NOT
  file it as a broker regression until you have ruled out the cheaper
  explanations, because S1's handshake proves the subscription worked
  THEN, not that it survived. Check, from the artifacts: the monitor
  process is still alive (NOT that its log is growing -- a healthy
  filtered monitor is silent between matching signals, so log growth
  is not a health signal; if you need to know the subscription is
  still live, send another delivery probe as a DIAGNOSTIC, after the
  failed assertion, never as evidence that satisfies the poll); the
  rule file actually landed in `/etc/qdistro/rules.d/` with readable
  content;
  the broker did not restart mid-scenario; and the runner's own poll
  is sound (the passing run gui-20260911T105937Z had a rewritten poll
  report TIMEOUT while the correct signal was in the log — an agent
  bug, not a product one). Also check the broker journal for a reload
  or LOAD ERROR after the drop: a readable rule file is not proof it
  loaded. With all of those excluded, one CANDIDATE cause is the
  inotify watcher tearing down on a broker reload loop and never
  re-arming -- named as a place to look, not as a ranking: we have no
  evidence about which cause is most common.
- TWO `RulesReloaded` emits legitimately predate the drop: the startup
  one from `_emit_startup_rules_reloaded` (captured or not, depending
  on monitor-attach timing) and S1's own handshake reload. Neither is
  evidence of inotify. Grade ONLY a signal stamped after `$DROP_T`
  with a count >= 1. The COUNT is what makes this safe: the baseline
  is verified empty in S1, so both pre-drop emits carry 0 and are
  rejected even if the monitor writes one of them late, with a
  receive stamp after `$DROP_T`.
- S2 polls for roughly 30s (60 half-second iterations plus overhead,
  not a hard deadline) rather than sleeping a fixed interval. The 200ms debounce + tiny reload work means a passing
  scenario typically reports it within a second; the budget is there
  to stay robust under an 8-worker run, and it fails LOUD on timeout.
