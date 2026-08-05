# 21 — closing a tier-5 toplevel tears down the guest VM cleanly

**What**: bring up a tier-5 `--vm` weston-terminal, drive
`Qdwin.closeWindow(handle)` through qdshell's IPC (the same supported
test surface scenario 17 uses), verify the inner app exits AND the
per-app guest VM domain is destroyed + undefined AND the per-VM
overlay qcow2 is unlinked. No orphans.

**Why**: this is the orphan-resource budget for tier-5. Anything
left around after the toplevel goes away is a leak (libvirt
domain, qcow2 disk, dangling waypipe processes). The bats variant
`tests/integration/vm/s48-tier5-close-cleanup.sh` SIGTERMs the
wrapper directly; here we exercise the `xdg_toplevel.close` path —
qdshell's binding issues `qdwin_shell_v1.request_close`, qdwin relays
the close through waypipe to the tier-5 inner app, and the toplevel
is torn down. The close is driven by IPC, **not** by agent-clicking
the title-bar glyph: clicking depends on OCR aim and on the idle
locker not stealing the frame, so a missed click was historically
mis-read as "window persists after close". The IPC path removes both
variables, so a toplevel that survives the close is an unambiguous
product signal. The title-bar chrome close button itself is covered
separately by `qdwin/tests/gui/08-titlebar-close-button.md`.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM (these scenarios are driven with an explicit VM)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Preconditions (same as scenario 20).
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'test -e /dev/kvm'
$VMEXEC "$VM" 'test -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2'
$VMEXEC "$VM" 'test -d /root/qdistro-src/qdistro/tier5-vm'

