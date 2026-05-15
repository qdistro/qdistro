# 02 — fprintd verifies in parallel with the password field

**Acceptance criterion:** while the lock UI is up, the locker has an
fprintd `VerifyStart` in flight; a fingerprint match unlocks even
when the password field is empty. Confirms the spec's "fingerprint =
the owner is present" path (sessions.md:13-14, 56-72).

This test requires the guest image to carry a faked fprintd that
emits a `VerifyStatus("verify-match", true)` signal on demand. The
qdistro tier4-vm image bakes one in at `/usr/libexec/qdistro-fprintd-fake`
(see qdistro/tier4-vm/build-guest-image.sh).

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

# Ensure the fake fprintd is the active provider on the system bus.
"$QDWIN_VM_EXEC" "$VMNAME" \
    'systemctl restart qdistro-fprintd-fake.service; sleep 1' >/dev/null

qdlocker_session_healthy || { echo "FAIL: locker session not up"; exit 1; }
```

## Steps

### Step 1 — engage the locker

```bash
qdwin_chord ctrl alt -- l
qdlocker_wait_for_lock 5
qdlocker_ctrl status
```

**Assert (1.1):** `locked=True prompt-len=0`.

### Step 2 — trigger a fingerprint match (no password typed)

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
    'busctl --system call \
        net.reactivated.Fprint \
        /net/reactivated/Fprint/Device/0 \
        qdistro.FprintFake \
        EmitMatch' >/dev/null
qdlocker_wait_for_unlock 5
qdlocker_ctrl status
```

**Assert (2.1):** `last=success` despite `prompt-len=0`.
**Assert (2.2):** qdwin ctrl reports the lock surface was destroyed.

## Pass criteria

The fingerprint path completed without the user touching the
keyboard, confirming the parallel fprintd D-Bus subscription in
auth.py:`_fprint_async`.

## Known-broken-if

- Step 2 hangs and times out — the locker isn't subscribed to
  `VerifyStatus` (auth.py registered the handler after the call to
  `VerifyStart` returned). Fix: `dev.on_verify_status(...)` must
  happen before `VerifyStart`.
- Step 2 reports `last=failed` — the locker bailed on a non-match
  signal that arrived first. fprintd legitimately emits
  `verify-no-match` for failed attempts; the auth handler must wait
  for `verify-match` OR a `done=true` end-of-verification signal,
  not stop on the first event.
