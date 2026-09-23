# 04 — content client hangs; chrome stays responsive

**What**: SIGSTOP the content process (weston-terminal) and verify
that qdshell's chrome is still responsive — `ctrl.sh state 1` returns
promptly (it scrapes `qdshell.log`, which only stays current if the
bystander keeps emitting `toplevel_state` lines), `min 1` succeeds at
the protocol level (qdwin moves the view to `minimized_layer`), and
the screenshot reflects the change.
Then SIGCONT and verify nothing broke.

**Why**: this is the canonical demonstration of the chrome/content
independence principle (`the architecture doc` § "Chrome and
content independence" — property 1: hung content doesn't freeze
chrome). qdshell's event loop, frame callbacks, and protocol
exchange with qdwin are independent of the application client. A
regression that wired chrome lifecycle to content lifecycle would
manifest here as "qdshell stalls when terminal stalls."

## Setup

```bash
ID=04-content-hung-chrome-responsive
HT=tests/host
$HT/start.sh $ID
```

## Steps

### S1 — baseline + hang the content

```bash
SHOT=$($HT/screenshot.sh $ID 01-baseline)
TERM_PID=$(cat /tmp/qdwin-host-tests/$ID/pids/terminal)
echo "terminal pid=$TERM_PID"
kill -STOP $TERM_PID
sleep 0.3
```

**Read `$SHOT`.**

**Assert (baseline + hang prepared):**
- Baseline screenshot looks like scenarios 01–03 (terminal +
  cyan chrome).
- `kill -STOP` succeeds (no error in the output).

### S2 — chrome ctrl-socket still responds

```bash
$HT/ctrl.sh $ID list
$HT/ctrl.sh $ID state 1
```

**Assert (qdshell alive):**
- `list` returns within ~1 second; doesn't hang. Its `ok` is from
  ctrl.sh acknowledging the FIFO write (timeout 5s) — the actual
  toplevel handles land in `qdshell.log` as
  `qdwin-bystander: tracked toplevels: 1`.
- `ctrl.sh $ID state 1` prints `ok state=0x0` (scraped from
  `qdshell.log`).
- Both ctrl.sh invocations print a line starting with `ok` on
  stdout.

### S3 — minimise the hung window

```bash
$HT/ctrl.sh $ID min 1
sleep 0.4
SHOT=$($HT/screenshot.sh $ID 02-min-while-hung)
$HT/ctrl.sh $ID state 1
```

**Read `$SHOT`.**

**Assert (minimised even though content hung):**
- Screenshot is entirely black — qdwin moved the view (and chrome)
  to the hidden layer regardless of the content client's state.
  The compositor doesn't wait for the client to ACK anything; the
  view simply isn't composited any more.
- `ctrl.sh $ID state 1` prints `ok state=0x4` (minimised bit).

### S4 — un-hang and un-minimise

```bash
kill -CONT $TERM_PID
$HT/ctrl.sh $ID raise 1
sleep 0.4
SHOT=$($HT/screenshot.sh $ID 03-after-cont-raise)
$HT/ctrl.sh $ID state 1
```

**Read `$SHOT`.**

**Assert (everything recovers):**
- Window is back at the baseline position + size.
- Cyan chrome present.
- Terminal prompt visible inside (the SIGCONT'd process is
  responsive again — its surface keeps the buffer it last
  committed before the STOP, so the prompt is unchanged).
- `ctrl.sh $ID state 1` prints `ok state=0x0`.

## Teardown

```bash
# Make sure the terminal isn't still SIGSTOP'd before we kill it.
TERM_PID=$(cat /tmp/qdwin-host-tests/$ID/pids/terminal 2>/dev/null || echo)
[ -n "$TERM_PID" ] && kill -CONT $TERM_PID 2>/dev/null
$HT/stop.sh $ID
```

## Notes for the runner

- The "ctrl-socket responds quickly" assertion (S2) is structural —
  if qdshell were sharing an event loop with the content client, a
  SIGSTOP'd terminal would back-pressure the wayland connection and
  qdshell's reads would block. ctrl.sh wraps the FIFO write in
  `timeout 5`; a dead-or-frozen bystander therefore manifests as a
  fast non-zero exit (exit 124 from timeout) rather than an infinite
  hang. If ctrl.sh exits non-zero in S2, FAIL.
- If S3's screenshot still shows the terminal (the minimise was
  ignored), FAIL — that's a real bug in the compositor: it should
  not wait for the client to acknowledge a hide.
- This scenario is also the most useful one for catching future
  regressions where someone "helpfully" adds a "wait for client
  ack" path in qdwin's chrome code.
