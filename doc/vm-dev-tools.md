# VM development tools

A small set of shell scripts in `scripts/vm/` for driving the development
VM from the host. They let the host run commands inside the guest, take
screenshots, and automate GUI interactions — the foundation of qdistro's
integration testing.

- **`vm-exec`** — run a shell command inside the VM; capture stdout,
 stderr, exit code.
- **`vm-gui`** — GUI automation: screenshot, click, type, activate
 windows, scroll, drag.
- **`vm-start-and-wait`** — start a VM and block until the QEMU guest
 agent is ready.
- **`vm-resize-display`** / **`vm-resize-display-loop`** — auto-resize the
 guest display to match the host viewer size.

## Why these matter

They provide three capabilities critical to the qdistro dev loop:

1. **Host-driven execution** — run any shell command in the VM from the
 host. No SSH setup needed; uses the QEMU guest agent over `virsh`.
2. **GUI automation** — click, type, screenshot, activate windows. Turns
 the VM into a scriptable GUI target.
3. **Boot + readiness** — `vm-start-and-wait` blocks until the guest
 agent is live, so scripts can reliably chain "boot VM → do stuff →
 take screenshot → assert."

Together they're enough for:

- Automated integration tests of the whole qdistro stack from outside the
 VM.
- Visual regression tests — screenshot a dialog, compare to a reference.
- Reproducible demos — script a sequence exercising the permission-
 approval flow end-to-end.
- Machine-readable UI exercise — feed the same primitives to AI agents
 later via the UIModel D-Bus interface.

## Tool summaries

### `vm-exec <vm> <command> [args...]`

Runs a shell command inside the named VM via the QEMU guest agent.
Prints stdout; errors go to stderr. Exit code is the guest command's
exit code.

```bash
vm-exec qdistro-dev whoami
vm-exec qdistro-dev systemctl status qdistro-admin-broker
vm-exec qdistro-dev "python3 -m qterminator --version"
```

Requires `qemu-guest-agent` inside the guest. Both quoted and unquoted
commands work.

### `vm-gui <vm> <subcommand> [args...]`

GUI automation against the guest's session. Subcommands:

| Subcommand | Purpose |
|---------------------------|-----------------------------------------------------------|
| `screenshot [file]` | PNG via `virsh screenshot`. Default `/tmp/vm-screenshot.png`. |
| `start <cmd>` | Launch a GUI app backgrounded. |
| `activate <title>` | Focus window by title wildcard match. |
| `click <x> <y>` | Left-click at coordinates. |
| `rightclick <x> <y>` | Right-click at coordinates. |
| `doubleclick <x> <y>` | Double-click at coordinates. |
| `drag <x1> <y1> <x2> <y2>`| Drag from → to. |
| `scroll <up\|down> [n]` | Mouse-wheel scroll (default 3 clicks). |
| `type <text>` | Type text character-by-character. |
| `key <keyname>...` | Send key events (`Return`, `ctrl+c`, `alt+F4`, ...). |
| `windowsize <title> <w> <h>` | Resize window. |
| `windowmove <title> <x> <y>` | Move window. |
| `wait [secs]` | Sleep. |

Screenshots use `virsh screenshot` directly — doesn't need guest-side
cooperation for framebuffer capture.

### `vm-start-and-wait <vm>`

Starts the VM if not running, then polls the guest agent until it
responds (120 s timeout). Useful as the first line of every test script.

## Integration with the dev cycle

Development happens in a virt-manager VM. These scripts turn the host
into a test harness for the guest:

```bash
# typical integration test
vm-start-and-wait qdistro-dev

vm-exec qdistro-dev "systemctl --user restart qdistro-admin-broker"
vm-gui qdistro-dev start "python3 -m qdistro.permission_test"

vm-gui qdistro-dev screenshot /tmp/before.png
vm-gui qdistro-dev activate 'qdistro — admin approvals'
vm-gui qdistro-dev click 450 320 # Approve button
vm-gui qdistro-dev screenshot /tmp/after.png

vm-exec qdistro-dev cat /tmp/permission_test.log
```

This complements the [dev](dev.md) testing conventions — in-VM apps use
`pytest-qt` with `QT_QPA_PLATFORM=offscreen` for unit tests, while these
vm-* tools drive **full-stack integration tests** from the host.

## Input automation under Wayland

Direct `xdotool`-based input only works against XWayland sessions. For
qdistro compositor sessions the right surface is the
`org.qdistro.App1.UIModel` D-Bus interface — tests reference buttons by
stable widget ID ("approve-button") rather than pixel coordinates.
Survives layout changes, resolution changes, and theme changes.

`ydotool` (uinput-based, compositor-agnostic) is a lower-level fallback
when only raw input is needed. Screenshots via `virsh screenshot` keep
working regardless of the compositor.

## Limitations

- **Single user assumed** in `vm-gui`. Parameterize for multi-user test
 scenarios.
- **`qemu-guest-agent` prerequisite.** Documented in the bootstrap guide.
- **No headless GUI mode** — apps launch on the guest's display; the guest
 needs a session.

## Relationship to other features

- **Report for debugging** — same primitives (screenshot, exec, UI-tree
 dump) but running *inside* the user's session, not host → VM.
- **Window projection to phone** — conceptually similar export-of-a-
 display, but uses full remote-output pipeline (RDP), not `virsh
 screenshot` + input injection.
- **AI-authored workflows** — an AI agent could use these tools to
 validate that a generated workflow actually works, closing the loop on
 agent-authored policy.