# qs_ipc <method> [args...] — call a qdwin IPC method on the running qdshell,
# mirroring scenario 17's proven `ipc_vm` shape: runuser -u admin -- env …
# WAYLAND_DISPLAY=wayland-1 qs ipc -p PATH call qdwin …. Falls back to PID
# targeting when the -p path lookup can't find the -p-launched instance (the
# fallback scenario 17 found is sometimes needed — without it a healthy binding
# can read as "no running instance").
QS_PATH=/usr/share/quickshell/qdshell
qs_ipc() {
    local out
    out=$($VMEXEC "$VM" \
        "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
         qs ipc -p $QS_PATH call qdwin $*" 2>&1)
    if printf '%s' "$out" | grep -qiE 'no running instance|No such'; then
        local pid
        pid=$($VMEXEC "$VM" \
            "pgrep -u admin -f '[q]s -p $QS_PATH' | while read p; do \
               grep -q dbus-run-session /proc/\$p/cmdline 2>/dev/null || { echo \$p; break; }; done")
        [ -n "$pid" ] || { printf '%s\n' "$out"; return 1; }
        out=$($VMEXEC "$VM" \
            "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
             qs ipc --pid $pid call qdwin $*" 2>&1)
    fi
    printf '%s\n' "$out"
}

# Binding preflight: if the qdwin IPC binding isn't reachable, the close path
# can't be driven — that's an ERROR (setup), never a product close-cleanup FAIL.
qs_ipc capabilities | grep -q 'bound=true' \
    || { echo "ERROR: qs ipc bridge or qdwin binding not reachable (bound!=true)"; exit 2; }

VM5="qdistro-tier5-s21-$RANDOM"
echo "VM5=$VM5"
$VMEXEC "$VM" 'pkill -u root -f "[s]pawn-tier5.sh" 2>/dev/null || true; sleep 1'
```

## Steps

### S1 — spawn + wait for the toplevel to be visible

```bash
B64=$(base64 -w0 <<EOF
rm -rf /tmp/qdistro-tier5
mkdir -p /tmp/qdistro-tier5
cp -r /root/qdistro-src/qdistro/tier5-vm /tmp/qdistro-tier5/tier5-vm
cp -r /root/qdistro-src/qdistro/lib /tmp/qdistro-tier5/lib
chmod -R a+rX /tmp/qdistro-tier5
find /tmp/qdistro-tier5 -name '*.sh' -exec chmod a+rx {} +
# TIER5_MEM_KIB=2097152 (2 GiB): the former 512 MiB CI accommodation was too
# tight for the guest kernel + compositor + app stack and could kill the guest
# mid-boot. Keep the scenario explicit so CI and the bats probes use one budget.
# TIER5_SHUTDOWN_METHOD=force: this scenario is the orphan-resource-completeness
# net (everything reclaimed after close), so it pins the deterministic hard reap
# — independent of whether the test base image carries the guest power-button
# wiring that graceful (the default) needs. The graceful ACPI path + per-app
# policy are covered by tests/integration/vm/s49-tier5-graceful-shutdown.sh.
TIER5_MEM_KIB=2097152 TIER5_SHUTDOWN_METHOD=force setsid bash /tmp/qdistro-tier5/tier5-vm/spawn-tier5.sh --vm "$VM5" \
    -- weston-terminal </dev/null >/tmp/s21-spawn.log 2>&1 &
disown
EOF
)
# Capture a compositor-journal cursor BEFORE the spawn so the mapped-check below
# cannot be satisfied by a STALE app_id event from a prior attempt. Persist it
# inside the guest: visual agents do not guarantee that a host-shell variable
# survives when they execute a long fenced block in smaller tool calls.
$VMEXEC "$VM" "mkdir -p /tmp/qci-qdistro_tests_integration_permissions-gui_21-tier5-close-cleanup.md; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p' > /tmp/qci-qdistro_tests_integration_permissions-gui_21-tier5-close-cleanup.md/journal.cur"

$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# Readiness gate (replaces the old weak host-log-proxy + blind `sleep 5` + single
# screenshot — the dominant S1 flake: a guest that had actually committed its
# secctx was caught mid-paint as an empty desktop and read as a false ERROR). Poll
# CONTINUOUSLY, classifying liveness vs death, until the inner tier-5 toplevel
# really maps (the SAME condition the screenshot asserts) or the ~3min budget
# expires. The domstate poll mirrors scenario 20's proven tolerant idiom (a
# transient empty/error read is NOT death; only an explicit terminal state on
# consecutive reads is). The mapped-check is journal-cursor-scoped to THIS attempt.
# Every non-mapped outcome ERRORs LOUD with a precise reason — a real nested-guest
# death (tier-5 memory budget), a never-running guest, or alive-but-never-mapped are all
# genuine infra/product signals, never masked.
mapped=0; saw_running=0; term_hits=0; reason=""; handle=""
deadline=$((SECONDS + 180))
while [ $SECONDS -lt $deadline ]; do
    state=$($VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        HOME=/home/admin LIBVIRT_DEFAULT_URI=qemu:///session \
        virsh domstate $VM5 2>/dev/null || true" | tr -d '[:space:]')
    case "$state" in
        running|paused|idle|pmsuspended) saw_running=1; term_hits=0 ;;
        shutoff|crashed)
            term_hits=$((term_hits + 1))
            [ "$term_hits" -ge 2 ] && { reason="guest domain reached terminal state '$state' on consecutive reads (real nested-guest death under TIER5_MEM_KIB=2GiB)"; break; } ;;
    esac
    # Stage 1: learn OUR toplevel's qdwin handle from the secctx line that carries
    # our app_id (cursor-scoped, so a stale prior-attempt handle can't be picked
    # up). The secctx line is emitted at security-context SETUP — necessary to map
    # app_id→handle, but NOT proof the window painted, hence stage 2.
    if [ -z "$handle" ]; then
        handle=$($VMEXEC "$VM" "cur=\$(cat /tmp/qci-qdistro_tests_integration_permissions-gui_21-tier5-close-cleanup.md/journal.cur 2>/dev/null); [ -n \"\$cur\" ] || { echo MISSING_CURSOR >&2; exit 2; }; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user --after-cursor \"\$cur\" --no-pager -o cat 2>/dev/null | grep -F 'app_id=qdistro.tier5.$VM5' | grep -oE 'toplevel_security_context handle=[0-9]+' | grep -oE '[0-9]+' | head -1" | tr -d '[:space:]')
    fi
    # Stage 2: that handle actually MAPPED. qdwin emits `qdwin: mapped handle=N`
    # ONLY after the toplevel's first buffer commit — i.e. it is painted, the same
    # condition the screenshot asserts. Matching `mapped` (not the earlier
    # secctx-committed setup line) is what makes this a true readiness gate and
    # avoids the original race where a screenshot beat the first frame.
    if [ -n "$handle" ] && $VMEXEC "$VM" "cur=\$(cat /tmp/qci-qdistro_tests_integration_permissions-gui_21-tier5-close-cleanup.md/journal.cur 2>/dev/null); [ -n \"\$cur\" ] || { echo MISSING_CURSOR >&2; exit 2; }; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user --after-cursor \"\$cur\" --no-pager -o cat 2>/dev/null | grep -qE 'qdwin: mapped handle=$handle '"; then
        mapped=1; break
    fi
    sleep 2
done

if [ "$mapped" != 1 ]; then
    [ -n "$reason" ] || { [ "$saw_running" = 1 ] \
        && reason="guest reached 'running' but the tier-5 toplevel never mapped within ~3min" \
        || reason="guest domain never reached 'running' within ~3min"; }
    echo "ERROR(S1): $reason"
    $VMEXEC "$VM" 'tail -40 /tmp/s21-spawn.log' 2>/dev/null
    exit 2
fi

# qdwin emitted `mapped handle=$handle` for our toplevel — it has painted its
# first frame. Persist the handle so S2 doesn't depend on the shell variable
# surviving across fenced blocks (the runner does not guarantee one persistent
# shell). Then capture the visual frame for the assertion.
printf '%s' "$handle" > "${QCI_SCENARIO_TMPDIR:-/tmp}/s21-handle"
sleep 1
$VMGUI "$VM" screenshot /tmp/s21-running.png
```

**Assert** (agent-visual, soft corroboration): `/tmp/s21-running.png`
shows a weston-terminal (or equivalent terminal) window rendered like
a normal app window. The readiness gate above already proved the
toplevel mapped — that is the **hard** S1 gate. Do **not** FAIL solely
because the window title is a generic client string such as
`Wayland Terminal`: weston-terminal often keeps its own title and does
not surface waypipe's `--title-prefix` (`[tier5:$VM5]`) on the
xdg_toplevel title. The prefix is optional soft evidence when visible
in the title bar or taskbar; its absence is not a product FAIL when
`mapped handle=…` was already observed. Hard FAIL S1 only if there is
no visible terminal window at all despite a mapped journal line
(screenshot black / empty desktop).

If S1 errors above (no visible toplevel within ~3min — guest never
ran, died, or never mapped), record ERROR (not FAIL): the close path
can't be exercised without something to close. The echoed reason
distinguishes a real nested-guest death from a slow/absent map.

### S2 — drive request_close via qdshell IPC (deterministic)

Close the tier-5 toplevel through `Qdwin.closeWindow(handle)` — the
supported Quickshell IPC surface, exactly as scenario 17 drives it —
not by clicking the title-bar glyph. `$handle` is the tier-5
toplevel handle the S1 readiness gate already learned.

```bash
handle=$(cat "${QCI_SCENARIO_TMPDIR:-/tmp}/s21-handle" 2>/dev/null)
[ -n "$handle" ] || { echo "ERROR: no tier-5 handle persisted from S1 (cannot drive close)"; exit 2; }

# Journal cursor BEFORE the close so the asserts below cannot match a stale
# request_close/toplevel_removed from S1 or a prior attempt.
CUR2=$($VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
  journalctl --user -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p'")

# Drive close via the qdwin IPC binding (qs_ipc handles the -p / --pid fallback).
qs_ipc closeWindow "$handle" \
  || echo "WARN: qs ipc closeWindow returned nonzero (see binding preflight)"

# Mechanically check BOTH product markers — do NOT rely on the screenshot:
#   request_seen — qdshell issued request_close (proves the IPC reached qdwin);
#                  for a tier-5 proxy this is the `(nested-proxy: …)` variant.
#   removed      — the tier-5 toplevel actually went away within ~5s.
# This split is the whole diagnostic: removed without request_seen would be an
# app that exited on its own, not a close we drove.
request_seen=0; removed=0
for _ in $(seq 1 10); do
    if [ "$request_seen" = 0 ] && $VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        journalctl --user --after-cursor '$CUR2' --no-pager -o cat 2>/dev/null \
        | grep -qE 'qdwin: request_close handle=$handle( |\$)'"; then
        request_seen=1
    fi
    if $VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        journalctl --user --after-cursor '$CUR2' --no-pager -o cat 2>/dev/null \
        | grep -qE 'qdwin: toplevel_removed handle=$handle( |\$)'"; then
        removed=1; break
    fi
    sleep 0.5
done
echo "request_seen=$request_seen removed=$removed"
sleep 1
$VMGUI "$VM" screenshot /tmp/s21-after-close.png
```

**Assert (2.1):** `request_seen=1` — a `qdwin: request_close handle=$handle`
line appeared after `$CUR2` (the `(nested-proxy: fired close_requested)`
variant for tier-5), so qdshell's binding issued `qdwin_shell_v1.request_close`
and it reached the compositor. **If `request_seen=0`** (and `qs ipc` warned
nonzero), this is an IPC/binding ERROR, not a product FAIL — re-check the
`qs_ipc capabilities`→`bound=true` preflight; do NOT bank a product result.
**Assert (2.2):** `removed=1` — a `qdwin: toplevel_removed handle=$handle`
line appeared within ~5s — the tier-5 inner app received `xdg_toplevel.close`
through waypipe and exited cleanly, and qdwin tore the proxy toplevel down.
**If `request_seen=1` but `removed=0`**, the close request reached the
compositor but the tier-5 inner app never closed: that is the genuine tier-5
close/cleanup defect this scenario guards — record FAIL with the
`--after-cursor "$CUR2"` journal slice as evidence, NOT a missed-click ERROR.
**Assert (2.3, soft):** `/tmp/s21-after-close.png` no longer shows the
weston-terminal window, and the taskbar/dock has no `[tier5:$VM5]` entry.

The bats variant `s48-tier5-close-cleanup.sh` exercises the same trap
via SIGTERM directly; it's the regression net underneath this scenario.

### S3 — wait for the reap, then verify the wrapper exited

Closing the app reaps the VM ASYNCHRONOUSLY: in `--vm` mode
`spawn-tier5.sh`'s wait loop notices the published app exited (polled
via the guest agent) and then runs its EXIT trap — `virsh destroy` +
`virsh undefine` + overlay unlink. That is a couple of poll cycles plus
the virsh teardown, not instant, so poll up to ~20s for it to complete
before asserting.

Check all three IN A SINGLE guest command per poll and ONLY interpret the
result when `vm-exec` itself succeeded — a transport/qga failure on the outer
VM must read as "unknown, keep polling", NEVER as "gone" (that would false-pass
this assertion on an outer-VM hiccup).

```bash
for _ in $(seq 1 40); do
    res=$($VMEXEC "$VM" "w=present; d=present; o=present; \
        pgrep -f '[s]pawn-tier5.sh.*$VM5' >/dev/null || w=gone; \
        runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/admin \
        LIBVIRT_DEFAULT_URI=qemu:///session virsh dominfo $VM5 >/dev/null 2>&1 || d=gone; \
        test -e /home/admin/.local/share/libvirt/images/$VM5.qcow2 || o=gone; \
        echo \"S3RESULT w=\$w d=\$d o=\$o\"") || { sleep 1; continue; }
    # Require the structured token (proves the guest command actually ran), then
    # stop once all three are gone.
    printf '%s\n' "$res" | grep -q 'S3RESULT ' || { sleep 1; continue; }
    printf '%s\n' "$res" | grep -q 'w=gone d=gone o=gone' && { echo "$res"; break; }
    sleep 1
done
# Final S3 probe — print an unambiguous token; treat vm-exec transport failure
# as ERROR (re-run), not as a pass.
# NOTE: the pgrep pattern uses the [s]pawn bracket trick so it cannot match the
# probe shell's own command line (which literally contains this pgrep string) —
# a plain 'spawn-tier5.sh.*$VM5' self-matches and false-reports STILL-RUNNING.
$VMEXEC "$VM" "pgrep -f '[s]pawn-tier5.sh.*$VM5' >/dev/null \
    && echo S3-WRAPPER-STILL-RUNNING || echo S3-WRAPPER-GONE-EXPECTED" \
    || echo "S3-TRANSPORT-FAIL (outer vm-exec/qga failed; re-run — NOT a product result)"
```

**Assert**: the final probe prints `S3-WRAPPER-GONE-EXPECTED` — no
`spawn-tier5.sh` process remains for `$VM5`. In `--vm` mode the wrapper's loop
ends when the single published app exits (detected via the guest agent), then
its trap reaps the domain. `S3-WRAPPER-STILL-RUNNING` after the ~40s budget is
the orphan leak this scenario guards; `S3-TRANSPORT-FAIL` is an
infrastructure/ERROR result (the outer guest agent was unreachable), not a
product FAIL — re-run.

### S4 — verify libvirt domain is gone

```bash
$VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    HOME=/home/admin LIBVIRT_DEFAULT_URI=qemu:///session \
    virsh dominfo $VM5 2>&1 || echo no-such-domain"
```

**Assert**: output contains `no-such-domain` (or
`error: failed to get domain`) — i.e., the domain is gone, not
merely stopped. The trap in `spawn-tier5.sh` does both `virsh destroy`
and `virsh undefine`, so a stopped-but-still-defined domain is a
**leak**, not a pass.

### S5 — verify the overlay qcow2 is unlinked

```bash
$VMEXEC "$VM" "ls /home/admin/.local/share/libvirt/images/$VM5.qcow2 2>&1 || echo overlay-gone"
```

**Assert**: output contains `No such file or directory` or
`overlay-gone`. The base image
(`/var/lib/libvirt/images/qdistro-tier5-base.qcow2`) MUST still be
present — we only reap the per-VM overlay. Cross-check:

```bash
$VMEXEC "$VM" 'ls -lh /var/lib/libvirt/images/qdistro-tier5-base.qcow2'
```

### S6 — cleanup (defensive)

```bash
$VMEXEC "$VM" "pkill -u root -f \"[s]pawn-tier5.sh.*$VM5\" 2>/dev/null || true; \
               runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/admin \
                 LIBVIRT_DEFAULT_URI=qemu:///session virsh destroy $VM5 2>/dev/null || true; \
               runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/admin \
                 LIBVIRT_DEFAULT_URI=qemu:///session virsh undefine $VM5 2>/dev/null || true; \
               rm -f /home/admin/.local/share/libvirt/images/$VM5.qcow2"
```

## Known caveats

- **Session libvirt environment is load-bearing**: every direct `virsh` probe
  sets admin's `XDG_RUNTIME_DIR`, `HOME`, and `LIBVIRT_DEFAULT_URI`. Omitting
  `XDG_RUNTIME_DIR` can connect the probe to a different socket-activated
  session daemon, making a running product domain appear absent or exposing an
  unrelated stale definition. Never shorten these probes to an environment-free
  admin `virsh` invocation.
- **IPC binding must be reachable**: `qs ipc -p $QS_PATH call qdwin
  closeWindow` needs the qdwin binding bound (`qs ipc capabilities`
  reporting `bound=true`, as scenario 16/17 assert). If the call
  returns nonzero or no `request_close` line appears (2.1 silent),
  that's an IPC/binding failure, not a close-cleanup defect — re-check
  the binding before reading the result as a product FAIL.
- **xdg_toplevel.close semantics**: weston-terminal listens for
  the close request and exits cleanly. Some inner apps ignore the
  request — for those, the scenario falls back to forcing the close
  by killing the in-guest waypipe-server (out of scope here).
- **Trap reliability**: if `spawn-tier5.sh` is killed with SIGKILL
  the trap doesn't run and the domain WILL leak. This scenario
  exercises a clean close (SIGTERM via toplevel close), not a kill.
  The bats s37 variant covers SIGTERM specifically.
- **TIER5_KEEP_DOMAIN=1** intentionally skips cleanup for debug.
  This scenario assumes the env var is unset; setup leaves it alone.
