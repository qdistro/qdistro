# 09 — lock-surface live-capture / egress indicators (J28)

<!-- qci:visual: required -->

**Acceptance criterion (security):** while the machine is locked, every
active-capture indicator matches reality — a live microphone, camera,
system-audio or screencast capture is visible on the lock surface, capture
that starts or stops *while locked* is reflected, an observer that dies or
hangs shows as a **failure** rather than as a quiet machine, and active silo
network egress (including a silo in transient `Stopping`) is shown.

This is the live gate for `todo/fable-release/06-human-test-plan.md` H5 and
exit criterion 11 in `07-release-checklist.md`. The unit suite
(`tests/unit/test_indicators.py`) pins the derivation against synthetic
`pw-dump` payloads; it cannot prove that the **installed** module sees a
**real** graph, that the banner is painted, or that the running observer's
lock-edge / timeout / freshness lifecycle behaves. That is what this is for.

Two channels are used. The ctrl-socket channel is machine-checked at every
step; banner pixels are machine-checked at the steps where the *rendering* is
the property under test (1, 2, 3, 4, 7, 8.3) and merely recorded elsewhere —
each step says which:

1. **The running observer** — `qdlocker_ctrl indicators` returns a
   space-separated `key=value` line snapshotted from the live
   `LockIndicators` object inside qdlocker (introspection-gated; the GUI lane
   enables it via `qdlocker_session_healthy`). This is the authoritative
   channel: it reflects the actual service lifecycle, not a re-derivation.
2. **Pixels** — the banner border is exactly `#FD4663` (`Color.mError`) when
   capture is observed *or the observer has failed*, and absent when the
   observer is healthy and nothing is observed. Per project convention the
   ctrl-socket assertion is authoritative and the screenshot backs it up.

Conditional steps SKIP with an explicit printed reason and are reported as
SKIP — never silently passed. Steps 4 (system audio), 5 (camera), 6
(screencast) and 10 (second output) are conditional; 1, 2, 3, 7, 8 and 9 are
not. Step 6 needs a **manually** driven view stream: the scenario does not
reimplement a Wayland client, and its correlation check turns "no new node"
into a SKIP rather than a pass.

## Setup

**The commands live in two files next to this one, not in this markdown. Run
them; do not re-type them into a driver of your own.**

- [`09-capture-indicators.d/guest.sh`](09-capture-indicators.d/guest.sh) is
  the ONE claimed guest driver (root, Setup through Cleanup). It prints one
  `ASSERT <id> <PASS|FAIL|SKIP> <detail>` line per ctrl-socket assertion below
  and a final `VERDICT <PASS|FAIL|ERROR>`.
- [`09-capture-indicators.d/run.sh`](09-capture-indicators.d/run.sh) is its
  host side. It starts `guest.sh` through vm-exec and serves EVERY host step
  the driver publishes, by the name it publishes: `<id>-quiet` / `-alarm` /
  `-rec` capture `$QCI_GUI_ARTIFACT_DIR/<id>.png` with `qdwin_screenshot` and
  check the banner band (top `max(H/4, 220)` px, full width) for `#FD4663`
  ABSENT / PRESENT / not at all; `<id>-drain` runs
  `qdlocker_drain_lock_state`; `s10-heads` runs Step 10's two-head checks.

Why: every failing run of this scenario before 2026-09-28 lost its verdict to
a different slip in a hand translation of host-side vm-exec blocks into a
guest driver — a host loop serving a FIXED step list that waited for the
skipped conditional `s4` while the guest sat on `s7` (full-20260928T154720Z,
recorded as a transport ERROR), busctl calls missing the interface argument
and a system unit stopped through `runuser` (full-20260928T111118Z), a
cleanup that left the fixture silo busy (full-20260926T153217Z). None of
those was the product.

