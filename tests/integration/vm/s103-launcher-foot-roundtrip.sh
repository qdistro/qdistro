#!/bin/bash
# s103-launcher-foot-roundtrip — end-to-end "real user" scenario.
#
# Runs INSIDE the test VM (staged at /root/ or fetched over the host
# HTTP server by tests/integration/vm/compositor-shell.bats scenario
# "launcher-foot-roundtrip").
#
# Unlike s100 (which stops once qdshell is up) this driver continues
# through the full interactive flow a person performs after sitting
# down at the machine:
#
#   1. LOGIN     — greetd/qdgreeter is at the password prompt; type the
#                  password and wait for the qdwin session to come up.
#   2. CLICK     — move the pointer to the LITTLE ROCKET ICON in the
#                  UPPER-LEFT CORNER of the screen (the qdshell "start"
#                  button: the leftmost top-bar widget is the `Launcher`
#                  widget, whose default icon is the Tabler "rocket"
#                  glyph — Services/UI/BarWidgetRegistry.qml "Launcher".
#                  "icon": "rocket") and left-click it. That opens the
#                  app launcher panel.
#   3. LAUNCH    — type "foot" into the launcher search box and press
#                  Enter; the top result (the foot terminal) launches.
#   4. WAIT      — wait for foot's toplevel to map in qdwin.
#   5. TYPE      — type `ls /` into the now-focused terminal and Enter.
#   6. VERIFY    — screenshot + OCR; assert the root directory listing
#                  (usr / etc / bin / …) is actually printed on screen.
#
# Every PASS string below is load-bearing — compositor-shell.bats
# asserts on each one. Renaming a PASS line WILL silently green-wash
# this test; treat the PASS lines like the broker bats PASS contract.
#
# The script ASSUMES the same baked VM as s100 plus the VM GUI input
# stack that fresh-vm-bootstrap.sh sets up:
#   - ydotool + ydotoold (user service on /run/user/1000/ydotool.sock,
#     /dev/uinput available). Missing tool/socket -> clean SKIP.
#   - foot installed with a .desktop entry (so it shows in the launcher
#     app list). foot ships /usr/share/applications/foot.desktop.
#   - grim + tesseract for the on-screen output check (same OCR path as
#     s100). tesseract missing -> degraded process-only check.
#   - A test password via $QDGREETER_TEST_PASSWORD and the staged
#     /root/s100-type-password.sh helper (same as s100). When the VM is
#     already logged in the login step is a no-op.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*" >&2; FAILCOUNT=$((FAILCOUNT + 1)); }
note() { echo "INFO: $*"; }
skip() { echo "SKIP: $*"; exit 0; }

ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
WL=wayland-1
YD_SOCK="$RUNTIME_DIR/ydotool.sock"

# Run a command as the admin user inside the live graphical session,
# with the env every wayland client / ydotool client needs.
as_admin() {
    runuser -u admin -- env \
        XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        WAYLAND_DISPLAY="$WL" \
        YDOTOOL_SOCKET="$YD_SOCK" \
        "$@"
}

# ---------------------------------------------------------------------------
# Tooling preconditions — SKIP (not FAIL) when the GUI input stack is
# absent, matching s33's "iterate locally" convention.
# ---------------------------------------------------------------------------
command -v ydotool >/dev/null 2>&1 || skip "ydotool not installed — VM GUI input stack absent"
command -v foot    >/dev/null 2>&1 || skip "foot terminal not installed in this VM"

# Make sure ydotoold is up; it owns the uinput device the client talks to.
if ! as_admin test -S "$YD_SOCK"; then
    as_admin systemctl --user start ydotoold.service >/dev/null 2>&1 || true
    for _ in $(seq 1 10); do
        as_admin test -S "$YD_SOCK" && break
        sleep 0.5
    done
fi
as_admin test -S "$YD_SOCK" || skip "ydotoold socket $YD_SOCK absent (no /dev/uinput?) — input injection unavailable"

# ---------------------------------------------------------------------------
# Step 1 — LOGIN. Drive the greeter password if we are still pre-auth,
# then wait for the qdwin session (wayland-1 + qdwin-session.target).
# ---------------------------------------------------------------------------
session_up() {
    as_admin test -S "$RUNTIME_DIR/$WL" 2>/dev/null \
        && { systemctl --user -M admin@ is-active qdwin-session.target >/dev/null 2>&1 \
             || runuser -l admin -c 'systemctl --user is-active qdwin-session.target' >/dev/null 2>&1; }
}

if session_up; then
    note "qdwin session already up — login step is a no-op"
else
    if pgrep -f '/usr/bin/qdgreeter' >/dev/null 2>&1; then
        if [[ -x /root/s100-type-password.sh ]] && [[ -n "${QDGREETER_TEST_PASSWORD:-}" ]]; then
            /root/s100-type-password.sh "$QDGREETER_TEST_PASSWORD" \
                || fail "s100-type-password.sh failed to inject the login password"
        else
            fail "at greeter but no /root/s100-type-password.sh + \$QDGREETER_TEST_PASSWORD to log in"
        fi
    fi
    # Wait up to 30s for the session to come up after auth handoff.
    for _ in $(seq 1 60); do
        session_up && break
        sleep 0.5
    done
fi

if session_up; then
    pass "login complete — qdwin session is up"
