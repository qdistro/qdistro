# 21 — window-manager policy: direct-compositor functional proof (v25)

<!-- qci:visual: none -->

**What**: drive the v25 window-manager-policy surface on the live qdwin DRM
session directly, independent of qdshell — `set_wm_policy` (focus model /
placement / snap), `request_tile` (left/right/restore), `request_fullscreen`
(fill/restore), and the v19 `register_hotkey` path the WM shortcuts ride on.
This is the direct-compositor sibling of the qdshell-driven capability gate in
`19-wm-policy.md`: it proves the compositor *enacts* the policy on a real
client, where 19 proves the qdshell side *exposes* the capability.

**Why**: the v25 contract has two halves — qdshell flips the WindowManager tab
to live-apply (covered by `19-wm-policy.md`) and the compositor actually
resizes the real client on a tile (not just chrome), fills the output on
fullscreen, and holds the registered hotkeys. This scenario asserts the second
half against `qdwin-bystander` so the proof does not depend on qdshell's init
order. All asserts are deterministic journal reads (no vision required).

## Environment

Standard qdwin GUI harness (`tests/gui/AGENTS.md`): a running libvirt domain on
`qemu:///session` with `qdwin-compositor.service` and `qdshell.service` active.
This scenario temporarily takes the shell role with `qdwin-bystander` (the
200-line C client built from `test-client/qdwin-bystander.c`, present on the
baked image), drives it over its FIFO, then restores `qdshell.service`. Use the
shared bystander helpers in `tests/apps/qdwin-apps-helpers.sh` (the canonical,
self-healing take-over path — do NOT hand-roll the `systemctl stop` + bystander
launch). Do NOT build or deploy anything in-VM.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_apps_set_vm "$VMNAME"
qdwin_session_healthy || { echo "ERROR: qdwin/qdshell user session not up"; exit 1; }

# PRECONDITION (infra): the bystander driver must be installed on the VM.
# Absence is an ERROR (the scenario cannot be exercised), NOT a product FAIL.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdwin-bystander >/dev/null' \
    || { echo "ERROR: qdwin-bystander not installed on VM (cannot drive compositor)"; exit 1; }
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdistro-test-window >/dev/null' \
    || { echo "ERROR: qdistro-test-window not installed on VM"; exit 1; }

# Take over the shell role: cleanly stop qdshell.service (Restart= is
# suppressed for an explicit stop), evict stray qs, launch the bystander with
# the canonical FIFO at /run/user/1000/qdwin-cmd.fifo, wait for the FIFO.
qdwin_apps_become_shell || { echo "ERROR: could not take over shell role with bystander"; exit 1; }
# Arm the restore trap IMMEDIATELY after a successful takeover (before the
# session-health check) so a later failure never leaves the desktop headless.
trap 'qdwin_apps_restore_shell' EXIT
qdwin_apps_session_up || { echo "ERROR: bystander session not healthy"; exit 1; }
```

## Step 1 — spawn the target toplevel, capture its real handle

The FIFO commands take the **literal compositor handle** (`request_tile(handle)`
etc.), and the compositor's handle counter persists across the qdshell→bystander
swap — so do NOT assume handle 1. Capture the actual handle the bystander
reports on `toplevel_added`.

```bash
: > /tmp/qd21-prelaunch.marker   # (host-side only; the real read is the VM log)
qdwin_apps_launch qd21-target \
    "qdistro-test-window --title qd21-target --width 400 --height 260 --color 0xff304050"
# Poll /tmp/bystander.log for the toplevel_added line and extract its handle.
HANDLE=
for _ in $(seq 1 30); do
    HANDLE=$("$QDWIN_VM_EXEC" "$VMNAME" \
      "grep -E 'qdwin-bystander: toplevel_added handle=[0-9]+ .*app_id=\"qdistro-test-window\"' \
         /tmp/bystander.log 2>/dev/null | tail -1 | sed -nE 's/.*handle=([0-9]+).*/\1/p'")
    [ -n "$HANDLE" ] && break
    sleep 0.3
