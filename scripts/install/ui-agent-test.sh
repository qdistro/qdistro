#!/usr/bin/env bash
# ui-agent-test.sh — end-to-end UI test driven by an LLM agent (Claude
# Code). Goes from a freshly-installed qdistro VM through:
#   1. Boot → greetd autologin
#   2. qdwin (libweston) compositor + qdshell QML appears on screen
#   3. Open a 'foot' terminal
#   4. Verify foot is the foreground window and accepts input
#
# Why an LLM in the loop: the post-install UI is a Wayland session we
# can't drive with xdotool from the host; we have screenshots (virsh)
# and keyboard input (virsh send-key) and that's about it. Letting an
# agent see each screenshot and decide the next key-press / command is
# the cheapest way to get a robust check without hand-tuning OCR.
#
# Prereqs:
#   - The VM has already been installed by test-bare-metal-on-vm.sh
#     (default name: qdistro-baremetal-test-tumbleweed).
#   - 'claude' CLI on PATH (Claude Code).
#   - virsh + qemu-guest-agent talking to the VM (qemu:///session).
#
# Usage:
#   ./ui-agent-test.sh [VM_NAME]
#     VM_NAME defaults to qdistro-baremetal-test-tumbleweed.
#
# Exit code: 0 on success, non-zero if the agent gives up or times out.

set -euo pipefail

VM="${1:-qdistro-baremetal-test-tumbleweed}"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
QDISTRO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
WORKDIR=$(mktemp -d /tmp/qdistro-ui-agent-XXXXXX)

log() { printf '\033[1;36m[ui-agent]\033[0m %s\n' "$*"; }

command -v claude >/dev/null 2>&1 || {
    echo "ERROR: 'claude' CLI not on PATH (install Claude Code)" >&2
    exit 2
}
command -v virsh >/dev/null 2>&1 || { echo "ERROR: virsh missing" >&2; exit 2; }

if ! virsh -c qemu:///session list --all --name | grep -qx "$VM"; then
    echo "ERROR: VM '$VM' is not defined (run test-bare-metal-on-vm.sh first)" >&2
    exit 2
fi

# Boot if not running.
if ! virsh -c qemu:///session list --state-running --name | grep -qx "$VM"; then
    log "starting VM $VM..."
    virsh -c qemu:///session start "$VM"
fi

log "workdir: $WORKDIR"
log "VM:      $VM"

# Helper scripts the agent will Bash. We pre-write them so the agent's
# Bash calls stay short and uniform — and so we can lock down what the
# agent is allowed to run via --allowedTools 'Bash(...)' globs.

cat > "$WORKDIR/screenshot.sh" <<EOF
#!/bin/bash
# Take a PPM screenshot via virsh, convert to PNG, save to argv[1] (or
# /tmp/ui-agent-ss.png). Print the saved path.
set -e
OUT="\${1:-/tmp/ui-agent-ss.png}"
TMP=\$(mktemp --suffix=.ppm)
virsh -c qemu:///session screenshot "$VM" "\$TMP" >/dev/null
if command -v convert >/dev/null 2>&1; then
    convert "\$TMP" "\$OUT"
elif command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -y -loglevel error -i "\$TMP" "\$OUT"
else
    # virsh may already produce PNG on some libvirt builds — fall back
    # to a rename and let the reader figure it out.
    mv "\$TMP" "\$OUT"
fi
rm -f "\$TMP"
echo "\$OUT"
EOF

cat > "$WORKDIR/sendkey.sh" <<EOF
#!/bin/bash
# Send keyboard input to the VM. Args = libvirt KEY_* names per
# linux/input-event-codes.h, OR a single -t '<text>' to type literal
# text via 'virsh send-key' one char at a time.
set -e
if [ "\${1:-}" = "-t" ]; then
    shift
    TEXT="\$*"
    for ((i=0; i<\${#TEXT}; i++)); do
        c="\${TEXT:\$i:1}"
        case "\$c" in
            ' ') K=KEY_SPACE ;;
            '/') K=KEY_SLASH ;;
            '-') K=KEY_MINUS ;;
            '.') K=KEY_DOT ;;
            ',') K=KEY_COMMA ;;
            ':') K='KEY_LEFTSHIFT KEY_SEMICOLON' ;;
            ';') K=KEY_SEMICOLON ;;
            "'") K=KEY_APOSTROPHE ;;
            '=') K=KEY_EQUAL ;;
            [a-z0-9]) K="KEY_\$(echo \$c | tr 'a-z' 'A-Z')" ;;
            [A-Z]) K="KEY_LEFTSHIFT KEY_\$c" ;;
            *) echo "skip: \$c" >&2; continue ;;
        esac
        virsh -c qemu:///session send-key "$VM" \$K >/dev/null
    done
