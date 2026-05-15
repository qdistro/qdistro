# 04 — lid-close engages the locker

**Acceptance criterion:** systemd-logind's lid-close event reaches
qdwin → qdwin fires `qdwin_locker_v1.lock_requested(reason=1=lid)`
→ qdlocker engages the lock.

> **Status: TODO until the C-side wiring lands.** Right now qdwin
> doesn't have the lid-close aggregation path on `qdwin_locker_v1`
> (only on the shell protocol). The scenario is written so it will
> pass as-is once the qdwin-side change in `qdwin/doc/locker.md §5`
> goes in. Until then it skips at Setup with a clear message.

VMs don't have a real laptop lid. qdwin's existing lid-close path
takes a logind D-Bus signal as input; for testing we synthesize the
signal directly. The qdistro tier4-vm image ships a helper at
`/usr/local/bin/qdistro-fake-lid-close` that emits the
`org.freedesktop.login1.Manager.PrepareForSleep` signal logind
publishes for lid-close events — same code path inside qdwin.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }

# Skip if the C-side lid-close wiring isn't there yet.
if ! "$QDWIN_VM_EXEC" "$VMNAME" \
    'test -x /usr/local/bin/qdistro-fake-lid-close' >/dev/null 2>&1; then
    echo "SKIP: qdistro-fake-lid-close not installed in guest"
    echo "      add to qdistro/tier4-vm/build-guest-image.sh once"
    echo "      qdwin's qdwin_locker_v1.lock_requested(reason=1) wiring lands"
    exit 77   # bats convention for skip
fi

# Drain stale locked state.
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*)
        "$QDWIN_VM_EXEC" "$VMNAME" \
          'runuser -u admin -- systemctl --user restart qdlocker.service; sleep 2' \
          >/dev/null
        ;;
esac
```

## Steps

### Step 1 — baseline unlocked

```bash
qdlocker_ctrl status
```

**Assert (1.1):** `locked=False`.

### Step 2 — fake the lid-close signal

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '/usr/local/bin/qdistro-fake-lid-close'
qdlocker_wait_for_lock 5
qdlocker_ctrl status
qdwin_screenshot /tmp/qdlocker-04-step2-locked.png
```

**Assert (2.1):** `locked=True`.
**Assert (2.2):** screenshot shows the qdlocker UI.
**Assert (2.3):** the journal line
`qdwin: lock_requested reason=lid_close` appears in the user journal
between Setup and now:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'journalctl --user -u qdwin.service --since "1 minute ago" | grep "lock_requested reason=lid_close"'
```

The journal assertion is the *primary* one — it proves the path
was via `lock_requested`, not, e.g., qdlocker's own idle timer
firing coincidentally during the test window.

## Cleanup

```bash
# Lid-close doesn't auto-unlock — the user has to authenticate.
# For test cleanup we just restart the service.
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- systemctl --user restart qdlocker.service'
```

## Known-broken-if

- Step 2 reports `locked=True` but the journal grep is empty — the
  lock happened via idle, not lid. Re-check that
  qdistro-fake-lid-close actually published the dbus signal and that
  qdwin's logind subscription is active.
- Step 2 reports `locked=False` after 5s — either qdwin didn't see
  the signal, or qdwin saw it but never sent
  `qdwin_locker_v1.lock_requested(reason=1)`. Check
  `journalctl --user -u qdwin.service` for `lock_requested` lines —
  if the shell-side `qdwin_shell_v1.idle_lock_hint(reason=1)` is
  there but the locker-side isn't, the fan-out is missing in
  `qdwin.c` (see `qdwin/doc/locker.md §5`).
