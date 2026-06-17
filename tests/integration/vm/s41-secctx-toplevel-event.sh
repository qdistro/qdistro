#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-secctx-toplevel-event.
#
# Validates the qdwin_shell_v1@v13+ toplevel_security_context event
# end-to-end: qdwin's wp_security_context_v1 commit handler triggers
# qdwin_shell_v1_send_toplevel_security_context, which qdshell's
# Qdwin singleton receives and fans out to Tier3Apps for silo-aware
# rendering.
#
# Load-bearing assertions per design doc / dead-bats-entries.md §s41:
#   1. qdwin-shell-v1.xml interface version >= 13 (the version that
#      added the toplevel_security_context event).
#   2. qdshell is up.
#   3. Compositor emits the event — qdwin journal log line at
#      qdwin/qdwin.c:814 ("qdwin: toplevel_security_context handle=...
#      engine=... app_id=... instance=...").
#   4. qdshell receives the event — qdshell journal log line at
#      Services/Qdwin/Qdwin.qml:108-115 ("[Qdwin]
#      toplevel_security_context handle=... engine=... app_id=...
#      instance=...").
#   5. qdshell derives silo from secctx app_id — Tier3Apps log line
#      at Services/Qdistro/Tier3Apps.qml:160 ("[tier3] toplevel
#      observed silo=user1 secctx=qdistro.tier3.user1 handle=...").
#   6. silo colour applied via secctx path — Tier3Apps log at
#      Services/Qdistro/Tier3Apps.qml:167 ("[tier3] silo=user1
#      color=#RRGGBB").
#
# Pairs with s40 (the secctx commit *into* qdwin — the prerequisite
# wire path that fires this event).

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SPAWN_PID=""
TRAP_FIRED=0
cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    [ -n "$SPAWN_PID" ] && kill -TERM "$SPAWN_PID" 2>/dev/null || true
    [ -n "$SPAWN_PID" ] && wait    "$SPAWN_PID" 2>/dev/null || true
    pkill -u user1 -x weston-terminal 2>/dev/null || true
    rm -f /tmp/s41-spawn.log 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. stage tier3 source -------------------------------------------
SRC=/root/qdistro-src/qdistro
TIER3_DIR=/tmp/qdistro-tier3-src
COMMON_LIB_DIR=/tmp/lib
if [ -d "$SRC/tier3" ]; then
    rm -rf "$TIER3_DIR" 2>/dev/null || true
    cp -r "$SRC/tier3" "$TIER3_DIR"
    chmod -R a+rX "$TIER3_DIR"
    find "$TIER3_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
if [ -d "$SRC/lib" ]; then
    rm -rf "$COMMON_LIB_DIR" 2>/dev/null || true
    cp -r "$SRC/lib" "$COMMON_LIB_DIR"
    chmod -R a+rX "$COMMON_LIB_DIR"
fi
[ -d "$TIER3_DIR" ] || skip "tier3 source not unpacked at $TIER3_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || skip "spawn-common library not unpacked at $COMMON_LIB_DIR"

command -v waypipe        >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v wayland-info   >/dev/null 2>&1 || skip "wayland-info not installed in this VM"
command -v runuser        >/dev/null 2>&1 || skip "runuser not available"

# --- 2. tier3 install ran --------------------------------------------
if ! getent group qdistro-tier3 >/dev/null; then
    skip "qdistro-tier3 group missing"
fi
id -u user1 >/dev/null 2>&1 || skip "silo user 'user1' missing"

# --- 3. outer admin compositor + qdshell -----------------------------
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
runuser -u admin -- test -S "$OUTER_SOCK" || skip "outer admin compositor not up"
pass "outer admin compositor up"

# --- 4. qdwin-shell-v1.xml interface version >= 13 -------------------
# The XML is staged into the VM under /root/qdistro-src/qdwin/qdwin/
# (per scripts/vm/fresh-vm-bootstrap.sh's $SRC). Required for the
# toplevel_security_context event to exist at all (added in v13;
# qdwin currently ships v21 on main as of 2026-05-16).
QDWIN_XML="/root/qdistro-src/qdwin/qdwin/qdwin-shell-v1.xml"
if [ ! -f "$QDWIN_XML" ]; then
    skip "qdwin-shell-v1.xml not staged at $QDWIN_XML"
fi
XML_VERSION=$(grep -oE 'name="qdwin_shell_v1" version="[0-9]+"' "$QDWIN_XML" \
              | sed -n 's/.*version="\([0-9]\+\)".*/\1/p' | head -1)
if [ -z "$XML_VERSION" ]; then
    fail "could not parse qdwin_shell_v1 version from $QDWIN_XML"
elif [ "$XML_VERSION" -ge 13 ]; then
    pass "qdwin_shell_v1 protocol XML at version $XML_VERSION (>=13 required for toplevel_security_context)"
