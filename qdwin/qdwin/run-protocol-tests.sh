#!/bin/bash
# Run the zwlr_layer_shell_v1 protocol unit tests inside a VM.
#
# Spawns a fresh headless weston with qdwin-shell.so, runs
# test_zwlr_layer_shell.py against it, captures the result, kills
# weston. Single-test invocation (the test client itself iterates
# through all sub-tests).
#
# This is the IN-VM full-session variant: it runs as `admin` against a
# fixed wayland-77 socket under /run/user/1000 with a bootstrap-provided
# /home/admin/weston.ini, and is the path to exercise all 12 sub-tests
# against the qdistro-vendored libweston-16 (QDWIN_USE_VENDORED_LIBWESTON=1).
# For headless host / CI use, prefer the self-contained meson harness
# `tests/protocol/run-layer-shell-protocol-test.sh`, wired as
# `meson test --suite protocol` — it stages the pywayland bindings and a
# private weston itself and needs neither root nor a bootstrapped VM.
#
# Phase 1.6 of .

set -u
set +e

# Opt-in: load the qdistro-vendored libweston-16 (NULL-parent xdg_popup
# patch) for this run. See compositor/run-noctalia-smoke.sh for the
# same knob; the test client reads QDWIN_USE_VENDORED_LIBWESTON to
# flip its expected outcome on test_null_parent_popup.
QDWIN_USE_VENDORED_LIBWESTON="${QDWIN_USE_VENDORED_LIBWESTON:-0}"
QDWIN_VENDORED_LIBWESTON_PREFIX="${QDWIN_VENDORED_LIBWESTON_PREFIX:-/usr/libexec/qdistro/qdwin-libweston}"
WESTON_LD_PREFIX=""
if [ "$QDWIN_USE_VENDORED_LIBWESTON" = "1" ]; then
    if [ ! -f "$QDWIN_VENDORED_LIBWESTON_PREFIX/lib64/libweston-16.so.0.0.0" ]; then
        echo "ERROR: QDWIN_USE_VENDORED_LIBWESTON=1 but no vendored .so at $QDWIN_VENDORED_LIBWESTON_PREFIX/lib64" >&2
        exit 2
    fi
    WESTON_LD_PREFIX="LD_LIBRARY_PATH=$QDWIN_VENDORED_LIBWESTON_PREFIX/lib64\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH} "
    echo "protocol-tests: using vendored libweston at $QDWIN_VENDORED_LIBWESTON_PREFIX"
fi

export XDG_RUNTIME_DIR=/run/user/1000
mkdir -p $XDG_RUNTIME_DIR
chown admin:users $XDG_RUNTIME_DIR
chmod 700 $XDG_RUNTIME_DIR

# Stale weston sockets gone.
rm -f $XDG_RUNTIME_DIR/wayland-* 2>/dev/null

runuser -u admin -- bash -c "
  export XDG_RUNTIME_DIR=/run/user/1000
  export WLD=wayland-77
  export WAYLAND_DISPLAY=wayland-77
  exec ${WESTON_LD_PREFIX}weston \
    --config=/home/admin/weston.ini \
    --socket=\$WLD \
    > /tmp/weston-proto-test.log 2>&1
" &
WPID=$!
echo "weston pid=$WPID"

# Wait for socket.
for i in 1 2 3 4 5 6 7 8 9 10; do
  if [ -e $XDG_RUNTIME_DIR/wayland-77 ]; then
    echo "weston socket ready after ${i}s"
    break
  fi
  sleep 1
done
if [ ! -e $XDG_RUNTIME_DIR/wayland-77 ]; then
  echo "ERROR: weston socket not appearing"
  cat /tmp/weston-proto-test.log
  exit 1
fi

# Run the test client as admin against the headless compositor. The
# QDWIN_USE_VENDORED_LIBWESTON env propagates so test_null_parent_popup
# matches the libweston the compositor was launched with.
runuser -u admin -- bash -c "
  export XDG_RUNTIME_DIR=/run/user/1000
  export QDWIN_USE_VENDORED_LIBWESTON=$QDWIN_USE_VENDORED_LIBWESTON
  export QDWIN_COMPOSITOR_LOG=/tmp/weston-proto-test.log
  # QDWIN_WESTON_LOG is what test_v19_register_hotkey_live reads; without it
  # that sub-test SKIPs. Point it at the same log so the in-VM run really
  # does exercise all 12 sub-tests.
  export QDWIN_WESTON_LOG=/tmp/weston-proto-test.log
  cd /home/admin/qdwin-test
  python3 test_zwlr_layer_shell.py wayland-77
"
RC=$?

# Kill weston.
kill $WPID 2>/dev/null
wait $WPID 2>/dev/null

echo ""
echo "=== weston log (zwlr lines) ==="
grep -nE 'zwlr_layer|qdwin: layer' /tmp/weston-proto-test.log || echo "(none)"
echo ""
echo "=== test exit=$RC ==="
exit $RC