done
echo "target handle=$HANDLE"
[ -n "$HANDLE" ] || { echo "ERROR: bystander never saw the test window (precondition)"; exit 1; }
```

**Assert (1.0):** `$HANDLE` is a non-empty integer (the bystander observed the
client we launched). If none appears, record ERROR (precondition), not FAIL.

## Step 2 — drive the v25 policy/tile/fullscreen/hotkey sequence

Record a journal cursor first, then push the FIFO command sequence (one per
line) via `qdwin_apps_ctl`:

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")

qdwin_apps_ctl wmpolicy 1 250 1 0 2 1 24   # focus=follow-mouse, placement, snap 24px (global; not a handle)
sleep 0.5
qdwin_apps_ctl tile "$HANDLE" left         # left half
sleep 0.5
qdwin_apps_ctl tile "$HANDLE" none         # restore
sleep 0.5
qdwin_apps_ctl tile "$HANDLE" right        # right half
sleep 0.5
qdwin_apps_ctl fullscreen "$HANDLE" 1      # fill output
sleep 0.5
qdwin_apps_ctl fullscreen "$HANDLE" 0      # restore
sleep 0.5
qdwin_apps_ctl hotkey 7101 2 62            # Alt+F4 (hotkey id is not a handle)
sleep 1

LOGS=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager 2>/dev/null")
printf '%s\n' "$LOGS" | grep -E 'qdwin: (set_wm_policy|tile|set_fullscreen|register_hotkey)'
```

> Note: `wmpolicy` is the FIFO command name; its first argument is the
> focus-policy value (`1` = follow-mouse), NOT a handle. The compositor logs it
> as `set_wm_policy focus=1 …`. `tile`/`fullscreen` take `$HANDLE` first; the
> `hotkey` id `7101` is a registration id, not a handle.

**Assert** (all in the compositor journal since `$CURSOR`; `H` = `$HANDLE`):

- **(2.1)** `qdwin: set_wm_policy focus=1 ffm_delay=250 … placement=2 snap=1 dist=24`.
- **(2.2)** `qdwin: tile handle=H edge=left outer=960x1080 at (0,0)` — the
  client geometry moves to `(0,0)` and **resizes** (not just chrome), proving
  `apply_inset → set_size` reaches the client.
- **(2.3)** `qdwin: tile handle=H restored …@(<float-x>,<float-y>)` (pre-tile geometry).
- **(2.4)** `qdwin: tile handle=H edge=right outer=960x1080 at (960,0)`.
- **(2.5)** `qdwin: set_fullscreen handle=H fs=1 outer=1920x1080 at (0,0)` then
  `fs=0 restored=…`.
- **(2.6)** `qdwin: register_hotkey id=7101 mods=0x2 key=62`.
- **(2.7)** No `qdwin_shell_v1: … protocol error` between `$CURSOR` and now
  (the bystander bound the shell role cleanly at >= v25).

(Outer dimensions assume a 1920×1080 DRM output; if the VM output differs,
assert the left tile at `(0,0)`, the right tile at half-width, and fullscreen
filling the full output, rather than the exact 960/1920 pixels.)

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; true' >/dev/null
qdwin_apps_restore_shell   # also fires on the EXIT trap; idempotent
```

## Pass criteria

2.1–2.6 mandatory (the policy/tile/fullscreen/hotkey requests all reached the
compositor and the tiled client resized). 2.7 mandatory (clean bind). Write
`status.txt` PASS once they hold and STOP.

## Known-broken-if

- 2.2 logs the tile but the client doesn't resize (`outer` moved, client
  geometry unchanged): `apply_inset → set_size` didn't reach the client — a
  real compositor defect (mirror of `tests/host/13-wm-policy.md`).
- 2.7 protocol error / bystander crash 255: the shell role was still held by
  qdshell when the bystander tried to bind — `qdwin_apps_become_shell` didn't
  fully stop `qdshell.service`. Re-check `systemctl --user is-active
  qdshell.service` is `inactive` before the bystander launch.
- The desktop is left headless after the run: the EXIT trap /
  `qdwin_apps_restore_shell` didn't restart `qdshell.service`. Restart it
  manually before reporting.

**Validated 2026-05-29** on `qdistro-daily-2026-05-29` (1920×1080 DRM, then
under the original combined `19-wm-policy.md` Path B): all of the above logged
exactly; the terminal client resized to (0,0) / (960,0) / full 1920×1080 and
restored across the sequence. Split into its own scenario 2026-06-28 so the
qdshell-driven gate (19) and this direct-compositor proof each fit the agent
budget.
