# 22 — nested-proxy teardown with a LIVE dependent (iso2/10 E2 gate)

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

**Mostly non-visual**: S1-S3 assert on probe exit codes and journal reads only.
S4 is the exception — proving keyboard delivery means reading characters off a
screenshot, the way `tests/apps/12-keystroke-roundtrip.md` does, so this
scenario needs a graphic-aware runner. The one pixel-dependent *input* step is
S3's single injected click, and its target is printed by the probe rather than
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

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_apps_set_vm "$VMNAME"

: "${QDWIN_SCREEN_W:=1024}"
: "${QDWIN_SCREEN_H:=768}"
export QDWIN_SCREEN_W QDWIN_SCREEN_H

qdwin_session_healthy || { echo "ERROR: qdwin/qdshell user session not up"; exit 1; }

# PRECONDITION (infra): the probe must be installed on the VM. Absence is an
# ERROR (the scenario cannot be exercised), NOT a product FAIL.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdwin-nested-probe >/dev/null' \
    || { echo "ERROR: qdwin-nested-probe not installed on VM"; exit 1; }
"$QDWIN_VM_EXEC" "$VMNAME" \
    'qdwin-nested-probe --help 2>&1 | grep -q -- --destroy-with-stream' \
    || { echo "ERROR: deploy qdwin-nested-probe with the --destroy-with-* modes"; exit 1; }

# Record the compositor identity ONCE, before anything is torn down. Every step
# re-reads it: a crash-and-restart is the failure this whole lane exists to
# catch, and a restarted compositor would otherwise look like a clean session.
COMP_PID_BEFORE=$(qdwin_compositor_pid)
[ -n "$COMP_PID_BEFORE" ] || { echo "ERROR: no compositor pid"; exit 1; }
echo "compositor pid before = $COMP_PID_BEFORE"

# Free the singleton shell role for the probe (stops qdshell, evicts any suite
# bystander, waits for qdwin to log `shell unbound`). Arm the restore trap
# IMMEDIATELY after a successful takeover so a later failure never leaves the
# desktop headless.
qdwin_apps_prepare_shell_probe \
    || { echo "ERROR: could not reserve the singleton shell role"; exit 1; }
trap 'qdwin_apps_restore_shell' EXIT

# LANE CONSTRAINT (enforced by the probe, not here): S3's click target is
# computed in output-local pixels against the first wl_output, so it assumes
# exactly ONE output, unscaled, untransformed, at the origin. The probe checks
# all four itself from wl_output and exits 77 naming the one that differs —
# the right place for it, since only the probe sees what the compositor
# actually advertises. It reports them in its PROXY_GEOM line
# (out=WxH@x,y outputs= scale= transform=); assert 3.3 checks them.

# Never hard-code wayland-1: the socket name moves across compositor restarts
# (see tests/apps/13-rdp-subscribe-frame.md Setup).
ACTIVE_SOCKET=$(qdwin_apps_active_socket)
[ -n "$ACTIVE_SOCKET" ] || { echo "ERROR: qdwin stopped during shell handoff"; exit 1; }

# Helper: run one probe mode as admin in the session, echo `rc=<n>` plus output.
qd22_probe() {   # $@ = probe args
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         WAYLAND_DISPLAY=$ACTIVE_SOCKET \
         qdwin-nested-probe $* 2>&1; echo rc=\$?"
}
```

**Assert (0.1):** `qdwin_apps_prepare_shell_probe` succeeded — the compositor
logged `shell unbound`. Without that the probe's `bind_as_shell` loses to the
incumbent and every step below is vacuous.

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
which is what S4 is for — and S4 runs after S2 and S3, whose own grabs could
have replaced the stale one. Treat S1 as a crash/liveness check, and see the
open follow-up for the missing oracle.

```bash
CURSOR=$(qdwin_apps_journal_cursor)
qd22_probe --destroy-with-move
```

**Assert (1.1):** `rc=0` and stdout carries `proxy destroyed under a live
move-drag; compositor alive`.
**Assert (1.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`.
**Assert (1.3):** the journal after `$CURSOR` shows no `qdwin: ` line containing
`SIGSEGV`, `use-after-free`, or `assertion`.

`rc=77` here means the seat has no pointer (a headless backend). On the DRM VM
session that is an environment fault: report ERROR.

## S2 — destroy under a LIVE view_stream

The load-bearing case. The stream pointer is freed at seat release, so an
ordering slip here is a real use-after-free rather than a stale-pointer read.

```bash
CURSOR=$(qdwin_apps_journal_cursor)
qd22_probe --destroy-with-stream
```

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

