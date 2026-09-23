#!/usr/bin/env bash
set -euo pipefail

# Agent-assisted VM smoke for qdwin internal-idle + qdshell display-power
# recovery. It intentionally sets qdshell display-off to 1 minute and a safe
# inactivity action to 2 minutes, waits for the VM scanout to go black, injects
# real evdev-layer key + mouse input via QMP, and verifies screenshots recover
# to a painted desktop.
#
# This catches the qdwin internal-idle regression where ext-idle idled fired
# while libweston stayed ACTIVE, so later input did not emit wake_signal;
# qdshell never received ext-idle `resumed` and never called
# qdwin_shell_v1.set_display_power(true).
#
# Usage:
#   VMNAME=gui2tk-260603-1758 qdwin/tests/gui/agent-idle-dpms-recovery-smoke.sh
#
# Env:
#   VMNAME                         target libvirt domain (qemu:///session)
#   QDWIN_IDLE_DPMS_WAIT_S         seconds to wait for 1-minute blanking (default 75)
#   QDWIN_IDLE_DPMS_ACTIVE_CHECK_S seconds to keep input active before blanking (default 75; 0 disables)
#   QDWIN_IDLE_DPMS_ACTIVE_STEP_S  seconds between active-check pointer moves (default 10)
#   QDWIN_IDLE_DPMS_RECOVER_WAIT_S seconds to wait after input before screenshot (default 5)
#   QDWIN_ARTIFACT_DIR             screenshot directory (default /tmp)
#
# Requires an ABI-matched qdwin build deployed in the guest. Dropping only a
# locally built qdwin-shell.so into an older image can fail before this test
# reaches the compositor behavior it is meant to cover.
#
# Exit: 0 pass; 1 discriminator failed; 2 setup/environment failure.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
QDWIN_REPO=${QDWIN_REPO:-$ROOT}
VMNAME=${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -n1)}

