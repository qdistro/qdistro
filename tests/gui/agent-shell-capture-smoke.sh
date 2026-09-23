#!/bin/bash
# Ensures: GUI evidence comes from qdwin's real Virtual-1 framebuffer without
# opening SECCTX, exposing capture to ordinary/silo clients, or falling back to
# a virsh tty screenshot when qdshell/compositor health fails.
set -euo pipefail

QDWIN_REPO=${QDWIN_REPO:-$(cd -- "$(dirname -- "$0")/../.." && pwd)}
QDLOCKER_REPO=${QDLOCKER_REPO:-$QDWIN_REPO/../qdlocker}
# shellcheck source=qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh"
# shellcheck source=../../../qdlocker/tests/gui/qdlocker-helpers.sh
source "$QDLOCKER_REPO/tests/gui/qdlocker-helpers.sh"

VMNAME=${VMNAME:-$($QDWIN_VIRSH list --name --state-running | head -1)}
[ -n "$VMNAME" ] || { echo "FAIL: no running VM (set VMNAME)"; exit 2; }
ART=${QDWIN_CAPTURE_ARTIFACTS:-/tmp/qdwin-shell-capture-smoke-$$}
mkdir -p "$ART"

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
vm_exec() { "$QDWIN_VM_EXEC" "$VMNAME" "$1"; }
user_systemctl() {
    vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user $*"
}
capture() {
    local path=$1
    rm -f "$path"
    qdwin_screenshot "$path" >/dev/null
    [ -s "$path" ] || fail "capture did not create $path"
}
wait_healthy() {
    local end=$((SECONDS + 15))
    until qdwin_session_healthy >/dev/null 2>&1; do
        [ "$SECONDS" -lt "$end" ] || fail "session did not recover within 15s"
        sleep 0.5
    done
}

cleanup() {
    vm_exec "rm -f /run/user/1000/capture-{admin,bad,pipewire,ordinary}.png" \
        >/dev/null 2>&1 || true
    user_systemctl stop qdwin-capture-mutation.service >/dev/null 2>&1 || true
    user_systemctl restart qdwin-session.target >/dev/null 2>&1 || true
    # The L1 failure-mode steps above legitimately burn several qdshell /
    # compositor start attempts in a short window (qdshell stop/start,
    # compositor stop, session-target restart). Clear the per-unit start-rate
    # counters so the NEXT gate lane on this shared session VM (the
    # qdshell-ui vision harness restarts qdshell once per test) does not
    # inherit them and trip StartLimitBurst=5/30s with "start of the service
    # was attempted too often".
    user_systemctl reset-failed qdshell.service qdwin-compositor.service \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

qdwin_session_healthy || fail "L1 session/DRM ownership gate failed"
qdlocker_session_healthy || fail "locker session/introspection fixture unavailable"
pass "L1 session gate: units/socket active and weston owns the DRM-master card"

compositor_env=$(vm_exec 'pid=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show qdwin-compositor.service -p MainPID --value); tr "\0" "\n" </proc/$pid/environ')
grep -qx 'QDWIN_ENABLE_SHELL_CAPTURE=1' <<<"$compositor_env" \
    || fail "compositor is missing QDWIN_ENABLE_SHELL_CAPTURE=1"
if grep -q '^QDWIN_SECCTX_OPEN=' <<<"$compositor_env"; then
    fail "security lane contains forbidden QDWIN_SECCTX_OPEN"
fi
pass "capture authority enabled with QDWIN_SECCTX_OPEN absent"

ordinary_info=$(vm_exec 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 wayland-info 2>/dev/null')
if grep -q "interface: 'weston_capture_v1'" <<<"$ordinary_info"; then
    fail "ordinary client discovered weston_capture_v1"
fi
output_count=$(grep -c "interface: 'wl_output'" <<<"$ordinary_info" || true)
[ "$output_count" -eq 3 ] \
    || fail "expected exactly 3 wl_output globals, observed $output_count"
for name in Virtual-1 pipewire-0 pipewire-1; do
    grep -q "name: $name" <<<"$ordinary_info" \
        || fail "expected output $name missing from ordinary enumeration"
done
pass "ordinary client sees 3 outputs but not weston_capture_v1"

secctx_info=$(vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 qdistro-secctx-exec --sandbox-engine qdistro.tier1 --app-id capture-test --instance-id capture-test-$$ -- wayland-info 2>/dev/null") \
    || fail "could not enumerate globals from a real SECCTX client"
if grep -q "interface: 'weston_capture_v1'" <<<"$secctx_info"; then
    fail "SECCTX client discovered weston_capture_v1"
fi
pass "SECCTX client cannot discover weston_capture_v1"

admin_reply=$(vm_exec "runuser -u admin -- bash -c \"printf 'capture Virtual-1 /run/user/1000/capture-admin.png\\n' | socat -T 2 -t 2 - UNIX-CONNECT:/run/user/1000/qdshell.sock\"")
case "$admin_reply" in
    *"error: capture requires authenticated root peer"*) ;;
    *) fail "same-uid ctrl peer was not denied: $admin_reply" ;;
esac
pass "qdshell capture IPC denies a same-uid confused-deputy request"

# Anything that is not the single designated output (Virtual-1) is refused
# up front — absent names and PipeWire forwards alike. The compositor
# authority enforces the same exact-name pin independently.
bad_reply=$(vm_exec "printf 'capture absent-output /run/user/1000/capture-bad.png\\n' | socat -T 10 -t 10 - UNIX-CONNECT:/run/user/1000/qdshell.sock")
case "$bad_reply" in *"error: refusing capture of non-designated output"*) ;; *) fail "wrong output did not fail: $bad_reply" ;; esac
pipe_reply=$(vm_exec "printf 'capture pipewire-0 /run/user/1000/capture-pipewire.png\\n' | socat -T 10 -t 10 - UNIX-CONNECT:/run/user/1000/qdshell.sock")
case "$pipe_reply" in *"error: refusing capture of non-designated output"*) ;; *) fail "PipeWire output did not fail: $pipe_reply" ;; esac
pass "non-designated output names (absent + PipeWire) fail without killing qdshell"

