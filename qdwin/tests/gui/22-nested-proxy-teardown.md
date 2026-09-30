# 22 — nested-proxy teardown with a LIVE dependent (iso2/10 E2 gate)

<!-- qci:visual: required -->

**What**: destroy a nested-compositor advertiser while the shell still holds a
server-owned dependent on the proxy it advertised — a live `view_stream`, a
live chrome popup, or a live move-drag — and assert the compositor survives and
the session keeps working.

**The three steps are not equally strong, and this document does not pretend
otherwise.** S2 (stream) has a real oracle and is the one already proven
non-vacuous by fault injection. S3 (popup) has an EVENT-only oracle:
`dismissed` firing is necessary but not sufficient for the parent pointer
having been released, and it has never run against a pointer. S1 (move) has no
oracle at all for grab release — it is a crash/liveness check. S4 is what
proves only that KEYBOARD input still reaches an ordinary client afterwards —
it does not exercise the pointer, so it does not detect a leftover pointer
grab. Each section says which it is; do not summarise this scenario as "proves
the dependents are released".

**Why**: `qdwin_toplevel` is pointed at by three things that outlive an ordinary
client request: `qdwin_popup::parent`, the active move-drag (by handle), and any
exported `view_stream` (`qdwin_view_stream::tl`). Before qdwin `0ed786d`,
`qdwin_surface_removed` released all three before `free(tl)` and
`qdwin_nested_proxy_destroy` released none of them, so an advertiser disconnect
while qdshell held a stream or a popup left `s->tl` / `p->parent` dangling into
the input-inject and grab callbacks — a compositor use-after-free, not a client
disconnect (`todo/iso2/10-qdwin-shell-lock.md` E2). `0ed786d` routed both paths
through one `qdwin_toplevel_release_dependents()`.

That fix shipped with a source invariant and an ASan behavioural companion, but
**no live lane**: codex checked every existing lane and none combines an
*allowed* proxy, a *live* dependent and advertiser destruction
(`todo/open-followups.md`, "qdwin nested-proxy teardown has no live VM lane").
`tests/integration/vm/s34-tier2-lifecycle.sh` makes two proxies and stops a
container but subscribes no stream and opens no popup; `tests/host/16-nested-
protocol.md` S8 destroys the advertiser with no dependent attached and, being
headless, has no pointer at all; `tests/apps/13-rdp-subscribe-frame.md`
subscribes a *regular* toplevel and kills the forward, not the source. This
scenario is that missing lane.

**Mostly non-visual**: S1-S3 assert on probe exit codes and journal reads.
Two frames are taken. `s3-preview.png` is taken while the popup probe waits
for its click and must show the proxy and its chrome band before anything is
clicked (run.sh checks the two pixels that matter; you open it too).
`s4-typed.png` proves keyboard delivery by the characters on screen, the way
`tests/apps/12-keystroke-roundtrip.md` does, so this scenario needs a
graphic-aware runner. The one pixel-dependent *input* step is S3's single
injected click, and its target is printed by the probe rather than
hard-coded.

## What "destroying the advertiser" means here

`qdwin-nested-probe` is BOTH the nested compositor and the bound shell on one
`wl_client` (see its header comment and `tests/host/16-nested-protocol.md`
"Single-client design"), because `nested_proxy_decision` requires the issuing
resource to be the bound shell. So the probe destroys its own
`qdwin_nested_toplevel_v1` rather than exiting.

That reaches the same per-resource code path a disconnect reaches:
`qdwin_nested_toplevel_destroy_req` (`qdwin/qdwin.c:19445`) is a bare
`wl_resource_destroy(resource)`, and libwayland runs the SAME destructor —
`qdwin_nested_toplevel_resource_destroy` (`:19620`) → `qdwin_nested_proxy_
destroy` — on a client disconnect.

