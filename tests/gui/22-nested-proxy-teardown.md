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

# PRECONDITION (infra): S3's click calibration must be OUTPUT-AWARE. The qdwin
# golden advertises THREE wl_outputs — the DRM scanout head `Virtual-1` plus the
# PipeWire forwarding outputs `pipewire-0`/`pipewire-1`
# (tests/gui/agent-shell-capture-smoke.sh asserts exactly those three) — and a
# PipeWire output takes no seat input at all, so "the first wl_output" is not the
# click space and an output COUNT was never the real precondition. A probe that
# refuses on `3 outputs` predates the fix; it cannot be driven here, and that is
# a stale deployment, NOT a property of the product and NOT something to skip
# past. Name the remedy instead of hiding the gap:
"$QDWIN_VM_EXEC" "$VMNAME" \
    'qdwin-nested-probe --help 2>&1 | grep -q -- --output' \
    || { echo "ERROR: the installed qdwin-nested-probe predates multi-output click calibration (no --output). S3 cannot run on a multi-head image without it. Rebuild qdwin's test-client and rebake the golden."; exit 1; }

# The head S3 aims at. `qdwin_click` normalises pixels with QDWIN_SCREEN_W/H
# against QEMU's tablet, which spans the single DRM scanout — the same output
# qdwin pins shell capture to (QDWIN_SHELL_CAPTURE_OUTPUT in qdwin/qdwin.c) and
# the same one the GUI lane screenshots. Declared here so the click space, the
# capture space and the probe's arithmetic are one named thing.
: "${QD22_OUTPUT:=Virtual-1}"

# Record the compositor identity ONCE, before anything is torn down. Every step
# re-reads it: a crash-and-restart is the failure this whole lane exists to
# catch, and a restarted compositor would otherwise look like a clean session.
COMP_PID_BEFORE=$(qdwin_compositor_pid)
[ -n "$COMP_PID_BEFORE" ] || { echo "ERROR: no compositor pid"; exit 1; }
echo "compositor pid before = $COMP_PID_BEFORE"

# Per-invocation state. `$$` alone repeats if the scenario is rerun in one
# shell, and a stale file from an earlier attempt would then be read as this
# run's.
QD22_RUN="$$-$(date +%s)-$RANDOM"
QD22_LOG=/tmp/qd22-popup.$QD22_RUN.log
QD22_PID=/tmp/qd22-popup.$QD22_RUN.pid
QD22_CANCEL=/tmp/qd22-popup.$QD22_RUN.cancel
QD22_INTENT=/tmp/qd22-popup.$QD22_RUN.intent
QD22_FAILED=0

# ---------------------------------------------------------------------------
# ONE cleanup handler, defined and installed BEFORE anything is launched.
# Do not redefine or replace it later in this scenario: an earlier draft
# defined it in S3, told the reader to insert the terminal cleanup afterwards,
# and then replaced the whole thing in Teardown — which silently dropped both
# (proxy-lane-review-r4.md finding 2).
# ---------------------------------------------------------------------------

# Reap S3's popup probe. Exit 0 only when nothing of ours can be running.
#
# The hard case is a launcher that has been BACKGROUNDED but not yet reached
# its publication step: an absent pid-file then means "pending", not "never
# started", and cleanup that returns success there lets the probe start
# afterwards and hold the shell role (r4 finding 1, reproduced by pausing the
# launcher). The launcher and the reaper therefore both check both flags:
#   - the launcher tests $QD22_CANCEL before publishing AND again immediately
#     after, removing its pid-file and exiting rather than starting the probe;
#   - the reaper sets $QD22_CANCEL first, then watches for a late pid.
# One of the two always observes the other, so after this returns 0 no probe
# can start. $QD22_INTENT distinguishes "never launched" from "launched".
qd22_reap_probe() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "touch $QD22_CANCEL || { echo 'could not record cancellation'; exit 1; }; \
       [ -e $QD22_INTENT ] || { echo 'probe never launched'; exit 0; }; \
       p=''; \
       for _i in \$(seq 1 40); do \
         p=\$(cat $QD22_PID 2>/dev/null); \
         case \"\$p\" in ''|*[!0-9]*) sleep 0.1; continue;; esac; \
         break; \
       done; \
       case \"\$p\" in ''|*[!0-9]*) \
         echo 'cancellation recorded; no pid published'; exit 0;; \
       esac; \
       kill -TERM -\"\$p\" 2>/dev/null; \
       for _i in \$(seq 1 40); do kill -0 -\"\$p\" 2>/dev/null || { rm -f $QD22_PID; echo \"group \$p reaped\"; exit 0; }; sleep 0.1; done; \
       kill -KILL -\"\$p\" 2>/dev/null; \
       for _i in \$(seq 1 20); do kill -0 -\"\$p\" 2>/dev/null || { rm -f $QD22_PID; echo \"group \$p killed\"; exit 0; }; sleep 0.1; done; \
       echo \"probe group \$p SURVIVED\"; exit 1" 2>&1
}