# Baseline hygiene: earlier smokes on this shared session VM can leak UI
# state — notably an open launcher overlay (agent-click-smoke's final probe
# clicks the launcher icon), which occludes the center of the screen where
# the compositor places new toplevels. With the launcher panel leaked open,
# the 480x320 mutation window below mapped fully underneath it and the
# capture diff stayed at ~0 deterministically. Dismiss any overlay and
# clear stray test windows BEFORE taking the static baseline.
qdwin_send_key KEY_ESC
vm_exec "pkill -u admin -f '[q]distro-test-window' 2>/dev/null; true" >/dev/null 2>&1 || true
sleep 2

capture "$ART/static-1.png"
capture "$ART/static-2.png"
python3 - "$ART/static-1.png" "$ART/static-2.png" <<'PY' \
    || fail "two captures of an unchanged scene were not stable"
from PIL import Image, ImageChops
import sys
a = Image.open(sys.argv[1]).convert("RGB")
b = Image.open(sys.argv[2]).convert("RGB")
assert a.size == b.size and a.size[0] > 0 and a.size[1] > 0
d = ImageChops.difference(a, b)
changed = sum(1 for px in d.getdata() if px != (0, 0, 0)) / (a.width * a.height)
assert changed < 0.05, changed
print(f"static_changed_fraction={changed:.6f} dimensions={a.width}x{a.height}")
PY
pass "unchanged scene captured twice through Virtual-1 ($ART/static-{1,2}.png)"

sleep "${QDWIN_CAPTURE_IDLE_SECONDS:-5}"
user_systemctl start qdwin-capture-mutation.service >/dev/null 2>&1 || \
    vm_exec "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemd-run --user --unit=qdwin-capture-mutation --collect env WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title qdwin-capture-mutation --width 480 --height 320 --color 0xffe04040" >/dev/null
# The mutation window is a cold-starting Qt client; under host load it can
# take (much) longer than a fixed 2 s to launch and map. Poll the capture
# until the mutation shows up instead of trusting one blind-sleep capture —
# a zero-diff from that single early capture was this smoke's dominant
# flake, and it indicts the harness timing, not the capture path.
mutation_deadline=$((SECONDS + ${QDWIN_CAPTURE_MUTATION_WAIT:-30}))
mutation_seen=0
while [ "$SECONDS" -lt "$mutation_deadline" ]; do
    sleep 2
    capture "$ART/mutated.png"
    if python3 - "$ART/static-2.png" "$ART/mutated.png" 2>/dev/null <<'PY'
from PIL import Image, ImageChops
import sys
a = Image.open(sys.argv[1]).convert("RGB")
b = Image.open(sys.argv[2]).convert("RGB")
assert a.size == b.size
d = ImageChops.difference(a, b)
changed = sum(1 for px in d.getdata() if px != (0, 0, 0)) / (a.width * a.height)
assert changed > 0.01, changed
print(f"mutation_changed_fraction={changed:.6f}")
PY
    then
        mutation_seen=1
        break
    fi
done
if [ "$mutation_seen" -ne 1 ]; then
    user_systemctl status qdwin-capture-mutation.service --no-pager >&2 || true
    fail "mutation-after-idle was absent from the capture within ${QDWIN_CAPTURE_MUTATION_WAIT:-30}s"
fi
pass "mutation after idle appears in the captured framebuffer"

qdlocker_drain_lock_state || fail "could not establish unlocked baseline"
case "$(qdlocker_ctrl lock)" in ok) ;; *) fail "locker ctrl request failed" ;; esac
qdlocker_wait_for_lock 8 || fail "locker did not engage"
capture "$ART/locked.png"
qdlocker_unlock_with_password || fail "could not unlock after capture"
pass "capture completes while the lock curtain is active ($ART/locked.png)"

user_systemctl stop qdshell.service >/dev/null
if qdwin_session_healthy >/dev/null 2>&1; then
    fail "L1 health passed with qdshell stopped"
fi
if qdwin_screenshot "$ART/qdshell-dead.png" >/dev/null 2>&1 ||
   [ -e "$ART/qdshell-dead.png" ]; then
    fail "content capture passed/fell back with qdshell stopped"
fi
user_systemctl start qdshell.service >/dev/null
wait_healthy
pass "qdshell death yields L1 failure and no fallback content image"

user_systemctl stop qdwin-compositor.service >/dev/null
if qdwin_session_healthy >/dev/null 2>&1; then
    fail "L1 health passed with compositor stopped"
fi
if qdwin_screenshot "$ART/compositor-dead.png" >/dev/null 2>&1 ||
   [ -e "$ART/compositor-dead.png" ]; then
    fail "content capture passed/fell back with compositor stopped"
fi
user_systemctl restart qdwin-session.target >/dev/null
wait_healthy
pass "compositor death yields L1 failure and no tty fallback PASS"

echo "ALL SHELL-CAPTURE REGRESSIONS PASSED on $VMNAME; artifacts: $ART"
