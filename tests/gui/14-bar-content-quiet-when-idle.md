# 14 — bar-content does not remap-storm when idle

<!-- qci:visual: none -->

**Acceptance criterion:** in the absence of user input and window
state changes, the journal grows by ≤2 `qdshell-bar-content-Virtual-1`
entries over 10 seconds. Before the fix in
`todo/qdshell-bar-remap-storm.md`, `qdwin: layer-shell mapped` fired
on every layer-surface commit (~60 Hz, vsync-driven), drowning real
signal in ~600 entries per 10 s of idle.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[f]oot|[w]eston-terminal|[q]distro-test-window" 2>/dev/null; sleep 1' >/dev/null
sleep 2  # let session settle before sampling
```

## Steps

### Step 1 — sample bar-content log lines over 10 s of idle

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
sleep 10
N=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager 2>/dev/null \
   | grep -c 'qdshell-bar-content-Virtual-1'")
echo "bar-content lines in 10 s idle: $N"
```

**Assert (1.1):** `$N -le 2`. A storm would produce ~200 (at 20 Hz)
or ~600 (at 60 Hz). One or two stray entries on an unusually noisy
settle are acceptable.

### Step 2 — perform a benign action and confirm no storm bleeds through

A window cycle (open + close) legitimately triggers a few bar
operations as state propagates. After the cycle, the bar should
settle back to quiet.

```bash
"$QDWIN_VM_EXEC" "$VMNAME" '
  if command -v qdistro-test-window >/dev/null 2>&1; then
    setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
      qdistro-test-window --title qd14-cycle --width 320 --height 200 >/tmp/qd14-cycle.log 2>&1
  elif command -v weston-terminal >/dev/null 2>&1; then
    setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
      weston-terminal >/tmp/qd14-cycle.log 2>&1
  else
    echo "ERROR: no qdistro-test-window or weston-terminal available for window cycle"; exit 1
  fi' >/dev/null
sleep 1
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -9 -f "[q]distro-test-window|[w]eston-terminal" 2>/dev/null || true' >/dev/null
sleep 2
CURSOR2=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
sleep 5
N2=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR2' --no-pager 2>/dev/null \
   | grep -c 'qdshell-bar-content-Virtual-1'")
echo "bar-content lines in 5 s idle after window cycle: $N2"
```

**Assert (2.1):** `$N2 -le 2`. The bar must re-settle after the cycle;
a persistent storm post-cycle indicates the QML reactivity loop has
returned or qdwin's `qdwin_layer_surface_apply` is logging on every
commit again.

### Step 3 — qualitative: journal is still useful for diagnostics

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 -n 20 --no-pager | grep '^.*qdwin:'" | head -20
```

**Assert (3.1):** diagnostic only. The load-bearing checks are the
cursor-bounded counts in 1.1 and 2.1. If recent journal output is sparse
or lacks toplevel/focus lines, record it for debugging but do not fail
unless it contradicts the bounded counts (for example, a wall of identical
`layer-shell mapped` lines after the cursor).

## Cleanup

Nothing to clean up; the test only observes.

## Pass criteria

Asserts 1.1 and 2.1 pass; Step 3 is supporting evidence. Confirms the qdwin one-shot map-log fix
holds AND no new QML repaint loop has slipped in.

## Known-broken-if

- $N at step 1 is ≥50 → either the qdwin fix was reverted
  (`weston_log("qdwin: layer-shell mapped ...")` is firing on every
  same-shape commit), or a QML widget in `qdshell/Modules/Bar/` is
  binding a per-frame property (height, implicitHeight) to a
  changing source. Grep `Modules/Bar/Widgets/*.qml` for `Time.`,
  `Date.`, `Network.`, `Battery.charge` bindings on `height`.
- $N is 0 → the bar might be hidden (`displayMode: auto_hide` and
  cursor isn't over it). Verify the bar is visible in a screenshot
  before treating 0 as a pass.
- $N2 in step 2 is high but step 1 was quiet → the spawn/close
  cycle is triggering legitimate panel reflows. If `$N2` is still
  ≤5 that's acceptable (max + unmax of toplevels can reflow the
  exclusive-zone client).