qd22_cleanup() {
    local reaped=ok
    qd22_reap_probe || { reaped=failed; QD22_FAILED=1
        echo "FAIL: could not reap the popup probe — it may still hold the shell role"; }

    # S4's terminal, matched on the per-run title. The bracket stops `pkill -f`
    # matching the guest-agent shell running it; harmless if S4 never ran.
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "pkill -u admin -f \"qd22-af[t]er-$QD22_RUN\" 2>/dev/null" >/dev/null 2>&1 || true

    if [ "$reaped" = failed ]; then
        # Restore anyway so the desktop is not left headless, but never let a
        # recovery that ran with ownership unresolved read as a clean exit.
        echo "WARN: restoring qdshell with probe ownership UNRESOLVED — this is"
        echo "      recovery, not a pass; verify the session by hand."
    fi
    qdwin_apps_restore_shell || { echo "FAIL: qdshell restore failed"; QD22_FAILED=1; }

    local after; after=$(qdwin_compositor_pid)
    if [ "$after" != "$COMP_PID_BEFORE" ]; then
        echo "FAIL (T.1): compositor pid $COMP_PID_BEFORE -> $after"
        QD22_FAILED=1
    else
        echo "T.1 ok: compositor pid unchanged ($after)"
    fi
    # The cancel flag is a TOMBSTONE and is deliberately NOT removed. A
    # launcher descheduled before publication can still resume after this
    # function returns; its post-publication check is what stops it, and that
    # check needs the flag to still exist. An earlier version deleted these
    # three files here and reopened the exact race the protocol was written to
    # close (proxy-lane-review-r5.md finding 1) — the stress runs missed it
    # because they exercised qd22_reap_probe directly, with the flag retained,
    # rather than this handler. The files are empty, per-invocation, and in the
    # VM's /tmp; leaving them is the cheap half of the trade.

    if [ "$QD22_FAILED" != 0 ]; then
        echo "SCENARIO VERDICT: FAIL (see the FAIL lines above)"
    fi
}

# Free the singleton shell role for the probe (stops qdshell, evicts any suite
# bystander, waits for qdwin to log `shell unbound`). Arm cleanup IMMEDIATELY
# after a successful takeover so a later failure never leaves the desktop
# headless.
qdwin_apps_prepare_shell_probe \
    || { echo "ERROR: could not reserve the singleton shell role"; exit 1; }
trap 'qd22_cleanup' EXIT

# LANE CONSTRAINT: S3's click target is computed against ONE NAMED output —
# $QD22_OUTPUT, the DRM scanout head the injected pointer actually lands on.
# The probe binds every wl_output, selects that one by name, maps the proxy's
# GLOBAL rectangle into the head's LOCAL pixels (subtracting the head's origin),
# and moves the proxy onto that head with request_set_position if the compositor
# placed it elsewhere. So the number of outputs is irrelevant here; what must
# hold is that the head exists, is unscaled and untransformed, and that its mode
# equals QDWIN_SCREEN_W/H. The probe checks the first three itself — the right
# place, since only the probe sees what the compositor advertises — exits 77
# naming the one that differs, and reports them all in its PROXY_GEOM line
# (output= out=WxH@x,y outputs= scale= transform=). Assert 3.3 checks the set.

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
and NOTHING in this scenario observes that: S4 proves keyboard delivery only,
and the bystander focuses the keyboard on `toplevel_added`, so a stale pointer
grab would swallow pointer events while typing keeps working. Treat S1 as a
crash/liveness check; the missing oracle is `todo/open-followups.md` item 2.

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