export QDWIN_REPO
export QDWIN_VIRSH=${QDWIN_VIRSH:-virsh -c qemu:///session}

ART=${QDWIN_ARTIFACT_DIR:-/tmp}
WAIT_S=${QDWIN_IDLE_DPMS_WAIT_S:-75}
ACTIVE_CHECK_S=${QDWIN_IDLE_DPMS_ACTIVE_CHECK_S:-75}
ACTIVE_STEP_S=${QDWIN_IDLE_DPMS_ACTIVE_STEP_S:-10}
RECOVER_WAIT_S=${QDWIN_IDLE_DPMS_RECOVER_WAIT_S:-5}
SETTINGS=/home/admin/.config/qdshell/settings.json
BACKUP=/tmp/qdshell-settings.idle-dpms-recovery.$$.json
MISSING_MARKER=/tmp/qdshell-settings.idle-dpms-recovery.$$.missing

# shellcheck source=qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh"
qdwin_set_vm "$VMNAME"

fail()       { echo "FAIL: $*" >&2; exit 1; }
setup_fail() { echo "SETUP: $*" >&2; exit 2; }
pass()       { echo "PASS: $*"; }

vm_exec() { "$QDWIN_VM_EXEC" "$VMNAME" "$@"; }

restore_settings() {
    set +e
    if [ -n "${VMNAME:-}" ]; then
        vm_exec "if [ -f '$MISSING_MARKER' ]; then
    rm -f '$SETTINGS'
elif [ -f '$BACKUP' ]; then
    install -D -o admin -g users -m 600 '$BACKUP' '$SETTINGS'
fi
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart noctalia-shell.service >/dev/null 2>&1 || true
rm -f '$BACKUP' '$MISSING_MARKER'" >/dev/null 2>&1
    fi
}
trap restore_settings EXIT

nonblack_fraction() {
    python3 -W ignore - "$1" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB").resize((192, 108))
b = im.tobytes()
n = len(b) // 3
nb = sum(1 for i in range(0, len(b), 3) if max(b[i], b[i + 1], b[i + 2]) > 24) / n
print(f"{nb:.4f}")
PY
}

is_number() {
    [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

assert_black() {
    local shot="$1" frac
    frac=$(nonblack_fraction "$shot")
    is_number "$frac" || setup_fail "could not measure screenshot brightness: $shot"
    awk "BEGIN { exit !($frac <= 0.005) }" \
        || fail "expected blank/black screenshot, got non-black fraction $frac: $shot"
    pass "display blanked as expected (non-black fraction=$frac): $shot"
}

assert_lit() {
    local shot="$1" frac
    frac=$(nonblack_fraction "$shot")
    is_number "$frac" || setup_fail "could not measure screenshot brightness: $shot"
    awk "BEGIN { exit !($frac >= 0.02) }" \
        || fail "expected recovered painted screenshot, got non-black fraction $frac: $shot"
    pass "display recovered/painted (non-black fraction=$frac): $shot"
}

take_screenshot() {
    local out="$1"
    rm -f "$out"
    qdwin_screenshot "$out" >/dev/null && [ -s "$out" ]
}

[ -n "$VMNAME" ] || setup_fail "no running VM; set VMNAME"
$QDWIN_VIRSH domstate "$VMNAME" >/dev/null 2>&1 || setup_fail "VM '$VMNAME' not found"
vm_exec "test -S /run/user/1000/wayland-1" >/dev/null \
    || setup_fail "qdwin Wayland socket is not up"
pass "qdwin Wayland socket is up on $VMNAME"
python3 - <<'PY' >/dev/null || setup_fail "host python3 Pillow is required"
from PIL import Image
PY

mkdir -p "$ART"
stamp=$(date +%Y%m%d-%H%M%S)
before="$ART/qdwin-idle-dpms-before-$stamp.png"
active="$ART/qdwin-idle-dpms-active-$stamp.png"
blank="$ART/qdwin-idle-dpms-blank-$stamp.png"
recovered="$ART/qdwin-idle-dpms-recovered-$stamp.png"

journal_since="@$(date -u +%s)"

# Save settings, force qdshell display-off to one minute on both AC/battery,
# arm a safe 2-minute inactivity notification, then restart qdshell so the
# PowerService arms both ext-idle notifications from a clean binding.
vm_exec "set -eu
install -d -o admin -g users -m 700 /home/admin/.config/qdshell
if [ -f '$SETTINGS' ]; then cp '$SETTINGS' '$BACKUP'; else touch '$MISSING_MARKER'; fi
python3 - <<'PY'
import json
from pathlib import Path
p = Path('$SETTINGS')
try:
    data = json.loads(p.read_text()) if p.exists() else {}
except Exception:
    data = {}
power = data.setdefault('power', {})
power.update({
    'displayOffBattery': 1,
    'displayOffAC': 1,
    'inactivityTimeoutBattery': 2,
    'inactivityTimeoutAC': 2,
    'inactivityAction': 'ask',
    'presentationMode': False,
})
p.write_text(json.dumps(data, indent=2, sort_keys=False) + '\\n')
PY
chown admin:users '$SETTINGS' '$BACKUP'
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart noctalia-shell.service"

# Wait for qdshell IPC so we know PowerService had a chance to initialize.
vm_exec "for i in \$(seq 1 30); do
  if runuser -u admin -- bash -lc 'printf \"status\\n\" | socat -t 1 - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>/dev/null | grep -qx ok'; then
    exit 0
  fi
  sleep 1
done
exit 1" >/dev/null || setup_fail "qdshell ctrl socket did not return after restart"
pass "qdshell restarted with display-off=1 minute"

qdwin_qmp_key shift down
sleep 0.05
qdwin_qmp_key shift up
sleep 1

take_screenshot "$before" || setup_fail "initial screenshot failed; output may already be inactive"
assert_lit "$before"

if [ "$ACTIVE_CHECK_S" -gt 0 ]; then
    read -r active_sw active_sh < <(python3 - "$before" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1])
print(im.size[0], im.size[1])
PY
)
    export QDWIN_SCREEN_W=$active_sw QDWIN_SCREEN_H=$active_sh
    echo "keeping pointer motion active for ${ACTIVE_CHECK_S}s; display must not blank..."
    elapsed=0
    side=0
    while [ "$elapsed" -lt "$ACTIVE_CHECK_S" ]; do
        if [ "$side" -eq 0 ]; then
            qdwin_mouse_move "$((active_sw / 2 - 40))" "$((active_sh / 2))"
            side=1
        else
            qdwin_mouse_move "$((active_sw / 2 + 40))" "$((active_sh / 2))"
            side=0
        fi
        sleep "$ACTIVE_STEP_S"
        elapsed=$((elapsed + ACTIVE_STEP_S))
    done
    take_screenshot "$active" || fail "active-use screenshot failed; output blanked during periodic input"
    assert_lit "$active"
fi

echo "waiting ${WAIT_S}s for qdshell display-off idle..."
sleep "$WAIT_S"
if take_screenshot "$blank"; then
    assert_black "$blank"
else
    pass "display blanked as expected: virsh screenshot failed while output was inactive"
fi

# Real input path: one harmless key tap and a mouse move. This must trigger
# qdwin internal-idle resume, qdshell's `resumed`, and set_display_power(true).
qdwin_qmp_key shift down
sleep 0.05
qdwin_qmp_key shift up
sleep 0.05
read -r sw sh < <(python3 - "$before" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1])
print(im.size[0], im.size[1])
PY
)
export QDWIN_SCREEN_W=$sw QDWIN_SCREEN_H=$sh
qdwin_mouse_move "$((sw / 2))" "$((sh / 2))"
sleep "$RECOVER_WAIT_S"

take_screenshot "$recovered" || fail "recovered screenshot failed; output did not become capturable"
assert_lit "$recovered"

# Log evidence is diagnostic but important: qdshell should have armed the
# policy, qdwin should have powered off, then qdshell should have resumed and
# powered on.
logs=$(vm_exec "journalctl _UID=1000 --since '$journal_since' --no-pager 2>/dev/null || true" 2>/dev/null || true)
grep -q 'idle policy armed: inactivity=120000ms (ask), displayOff=60000ms' <<<"$logs" \
    || fail "missing qdshell idle policy armed log since '$journal_since'"
if grep -q 'inactivity idle reached -> ask' <<<"$logs"; then
    fail "inactivity notification fired before its 2-minute timeout"
fi
grep -q 'display-off idle -> DPMS off' <<<"$logs" \
    || fail "missing qdshell display-off idle log since '$journal_since'"
grep -q 'display-off idle resume -> DPMS on' <<<"$logs" \
    || fail "missing qdshell display-off resume log since '$journal_since'"
grep -q 'qdwin: set_display_power on=0' <<<"$logs" \
    || fail "missing qdwin set_display_power on=0 log since '$journal_since'"
grep -q 'qdwin: set_display_power on=1' <<<"$logs" \
    || fail "missing qdwin set_display_power on=1 log since '$journal_since'"
pass "journal confirms display_power off -> resumed/on"

echo
echo "PASS: idle DPMS recovery scenario on $VMNAME"
echo "artifacts:"
echo "  before:    $before"
echo "  active:    $active"
echo "  blank:     $blank"
echo "  recovered: $recovered"