else
    fail "qdwin_shell_v1 protocol XML at version $XML_VERSION; need >=13"
fi

# --- 5. qdshell --------------------------------------------------------
if pgrep -u admin -af "qs -p" >/dev/null 2>&1; then
    pass "qdshell up"
elif systemctl --user --machine=admin@.host status qdshell.service >/dev/null 2>&1; then
    pass "qdshell up"
else
    fail "qdshell (qdshell) not running under admin uid"
fi

# --- 6. journal cursor + spawn the silo client -----------------------
JCURSOR_FILE=$(mktemp)
journalctl --since=now -n0 --show-cursor >"$JCURSOR_FILE" 2>/dev/null || true
CURSOR=$(awk -F': ' '/-- cursor:/ {print $2}' "$JCURSOR_FILE")

SPAWN_LOG=/tmp/s41-spawn.log
: >"$SPAWN_LOG"

# Use weston-terminal so the toplevel actually persists long enough
# for the qdshell-side log fan-out to run (wayland-info exits before
# Qdwin.qml's onToplevelSecurityContext can fully process it on a
# slow CI VM). Tear down at the end via SIGTERM.
TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- weston-terminal >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# Wait for the toplevel arrival to settle (~5–10s warm; allow 30s).
jgrep_once() {
    local pat="$1"
    if [ -n "$CURSOR" ]; then
        journalctl --after-cursor="$CURSOR" 2>/dev/null | grep -m1 -E "$pat" || true
    else
        journalctl --since="-2min" 2>/dev/null | grep -m1 -E "$pat" || true
    fi
}

QDWIN_LINE=""
QDSHELL_LINE=""
TIER3_OBS=""
TIER3_COL=""
deadline=$(( $(date +%s) + 90 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    [ -z "$QDWIN_LINE" ] && \
        QDWIN_LINE=$(jgrep_once 'qdwin: toplevel_security_context handle=[0-9]+ engine=qdistro\.tier3 app_id=qdistro\.tier3\.user1 instance=')
    # Logger.i format is "<14-char-padded module name> <message>", not
    # "[Module] message". The actual journal text reads:
    #   INFO qml: [20260516-053949]          Qdwin toplevel_security_context handle=…
    # So we match the module name as a literal token, not a bracketed
    # prefix.
    [ -z "$QDSHELL_LINE" ] && \
        QDSHELL_LINE=$(jgrep_once 'Qdwin toplevel_security_context handle=[0-9]+ engine=qdistro\.tier3 app_id=qdistro\.tier3\.user1 instance=')
    [ -z "$TIER3_OBS" ] && \
        TIER3_OBS=$(jgrep_once '\[tier3\] toplevel observed silo=user1 secctx=qdistro\.tier3\.user1 handle=[0-9]+')
    [ -z "$TIER3_COL" ] && \
        TIER3_COL=$(jgrep_once '\[tier3\] silo=user1 color=#[0-9a-fA-F]{6}')
    if [ -n "$QDWIN_LINE" ] && [ -n "$QDSHELL_LINE" ] && \
       [ -n "$TIER3_OBS" ] && [ -n "$TIER3_COL" ]; then
        break
    fi
    sleep 0.5
done

# --- 7. tear the spawn down before asserting -------------------------
kill -TERM "$SPAWN_PID" 2>/dev/null || true
for _ in $(seq 1 20); do
    if ! kill -0 "$SPAWN_PID" 2>/dev/null; then break; fi
    sleep 0.25
done
kill -KILL "$SPAWN_PID" 2>/dev/null || true
wait "$SPAWN_PID" 2>/dev/null || true

# --- 8. assertions ----------------------------------------------------
if [ -n "$QDWIN_LINE" ]; then
    pass "compositor emitted toplevel_security_context"
else
    cat "$SPAWN_LOG" >&2 || true
    fail "no qdwin: toplevel_security_context line for app_id=qdistro.tier3.user1"
fi
if [ -n "$QDSHELL_LINE" ]; then
    pass "qdshell received toplevel_security_context"
else
    fail "no 'Qdwin toplevel_security_context' line in qdshell journal — Qdwin.qml's onToplevelSecurityContext didn't fire (or Logger.i regression)"
fi
if [ -n "$TIER3_OBS" ]; then
    pass "qdshell derived silo=user1 from secctx app_id"
else
    fail "no [tier3] toplevel observed line — Tier3Apps.qml not loaded or filter regression"
fi
if [ -n "$TIER3_COL" ]; then
    pass "silo colour override applied via secctx path"
else
    fail "no [tier3] silo=user1 color=# line — Tier3Apps colour-resolve broken"
fi

rm -f "$SPAWN_LOG" "$JCURSOR_FILE" 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 v13 toplevel_security_context end-to-end"
    echo "[s41] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s41] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
