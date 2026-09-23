# 01 — qdlocker locks, accepts password, unlocks

<!-- qci:visual: required -->

**Acceptance criterion:** Ctrl+Alt+L locks the desktop via the
`qdwin_locker_v1` path (qdlocker is the actor, not qdshell); the user
can type a password; correct password unlocks; lock surface is torn
down; post-unlock typing reaches the previously-focused toplevel.

Equivalent of qdwin/tests/gui/03-locker-cycle.md but driving the new
qdlocker process instead of qdshell's deprecated LockScreen module.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

pgrep -f "http.server 8765" >/dev/null || (
    cd "${QDWIN_REPO}" && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1

qdlocker_session_healthy || { echo "FAIL: session not up"; exit 1; }

# foot is the throwaway toplevel we type into to prove pre/post-unlock
# keystroke routing (asserts 1.1, 4.3, 5.1). It ships only in the
# QDWIN_APP_DEPS=1 app-test lane (fresh-vm-bootstrap.sh), not the default
# golden — SKIP rather than ERROR when it is absent. The foot-free
# security path (overlay_key routing) is also covered by
# 05-keystroke-isolation.md. Mirrors 04-lid-close-lock.md's skip-guard.
if ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v foot >/dev/null 2>&1'; then
    echo "SKIP: foot not installed in guest (QDWIN_APP_DEPS=1 lane only)"
    exit 77   # bats convention for skip
fi

# qdlocker should be running under systemd --user. If a previous test
# left the locker engaged, unstick it before we start. vm-exec runs
# as root by default; bare `systemctl --user` from root has no user
# manager — wrap in runuser so the admin user's manager handles it.
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*)
        "$QDWIN_VM_EXEC" "$VMNAME" \
          'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2' >/dev/null
        ;;
esac

# Spawn a foot directly in the guest user session so the scenario does not
# depend on the removed qdshell.py launcher ctrl commands. systemd-run returns
# after the transient unit is accepted and keeps the app detached from
# vm-exec's guest-agent command channel.
"$QDWIN_VM_EXEC" "$VMNAME" \
  'pkill -u admin -x foot 2>/dev/null; \
   runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user reset-failed qdlocker-test-foot.service 2>/dev/null || true; \
   runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemd-run --user --collect --unit=qdlocker-test-foot \
       --setenv=WAYLAND_DISPLAY=wayland-1 \
       foot --title qdlocker-test' >/dev/null
sleep 1.5
```

## Steps

### Step 1 — pre-lock typing reaches foot

```bash
qdwin_type_lower "echo before"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/qdlocker-01-step1-pre.png
```

**Assert (1.1):** screenshot shows `echo before` and `before` output
in foot.

### Step 2 — Ctrl+Alt+L engages qdlocker (not qdshell)

```bash
qdwin_chord ctrl alt -- l
qdlocker_wait_for_lock 5
qdwin_screenshot /tmp/qdlocker-01-step2-locked.png
qdlocker_ctrl status
```

**Assert (2.1):** qdlocker ctrl reports `locked=True prompt-len=0
pam-ready=True`.
**Assert (2.2):** _Optional — depends on `qdwin_ctrl "locker"` being
implemented in qdshell, which it currently is not._ If implemented,
verify the promoted lock toplevel attribution is `qdlocker_uid` (not
the shell's uid). Until then, skip 2.2 and rely on the qdwin journal
line `qdwin: promoted locker toplevel` as the equivalent proof.
**Assert (2.3):** screenshot shows the qdlocker UI: clock, date,
password field, both rendered with qdshell styling
(NText / NIcon / Color.mSurface palette — visually identical to the
old qdshell-embedded locker).

### Step 3 — type the password (overlay_key forwarded to qdlocker)

```bash
qdlocker_type_password_chars
sleep 0.3
qdwin_screenshot /tmp/qdlocker-01-step3-typed.png
qdlocker_assert_prompt_len 11
```

**Assert (3.1):** qdlocker ctrl reports `prompt-len=11`. This is the
critical security assert — it confirms `qdwin_locker_v1.overlay_key`
is reaching qdlocker. If keystrokes leaked to qdshell instead,
qdshell's prompt-len would advance and qdlocker's would stay 0.
**Assert (3.2):** screenshot shows 11 password dots.

### Step 4 — Enter triggers PAM and unlocks

```bash
qdwin_send_key KEY_ENTER
qdlocker_wait_for_unlock 5
qdwin_screenshot /tmp/qdlocker-01-step4-unlocked.png
qdlocker_ctrl status
```

**Assert (4.1):** qdlocker ctrl reports `last=success`; status
reports `locked=False`.
**Assert (4.2):** verify the lock-surface proxy was destroyed, not just
the flag flipped (B1 regression guard from
qdwin/tests/gui/03-locker-cycle.md:94), using the qdwin journal line `qdwin:
locked_changed=0 cause=locker_set_locked`:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --since \"1 minute ago\" --no-pager"' \
  | grep "locked_changed=0 cause=locker_set_locked"
```
**Assert (4.3):** screenshot shows the foot terminal (no lock UI).

### Step 5 — post-unlock typing reaches foot

```bash
# The lock promotion can bisect the Ctrl+Alt+L chord: QEMU delivered the
# releases while the overlay owned input, so explicitly resynchronize the
# normal seat before proving post-unlock terminal routing.
qdwin_release_modifiers
qdwin_type_lower "echo after"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/qdlocker-01-step5-post.png
```

**Assert (5.1):** screenshot shows BOTH `echo before / before` AND
`echo after / after`.
**Assert (5.2):** qdlocker ctrl still reports `locked=False`.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user stop qdlocker-test-foot.service 2>/dev/null || true; \
   pkill -u admin -x foot 2>/dev/null || true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.2 pass. Confirms:

- The `qdwin_locker_v1` protocol is wired (binding accepted, locker
  Qt toplevel promoted to the LOCK layer, set_locked round-trips).
- Overlay keystrokes route to qdlocker — i.e. the security boundary
  from the spec is real, not just declared in the XML.
- qdlocker's auth path (PAM) accepts a valid password.
- Lock surface lifecycle is symmetric (promote on lock, hide on
  unlock).

## Known-broken-if

- Step 2 PASS at screenshot but FAIL at `qdlocker_ctrl status` —
  qdlocker isn't running. Check `systemctl --user status qdlocker`
  inside the guest.
- Step 3 PASS at screenshot (password dots shown) but qdlocker reports
  `prompt-len=0` — keys are reaching qdshell instead of qdlocker.
  The overlay_key routing in qdwin.c needs to check
  `qdwin->locker_resource != NULL` before falling back to the
  shell path (see qdwin/doc/locker.md §5).
- Step 4 FAIL at `last=success` with `last=failed` for a known-good
  password — PAM service mismatch. Set `QDLOCKER_PAM_SERVICE` in the
  systemd unit's Environment= to the right service file.
- Step 4 PASS at qdlocker but qdwin still reports `attached=yes` —
  the lock-surface destroy is gated on the wrong protocol; verify
  the locker calls `qdwin_lock_surface_v1.destroy` after
  `set_locked(0)`.
