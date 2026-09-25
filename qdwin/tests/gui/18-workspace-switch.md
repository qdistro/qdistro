# 18 — workspaces: switch hides/shows windows; count change (v24)

<!-- qci:visual: required -->

**Acceptance criterion:** with real qdwin workspaces (v24, ext-workspace-v1
+ the qdshell `workspace` IPC), a window spawned on the active workspace
disappears when the user switches to another workspace and reappears on
switching back; a window spawned while on workspace 2 is visible there and
hidden on workspace 1. Also: changing the workspace count from qdshell
settings reconciles the compositor's workspace count. This is the
user-facing behaviour the settings UI promised
(`todo/issues/qdshell/qdshell-workspaces-and-appearance-settings.md`);
before v24 `Qdwin.switchToWorkspace()` was a no-op.

## Prerequisites

- A qdwin + qdshell session (same as `16`/`17`).
- The qdshell `workspace` IpcHandler (`Services/Control/IPCService.qml`),
  reachable via:

  ```bash
  qs -p /usr/share/quickshell/qdshell ipc --any-display call workspace list
  # → "count=4 active=0 occupied=..."
  ```

  Fail loudly if `qs ipc list` does not show a `workspace` target; do not
  skip.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

QS="qs -p /usr/share/quickshell/qdshell ipc --any-display"
qsipc() { "$QDWIN_VM_EXEC" "$VMNAME" \
    "runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 $QS $*'"; }

qsipc list 2>&1 | grep -q workspace \
    || { echo "FAIL: qdshell 'workspace' IPC target not reachable"; exit 1; }

# PRECONDITION (infra): the workspace windows are spawned via
# qdistro-test-window (the reliable test client the qdwin smokes use),
# NOT foot — foot is not installed on the VM and its run only ever
# produced a tty login console, never a visible toplevel, which is why
# `occupied=` stayed empty. Absence of the client is an ERROR (the
# scenario cannot be exercised), NOT a workspace FAIL.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdistro-test-window >/dev/null' \
    || { echo "ERROR: qdistro-test-window not installed on VM (cannot spawn workspace windows)"; exit 1; }

# Spawn a titled qdistro-test-window and hard-gate on its toplevel_added.
# Echoes the resolved handle on stdout. On timeout it prints nothing and
# returns 1, so the caller can report ERROR (precondition), not FAIL.
ws_spawn_gated() {
    local title=$1 color=${2:-0xff304050}
    local cur
    cur=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
      --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title '$title' \
         --width 360 --height 220 --color $color >/tmp/$title.log 2>&1"
    local h
    for _ in $(seq 1 30); do
        h=$("$QDWIN_VM_EXEC" "$VMNAME" \
          "journalctl _UID=1000 _SYSTEMD_USER_UNIT=qdwin-compositor.service --after-cursor='$cur' --no-pager | \
           grep -E 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdistro-test-window' | \
           tail -1 | sed -nE 's/.*handle=([0-9]+).*/\1/p'")
        [ -n "$h" ] && { echo "$h"; return 0; }
        sleep 0.2
    done
    return 1
}

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; sleep 1' >/dev/null
# Start on workspace 0.
qsipc call workspace activate 0 >/dev/null
sleep 0.5
```

## Steps

### Step 1 — spawn WIN-A on workspace 0

The window's title is `ws-zero` so it can be identified visually in
the chrome titlebar. We HARD-GATE on its `toplevel_added` before any
visual assertion — if the toplevel never appears, this is an ERROR
(precondition / infra), not a workspace FAIL.

```bash
HANDLE_A=$(ws_spawn_gated ws-zero 0xff304050) \
    || { echo "ERROR: WIN-A toplevel never appeared (precondition)"; exit 1; }
