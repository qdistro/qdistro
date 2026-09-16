# 02 — fprintd verifies in parallel with the password field

<!-- qci:visual: none -->

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

# Widen the fprintd verify window for this lane. The product arms a single,
# bounded one-shot fprintd VerifyStart per lock event (default fprintd_timeout_s
# = 10s, auth.py); on timeout it calls off_verify_status and stops listening. The
# agent-driven cadence between Step 1's lock and Step 2's EmitMatch (a
# wait_for_lock + a ctrl-socket round-trip + a separate vm-exec) reliably exceeds
# 10s, so the match would land after the listener is torn down -> deterministic
# false FAIL. Install a root-owned (trusted: uid != the service uid, 0644, parent
# chain 0755 root:root — app.py:_system_config_is_trusted) locker.conf that pushes
# the window to 120s so EmitMatch always lands inside a live verify, then restart
# the locker so it reloads config at startup.
"$QDWIN_VM_EXEC" "$VMNAME" 'set -e
    install -d -m 0755 -o 0 -g 0 /etc/qdistro
    cat >/etc/qdistro/locker.conf <<EOF
fprintd_timeout_s = 120
EOF
    chown 0:0 /etc/qdistro/locker.conf
    chmod 0644 /etc/qdistro/locker.conf
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        systemctl --user restart qdlocker.service
    sleep 2' >/dev/null

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

- Step 2 hangs and times out — typically the EmitMatch landed AFTER the
  bounded per-lock verify window closed (off_verify_status removed the
  listener). Setup widens `fprintd_timeout_s` to 120s to prevent this; if it
  still times out, check that locker.conf was accepted (root-owned + trusted)
  and that the restart actually reloaded it. (Note: the handler registration
  order is already correct — `dev.on_verify_status(...)` runs before
  `VerifyStart` in auth.py; do NOT chase a registration-order bug here.)
- Step 2 reports `last=failed` — the locker bailed on a non-match
  signal that arrived first. fprintd legitimately emits
  `verify-no-match` for failed attempts; the auth handler must wait
  for `verify-match` OR a `done=true` end-of-verification signal,
  not stop on the first event.