It is NOT equivalent to a disconnect, though, and two differences matter. A
disconnect destroys *all* of the advertiser's resources, in libwayland's order,
so teardown interactions between them are not exercised here at all. And the
advertiser and the shell are one `wl_client` in this lane, so a shell that
reacted to `toplevel_removed` by touching the freed proxy is a different,
untested shape. Both are recorded in `todo/open-followups.md` item 4.

## Environment

Standard qdwin GUI harness (`tests/gui/AGENTS.md`): a running libvirt domain on
`qemu:///session` with `qdwin-compositor.service` and `qdshell.service` active.
This scenario temporarily frees the singleton shell role so the probe can bind
it, then restores `qdshell.service`. Do NOT build or deploy anything in-VM.

## How to run it

**The commands live in [`22-nested-proxy-teardown.d/run.sh`](22-nested-proxy-teardown.d/run.sh),
not in this markdown. Run it; do not re-type it into a driver of your own.**
It is a HOST script: Setup, S1-S4 and Teardown run in ONE host bash process,
each guest action is its own short `vm-exec`, and the cleanup trap lives on the
host, so no guest shell exiting between steps can tear the scenario down.
Do not write or claim a guest driver for this scenario, and do not wrap
run.sh in one. The "one claimed guest shell" rule protects hand-written
drivers from their own EXIT traps and from a second driver; here the trap is
on the host, and ownership is run.sh's own: an flock per VM on this host (a
second copy refuses with ERROR), and a guest record of the current run id so
the next run.sh reaps a popup probe orphaned by one that was SIGKILLed. A
`qci_claim_driver` claim could not cover this scenario anyway: the popup probe
is deliberately detached from every guest shell (`setsid`, own stdio) so the
launching `vm-exec` returns while it waits, and a claim's cgroup would not
contain it.

Why: every translation of this scenario's old host code blocks into a guest
driver dropped the one host-only precondition, `qdwin_prime_pointer` (a QMP
motion, which cannot come from inside the guest), and with it the scenario.
In gui-20260930T114916Z-4162368 the translated driver never primed, the popup
probe exited 77 "no pointer on the seat" 1 ms after creating its proxy
(`created handle=3` 12:10:54.284, `destroy handle=3` 12:10:54.285), and the S3
preview taken seconds later was legitimately black -- and was reported as
"captured while the probe was still waiting". Re-run on the preserved disk:
unprimed, rc=77 and a black frame; primed, the teal band at `CLICK_TARGET` and
the #333847 body. Neither the product nor the capture path was at fault.

Run, from any directory, with the VM you were given, in the FOREGROUND of ONE
long-running command (2-4 min; it owns every step and its cleanup, so let it
finish and never start a second copy while one runs):

```bash
bash <directory of this scenario>/22-nested-proxy-teardown.d/run.sh "$VMNAME" \
    >"$QCI_GUI_ARTIFACT_DIR/run.log" 2>&1; echo "run.sh exit=$?"
cat "$QCI_GUI_ARTIFACT_DIR/summary.txt"
```

It refuses (ERROR, touching nothing) an artifact directory that already holds
its frames or summary -- evidence is never overwritten. It writes to
`$QCI_GUI_ARTIFACT_DIR`: `asserts.tsv` (one row per assertion
below, also printed as `ASSERT <id> <PASS|FAIL|ERROR|SKIP|VISUAL> <detail>`
lines in `run.log`), `s3-preview.png`, `s4-typed.png` and `summary.txt`, which
ends with `RESULT <PASS|FAIL|ERROR>`. Exit 0 = every machine assertion passed,
1 = one FAILED in a run that completed, 3 = ERROR (an assertion could not be
decided, or the run stopped early). ERROR wins over FAIL; the FAIL rows are
still listed.

Then grade: OPEN BOTH FRAMES. `s3-preview.png` must show the proxy as S3
describes (run.sh has already checked its pixels at the click target and the
body centre -- a frame that contradicts its `3.4` row is a FAIL whatever the
row says). `s4-typed.png` decides assertion 4.3, the one assertion run.sh
records as `VISUAL` because only reading the frame can decide it. The scenario
verdict is run.sh's RESULT, turned FAIL if 4.3 fails. If run.sh ends in ERROR,
report ERROR with its `ERROR` rows and the probe lines above them in
`run.log` -- do not re-drive the scenario by hand.

