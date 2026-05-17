# 09 — TUI broker-offline banner + recovery

**What**: start the TUI while the broker is up, kill the broker,
verify the TUI shows a sticky `⚠ BROKER OFFLINE` banner in the
subtitle (replacing the normal `scope: Just this once` chunk).
Restart the broker and verify a recovery notification appears and
the subtitle returns to normal.

**Why**: transient toasts fade after their timeout, leaving a
deceptively-healthy chrome. The sticky subtitle override drives
the point home even after the toast is gone. This scenario is the
acceptance test for that UX affordance (per the review fixes in
commit `45d3149`).

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
```

## Steps

### S1 — launch TUI on a healthy broker, baseline subtitle

```bash
# Repo-supplied launcher; see scenarios 01/02 for D-Bus-session env reason.
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-tui'
sleep 3
$VMGUI "$VM" screenshot /tmp/09-s1-healthy.png
```

**Assert:**
- Header subtitle reads `(no pending requests) • scope: Just this once`.
- No `⚠` warning glyph in the subtitle.
- Right pane shows `(no request selected)`.

### S2 — kill the broker, verify sticky offline banner

```bash
# Stop (don't restart) — we want the broker down for a measurable
# interval so the subtitle override has time to render.
$VMEXEC "$VM" 'systemctl stop qdistro-admin-broker.service'
# TUI's safety poll runs every POLL_INTERVAL_S (30s); the broker
# also drops the D-Bus name, which triggers NameOwnerChanged →
# dbus-python may eventually surface errors. Wait well past the
# poll interval to get a stable offline state.
sleep 35
$VMGUI "$VM" screenshot /tmp/09-s2-offline.png
```

**Assert:**
- Screenshot header subtitle is replaced by `⚠ BROKER OFFLINE —
 press r to retry (<error detail>)` — the exact error detail
 after the parenthesis varies (ConnectionError / ServiceUnknown /
 similar) but the `BROKER OFFLINE` + `press r to retry` phrasing
 must be present.
- Left pane table is empty (TUI clears it on broker error to
 avoid mis-targeting a subsequent decide against a stale row).
- Right pane shows `(no request selected)`.

### S3 — restart broker, verify recovery

```bash
$VMEXEC "$VM" 'systemctl start qdistro-admin-broker.service'
sleep 2
# Ask the TUI to refresh now so we don't wait another poll cycle.
# `vm-gui key r` sometimes works against a focused qterminal but
# not reliably under labwc — virsh send-key is the portable path.
virsh send-key "$VM" --codeset linux KEY_R
sleep 1
$VMGUI "$VM" screenshot /tmp/09-s3-recovered.png
```

**Assert:**
- Subtitle is back to `(no pending requests) • scope: Just this once`.
- No `BROKER OFFLINE` / `⚠` banner.
- A green info toast `broker connection restored` may be visible
 in the lower-right (shown for ~4s after recovery). Presence is a
 bonus assertion; absence is fine if the screenshot landed after
 the toast faded.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'rm -f /tmp/qterminal-tui.log'
```

## Notes for the runner

- The 35s wait in S2 is deliberate — the TUI's POLL_INTERVAL_S is
 30s, and the first refresh after the broker stop is what turns
 the offline state sticky. Shorter waits may catch the app
 mid-error-handling and give flaky results.
- If S2's screenshot still shows the normal subtitle, the broker
 didn't actually go down or the TUI lost its subscription and
 never noticed — check `systemctl is-active qdistro-admin-broker`
 and `pgrep qdistro_admin_tui` before calling FAIL.
- `vm-gui key r` targets the focused qterminal. If the terminal
 lost focus (another window came up), click into the terminal
 content area once before sending `r`.
