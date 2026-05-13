# 02 — TUI scope picker keys shift the active scope chip

**What**: with one pending request in view, press digit keys `1`..`3`
and verify the right pane's `Scope:` label and the header subtitle's
scope chunk both update. The footer's digit chips are Textual
binding metadata and do **not** change with selection — that's by
design; don't assert on them.

**Why**: the scope label is what the admin actually reads before
deciding. A regression where keypresses stop updating `_scope` would
silently apply the default "once" scope even though the admin
pressed `2` or `3`. Right-pane label + subtitle warning are the two
places the active scope surfaces visibly.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-0052}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'systemctl is-active qdistro-admin-broker.service'
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; pkill -u work -f qdistro-test-permission 2>/dev/null; true'
```

## Steps

### S1 — launch TUI, inject one pending request

```bash
# Repo-supplied launcher sets WAYLAND_DISPLAY / XDG_RUNTIME_DIR /
# DBUS_SESSION_BUS_ADDRESS; a naive `runuser -u admin -- env DISPLAY=:0
# ...` doesn't, and qterminal would crash on D-Bus connect.
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-tui'
sleep 3

B64=$(base64 -w0 <<'EOF'
#!/bin/bash
sudo -u work bash -c 'python3 /usr/local/bin/qdistro-test-permission \
 >/tmp/test-output.txt 2>&1 & echo $! >/tmp/test-pid'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 2

$VMGUI "$VM" screenshot /tmp/02-tui-scope-picker-s1-default.png
```

**Assert (default scope = once):**
- Right pane shows `Scope: Just this once` (label "Just this once"
 rendered bold after the `Scope:` prefix).
- Header subtitle's scope chunk reads `scope: Just this once` with
 **no** warning glyph (no `⚠`) — default scope is not warned about.

### S2 — press `2` (1h)

```bash
virsh send-key "$VM" --codeset linux KEY_2 # see AGENTS.md : vm-gui key no-ops on labwc XWayland
sleep 1
$VMGUI "$VM" screenshot /tmp/02-tui-scope-picker-s2-1h.png
```

**Assert:**
- Right pane: `Scope: 1 hour` (bold "1 hour").
- Header subtitle's scope chunk includes a `⚠` warning glyph and
 reads `⚠ scope: 1 hour` (any non-`once` scope must show the
 warning).

### S3 — press `3` (24h)

```bash
virsh send-key "$VM" --codeset linux KEY_3
sleep 1
$VMGUI "$VM" screenshot /tmp/02-tui-scope-picker-s3-24h.png
```

**Assert:**
- Right pane: `Scope: 24 hours` (bold "24 hours").
- Header subtitle: `⚠ scope: 24 hours`.

### S4 — press `1` (back to once)

```bash
virsh send-key "$VM" --codeset linux KEY_1
sleep 1
$VMGUI "$VM" screenshot /tmp/02-tui-scope-picker-s4-once.png
```

**Assert:**
- Right pane: `Scope: Just this once`.
- Header subtitle: `scope: Just this once` with the `⚠` warning
 gone — pressing `1` returns to the default, non-warned state.

## Teardown

```bash
virsh send-key "$VM" --codeset linux KEY_D # deny the request so it doesn't linger
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qdistro-test-permission 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/test-pid /tmp/test-output.txt /tmp/qterminal-tui.log'
```

## Notes for the runner

- The Textual `Footer()` widget renders binding metadata only. All
 `1`/`2`/`3`/`^p` chips always render identically regardless of the
 current scope — that is intentional. **Do not** assert on footer
 chip color or highlight.
- qterminal's default geometry is pinned to 1200×700 so the subtitle
 scope chunk is fully visible (see AGENTS.md ). If a screenshot
 still shows a truncated subtitle, treat it as environment
 breakage and return ERROR, not SKIP.