`rc=77` with `subscribe DENIED (no free pipewire output ...)` establishes only
that no output was FREE: `qdwin_handle_subscribe_view_stream` reports the same
thing whether weston.ini configures none (`[pipewire] num-outputs`) or every
configured one is already occupied by another stream — check both. A
spawn-related reason likewise means the spawn failed, not specifically that
`qdistro-forward` is missing. Both are ERROR (infra), same rule as
`13-rdp-subscribe-frame.md`, but neither names its own cause.

## S3 — destroy under a LIVE chrome popup

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
to be running once `qd22_cleanup` (defined in Setup) has returned — otherwise
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

```bash
CURSOR=$(qdwin_apps_journal_cursor)

"$QDWIN_VM_EXEC" "$VMNAME" "rm -f $QD22_LOG $QD22_PID $QD22_CANCEL $QD22_INTENT" >/dev/null \
    || { echo "ERROR: could not clear per-run state in the VM"; exit 1; }
# SYNCHRONOUS: after this returns, an absent pid means "pending", not "never".
"$QDWIN_VM_EXEC" "$VMNAME" "touch $QD22_INTENT" >/dev/null \
    || { echo "ERROR: could not record launch intent in the VM"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=$ACTIVE_SOCKET \
     setsid sh -c '[ -e $QD22_CANCEL ] && exit 91; \
                   echo \$\$ > $QD22_PID.tmp && mv $QD22_PID.tmp $QD22_PID || exit 90; \
                   [ -e $QD22_CANCEL ] && { rm -f $QD22_PID; exit 91; }; \
                   qdwin-nested-probe --destroy-with-popup --click-timeout 60 \
                     --output $QD22_OUTPUT \
                     >$QD22_LOG 2>&1; \
                   echo rc=\$? >>$QD22_LOG' &" \
  >/dev/null

# Acknowledge ownership before doing anything that could fail.
PROBE_PID=
for _ in $(seq 1 40); do
    PROBE_PID=$("$QDWIN_VM_EXEC" "$VMNAME" "cat $QD22_PID 2>/dev/null")
    [ -n "$PROBE_PID" ] && break
    sleep 0.25
done
# Not fatal by itself: qd22_cleanup can still cancel a pending launcher. But the
# step cannot proceed without a probe.
[ -n "$PROBE_PID" ] || { echo "ERROR: probe never published its pid within 10s"; exit 1; }
echo "probe group pid=$PROBE_PID"

# The probe prints CLICK_TARGET once the chrome is attached and committed. The
# trailing space in the pattern matters: the probe also prints
# CLICK_TARGET_GLOBAL (the same point before the output origin is subtracted,
# for diagnosis), and clicking THAT on a head with a nonzero origin would aim
# outside the scanout.
TARGET=
for _ in $(seq 1 40); do
    TARGET=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "grep -m1 '^CLICK_TARGET ' $QD22_LOG 2>/dev/null")
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

# The probe must be gone AND the compositor must have released its shell role
# before S4 claims it. qdwin_apps_restore_shell's own pre-start wait watches
# for a BYSTANDER unbind, which says nothing about this probe.
qd22_reap_probe || { echo "ERROR: probe still holds the shell role; not proceeding to S4"; exit 1; }
SHELL_FREE=0   # Setup supports rerunning in one shell; a previous run's 1
               # would otherwise satisfy this gate with no evidence from THIS
               # run (proxy-lane-review-r5.md finding 3).
for _ in $(seq 1 40); do
    "$QDWIN_VM_EXEC" "$VMNAME" \
      "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         journalctl --user -b -u qdwin-compositor.service \
         --after-cursor '$CURSOR' --no-pager -o cat 2>/dev/null" \
      | grep -qE '^(\[[0-9:.]+\] )?qdwin: shell unbound$' && { SHELL_FREE=1; break; }
    sleep 0.25
done
[ "${SHELL_FREE:-0}" = 1 ] \
    || { echo "ERROR: compositor never reported 'shell unbound' after the probe"; exit 1; }
```

