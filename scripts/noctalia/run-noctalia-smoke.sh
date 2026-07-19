#!/bin/bash
# Phase 1.8: smoke test that noctalia-shell speaks zwlr_layer_shell_v1
# correctly against qdwin. Goal is protocol acceptance, not visual
# correctness — the bar should appear, no zwlr_layer_* errors in
# weston log, no crash for the test duration.
set -u
set +e

DURATION="${DURATION:-15}"   # seconds

# Opt-in: load the qdistro-vendored libweston-16 (NULL-parent xdg_popup
# patch) for this run. The .so must already be deployed under
# $QDWIN_VENDORED_LIBWESTON_PREFIX (default: /usr/libexec/qdistro/qdwin-libweston).
# Compositor lib + matching backend module both need to be present.
QDWIN_USE_VENDORED_LIBWESTON="${QDWIN_USE_VENDORED_LIBWESTON:-0}"
QDWIN_VENDORED_LIBWESTON_PREFIX="${QDWIN_VENDORED_LIBWESTON_PREFIX:-/usr/libexec/qdistro/qdwin-libweston}"
WESTON_LD_PREFIX=""
if [ "$QDWIN_USE_VENDORED_LIBWESTON" = "1" ]; then
    if [ ! -f "$QDWIN_VENDORED_LIBWESTON_PREFIX/lib64/libweston-16.so.0.0.0" ]; then
        echo "ERROR: QDWIN_USE_VENDORED_LIBWESTON=1 but no vendored .so at $QDWIN_VENDORED_LIBWESTON_PREFIX/lib64" >&2
        exit 2
    fi
    WESTON_LD_PREFIX="LD_LIBRARY_PATH=$QDWIN_VENDORED_LIBWESTON_PREFIX/lib64\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH} "
    echo "noctalia-smoke: using vendored libweston at $QDWIN_VENDORED_LIBWESTON_PREFIX"
fi

export XDG_RUNTIME_DIR=/run/user/1000
mkdir -p $XDG_RUNTIME_DIR
chown admin:users $XDG_RUNTIME_DIR
chmod 700 $XDG_RUNTIME_DIR

rm -f $XDG_RUNTIME_DIR/wayland-* 2>/dev/null
rm -f /tmp/weston-noct.log /tmp/noctalia.log

runuser -u admin -- bash -c "
  export XDG_RUNTIME_DIR=/run/user/1000
  export WLD=wayland-66
  export WAYLAND_DISPLAY=wayland-66
  exec ${WESTON_LD_PREFIX}weston \
    --config=/home/admin/weston.ini \
    --socket=\$WLD \
    > /tmp/weston-noct.log 2>&1
" &
WPID=$!
echo "weston pid=$WPID"

for i in 1 2 3 4 5 6 7 8 9 10; do
  if [ -e $XDG_RUNTIME_DIR/wayland-66 ]; then
    echo "weston socket ready after ${i}s"
    break
  fi
  sleep 1
done
if [ ! -e $XDG_RUNTIME_DIR/wayland-66 ]; then
  echo "ERROR: weston socket not appearing"
  cat /tmp/weston-noct.log
  exit 1
fi

# Run noctalia under dbus-run-session for the same reason waybar
# needs it. WAYLAND_DEBUG off — we want runtime behaviour, not the
# protocol trace.
runuser -u admin -- bash -c "
  export XDG_RUNTIME_DIR=/run/user/1000
  export WAYLAND_DISPLAY=wayland-66
  export QML_DISABLE_DISK_CACHE=1
  timeout ${DURATION} dbus-run-session -- \
    qs -p /usr/share/quickshell/noctalia-shell \
    > /tmp/noctalia.log 2>&1
"
NOCT_RC=$?
echo "qs exit=$NOCT_RC (124 means timeout — expected; >0 non-124 means crash)"

kill $WPID 2>/dev/null
wait $WPID 2>/dev/null

echo ""
echo "=== weston.log: zwlr / qdwin layer-shell lines ==="
grep -nE 'zwlr_layer|qdwin: layer' /tmp/weston-noct.log | head -50
echo ""
echo "=== noctalia.log: errors / criticals ==="
grep -nE 'error|critical|fatal|FAIL|protocol|invalid' /tmp/noctalia.log | head -40 || echo "(none)"
echo ""
echo "=== noctalia.log first 30 lines ==="
head -30 /tmp/noctalia.log
echo ""
echo "=== final exit ==="
# Pass criteria:
# - qs exited via timeout (124) — i.e. did not crash within DURATION
# - weston log shows at least one "qdwin: layer-shell mapped" line
# - weston log shows NO protocol errors attributed to zwlr_layer_*
if [ "$NOCT_RC" = "124" ]; then
  if grep -q "qdwin: layer-shell mapped" /tmp/weston-noct.log; then
    if grep -E "zwlr_layer.*error|qdwin.*layer.*error" /tmp/weston-noct.log >/dev/null; then
      echo "FAIL: qs survived but zwlr protocol errors fired"
      exit 1
    fi
    echo "PASS: noctalia survived ${DURATION}s, ≥1 layer surface mapped, no zwlr errors"
    exit 0
  fi
  echo "FAIL: noctalia survived but no layer-surface mapped"
  exit 1
fi
echo "FAIL: qs crashed before timeout (rc=$NOCT_RC)"
exit 1
