# 21 — closing a tier-5 toplevel tears down the guest VM cleanly

**What**: bring up a tier-5 `--vm` weston-terminal, click the close
button on its title bar (or close via the taskbar context menu),
verify the inner app exits AND the per-app guest VM domain is
destroyed + undefined AND the per-VM overlay qcow2 is unlinked. No
orphans.

**Why**: this is the orphan-resource budget for tier-5. Anything
left around after the toplevel goes away is a leak (libvirt
domain, qcow2 disk, dangling waypipe processes). The bats variant
`tests/integration/vm/s48-tier5-close-cleanup.sh` SIGTERMs the
wrapper directly — here we exercise the actual user-visible close
path through qdwin's title-bar chrome.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Preconditions (same as scenario 20).
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'test -e /dev/kvm'
$VMEXEC "$VM" 'test -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2'
$VMEXEC "$VM" 'test -d /root/qdistro-src/qdistro/tier5-vm'

VM5="qdistro-tier5-s21-$RANDOM"
echo "VM5=$VM5"
$VMEXEC "$VM" 'pkill -u root -f spawn-tier5.sh 2>/dev/null || true; sleep 1'
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
# TIER5_MEM_KIB=524288 (512 MiB): the 1.5 GiB default overcommits the nested CI
# VM and kills the guest mid-boot; mirror s45-tier5-vm.sh's CI accommodation.
TIER5_MEM_KIB=524288 setsid bash /tmp/qdistro-tier5/tier5-vm/spawn-tier5.sh --vm "$VM5" \
    -- weston-terminal </dev/null >/tmp/s21-spawn.log 2>&1 &
disown
EOF
)
# Capture a compositor-journal cursor BEFORE the spawn so the mapped-check below
# cannot be satisfied by a STALE app_id event from a prior attempt.
CUR=$($VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -n0 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p'")

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
# death (512 MiB budget), a never-running guest, or alive-but-never-mapped are all
# genuine infra/product signals, never masked.
mapped=0; saw_running=0; term_hits=0; reason=""; handle=""
deadline=$((SECONDS + 180))
while [ $SECONDS -lt $deadline ]; do
    state=$($VMEXEC "$VM" "runuser -u admin -- virsh domstate $VM5 2>/dev/null || true" | tr -d '[:space:]')
    case "$state" in
        running|paused|idle|pmsuspended) saw_running=1; term_hits=0 ;;
        shutoff|crashed)
            term_hits=$((term_hits + 1))
            [ "$term_hits" -ge 2 ] && { reason="guest domain reached terminal state '$state' on consecutive reads (real nested-guest death under TIER5_MEM_KIB=512MiB)"; break; } ;;
    esac
    # Stage 1: learn OUR toplevel's qdwin handle from the secctx line that carries
    # our app_id (cursor-scoped, so a stale prior-attempt handle can't be picked
    # up). The secctx line is emitted at security-context SETUP — necessary to map
    # app_id→handle, but NOT proof the window painted, hence stage 2.
    if [ -z "$handle" ]; then
        handle=$($VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user --after-cursor '$CUR' --no-pager -o cat 2>/dev/null | grep -F 'app_id=qdistro.tier5.$VM5' | grep -oE 'toplevel_security_context handle=[0-9]+' | grep -oE '[0-9]+' | head -1" | tr -d '[:space:]')
    fi
    # Stage 2: that handle actually MAPPED. qdwin emits `qdwin: mapped handle=N`
    # ONLY after the toplevel's first buffer commit — i.e. it is painted, the same
    # condition the screenshot asserts. Matching `mapped` (not the earlier
    # secctx-committed setup line) is what makes this a true readiness gate and
    # avoids the original race where a screenshot beat the first frame.
    if [ -n "$handle" ] && $VMEXEC "$VM" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user --after-cursor '$CUR' --no-pager -o cat 2>/dev/null | grep -qE 'qdwin: mapped handle=$handle '"; then
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
# first frame. Now capture the visual frame for the assertion.
sleep 1
$VMGUI "$VM" screenshot /tmp/s21-running.png
```

**Assert** (agent-visual): `/tmp/s21-running.png` shows a
weston-terminal window labelled `[tier5:$VM5]` rendered like a
normal app window. (The readiness gate above already proved the
toplevel mapped, so this is now a stable corroboration rather than a
race against a blind sleep.)

If S1 errors above (no visible toplevel within ~3min — guest never
ran, died, or never mapped), record ERROR (not FAIL): the close path
can't be exercised without something to close. The echoed reason
distinguishes a real nested-guest death from a slow/absent map.

### S2 — click the close button on the title bar

qdwin chromes tier-5 toplevels with `zxdg_decoration_manager_v1` in
server-side mode (per `qdwin/qdwin.c:62b2702`). The close button is
in the top-right corner of the title bar. OCR-find it (typically a
`×` glyph or labelled "Close" — depends on theme).

```bash
# Runner: read /tmp/s21-running.png, locate the close button on the
# tier-5 toplevel's title bar, click it.
$VMGUI "$VM" click <close_x> <close_y>
sleep 1
$VMGUI "$VM" screenshot /tmp/s21-after-close.png
```

**Assert** (agent-visual): `/tmp/s21-after-close.png` no longer
shows the weston-terminal window. The taskbar/dock has no entry
for `[tier5:$VM5]`.

If the close glyph is not detectable, fallback to a context-menu
close: right-click the toplevel's title bar → "Close" entry. Apply
the same screenshot-after assertion.

The bats variant `s48-tier5-close-cleanup.sh` exercises the same
trap via SIGTERM directly; it's the regression net underneath this
scenario.

### S3 — verify the wrapper exited

```bash
$VMEXEC "$VM" 'pgrep -af "spawn-tier5.sh.*'"$VM5"'" || echo "no spawn-tier5 wrapper running (expected)"'
```

**Assert**: no `spawn-tier5.sh` process remains for `$VM5`. The
wrapper's `wait $CLIENT_PID` returns when waypipe-client tears down
its side after the toplevel goes away.

### S4 — verify libvirt domain is gone

```bash
$VMEXEC "$VM" "runuser -u admin -- virsh dominfo $VM5 2>&1 || echo no-such-domain"
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
$VMEXEC "$VM" "pkill -u root -f \"spawn-tier5.sh.*$VM5\" 2>/dev/null || true; \
               runuser -u admin -- virsh destroy $VM5 2>/dev/null || true; \
               runuser -u admin -- virsh undefine $VM5 2>/dev/null || true; \
               rm -f /home/admin/.local/share/libvirt/images/$VM5.qcow2"
```

## Known caveats

- **Title-bar close button position** depends on the active qdshell
  theme. OCR/vision identification, NOT hard-coded pixel offsets.
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