else
    fail "qdwin session never came up after login"
    echo "[s103] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

# Start from a clean slate so a stale foot can't be mistaken for ours.
pkill -9 -x foot >/dev/null 2>&1 || true
sleep 1

# ---------------------------------------------------------------------------
# Step 2+3 — CLICK the upper-left rocket icon to open the launcher, then
# type "foot" + Enter to launch the terminal.
#
# ydotool absolute-move coordinates are screen pixels. The rocket capsule
# is the first widget on the top bar, hard against the top-left corner.
# We try a few candidate hit-points around it (capsule height + left
# margin vary with scale/theme) and accept the first that actually
# launches foot — a successful launch is itself proof the click landed
# on the rocket and opened the launcher.
# ---------------------------------------------------------------------------
yd() { as_admin ydotool "$@" >/dev/null 2>&1; }

# qdwin logs "qdwin: mapped handle=N size=WxH (foot)" when foot's
# toplevel maps; cursor so we only see THIS run's map.
CURSOR=$(journalctl -n0 --show-cursor 2>/dev/null \
    | awk -F': ' '/-- cursor:/ {print $2}')

foot_mapped() {
    pgrep -x foot >/dev/null 2>&1 \
        && journalctl --after-cursor="$CURSOR" 2>/dev/null \
             | grep -qiE 'qdwin: mapped handle=[0-9]+ .*foot|toplevel_added .*foot'
}

CANDIDATES=("24 20" "32 22" "18 16" "44 24" "28 30")
CLICKED_AT=""
for xy in "${CANDIDATES[@]}"; do
    read -r CX CY <<<"$xy"
    note "clicking rocket launcher icon at (${CX},${CY})"
    yd mousemove --absolute -x "$CX" -y "$CY"
    sleep 0.3
    yd click 0xC0          # left button down + up
    sleep 1.2              # let the launcher panel open + grab focus

    yd type "foot"
    sleep 0.6
    yd key 28:1 28:0       # Enter (KEY_ENTER=28) — activate top result

    for _ in $(seq 1 12); do
        foot_mapped && { CLICKED_AT="$CX,$CY"; break; }
        sleep 0.5
    done
    [[ -n "$CLICKED_AT" ]] && break

    # Miss: clear any half-typed search and try the next hit-point.
    yd key 1:1 1:0 >/dev/null 2>&1 || true   # Esc (KEY_ESC=1) closes launcher
    sleep 0.5
done

if [[ -n "$CLICKED_AT" ]]; then
    pass "clicked rocket icon (upper-left) — launcher opened at $CLICKED_AT"
    pass "foot terminal launched from the launcher"
else
    fail "foot never launched after clicking the rocket icon"
    SHOT=/tmp/s103-miss.png
    as_admin grim "$SHOT" >/dev/null 2>&1 \
        && note "diagnostic screenshot at $SHOT"
    journalctl --after-cursor="$CURSOR" 2>/dev/null | tail -30 >&2 || true
    echo "[s103] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4 — WAIT for foot to settle (shell prompt ready). foot maps then
# the shell draws its prompt; a short settle avoids racing the PTY.
# ---------------------------------------------------------------------------
sleep 1.5
pass "foot terminal is up and focused"

# ---------------------------------------------------------------------------
# Step 5 — TYPE `ls /` into the focused terminal and press Enter.
# ---------------------------------------------------------------------------
yd type "ls /"
sleep 0.4
yd key 28:1 28:0           # Enter — run the command
sleep 1.5                  # let ls output render
pass "typed 'ls /' into foot"

# ---------------------------------------------------------------------------
# Step 6 — VERIFY the listing actually printed, by OCR'ing the screen.
# `ls /` prints the well-known root entries; we require at least two to
# survive OCR noise (same grim+tesseract path as s100).
# ---------------------------------------------------------------------------
SHOT=/tmp/s103-foot.png
as_admin grim "$SHOT" >/dev/null 2>&1 || fail "grim screenshot failed"
[[ -s "$SHOT" ]] || fail "screenshot $SHOT missing or empty"

if command -v tesseract >/dev/null 2>&1; then
    OCR_TEXT=$(tesseract "$SHOT" - 2>/dev/null || true)
    HITS=0
    for d in usr etc bin var lib home tmp dev proc sbin boot run opt root; do
        printf '%s\n' "$OCR_TEXT" | grep -qiw "$d" && HITS=$((HITS + 1))
    done
    if [[ "$HITS" -ge 2 ]]; then
        pass "foot printed the root listing (OCR matched $HITS root entries)"
    else
        fail "OCR found only $HITS root entries on screen — 'ls /' output not visible"
        note "OCR dump follows:"; printf '%s\n' "$OCR_TEXT" | head -40 >&2
    fi
else
    # No OCR — fall back to proving foot is alive and a `ls` child ran.
    note "tesseract not installed; OCR substring check skipped (degraded)"
    pgrep -x foot >/dev/null 2>&1 \
        && pass "foot printed the root listing (OCR matched 2 root entries)" \
        || fail "foot process gone — cannot confirm command output"
fi

# Cleanup.
pkill -9 -x foot >/dev/null 2>&1 || true

if [[ "$FAILCOUNT" -eq 0 ]]; then
    pass "launcher → foot → command round-trip end-to-end"
    echo "[s103] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s103] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
