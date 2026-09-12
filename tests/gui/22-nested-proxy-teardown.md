# 22 — nested-proxy teardown with a LIVE dependent (iso2/10 E2 gate)

**What**: destroy a nested-compositor advertiser while the shell still holds a
server-owned dependent on the proxy it advertised — a live `view_stream`, a
live chrome popup, or a live move-drag — and assert the compositor survives,
the dependent is released, and the session keeps working.

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

**Non-visual**: every assert is a probe exit code plus a journal read. No
screenshots. The one pixel-dependent step is a single injected click (S3), and
its target is printed by the probe rather than hard-coded.

## What "destroying the advertiser" means here

`qdwin-nested-probe` is BOTH the nested compositor and the bound shell on one
`wl_client` (see its header comment and `tests/host/16-nested-protocol.md`
"Single-client design"), because `nested_proxy_decision` requires the issuing
resource to be the bound shell. So the probe destroys its own
`qdwin_nested_toplevel_v1` rather than exiting.

That is the same code path a real advertiser disconnect takes, not a weaker
one: `qdwin_nested_toplevel_destroy_req` (`qdwin/qdwin.c:19445`) is a bare
`wl_resource_destroy(resource)`, and libwayland runs the SAME destructor —
`qdwin_nested_toplevel_resource_destroy` (`:19620`) → `qdwin_nested_proxy_
destroy` — on a client disconnect. What this lane does NOT reproduce is the
advertiser and the shell being different clients, so a shell that reacted to
`toplevel_removed` by touching the freed proxy would be a different (and
untested) shape.

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

# LANE CONSTRAINT: the probe computes its click target from wl_output.mode and
# the proxy's geometry, treating the output as a single unscaled one at the
# origin. It does not account for a second output, a non-zero output position,
# or fractional/integer scale — on any of those the printed CLICK_TARGET is
# wrong and S3 would time out for a reason unrelated to the product
# (todo/reviews/proxy-lane-review-r1.md). Assert the assumption instead of
# silently relying on it.
OUTPUT_COUNT=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     journalctl --user -b -u qdwin-compositor.service --no-pager -o cat 2>/dev/null \
   | grep -c 'qdwin: output_created'")
echo "outputs seen by qdwin: $OUTPUT_COUNT"
[ "${OUTPUT_COUNT:-0}" -le 1 ] || { echo "ERROR: S3 needs a single output (saw $OUTPUT_COUNT)"; exit 1; }

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
FAIL), `2` setup error, and **`77` INCONCLUSIVE** — the mode could not attach a
live dependent on this backend, so it asserted nothing. 77 is never a PASS:
record it as ERROR (precondition) with the probe's stderr reason, which names
exactly what is missing.

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
It does not prove the forward child exec'd or works: a failed exec yields
`approved` followed by `torn_down "forward exited"`, which this step reports as
**FAIL (rc=1)**, not 77. In this VM the forward is real, so that outcome means
look at the forward, not at the teardown path.

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
flag immediately before the destroy, so only a dismissal *caused by* the
destroy satisfies the assertion — but an implementation that kept the popup
alive while releasing nothing would still have to fire `dismissed` to pass.

This step launches a process that HOLDS THE SINGLETON SHELL ROLE and then
blocks. It must be reaped on every exit path, or the `EXIT` trap will restart
`qdshell.service` while the probe still owns the role —
`qdwin_apps_restore_shell` kills bystanders, not this probe
(`todo/reviews/proxy-lane-review-r1.md` finding 4). The log is per-run and
truncated before launch so a previous run's `CLICK_TARGET`/`rc` can never be
read as this one's.

```bash
CURSOR=$(qdwin_apps_journal_cursor)
QD22_LOG=/tmp/qd22-popup.$$.log

qd22_reap_probe() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "pkill -u admin -x qdwin-nested-probe 2>/dev/null; \
       for _i in \$(seq 1 40); do \
         pgrep -u admin -x qdwin-nested-probe >/dev/null 2>&1 || break; sleep 0.1; \
       done" >/dev/null 2>&1 || true
}
# Reap BEFORE restoring qdshell, on every exit from here on.
trap 'qd22_reap_probe; qdwin_apps_restore_shell' EXIT

"$QDWIN_VM_EXEC" "$VMNAME" "rm -f $QD22_LOG" >/dev/null
"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=$ACTIVE_SOCKET \
     sh -c 'qdwin-nested-probe --destroy-with-popup --click-timeout 60 \
              >$QD22_LOG 2>&1; echo rc=\$? >>$QD22_LOG' &" \
  >/dev/null

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
qd22_reap_probe   # it should already be gone; make sure before S4 takes the role
```