What Setup does (in run.sh): checks the qdwin/qdshell session and that the
installed `qdwin-nested-probe` has the `--destroy-with-*` modes and output-aware
calibration (`--output`; absence is ERROR -- a stale deployment, never a
product FAIL); records the compositor pid ONCE (`$COMP_PID_BEFORE`: every step
re-reads it, because a crash-and-restart is the failure this lane exists to
catch); frees the singleton shell role with `qdwin_apps_prepare_shell_probe`
(stops qdshell, evicts any suite bystander, waits for `shell unbound`) and arms
the ONE cleanup handler immediately after; then primes the pointer.

**Assert (0.1):** the compositor logged `shell unbound`. Without that the
probe's `bind_as_shell` loses to the incumbent and every step below is vacuous.

**Assert (0.2) -- pointer priming.** libweston 16 adds the pointer capability
to the seat lazily, on a pointer device's first event, so a fresh worker whose
tablet never moved advertises a keyboard-only seat and S1/S3 exit 77 "no
pointer on the seat". run.sh moves the pointer twice over QMP (two positions:
QEMU's tablet sends nothing for a move to where it already is) to the
bottom-right corner, outside the proxy's 800x600 placement at (240,100), AFTER
qdshell is stopped so nothing reacts to the hover.

**Lane constraint.** S3's click target is computed against ONE NAMED output,
`$QD22_OUTPUT` (default `Virtual-1`): the DRM scanout head that QEMU's tablet
spans, the head qdwin pins shell capture to and the one the lane screenshots.
The golden also advertises `pipewire-0`/`pipewire-1`, which take no seat input.
The probe binds every `wl_output`, selects that one by name, maps the proxy's
GLOBAL rectangle into the head's LOCAL pixels, and moves the proxy onto that
head with `request_set_position` if the compositor placed it elsewhere. The
number of outputs is irrelevant; the head must exist, be unscaled and
untransformed, and its mode must equal `QDWIN_SCREEN_W`x`QDWIN_SCREEN_H`
(1280x800, the lane's pinned mode). The Wayland socket is read from the
running compositor, never hard-coded (it moves across compositor restarts).

## Exit-code contract

`qdwin-nested-probe` reports `0` PASS, `1` a failed postcondition (product
FAIL), `2` a setup/allocation error, and **`77` INCONCLUSIVE** — a precondition
for attaching a live dependent was not met, so the mode asserted nothing. 77 is
never a PASS: record it as ERROR with the probe's stderr reason, which names
exactly what is missing.

The split is not "every no-dependent path is 77": an allocation failure is 2,
and a protocol error — including a refused `show_popup` — is 1, because those
indicate something wrong rather than something absent.

## S1 — destroy under a live move-drag

The mild sibling: `qdwin_move_grab_end_for` was never called on the proxy path,
so motion looked up the handle and no-op'd while the pointer grab stayed
installed until button-release.

**What this step actually proves is narrow.** The probe checks that the request
round-tripped, the proxy was removed, and the connection survived. It does NOT
observe the grab. Codex confirmed the gap (`todo/reviews/proxy-lane-review-r1.md`
finding 2): removing `qdwin_move_grab_end_for` leaves every one of those checks
green. A leftover grab is only visible as *input not reaching other windows*,
and NOTHING in this scenario observes that: S4 proves keyboard delivery only,
and the bystander focuses the keyboard on `toplevel_added`, so a stale pointer
grab would swallow pointer events while typing keeps working. Treat S1 as a
crash/liveness check; the missing oracle is `todo/open-followups.md` item 2.

run.sh takes a journal cursor and runs `qdwin-nested-probe --destroy-with-move`
as admin in the session.

**Assert (1.1):** `rc=0` and stdout carries `proxy destroyed under a live
move-drag; compositor alive`.
**Assert (1.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`.
**Assert (1.3):** the journal after `$CURSOR` shows no line containing
`SIGSEGV`, `use-after-free`, `double free` or `Assertion`.

`rc=77` here means the seat advertises no pointer: a headless backend, or a
libweston-16 seat whose pointer devices have sent no event yet (assert 0.2
exists to rule that out). On the DRM VM session, after priming, that is an
environment fault: run.sh records 1.1 as ERROR.

## S2 — destroy under a LIVE view_stream

The load-bearing case. The stream pointer is freed at seat release, so an
ordering slip here is a real use-after-free rather than a stale-pointer read.

run.sh takes a journal cursor and runs `qdwin-nested-probe --destroy-with-stream`.

The probe allows the proxy, `subscribe_view_stream`s it read-only, and waits up
to 20s for `approved` — qdwin allocates a PipeWire output, pins the view, mints
a token and forks `qdistro-forward`. It then re-checks that the stream is still
un-torn-down at the moment it destroys the advertiser, so a stream that had
already ended for an unrelated reason reports 77 rather than satisfying a sticky
flag.

`approved` proves the *server-side* stream state exists and points at the proxy.
It does not prove the forward child exec'd or works. A failed exec yields
`approved` followed by `torn_down "forward exited"`, and which code that
produces depends on timing: if that teardown is already dispatched when the
probe re-checks, the step reports **77** ("stream was already torn down BEFORE
the destroy"); if it arrives afterwards, the reason assertion reports **FAIL
(rc=1)**. Either way the verdict is about the forward, not the teardown path —
in this VM the forward is real, so treat both as "look at qdistro-forward".

**Assert (2.1):** `rc=0`. The probe's own postconditions are: `torn_down` fired,
its reason is exactly `source toplevel closed`, `toplevel_removed` fired for the
proxy handle, and a further request round-tripped afterwards.
**Assert (2.2):** stdout carries `torn_down reason="source toplevel closed"` —
this is what distinguishes the source-closed path from the `forward exited` path
`tests/apps/13-rdp-subscribe-frame.md` already covers.
**Assert (2.3):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`.
**Assert (2.4):** the journal after `$CURSOR` shows `qdwin: view_stream_server_
state_released` for the stream — the server-owned state was revoked, not just
the client told.
**Assert (2.5):** no `SIGSEGV`, `use-after-free`, `double free` or `Assertion`
line in the compositor journal since the step began.

`rc=77` with `subscribe DENIED (no free pipewire output ...)` establishes only
that no output was FREE: `qdwin_handle_subscribe_view_stream` reports the same
thing whether weston.ini configures none (`[pipewire] num-outputs`) or every
configured one is already occupied by another stream — check both. A
spawn-related reason likewise means the spawn failed, not specifically that
`qdistro-forward` is missing. Both are ERROR (infra), same rule as
`13-rdp-subscribe-frame.md`, but neither names its own cause.

## S3 — destroy under a LIVE chrome popup

**What the screen looks like in this lane (read before judging any frame).**
From Setup until cleanup qdshell is STOPPED and the probe holds the shell
role, so there is no wallpaper, bar or background surface: qdwin's opaque
BLACK background curtain is all that is left. S1 and S2 proxies live for
milliseconds, so frames taken around S1/S2 are legitimately all-black
(0,0,0) — `vm-gui` rejecting them as "near-black" says nothing about the
product and they are not evidence either way; do not take them. During S3,
while the probe waits for the click, the scanout MUST show the proxy: an
800x600 grey-blue (~#333847) rectangle with a teal (#00aaaa) 32px chrome band
on the side the probe printed (`side=N` → the band spans y=68..99 for the
default `x=240 y=100`), and `CLICK_TARGET` lies inside that band (verified
live 2026-09-23 and again 2026-09-30 on the preserved disk of
gui-20260930T114916Z-4162368: band rgb(0,170,170) at (640,84), body
rgb(51,56,71) at (640,400)). run.sh takes `s3-preview.png` only between two
checks that the probe is still WAITING (one guest read: its log exists, has
no `rc=`, and its process group is alive -- a failed read is never "waiting")
and grades those
two pixels before it clicks (assert 3.4). Every all-black S3 preview recorded
from 2026-09-17 to 09-30 was taken AFTER the probe had exited -- by `rc=77` "no
pointer on the seat" because nothing primed the pointer, or by its click
timeout expiring while an old launch command held vm-exec -- so the proxy was
gone and black was the truth. The probe now checks for a pointer BEFORE it
prints `CLICK_TARGET`, so a printed target always means "mapped and waiting".
Only a frame taken while the probe is verifiably still waiting would be an
observation defect (scanout not repainting); none has been observed. run.sh
records that case as ERROR (3.4) and does not click blind; quote the frame
against the probe's `created handle=`/`destroy handle=` journal lines.

`show_popup` is v29-gated on a live input-grab serial, so this case cannot be
faked: the probe must receive a real `chrome_button` press. It therefore
attaches a 32px chrome band to the proxy — north or south, whichever the
proxy's position leaves ON-SCREEN, which is why the click target is printed
rather than hard-coded — then prints where to click and blocks.

"On-screen" means on `$QD22_OUTPUT`, not "somewhere in the compositor's global
space". The probe selects that head by name out of everything advertised, moves
the proxy onto it (`request_set_position`, v30) if the compositor placed it on
another output, and prints `CLICK_TARGET` in the head's LOCAL pixels — the space
`qdwin_click` normalises with `QDWIN_SCREEN_W/H` against QEMU's tablet. That is
what makes this step run on the standard three-output golden instead of
assuming the session has a single head.

Oracle limits, stated up front: the protocol has no "popup created" event, so
"the popup was live at the destruction boundary" is established by `show_popup`
round-tripping without a protocol error AND `qdwin_popup_v1.dismissed` not
having fired by the time the advertiser is destroyed. The probe re-arms that
flag immediately before the destroy, which narrows the window but does NOT
establish causality. Two sequences still reach a pass without the popup having
been released (`proxy-lane-review-r2.md` finding 1): an outside press processed
after the probe's re-check but before the server handles the destroy, whose
`dismissed` is dispatched by the following round-trip; and an implementation
that keeps `send_dismissed` while dropping `qdwin_popup_teardown`, which
satisfies an event-only oracle by construction. Closing this needs a view of
server-side popup state that the protocol does not expose — see
`todo/open-followups.md` item 5. The only mitigation here is procedural: the
lane injects exactly one click, and it happens before `show_popup`.

**The launcher's half of the cancel protocol.** This step starts a process that
HOLDS THE SINGLETON SHELL ROLE and then blocks, so it must be impossible for it
to be running once run.sh's `qd22_cleanup` has returned — otherwise
the `EXIT` trap restarts `qdshell.service` while the probe still owns the role,
and `qdwin_apps_restore_shell` kills bystanders, not this probe
(`todo/reviews/proxy-lane-review-r1.md` finding 4). Three earlier
attempts at this were each wrong one level down, so the reasoning is written out:

- `pkill -x qdwin-nested-probe` matches nothing — Linux `comm` truncates to 15
  characters and that name is 18, so `pkill -x` and `pgrep -x` both return no
  match and a `pgrep`-based wait declares success instantly (r2 finding 2).
- `pkill -f` matches the guest-agent shell running the command itself (see the
  vm-exec pkill self-match note in the project memory).
- Killing the published pid is not enough either: the probe is a child of the
  wrapper. `setsid` makes the wrapper a group leader and the reaper signals the
  **group**.
- Publishing the pid before running the probe fixes publication *failure* but
  not publication *pending*: a backgrounded launcher can be descheduled before
  it publishes, outlive the acknowledgement timeout, and start the probe after
  cleanup reported success (r4 finding 1, reproduced by pausing the launcher).

So the launcher checks `$QD22_CANCEL` **before** publishing and **again
immediately after**, removing its pid-file and exiting rather than starting the
probe. The reaper sets that flag first and then watches for a late pid. Whoever
loses the race observes the other's flag, so after `qd22_cleanup` has REAPED
SUCCESSFULLY neither a pending launcher nor a live probe can survive. When
cancellation cannot even be recorded the handler still attempts recovery, and
reports that as FAIL with ownership unresolved rather than as a clean exit. `$QD22_INTENT` is written *synchronously*
before the launcher is backgrounded, so an absent pid is unambiguous: with no
intent nothing was ever started.

In run.sh, in order: take a journal cursor; stage the launcher as a guest
script from a QUOTED heredoc (so `$$` and `$?` reach the guest shell exactly as
written -- full-20260930T051422Z-65193 hand-transcribed an inline
`sh -c '... echo rc=\$? ...'` with one backslash too many and the log said a
literal `rc=$?`); record `$QD22_INTENT` synchronously; launch it with `setsid`
as admin with its OWN stdio detached to a regular file (vm-exec runs through
qga guest-exec with capture-output, and qga reports a command finished only
once every holder of its stdout/stderr pipes has closed them -- a launcher that
kept them pinned vm-exec until the probe's click timeout, which is how every
preview of 2026-09-17..09-24 was taken after the proxy died); acknowledge the
published group pid; wait for `CLICK_TARGET ` (trailing space: not
`CLICK_TARGET_GLOBAL`, which is in global space), stopping early if `rc=`
appears first; check 3.3; take and grade the frame (3.4); click the target
with `qdwin_click`; wait up to 20 s for `rc=`; reap the probe group and wait
for `shell unbound` before S4 takes the role.

**Assert (3.1):** the log ends with `rc=0` and carries `proxy destroyed under a
LIVE chrome popup; dismissed fired; compositor alive`. The launcher writes the
probe's numeric status; a literal `rc=$?` means the launcher was not run as
staged and is an ERROR, never a product verdict.
**Assert (3.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`.
**Assert (3.4) — the frame:** `s3-preview.png`, taken while the probe was
waiting both before and after the capture, shows the teal (#00aaaa) band at
`CLICK_TARGET` and the grey-blue (#333847) proxy body at its centre (each
channel within 24). Open it too: it must show the whole rectangle and band.
**Assert (3.5):** after the click the probe group is reaped and the compositor
logs `shell unbound` (the role is free for S4).
**Assert (3.6):** no fault line in the compositor journal since S3 began.

**Assert (3.3) — calibration, check this FIRST if S3 goes wrong:** in the
probe's `PROXY_GEOM` line, `output=` equals `$QD22_OUTPUT`, `out=WxH` equals
`$QDWIN_SCREEN_W x $QDWIN_SCREEN_H`, and `scale=1 transform=0`. **`outputs=` is
reported, not constrained** — three outputs is the normal golden
(`Virtual-1` + `pipewire-0` + `pipewire-1`), and the click target is mapped
against the named head regardless of how many others exist. `out=...@x,y` is
that head's global origin; it need not be `0,0`, because the probe subtracts it
from the printed `CLICK_TARGET`. The probe reads all of this from `wl_output` —
the compositor's real configuration — and refuses (77) if the named head is
absent, scaled or transformed. `qdwin_click` uses the two env vars to convert
pixels into QMP's 0..32767 axis range, and nothing else checks that they match
reality: if they disagree, every click in this scenario lands somewhere other
than where it was aimed and S3's result says nothing about the product, so
run.sh stops (ERROR) before clicking when 3.3 does not hold.

`rc=77` with `no pointer on the seat` (run.sh: 3.0 "never printed
CLICK_TARGET") means the priming of 0.2 did not take: an environment fault on
the DRM session, ERROR.

`rc=77` naming the OUTPUT (`no output named "Virtual-1" among N advertised
[...]`) means the head this lane injects into is not the one the probe was
asked for. The message lists every advertised head with its geometry: pick the
scanout one and re-run with `QD22_OUTPUT=<name>` exported for run.sh. This is a lane/config
mismatch — report ERROR with that list, never a pass and never a silent skip.

`rc=77` saying the proxy `cannot be placed on output` means
`request_set_position` did not move it onto the clickable head (a v29-only
shell, or the compositor refusing the move). Report ERROR: the calibration
could not be established, so nothing about the teardown path was tested.

`rc=77` with `no chrome_button within 120s` means the precondition was not
established — the cause is NOT determined by the timeout alone. Calibration
(3.3) is the first thing to check, but broken chrome-button routing in the
compositor produces exactly the same observation, and that would be a product
defect. Do not report a calibration verdict without checking 3.3 first.
`rc=77` with `does not overlap output` means the chrome band and the selected
head share no pixels, so no click point exists inside BOTH. The probe aims at
the middle of that intersection and refuses here rather than falling back to
the output centre, which could sit outside the chrome and time out as if the
compositor had dropped the button. Report ERROR with the two x-ranges the
message prints; it is a placement/geometry problem, not a product verdict.

`rc=77` with `was removed during calibration` means the named head was
unadvertised while the probe was placing the proxy, so its geometry is stale.
Re-run; if it repeats, the lane's output configuration is unstable and no
calibration is possible.

`rc=77` with `QD_MAX_OUTPUTS` means the session advertises more outputs than
the probe tracks and the requested head was not among the tracked ones. The
probe refuses by NAME of the cap instead of reporting the head absent, because
"absent" would be a wrong answer rather than an inconclusive one. Raise the cap
in `test-client/qdwin-nested-probe.c` and rebuild. The three-output golden is
far below it.

`rc=77` with `leaves neither chrome band on-screen` means the probe computed no
clickable band from the rectangles it was given — most often a proxy covering
the whole output, but the probe knows only those rectangles and cannot tell that
from other causes. Neither reason is self-diagnosing: report both as an
unestablished precondition (ERROR) whose cause is still open, and check 3.3
before blaming calibration. The probe deliberately
refuses to pass without a real grab serial, because `show_popup` would then
never have been called and there would be no popup to destroy under.

## S4 — KEYBOARD input still reaches an ordinary window afterwards

Three proxies have now been destroyed — two of them (S2, S3) with a dependent
established as live at the destruction boundary, one (S1) without move liveness
being observed at all. This step checks the one thing the probe cannot check
about itself: that the seat still delivers to an ordinary client.

**What it proves is keyboard delivery, and only that.** An earlier draft also
claimed it proved the pointer works and that no stale grab was left behind.
That was wrong (`proxy-lane-review-r3.md` finding 2): `qdwin-bystander` calls
`set_keyboard_focus` on `toplevel_added` (`test-client/qdwin-bystander.c:301`),
so the terminal is keyboard-focused *before* any click — drop the click
entirely and the same characters still appear. Worse, a stale pointer grab can
swallow pointer events while keyboard events keep flowing to the focused
window, which is exactly the defect that would go unseen.

So: no click here, no pointer claim. **Pointer recovery after a proxy teardown
has no oracle anywhere in this lane** — see `todo/open-followups.md` item 2.

Uses the repo's keyboard-delivery idiom (`tests/apps/12-keystroke-roundtrip.md`):
type into a terminal, read the characters off the framebuffer. `foot` is part of
the opt-in `QDWIN_APP_DEPS` matrix, so its absence is a SKIP — but then S4
asserts nothing and the scenario proves nothing about input surviving; say so
in the report.

The probe released the shell role when its last mode exited, so run.sh takes
the role with the bystander (its cleanup trap covers it).

run.sh: if `foot` is absent, record S4 as SKIP. Otherwise take a journal
cursor, `qdwin_apps_become_shell` + `qdwin_apps_session_up`, launch
`foot --title qd22-after-$QD22_RUN`, find THIS run's terminal by its pid
(`pgrep -f` on the per-run title, with the bracket trick against matching the
guest-agent shell, and `comm` = `foot`), and read its handle from the
compositor's own `toplevel_added handle=N uid=1000 pid=<that pid>` line. Not by
title: foot has no title yet when it maps (the bystander logs `title=""`,
`tests/apps/03-foot-vs-xterm-tagging.md`) and nothing logs later title
changes, so the title grep this step used to do never matched; not by app_id
alone, which would select any terminal. Then type `qdwinlives` over QMP --
each key's press AND release in ONE `input-send-event`, so no delivery stall
can separate them and let the terminal's key repeat run (the first Luna run
of run.sh saw `qdwinlive` + ~34 `e`; a 1.3 s gap between separate down/up
calls reproduces that frame exactly) -- wait 3 s, and capture `s4-typed.png`.
A run of one repeated letter in the frame is therefore a delivery defect to
report as FAIL, not an injection artifact to excuse.

**Assert (4.1):** a handle was found — the compositor still admits new
toplevels after three proxy teardowns. (No `foot` process at all is ERROR: the
terminal never started, which says nothing about the compositor.)

**Assert (4.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`. This
is what makes 4.1 mean anything: a compositor that died and was restarted by
systemd would also accept a new toplevel and would otherwise read as a clean
pass.

**Assert (4.3) — the point of S4, graded by YOU (run.sh records it as
`VISUAL`):** `s4-typed.png` shows `qdwinlives`
echoed in the terminal. Direct evidence that keyboard events still reach an
ordinary client, i.e. the seat survived the per-stream seat release S2
performed. If the window is visible and focused but the characters are absent,
that IS the failure this step exists to catch — report FAIL, not ERROR.

**Assert (4.4):** the compositor journal since `$CURSOR` contains no `SIGSEGV`,
`use-after-free`, `double free`, or `Assertion` line.

A failed remote read produces no stdout, and piping straight into `grep`
makes that indistinguishable from a successful read with nothing to report
(`proxy-lane-review-r4.md` finding 3), so run.sh captures the journal first,
checks the read (a failed read is ERROR, "assertion not made"), then searches.
The same check backs 1.3, 2.5 and 3.6.

The terminal is cleaned up by run.sh's `qd22_cleanup`, which kills it by the
per-run title -- deliberately not inline in the step, where an early stop would
skip it.

## Teardown

`qd22_cleanup` in run.sh, installed as the EXIT trap right after the shell role
is taken, is the only cleanup path (an earlier draft replaced it at this point
and silently dropped the terminal cleanup, the reaper's status check and the
scenario-failure line -- `proxy-lane-review-r4.md` finding 2). It also runs
when run.sh stops early on an ERROR, and when it receives TERM/INT/HUP.

In order, it: reaps S3's probe group (setting the cancel flag first, so a
launcher that has not yet published cannot start one), kills S4's terminal by
its per-run title, restores `qdshell.service`, and compares the compositor pid
against `$COMP_PID_BEFORE`.

**Assert (T.1):** the compositor pid is unchanged -- the final handoff is
itself part of what this lane exercises, so a compositor that died during the
restore is a failure, not a clean exit.

**Assert (T.2):** `qdshell.service` was restored and owns the shell role
again.

**Assert (T.3):** no `T.0`/`T.3` FAIL row -- "could not reap the popup probe"
or "restoring qdshell with probe ownership UNRESOLVED". That path exists so the
desktop is not left headless, but it means cleanup ran without establishing
that the probe was gone -- recovery, not a pass.

Leftovers this scenario owns: the probe's proxies (destroyed by the probe
itself), the probe group and S4's terminal (both reaped by `qd22_cleanup`), and
`$QD22_LOG`, `$QD22_LAUNCH_LOG` and `$QD22_LAUNCHER` in the VM's /tmp -- per-run and
deliberately kept, since they hold the popup step's verdict and its launcher's
diagnostics. If run.sh is SIGKILLed (no trap runs), qdshell stays stopped and
the probe exits on its own at its 120 s click timeout; the VM is disposable.