`rc=77` with `subscribe DENIED (no free pipewire output ...)` means the VM's
weston.ini has no `[pipewire] num-outputs >= 1`; `rc=77` with a spawn-related
reason means `qdistro-forward` is missing. Both are ERROR (infra), same rule as
`13-rdp-subscribe-frame.md`.

## S3 — destroy under a LIVE chrome popup

`show_popup` is v29-gated on a live input-grab serial, so this case cannot be
faked: the probe must receive a real `chrome_button` press. It therefore
attaches a 32px chrome band to the proxy — north or south, whichever the
proxy's position leaves ON-SCREEN, which is why the click target is printed
rather than hard-coded — then prints where to click and blocks.

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

This step launches a process that HOLDS THE SINGLETON SHELL ROLE and then
blocks. It must be reaped on every exit path, or the `EXIT` trap will restart
`qdshell.service` while the probe still owns the role —
`qdwin_apps_restore_shell` kills bystanders, not this probe
(`todo/reviews/proxy-lane-review-r1.md` finding 4).

**Reap by process GROUP, and publish ownership before the probe exists.**
`pkill -x qdwin-nested-probe` matches nothing: Linux `comm` is truncated to 15
characters and that name is 18, so both `pkill -x` and `pgrep -x` silently
return no match, and a wait loop built on `pgrep` then declares success
immediately (`proxy-lane-review-r2.md` finding 2). `pkill -f` is the other trap:
it matches the guest-agent shell running the command (see the vm-exec
pkill self-match note in the project memory).

Reaping by PID alone is still not enough, because *publishing* a PID is not a
handshake (`proxy-lane-review-r3.md` finding 1). Three rules close it:

1. The inner shell writes **its own** pid and only then runs the probe, so a
   missing pid-file means the probe was never started — not that cleanup
   succeeded. If publication fails the shell exits without launching anything.
2. `setsid` makes that shell a process-group leader, and the reaper signals the
   **group**, so the probe cannot outlive the wrapper that owns it.
3. The launch is *acknowledged*: the step waits for the pid-file before doing
   anything else. Until it appears, ownership is unknown and the scenario must
   not restore qdshell — a delayed launcher would otherwise start the probe
   after cleanup declared itself finished.

State is per-invocation (`$$` repeats if the scenario is rerun in one shell)
and is retired once the probe exits, so the final trap's reaper is a no-op.

```bash
CURSOR=$(qdwin_apps_journal_cursor)
QD22_RUN="$$-$(date +%s)-$RANDOM"
QD22_LOG=/tmp/qd22-popup.$QD22_RUN.log
QD22_PID=/tmp/qd22-popup.$QD22_RUN.pid
QD22_FAILED=0          # set by the reaper; checked before restoring

# Reap the probe's whole process group. Exit status: 0 = nothing of ours is
# running, 1 = ownership unknown or a survivor — which must BLOCK restoration.
qd22_reap_probe() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "p=\$(cat $QD22_PID 2>/dev/null); \
       case \"\$p\" in ''|*[!0-9]*) echo 'no pid published'; exit 0;; esac; \
       kill -TERM -\"\$p\" 2>/dev/null; \
       for _i in \$(seq 1 40); do kill -0 -\"\$p\" 2>/dev/null || { rm -f $QD22_PID; exit 0; }; sleep 0.1; done; \
       kill -KILL -\"\$p\" 2>/dev/null; \
       for _i in \$(seq 1 20); do kill -0 -\"\$p\" 2>/dev/null || { rm -f $QD22_PID; exit 0; }; sleep 0.1; done; \
       echo \"probe group \$p SURVIVED\"; exit 1" 2>&1
}
# qdwin_apps_restore_shell must NOT run while the probe may still hold the
# singleton role. The GUI helpers do not set -e, so this is checked explicitly.
qd22_final_check() {
    if ! qd22_reap_probe; then
        echo "FAIL: could not reap the popup probe — it may still own the shell role"
        QD22_FAILED=1
    fi
    qdwin_apps_restore_shell || { echo "FAIL: qdshell restore failed"; QD22_FAILED=1; }
    local after; after=$(qdwin_compositor_pid)
    if [ "$after" != "$COMP_PID_BEFORE" ]; then
        echo "FAIL (T.1): compositor pid $COMP_PID_BEFORE -> $after"
        QD22_FAILED=1
    else
        echo "T.1 ok: compositor pid unchanged ($after)"
    fi
    [ "$QD22_FAILED" = 0 ] || echo "SCENARIO VERDICT: FAIL (see FAIL lines above)"
}
trap 'qd22_final_check' EXIT

"$QDWIN_VM_EXEC" "$VMNAME" "rm -f $QD22_LOG $QD22_PID" >/dev/null \
    || { echo "ERROR: could not clear per-run state in the VM"; exit 1; }

# setsid -> group leader. `echo $$ > pid` BEFORE the probe runs, so an absent
# pid-file provably means nothing was launched. `|| exit 90` refuses to launch
# an unreapable child.
"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=$ACTIVE_SOCKET \
     setsid sh -c 'echo \$\$ > $QD22_PID.tmp && mv $QD22_PID.tmp $QD22_PID || exit 90; \
                   qdwin-nested-probe --destroy-with-popup --click-timeout 60 \
                     >$QD22_LOG 2>&1; \
                   echo rc=\$? >>$QD22_LOG' &" \
  >/dev/null

# Acknowledge ownership before anything else can fail.
PROBE_PID=
for _ in $(seq 1 40); do
    PROBE_PID=$("$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_PID 2>/dev/null")
    [ -n "$PROBE_PID" ] && break
    sleep 0.25
done
[ -n "$PROBE_PID" ] || { echo "ERROR: probe never published its pid (ownership unknown)"; exit 1; }
echo "probe group pid=$PROBE_PID"

# The probe prints CLICK_TARGET once the chrome is attached and committed.
TARGET=
for _ in $(seq 1 40); do
    TARGET=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "grep -m1 '^CLICK_TARGET' $QD22_LOG 2>/dev/null")
    [ -n "$TARGET" ] && break
    sleep 0.5
done
[ -n "$TARGET" ] || { echo "ERROR: probe never printed CLICK_TARGET"; \
    "$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_LOG"; exit 1; }
"$QDWIN_VM_EXEC" "$VMNAME" "grep '^PROXY_GEOM' $QD22_LOG"   # for assert 3.3
CX=$(printf '%s' "$TARGET" | sed -nE 's/.*x=(-?[0-9]+).*/\1/p')
CY=$(printf '%s' "$TARGET" | sed -nE 's/.*y=(-?[0-9]+).*/\1/p')
echo "clicking chrome at ($CX, $CY)"
qdwin_click "$CX" "$CY" left

for _ in $(seq 1 40); do
    "$QDWIN_VM_EXEC" "$VMNAME" "grep -q '^rc=' $QD22_LOG" && break
    sleep 0.5
done
"$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_LOG"
# Must be gone before S4 takes the shell role. A `SURVIVED` line here is an
# ERROR for the whole scenario: the singleton role is still held.
qd22_reap_probe
```