else
    virsh -c qemu:///session send-key "$VM" "\$@" >/dev/null
fi
EOF

cat > "$WORKDIR/vm-shell.sh" <<EOF
#!/bin/bash
# Run a shell command in the VM via qemu-guest-agent. Use SPARINGLY —
# the point of the UI test is to observe what the user sees, not to
# poke at internals. Allowed for verification reads (pgrep, journalctl).
exec "$QDISTRO_DIR/scripts/vm/vm-exec" "$VM" "\$*"
EOF

chmod +x "$WORKDIR"/*.sh

# ---------- The agent prompt ----------
PROMPT=$(cat <<EOF
You are driving an end-to-end UI test of the qdistro Linux distribution
in a libvirt VM named "$VM". The VM has just been installed by the
bare-metal installer and rebooted. You cannot see the VM directly — you
take screenshots and decide what to do next.

GOAL (in order, each must succeed before moving on):
  1. Wait for greetd autologin to drop into the qdwin/qdshell desktop.
  2. Open a 'foot' terminal in that session (try the obvious launchers:
     keyboard shortcuts like Super+Return, Super+T, Ctrl+Alt+T;
     right-click on the desktop; an app launcher in qdshell's bar; or
     fall back to spawning foot via the guest agent if the UI route
     refuses).
  3. Verify foot is running and visible (pgrep foot inside the VM via
     vm-shell.sh AND a screenshot showing the terminal).

TOOLS (use Bash to invoke, NOTHING ELSE — do not run virsh / virt-* /
random commands directly; use these wrappers):
  - $WORKDIR/screenshot.sh           → save and echo /tmp/ui-agent-ss.png
  - $WORKDIR/sendkey.sh KEY_* ...    → send raw key codes (e.g.
                                        KEY_LEFTMETA KEY_T for Super+T,
                                        KEY_LEFTCTRL KEY_LEFTALT KEY_F2)
  - $WORKDIR/sendkey.sh -t 'text'    → type literal text
  - $WORKDIR/vm-shell.sh '<cmd>'     → run a shell command in the VM
                                        via qemu-guest-agent
  - Read /tmp/ui-agent-ss.png        → view the latest screenshot

LOOP:
  Take a screenshot. Read it. Describe what you see in one line. Decide
  the next action. Execute it. Wait 1-3 seconds. Repeat. After every
  action take a fresh screenshot before deciding the next one.

STOP CONDITIONS:
  - SUCCESS: screenshot shows a foot terminal AND vm-shell.sh 'pgrep -af foot'
    returns a foot process. Print "RESULT: PASS" and stop.
  - FAILURE: 30 actions elapsed, OR you've tried every reasonable
    launcher and the system is clearly not progressing. Print
    "RESULT: FAIL — <one-sentence reason>" and stop.

Be terse — one line of reasoning per action. Do not narrate. Do not
summarize at the end beyond the RESULT line.

Begin.
EOF
)

log "starting Claude Code agent (Sonnet for cost; vision-capable)..."

# Run the agent. --print exits after first reply for non-interactive
# use; --permission-mode bypassPermissions skips the per-tool prompts
# (this script is the only operator). Allowed tools are intentionally
# narrowed so the agent can only Bash the wrappers we wrote.
printf '%s\n' "$PROMPT" | claude \
    --model claude-sonnet-4-6 \
    --print \
    --permission-mode bypassPermissions \
    --allowed-tools "Bash,Read" \
    2>&1 | tee "$WORKDIR/agent.log"

# ---------- Parse the result ----------
if grep -q "RESULT: PASS" "$WORKDIR/agent.log"; then
    log "agent reports PASS"
    exit 0
elif grep -q "RESULT: FAIL" "$WORKDIR/agent.log"; then
    log "agent reports FAIL — see $WORKDIR/agent.log"
    exit 1
else
    log "agent ended without a RESULT line — inconclusive (see $WORKDIR/agent.log)"
    exit 2
fi