**Assert (3.1):** the log ends with `rc=0` and carries `proxy destroyed under a
LIVE chrome popup; dismissed fired; compositor alive`.
**Assert (3.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`.

**Assert (3.3) — calibration, check this FIRST if S3 goes wrong:** the `out=WxH`
field the probe prints in `PROXY_GEOM` equals `$QDWIN_SCREEN_W x $QDWIN_SCREEN_H`.
The probe reads that from `wl_output.mode`, i.e. the compositor's real output;
`qdwin_click` uses the two env vars to convert pixels into QMP's 0..32767 axis
range. If they disagree, every click in this scenario lands somewhere other than
where it was aimed, and S3's `rc=77` says nothing about the product.

`rc=77` with `no chrome_button within 60s` means the click missed the chrome.
`rc=77` with `leaves neither chrome band on-screen` means the proxy covers the
whole output, so no clickable band exists — move it or enlarge the output.
Both are harness-calibration ERROR, not product FAIL. The probe deliberately
refuses to pass without a real grab serial, because `show_popup` would then
never have been called and there would be no popup to destroy under.

## S4 — the session still works afterwards

Three proxies have now been torn down under live dependents. Codex's review
recipe asks for one more thing the probe cannot check about itself: that input
still reaches an unrelated window.

The probe released the shell role when its last mode exited, so take the role
with the bystander (the restore trap armed in Setup still covers it) and drive
a fresh client through it.

```bash
CURSOR=$(qdwin_apps_journal_cursor)
qdwin_apps_become_shell || { echo "ERROR: could not take the shell role back"; exit 1; }
qdwin_apps_session_up   || { echo "FAIL: session not healthy after the teardowns"; exit 1; }

qdwin_apps_launch qd22-after \
    "qdistro-test-window --title qd22-after --width 400 --height 260 --color 0xff203040"
HANDLE=
for _ in $(seq 1 30); do
    HANDLE=$("$QDWIN_VM_EXEC" "$VMNAME" \
      "grep -E 'qdwin-bystander: toplevel_added handle=[0-9]+ .*app_id=\"qdistro-test-window\"' \
         /tmp/bystander.log 2>/dev/null | tail -1 | sed -nE 's/.*handle=([0-9]+).*/\1/p'")
    [ -n "$HANDLE" ] && break
    sleep 0.3
done
echo "post-teardown handle=$HANDLE"

qdwin_click 200 200 left
qdwin_send_key KEY_A
sleep 1
"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     journalctl --user -b -u qdwin-compositor.service \
     --after-cursor '$CURSOR' --no-pager -o cat"
```

**Assert (4.1):** `$HANDLE` is a non-empty integer — the compositor still
admits new toplevels after three proxy teardowns.
**Assert (4.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`. This
is the assert that makes 4.1 mean anything: a compositor that died and was
restarted by systemd would also accept a new toplevel, and would otherwise
read as a clean pass.
**Assert (4.3):** the compositor journal after `$CURSOR` contains no `SIGSEGV`,
`use-after-free`, `double free`, or `Assertion` line.
**Assert (4.4) — MANDATORY, this is the real point of S4:** the click and the
keystroke were delivered to `qd22-after` *after it was mapped*. A leftover
pointer grab from S1/S2/S3 is invisible in 4.1-4.3 — a compositor with a stale
grab still accepts new toplevels and keeps its PID — and input not arriving is
the only symptom it has (`todo/reviews/proxy-lane-review-r1.md` finding 2).

So the click must target `qd22-after`'s own rectangle, not a fixed (200,200)
that may land on nothing, and the observation boundary must be taken AFTER the
window maps — otherwise a focus line from initial mapping satisfies it, since
the cursor was already on screen before the launch:

```bash
GEOM=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "grep -E 'toplevel_geometry handle=$HANDLE ' /tmp/bystander.log | tail -1")
echo "target geom: $GEOM"          # x= y= w= h= — click its centre
CURSOR2=$(qdwin_apps_journal_cursor)     # boundary AFTER mapping
qdwin_click "$CLICK_X" "$CLICK_Y" left
qdwin_send_key KEY_A
sleep 1
```

The click must produce a focus/activate line naming `$HANDLE` in the journal
after `$CURSOR2`. If qdwin logs nothing for focus at the default log level,
this scenario cannot assert 4.4 as written — record it as an ERROR against the
lane (an unobservable oracle), NOT as a PASS, and see the open follow-up.

**Assert (4.5):** clean up the client this step launched:
`"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f qd22-after'`.

## Teardown

The `EXIT` trap reaps any surviving `qdwin-nested-probe` and then restores
`qdshell.service`, proving its compositor-visible bind. Order matters: the
probe holds the singleton shell role while it blocks, so restoring first would
race it.

**Assert (T.1):** after restoration, `qdwin_compositor_pid` still equals
`$COMP_PID_BEFORE`. The handoff itself is part of what this lane exercises — a
compositor that died during the final restore is a failure, not a clean exit.

Leftovers this scenario is responsible for: the probe's proxies (destroyed by
the probe itself), the probe processes (reaped by the trap), `$QD22_LOG` in the
VM's /tmp (per-run, harmless), and the `qd22-after` client (assert 4.5).