echo "WIN-A handle=$HANDLE_A"
sleep 1
qdwin_screenshot /tmp/18-step1-ws0.png
```

**Assert (1.1):** screenshot shows the spawned test window on the
desktop. If title chrome is not rendered or OCR is unavailable, use the
gated `toplevel_added` handle and the visible client surface as evidence;
do not fail solely because the title text `ws-zero` is unreadable.
**Assert (1.2):** `qsipc call workspace list` reports `active=0` and
`occupied` includes `0`.

### Step 2 — switch to workspace 1: WIN-A must vanish

```bash
qsipc call workspace activate 1 >/dev/null
sleep 1
qdwin_screenshot /tmp/18-step2-ws1-empty.png
```

**Assert (2.1):** `qsipc call workspace list` reports `active=1`.
**Assert (2.2):** screenshot shows an **empty** desktop — WIN-A's
`ws-zero` titlebar is NOT visible (the window was parked on the
hidden workspace layer). HARD — this is the core hide behaviour.
**Assert (2.3):** the journal shows `qdwin: active_workspace=1/` after the
switch.

### Step 3 — spawn WIN-B on workspace 1

Same hard gate on `toplevel_added`.

```bash
HANDLE_B=$(ws_spawn_gated ws-one 0xff405060) \
    || { echo "ERROR: WIN-B toplevel never appeared (precondition)"; exit 1; }
echo "WIN-B handle=$HANDLE_B"
sleep 1
qdwin_screenshot /tmp/18-step3-ws1-winb.png
```

**Assert (3.1):** screenshot shows a test window visible + focused on
workspace 1. If title chrome is not rendered or OCR is unavailable, use
the gated WIN-B handle and visible client surface as evidence.
**Assert (3.2):** WIN-A is not visible on workspace 1. Prefer title text
when readable, but do not fail solely on missing chrome/OCR.

### Step 4 — switch back to workspace 0: WIN-A returns, WIN-B hides

```bash
qsipc call workspace activate 0 >/dev/null
sleep 1
qdwin_screenshot /tmp/18-step4-back-ws0.png
```

**Assert (4.1):** screenshot shows WIN-A's client surface again
(restored from the hidden layer). Prefer title text when readable, but
the visible client surface plus `active=0` is sufficient when chrome/OCR
is unavailable.
**Assert (4.2):** WIN-B is not visible on workspace 0. Prefer title text
when readable, but do not fail solely on missing chrome/OCR.
**Assert (4.3):** `qsipc call workspace list` reports `active=0`.

### Step 5 — `next`/`prev` cycle wraps

```bash
qsipc call workspace next >/dev/null; sleep 0.5
A1=$(qsipc call workspace active)
qsipc call workspace prev >/dev/null; sleep 0.5
A2=$(qsipc call workspace active)
```

**Assert (5.1):** `A1 == 1` (next from 0).
**Assert (5.2):** `A2 == 0` (prev wraps back).

### Step 6 — settings count reconciles to the compositor

```bash
# Raise the workspace count via the settings model. The exact IPC/QML
# path may be `qs ipc call settings ...` then editing workspaces.count,
# or driving Settings directly — the agent introspects with `qs ipc list`.
# Goal: set workspaces.count = 6 and confirm the compositor follows.
qsipc call settings openTab appearance >/dev/null 2>&1 || true
# (agent: set the workspace-count spinner to 6 in the Appearance tab, or
#  poke Settings.data.workspaces.count directly if a test hook exists)
sleep 1
qsipc call workspace count
```

**Assert (6.1):** after raising the setting to 6, `qsipc call workspace
count` reports `6` — qdshell pushed the new count down via
`setWorkspaceCount` → ext-workspace `create_workspace`. SOFT (depends on
the agent being able to drive the spinner; if not reachable, record SKIP
for Step 6 only, not the whole scenario).

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; true' >/dev/null
qsipc call workspace activate 0 >/dev/null 2>&1 || true
```

## Pass criteria

Steps 1–5 mandatory (the hide/show + active-index + cycle behaviour).
Step 6 soft (count reconciliation; SKIP allowed if the spinner can't be
driven headlessly).

## Known-broken-if

- 2.2 still shows WIN-A → the switch didn't hide the window. Check
  `qdwin_set_active_workspace` → `qdwin_toplevel_apply_workspace_visibility`
  moves off-workspace views to `workspace_hidden_layer`.
- 2.1 `active=1` but 2.2 shows WIN-A → the active index advanced but the
  view wasn't re-parked; suspect the layer move or a repaint not scheduled.
- 4.1 WIN-A doesn't return → restore path broken (apply_visibility didn't
  move it back to `normal_layer`), or focus recovery picked nothing.
- `workspace list` not found in `qs ipc list` → the IpcHandler didn't
  register; check `Services/Control/IPCService.qml` is loaded and the
  `target: "workspace"` handler compiles.
```
