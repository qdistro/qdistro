# 04 — logind suspend/lid engages the locker

<!-- qci:visual: required -->

**Acceptance criterion:** a systemd-logind D-Bus signal reaches
qdlocker's own `LogindWatcher` (in `qdlocker/logind.py`) → the watcher
injects `lock_requested` into the locker → qdlocker engages the lock.

> **Note on what this scenario actually exercises.** qdlocker does
> NOT go through qdwin for lid/suspend. `LogindWatcher` subscribes
> directly, over the system bus, to two logind signals:
>
> - `org.freedesktop.login1.Session.Lock` on the per-session object —
>   emitted when `HandleLidSwitch=lock` is set in a
>   `/etc/systemd/logind.conf.d/` drop-in (a true lid close). This is
>   `reason=1` (lid) in `logind.py`.
> - `org.freedesktop.login1.Manager.PrepareForSleep(start=true)` —
>   emitted just before the system suspends, so the locker engages
>   BEFORE suspend. This is `reason=2` (suspend) in `logind.py`.
>
> The fake helper below emits **`PrepareForSleep`**, so this scenario
> validates the **suspend pre-lock path (reason=2)**, not a true lid
> close. The two signals are distinct in `logind.py`; see "Testing a
> true lid close" at the bottom for the `Session.Lock`/reason=1
> variant. There is no qdwin C-side `qdwin_locker_v1.lock_requested`
> wiring on this path — that protocol carries only the manual hotkey
> (reason=3) and the compositor-side lock state.

VMs don't have a real laptop lid. The qdistro tier4-vm image ships a
helper at `/usr/local/bin/qdistro-fake-lid-close` that emits the
`org.freedesktop.login1.Manager.PrepareForSleep` signal on the system
bus — the same signal logind raises on suspend — which drives
qdlocker's `LogindWatcher.PrepareForSleep` subscription (reason=2).

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }

# Skip if the fake-suspend helper isn't provisioned in the guest.
if ! "$QDWIN_VM_EXEC" "$VMNAME" \
    'test -x /usr/local/bin/qdistro-fake-lid-close' >/dev/null 2>&1; then
    echo "SKIP: qdistro-fake-lid-close not installed in guest"
    echo "      (ship it from qdistro/tier4-vm/build-guest-image.sh)"
    exit 77   # bats convention for skip
fi

# Drain stale locked state.
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*)
        "$QDWIN_VM_EXEC" "$VMNAME" \
          'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2' \
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

### Step 2 — fake the PrepareForSleep signal

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '/usr/local/bin/qdistro-fake-lid-close'
qdlocker_wait_for_lock 5
qdlocker_ctrl status
qdwin_screenshot /tmp/qdlocker-04-step2-locked.png
```

**Assert (2.1):** `locked=True`.
**Assert (2.2):** screenshot shows the qdlocker UI.
**Assert (2.3):** qdlocker's own journal records that the lock came
from the logind subscription, not the idle timer. `LogindWatcher`
logs `logind PrepareForSleep(start=True) -> reason=suspend`, and the
bridge then logs `lock_requested reason=suspend`:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'journalctl --user -u qdlocker.service --since "1 minute ago" | grep -E "reason=suspend|PrepareForSleep"'
```

The journal assertion is the *primary* one — it proves the path was
via the logind subscription, not, e.g., qdlocker's own idle timer
firing coincidentally during the test window (which would log
`reason=idle`).

## Cleanup

```bash
# Suspend/lid lock doesn't auto-unlock — the user has to authenticate.
# For test cleanup we just restart the service.
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service'
```

## Testing a true lid close (reason=1)

To exercise the actual lid path instead of suspend, the watcher must
receive `org.freedesktop.login1.Session.Lock` on the session object
rather than `PrepareForSleep`. With `HandleLidSwitch=lock` in a
`/etc/systemd/logind.conf.d/` drop-in installed in the guest, logind
raises this on lid close; the watcher logs
`logind Session.Lock received -> reason=lid` and the bridge logs
`lock_requested reason=lid`. A guest-side helper can drive logind to
raise it — e.g. calling the session object's `Lock` method, which
makes logind emit the `Session.Lock` signal the watcher listens for:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'SESSION=$(loginctl --no-legend list-sessions | awk "NR==1{print \$1}"); \
   busctl --system call org.freedesktop.login1 \
     "/org/freedesktop/login1/session/_3${SESSION}" \
     org.freedesktop.login1.Session Lock' 2>/dev/null || true
```

(The session-object path encoding varies; prefer a maintained fake
helper if the image ships one.) Then assert
`journalctl --user -u qdlocker.service ... | grep "reason=lid"`.

## Known-broken-if

- Step 2 reports `locked=True` but the journal grep is empty — the
  lock happened via idle, not the logind subscription. Re-check that
  qdistro-fake-lid-close actually published the `PrepareForSleep`
  signal and that `LogindWatcher` logged `logind watcher ready` at
  startup (`journalctl --user -u qdlocker.service`).
- Step 2 reports `locked=False` after 5s — either the fake helper
  didn't emit the signal, or `LogindWatcher` never connected. Check
  `journalctl --user -u qdlocker.service` for `logind` lines: a
  `dbus-next not installed` or `could not connect to system bus`
  warning means the subscription is degraded and lid/suspend lock is
  unavailable while the rest of the locker still works.
- Lock engages but logs `reason=lid` when you expected
  `reason=suspend` (or vice versa) — the fake helper is emitting the
  wrong signal. `Session.Lock` is reason=1 (lid); `PrepareForSleep`
  is reason=2 (suspend). See `qdlocker/logind.py`.