Run, from any directory, with the scenario's own path and the VM you were
given, in the FOREGROUND of ONE long-running command (4-6 min; it owns the
driver's vm-exec, so let it finish; never start a second copy while one runs):

```bash
bash <directory of this scenario>/09-capture-indicators.d/run.sh "$VMNAME"
```

(The guest step directory defaults to `/tmp/qci/qdlocker_tests_gui_09-capture-indicators.md`;
if your prompt names a different `/tmp/qci/<slug>/`, export `QDLOCKER_09_SLUG=<slug>` first.)

It writes to `$QCI_GUI_ARTIFACT_DIR`: `driver.log` (the guest driver's
output), `host.log`, `host-checks.tsv` (one row per host check),
`summary.txt`, and the frames `s1.png` ... Exit 0 = every assertion that ran
passed, 1 = an assertion FAILED, 3 = no verdict (ERROR). It ends with the
summary and `RESULT <PASS|FAIL|ERROR>`.

Then grade: OPEN EVERY FRAME it lists (each is a fresh capture) and check it
against the step's pixel assertion below; quote the `ASSERT` lines and the
`host-checks.tsv` rows in your report. The colour counts are machine checks
that back your reading of the frame; a frame whose content contradicts its
row (e.g. `s7.png` shows no alarm banner) is a FAIL whatever the count says.
If run.sh ends in ERROR, report ERROR with the `ERROR:` line from
`driver.log` / the ERROR rows — do not re-drive the scenario by hand.

Setup (in `guest.sh`): first reclaim whatever an earlier attempt left — a
host-step timeout stops the driver WITHOUT its teardown, so stale
`pw-record`/`parec`/`gst-launch-1.0`, the Stopping fixture, the Step 7
`pw-dump` break and a stopped `qdistro-session-manager` are undone here
(`reset_state`) — then enable ctrl introspection and the GUI-lane idle
override, restart qdlocker and wait for its ctrl socket, require
`qdwin-compositor.service` and `qdlocker.service` active, drain any lock
state (host step `setup-drain`), reclaim this scenario's own fixture silo
`qdlocker09` if an earlier attempt left it (SIGKILL what is left in its
cgroup, `StopSilo`, `DeleteSilo`), then `CreateSilo qdlocker09 3909` with
egress `none`. The qdwin golden intentionally has no work silos; this one is
removed in Cleanup.

## Steps

### Preflight A — VM graphics backend is compositing

Same purpose as `07-lock-occludes-desktop.md`: a VM whose DRM atomic commits
are all rejected produces black screenshots and every pixel assertion below
would be a false FAIL. That is an environment ERROR, not a product defect.
(The ctrl-socket channel still works in that state, so if you hit this,
re-run with only the `assert_ind*` checks and report the pixel checks as
BLOCKED rather than passed.)

In `guest.sh`: five or more `atomic: couldn't commit` / `repaint-flush failed` lines in the compositor journal end the run as ERROR (black frames would make every pixel check a false FAIL).

### Preflight B — the observer is installed, from the installed prefix

In `guest.sh`: `ASSERT B.1` (module path from `python3 -I` in `/`), `B.2` (`pw-dump`, `busctl`), `B.3` (the `indicators` verb). A B.1/B.2 failure stops the run before Step 1; B.3 is ERROR.

**Assert (B.1):** the module imports in isolated mode from an installed
prefix. A checkout-only import is a release blocker, not a test-env issue.
**Assert (B.2):** `pw-dump` and `busctl` exist.
**Assert (B.3):** the `indicators` verb answers — otherwise the whole gate is
unfalsifiable and must be reported BLOCKED.

### Step 1 — locked with nothing capturing: healthy, quiet, and NOT an all-clear

In `guest.sh`: lock, wait for `locked=True`, sleep 2 (the lock edge forces an immediate scan), `ASSERT 1.1`; host step **`s1-quiet`** → `s1.png`, `#FD4663` ABSENT; then `ASSERT 1.3`.

**Assert (1.1):** the observer is healthy and saw nothing.
**Assert (1.2):** no `#FD4663` in the banner band — a healthy quiet scan must
not look like an alarm.
**Assert (1.3):** every kind is still `unverified`; nothing reports `clear`.

### Step 2 — a real microphone capture starts WHILE LOCKED

In `guest.sh`: `pw-record --target=@DEFAULT_SOURCE@` as admin (detached, output to a file), sleep 6 (two polls; deliberately NO lock cycle), `ASSERT 2.1`; host step **`s2-alarm`** → `s2.png`, `#FD4663` PRESENT; then `ASSERT 2.3` compares `capture_attributed` with `capture_detail`.

**Assert (2.1):** the running observer reports an active microphone without
any lock/unlock cycle — the poll saw a capture that began while locked. This
is the core J28 property.
**Assert (2.2):** the banner turns alarming.
**Assert (2.3):** the banner wording must match `capture_attributed`. Read the
banner text off the screenshot: `1` requires `LIVE CAPTURE` **and** a client
name; `0` requires `CAPTURE ACTIVITY` **and** `client unknown`. Either value is
acceptable — the mismatch is not. `capture_detail` in the same line carries the
suffix, so the two can be compared without OCR: with `ATTR=0` the detail must
contain `client_unknown`, and with `ATTR=1` it must not.

### Step 3 — the capture stops while locked

In `guest.sh`: `pkill -u admin -x pw-record`, sleep 7, `ASSERT 3.1`; host step **`s3-quiet`** → `s3.png`, `#FD4663` ABSENT.

**Assert (3.1):** the alarm clears within two polls, on both channels. A
stuck-on indicator is as much a failure as a stuck-off one — it trains the
owner to ignore it.

### Step 4 — system-audio (sink-monitor) capture

In `guest.sh`: `MON=$(pactl get-default-sink)`; empty → `ASSERT 4 SKIP`. Otherwise `parec -d $MON.monitor`, sleep 6, `ASSERT 4.1` (systemAudio, and NOT microphone); host step **`s4-alarm`** → `s4.png`, `#FD4663` PRESENT; stop it, sleep 7, `ASSERT 4.2`.

**Assert (4.1):** a monitor capture is classified `systemAudio`, not
`microphone`. If it lands in `microphone`, the `stream.capture.sink`
discriminator did not fire on this stack — record the node props; that is a
real classification defect, since "the mic is live" and "your speakers are
being recorded" are different statements to the owner.

### Step 5 — camera (CONDITIONAL)

In `guest.sh`: find a `Video/Source` node with the installed parser; none, or no `gst-launch-1.0` → `ASSERT 5 SKIP`. Otherwise drive it with `gst-launch-1.0 pipewiresrc path=<id> ! fakesink`, check it stayed up (else SKIP with its log), `ASSERT 5.1`; host step **`s5-alarm`** → `s5.png`, `#FD4663` PRESENT.

**Assert (5.1):** a camera stream classifies as `camera`, not `screencast`.
If it lands in `screencast`, the camera hints (`media.role`, `device.api`,
name matching) need this VM's real property shape added to them — capture the
offending node's props into the report.

### Step 6 — screencast via qdwin's view-stream (CONDITIONAL, MANUAL DRIVER)

The only screencast signal the observer can see is the `weston.pipewire-N`
node qdwin publishes when it pins a forwarded toplevel onto a
`backend-pipewire` output. Driving that needs the multimachine harness's
view-stream subscription — this scenario does **not** reimplement a Wayland
client. Correlation is asserted: the node set is sampled before and after, so
a pre-existing node cannot make the step trivially green.

In `guest.sh`: count `weston.pipewire` nodes, print the manual-runner notice, wait 5 s, count again. No new node → `ASSERT 6 SKIP` (the normal qci outcome: nothing in this lane starts a view stream). A new node → sleep 4, `ASSERT 6.1`; host step **`s6-alarm`** → `s6.png`, `#FD4663` PRESENT.

**Assert (6.1):** a newly created `weston.pipewire-N` node is observed as
`screencast` while the stream is live.
**Not asserted, by design:** a direct `weston_capture_v1` grab is invisible to
this observer (`doc/sessions.md`). Do not add an assertion that pretends
otherwise.

### Step 7 — observer failure must be visible (the most important step)

In `guest.sh`: a `pw-dump` that sleeps 300 s first on qdlocker.service's PATH (user drop-in `91-break-pwdump.conf`), daemon-reload, restart qdlocker and wait for its socket, lock, sleep 6 (past the 2.5 s scan timeout), `ASSERT 7.1`; host step **`s7-alarm`** → `s7.png`, `#FD4663` PRESENT; `ASSERT 7.3` greps admin's qdlocker.service journal.

**Assert (7.1):** `capture_observer=failed` — a hung `pw-dump` is reported as
a failed observer, not as a quiet machine.
**Assert (7.2):** the banner is ALARMING (error colour present). If the banner
is dim here, the fail-visible property is broken and this is the most serious
failure this scenario can report.
**Assert (7.3):** the journal shows the scan being killed — the hard timeout
fired rather than the scan hanging forever. vm-exec runs as root and
qdlocker.service is admin's USER unit, so the query must run as admin
(`runuser -l admin -c "journalctl --user ..."`, as `guest.sh` does); a root
`journalctl --user` reads root's own user journal and proves nothing.

Recovery (also asserted, so a failure cannot be sticky):

In `guest.sh`: remove the drop-in and the fake `pw-dump`, daemon-reload, restart qdlocker, lock, sleep 3, `ASSERT 7.4`.

**Assert (7.4):** the observer recovers to `ok` after the tool is restored.

### Step 8 — silo egress, including transient `Stopping` and an unreachable manager

In `guest.sh`: `SetSiloEgress qdlocker09 direct` + `StartSilo`, sleep 4, `ASSERT 8.1`; host step **`s8a-rec`** → `s8a.png` (recorded only). Plant a SIGTERM-ignoring process in the silo's cgroup, `StopSilo qdlocker09 30` in the background, observe `Stopping` through the installed parser (`ASSERT 8.2`), sleep 4, `ASSERT 8.2` (still shown). Then ROOT `systemctl stop qdistro-session-manager.service` (a system unit — never through `runuser`), sleep 5, `ASSERT 8.3`; host step **`s8b-alarm`** → `s8b.png`, `#FD4663` PRESENT (the egress-unverified row); start it again, sleep 4, `ASSERT 8.4`.

**Assert (8.1):** an `Active` silo with `direct` egress is shown.
**Assert (8.2):** the same silo is STILL shown while `Stopping` — the session
manager emits that state before SIGTERM, the grace wait, SIGKILL and egress
teardown, so the network path can still exist.
**Assert (8.3):** with the session manager stopped, egress reads
`egress_observer=failed`, and the banner shows the "network egress state
unverified" row — never a silent "no egress".
**Assert (8.4):** it recovers when the unit comes back.

### Step 9 — locked-state restart of qdlocker

In `guest.sh`: host step **`s9-drain`**, lock, restart qdlocker while locked, sleep 5 and wait for its socket, `ASSERT 9.1` (`locked=True` and `capture_observer=ok`); host step **`s9-rec`** → `s9.png` (recorded only).

**Assert (9.1):** after a restart while locked, qdlocker comes back locked
(`initially_locked` at bind time) **and the observer has produced a fresh
reading** (`capture_observer=ok`). A locker that came up locked without
re-scanning would leave the indicator permanently blank — that specific
regression is what this step exists to catch. The `ready(initially_locked=1)`
path is the only thing that can drive it, so this is a genuine test of that
wiring, not of the poll.

### Step 10 — second output (CONDITIONAL; documents a KNOWN GAP)

Requires a VM booted with two enabled heads. A successful
`virsh screenshot --screen 1` alone is **not** proof — a domain can expose a
scanout the compositor never enabled — so the compositor's own output count
is checked first.

In `guest.sh`: count distinct `output_created ... name=` in the compositor journal; fewer than 2 → `ASSERT 10 SKIP`. Otherwise host step `s10-drain`, lock, sleep 3, host step **`s10-heads`**: run.sh takes `virsh screenshot --screen 1` and `--screen 0` and checks 10.1 (secondary uniformly black) and 10.2 (primary not one flat colour — the weak check noted below).

**Assert (10.1):** the secondary output is uniformly black. No desktop pixel
may appear on any output. A failure here is a qdwin lock-curtain leak and
outranks every other finding in this scenario.
**Assert (10.2) — the known gap, asserted as current behaviour:** the banner
is on the PRIMARY output only, because qdlocker creates a single fullscreen
window and qdwin fullscreens it onto `qdwin_primary_output()`. See
`todo/fable-release/12-j28-multi-output-lock-indicators.md`. **When per-output
locker windows land, 10.2 inverts** — the banner must then be present on every
head, and this step becomes its gate.

**NOT AUTOMATED — output hotplug while locked.** `qdwin_on_output_changed`
does not re-install the lock curtain (only output *removal* and the
output-management apply path do), so an output attached while locked is
expected to fall outside the curtain. There is no scripted head-hotplug for
this VM setup, so this is a **manual** check: attach a head while locked and
observe. It is a pre-existing qdwin defect recorded in the decision note, not
a J28 regression — do not report it as a J28 failure.

## Cleanup

In `guest.sh` (also its EXIT trap if it dies mid-run; a host-step timeout stops it WITHOUT teardown by design): stop `pw-record`/`parec`/`gst-launch-1.0`, SIGKILL the Stopping fixture if it is still in the silo cgroup, remove the break drop-in (restarting qdlocker if it was still in place), daemon-reload, start the session manager, `SetSiloEgress none`, `StopSilo 0`, `DeleteSilo` (retried 20 s); then host step `cleanup-drain`.

## Known-broken-if

- **Preflight B.1 resolves to a checkout** — the wheel does not ship
  `indicators.py` and the whole feature is unreachable in production. Release
  blocker (see `10-reachability-audit-2026-07-26.md`), not a test-env issue.
- **Preflight B.2 fails on `pw-dump`** — the image lacks `pipewire-tools`.
  The indicator would read "unverified" forever: honest, but useless.
- **Preflight B.3 fails** — introspection is not enabled, so every
  `assert_ind` would be vacuous. Report BLOCKED, never PASS.
- **Step 2 passes but Step 3 never clears** — the poll or the freshness
  horizon is wedged; check the journal for a stuck scan.
- **Step 4 fails its "not microphone" check** — the `stream.capture.sink`
  discriminator (or the `.monitor` name check on the device node) did not fire
  on this stack. Record the node props; "your mic is live" and "your speakers
  are being recorded" say different things to the owner.
- **Step 7 shows the dim banner instead of an alarm** — the fail-visible
  property is broken: an unobserved machine looks safe. Most serious failure
  available here.
- **Step 8 hides the `Stopping` silo** — the egress row is fail-silent during
  teardown.
- **Step 9 comes back locked but `capture_observer` is not `ok`** — the
  `ready(initially_locked)` path does not drive a rescan, so a restart while
  locked leaves the indicator blank.
- **Step 10.1 shows desktop pixels on the secondary output** — qdwin
  lock-curtain leak; outranks everything else here.