**Assert (3.1):** the log ends with `rc=0` and carries `proxy destroyed under a
LIVE chrome popup; dismissed fired; compositor alive`.
**Assert (3.2):** `qdwin_compositor_pid` still equals `$COMP_PID_BEFORE`.

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
than where it was aimed and S3's result says nothing about the product.

`rc=77` naming the OUTPUT (`no output named "Virtual-1" among N advertised
[...]`) means the head this lane injects into is not the one the probe was
asked for. The message lists every advertised head with its geometry: pick the
scanout one and re-run with `QD22_OUTPUT=<name>`. This is a lane/config
mismatch — report ERROR with that list, never a pass and never a silent skip.

`rc=77` saying the proxy `cannot be placed on output` means
`request_set_position` did not move it onto the clickable head (a v29-only
shell, or the compositor refusing the move). Report ERROR: the calibration
could not be established, so nothing about the teardown path was tested.

`rc=77` with `no chrome_button within 60s` means the precondition was not
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

The probe released the shell role when its last mode exited, so take the role
with the bystander (the `qd22_cleanup` trap installed in Setup covers it).

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

A failed remote read produces no stdout, and piping straight into `grep`
makes that indistinguishable from a successful read with nothing to report —
it takes the same "clean" branch, `pipefail` or not
(`proxy-lane-review-r4.md` finding 3). Capture first, check the read, then
search:

```bash
JOURNAL=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     journalctl --user -b -u qdwin-compositor.service \
     --after-cursor '$CURSOR' --no-pager -o cat")
if [ $? -ne 0 ]; then
    echo "ERROR (4.4): could not read the compositor journal — assertion not made"
elif printf '%s\n' "$JOURNAL" | grep -E 'SIGSEGV|use-after-free|double free|Assertion'; then
    echo "FAIL (4.4): see above"
else
    echo "4.4 clean"
fi
```

The terminal is cleaned up by `qd22_cleanup` (Setup), which already kills it by
the per-run title — nothing to add here. It deliberately is NOT cleaned up at
this point in the step: an `exit 1` above, or a runner stopping on a failed
assert, would skip any cleanup written inline.

## Teardown

`qd22_cleanup`, defined and installed in **Setup**, is the only cleanup path.
Do not define a second handler here: an earlier draft replaced it at this point
and silently dropped the terminal cleanup, the reaper's status check and the
scenario-failure line (`proxy-lane-review-r4.md` finding 2).

In order, it: reaps S3's probe group (setting the cancel flag first, so a
launcher that has not yet published cannot start one), kills S4's terminal by
its per-run title, restores `qdshell.service`, and compares the compositor pid
against `$COMP_PID_BEFORE`.

**Assert (T.1):** `T.1 ok: compositor pid unchanged` — the final handoff is
itself part of what this lane exercises, so a compositor that died during the
restore is a failure, not a clean exit.

**Assert (T.2):** no `SCENARIO VERDICT: FAIL` line. The GUI helpers do not set
`-e` and a trap cannot change the shell's exit status usefully here, so this
line is the verdict a runner must read. Treat its presence as a scenario
failure regardless of what the individual steps printed.

**Assert (T.3):** no `WARN: restoring qdshell with probe ownership UNRESOLVED`.
That path exists so the desktop is not left headless, but it means cleanup ran
without establishing that the probe was gone — recovery, not a pass.

Leftovers this scenario owns: the probe's proxies (destroyed by the probe
itself), the probe group and S4's terminal (both reaped by `qd22_cleanup`), and
`$QD22_LOG` in the VM's /tmp — per-run and deliberately kept, since it holds the
popup step's verdict.