**Assert (3.1):** the log ends with `rc=0` and carries `proxy destroyed under a
LIVE chrome popup; dismissed fired; compositor alive`.
**Assert (3.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`.

**Assert (3.3) — calibration, check this FIRST if S3 goes wrong:** in the
probe's `PROXY_GEOM` line, `out=WxH` equals `$QDWIN_SCREEN_W x $QDWIN_SCREEN_H`,
and `outputs=1 scale=1 transform=0` with `out=...@0,0`. The probe reads all of
these from `wl_output` — the compositor's real configuration — and refuses (77)
if the count, scale, origin or transform is not the one its pixel arithmetic
assumes. `qdwin_click` uses the two env vars to convert pixels into QMP's
0..32767 axis range, and nothing else checks that they match reality: if they
disagree, every click in this scenario lands somewhere other than where it was
aimed and S3's result says nothing about the product.

`rc=77` with `no chrome_button within 60s` means the precondition was not
established — the cause is NOT determined by the timeout alone. Calibration
(3.3) is the first thing to check, but broken chrome-button routing in the
compositor produces exactly the same observation, and that would be a product
defect. Do not report a calibration verdict without checking 3.3 first.
`rc=77` with `leaves neither chrome band on-screen` means the proxy covers the
whole output, so no clickable band exists — move it or enlarge the output.
Neither reason is self-diagnosing. "Neither band on-screen" is most often a
proxy covering the whole output, but the probe only knows the rectangles it was
given. Report both as an unestablished precondition (ERROR) whose cause is
still open, and check 3.3 before blaming calibration. The probe deliberately
refuses to pass without a real grab serial, because `show_popup` would then
never have been called and there would be no popup to destroy under.

## S4 — KEYBOARD input still reaches an ordinary window afterwards

Three proxies have now been torn down under live dependents. This step checks
the one thing the probe cannot check about itself: that the seat still delivers
to an ordinary client.

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

The probe released the shell role when its last mode exited, so take the role
with the bystander (the trap from S3 still covers cleanup).

```bash
QD22_TERM_TITLE="qd22-after-$QD22_RUN"
if ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v foot >/dev/null 2>&1'; then
    echo "SKIP S4: foot not installed (qdwin app deps are opt-in; rerun with QDWIN_APP_DEPS=1)"
    echo "NOTE: with S4 skipped, nothing in this scenario proves input survived the teardowns"
else
    qdwin_apps_become_shell || { echo "ERROR: could not take the shell role back"; exit 1; }
    qdwin_apps_session_up   || { echo "FAIL: session not healthy after the teardowns"; exit 1; }

    qdwin_apps_launch qd22-after "foot --title $QD22_TERM_TITLE"
    # Identify by the per-run TITLE, not app_id: a `tail -1` on app_id alone
    # would happily select some other terminal.
    HANDLE=
    for _ in $(seq 1 40); do
        HANDLE=$("$QDWIN_VM_EXEC" "$VMNAME" \
          "grep -E 'toplevel_added handle=[0-9]+ .*title=\"$QD22_TERM_TITLE\"' \
             /tmp/bystander.log 2>/dev/null | tail -1 | sed -nE 's/.*handle=([0-9]+).*/\1/p'")
        [ -n "$HANDLE" ] && break
        sleep 0.5
    done
    [ -n "$HANDLE" ] || { echo "FAIL: compositor admitted no new toplevel after the teardowns"; exit 1; }
    echo "post-teardown handle=$HANDLE"

    qdwin_apps_type "qdwinlives"
    sleep 1
    qdwin_apps_screenshot /tmp/qd22-s4-typed.png
fi
```

**Assert (4.1):** `$HANDLE` is a non-empty integer — the compositor still admits
new toplevels after three proxy teardowns.

**Assert (4.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`. This
is what makes 4.1 mean anything: a compositor that died and was restarted by
systemd would also accept a new toplevel and would otherwise read as a clean
pass.

**Assert (4.3) — the point of S4:** `/tmp/qd22-s4-typed.png` shows `qdwinlives`
echoed in the terminal. Direct evidence that keyboard events still reach an
ordinary client, i.e. the seat survived the per-stream seat release S2
performed. If the window is visible and focused but the characters are absent,
that IS the failure this step exists to catch — report FAIL, not ERROR.

**Assert (4.4):** the compositor journal since `$CURSOR` contains no `SIGSEGV`,
`use-after-free`, `double free`, or `Assertion` line.

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     journalctl --user -b -u qdwin-compositor.service \
     --after-cursor '$CURSOR' --no-pager -o cat" \
  | grep -E 'SIGSEGV|use-after-free|double free|Assertion' \
  && echo "FAIL (4.4): see above" || echo "4.4 clean"
```

The terminal is cleaned up by the EXIT trap, not here — an `exit 1` above, or a
runner stopping on a failed assert, would skip any cleanup written at this
point. Fold it into `qd22_final_check` from S3, matched on the per-run title so
it cannot touch an unrelated terminal:

```bash
# add as the FIRST line of qd22_final_check(), defined in S3.
# The bracket is deliberate: `pkill -f` would otherwise match the guest-agent
# shell running this very command. `qd22-af[t]er-` as a REGEX matches the
# terminal's title; as literal text in the command line it does not match
# itself. $QD22_RUN is set in S3, so this is safe even if S4 was skipped —
# it then matches nothing.
"$QDWIN_VM_EXEC" "$VMNAME" \
  "pkill -u admin -f \"qd22-af[t]er-$QD22_RUN\" 2>/dev/null" >/dev/null 2>&1 || true
```

## Teardown

The `EXIT` trap reaps any surviving `qdwin-nested-probe` and then restores
`qdshell.service`, proving its compositor-visible bind. Order matters: the
probe holds the singleton shell role while it blocks, so restoring first would
race it.

**Assert (T.1):** after restoration, `qdwin_compositor_pid` still equals
`$COMP_PID_BEFORE`. The handoff itself is part of what this lane exercises — a
compositor that died during the final restore is a failure, not a clean exit.

A command written *after* the trap cannot run: the exiting shell is already
gone (`proxy-lane-review-r2.md`). Put the check inside the trap, so it runs on
the failure paths too:

```bash
qd22_final_check() {
    qd22_reap_probe
    qdwin_apps_restore_shell
    local after; after=$(qdwin_compositor_pid)
    if [ "$after" != "$COMP_PID_BEFORE" ]; then
        echo "FAIL (T.1): compositor pid $COMP_PID_BEFORE -> $after"
    else
        echo "T.1 ok: compositor pid unchanged ($after)"
    fi
}
trap 'qd22_final_check' EXIT
```

Install this in place of the S3 trap once S3 has defined `qd22_reap_probe`.

Leftovers this scenario is responsible for: the probe's proxies (destroyed by
the probe itself), the probe processes (reaped by the trap), `$QD22_LOG` in the
VM's /tmp (per-run, harmless), and the `qd22-after` client (assert 4.5).
