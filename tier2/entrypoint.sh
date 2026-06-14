#!/bin/bash
# qdistro-tier2-entrypoint — in-container PID 1.
#
# Starts the inner weston (with qdwin-shell.so in nested-publisher
# mode), waits for the inner socket, then exec's the inner app.
#
# Args: <app> [app-args...]    e.g. weston-terminal
#
# Env (set by host spawn-tier2.sh via `podman -e`):
#   XDG_RUNTIME_DIR        = /run/user/1000  (bind-mounted from host)
#   WAYLAND_DISPLAY        = wayland-secctx-NN  (host outer, via qdistro-secctx-exec)
#   QDWIN_NESTED_MODE      = 1
#   QDWIN_OUTER_DISPLAY    = wayland-secctx-NN  (same as WAYLAND_DISPLAY)
#   QDWIN_LAUNCH_TOKEN     = opaque token from host spawn; logged for correlation
#   TIER2_INNER_SOCKET     = wayland-tier2     (inner weston's own server socket)
set -u

INNER_SOCKET="${TIER2_INNER_SOCKET:-wayland-tier2}"
TOKEN="${QDWIN_LAUNCH_TOKEN:-no-token}"

log() { printf '[tier2-entrypoint pid=%d token=%s] %s\n' "$$" "$TOKEN" "$*" >&2; }

if [ "$#" -lt 1 ]; then
    log "FATAL: no inner app argv passed"
    exit 2
fi

# The XDG runtime dir from the host has the outer wayland socket
# (e.g. wayland-1 or wayland-secctx-NN). The inner weston wants its
# own server socket name; we use $INNER_SOCKET inside the same
# runtime dir. qdwin-shell.so's nested-mode publisher reaches the OUTER
# over its OWN wl_display connection to $QDWIN_OUTER_DISPLAY (not via a
# wayland-backend — the inner weston runs a pipewire-only backend, see
# weston.ini). The inner app's WAYLAND_DISPLAY points at the INNER.
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    log "FATAL: XDG_RUNTIME_DIR not set"
    exit 2
fi

# Inner weston is the publisher; its own client connection to the
# outer carries the qdwin_nested_manager_v1 + secctx tag.
#
# Stale-lock cleanup: weston creates /run/user/<uid>/<socket>.lock at
# startup and refuses to start if it already exists. Because XDG
# runtime dir is bind-mounted from the host, prior tier-2 containers
# may have left a stale lock+socket pair behind. Remove both before
# starting; they belong to this (inner) weston only.
rm -f "$XDG_RUNTIME_DIR/$INNER_SOCKET" "$XDG_RUNTIME_DIR/$INNER_SOCKET.lock" 2>/dev/null || true

log "starting inner weston: socket=$INNER_SOCKET outer=$WAYLAND_DISPLAY"
weston \
    --socket="$INNER_SOCKET" \
    >"$XDG_RUNTIME_DIR/tier2-weston-$TOKEN.log" 2>&1 &
WESTON_PID=$!

# Wait up to 5s for the inner socket to appear.
for _ in $(seq 1 50); do
    if [ -S "$XDG_RUNTIME_DIR/$INNER_SOCKET" ]; then
        break
    fi
    sleep 0.1
done

if [ ! -S "$XDG_RUNTIME_DIR/$INNER_SOCKET" ]; then
    log "FATAL: inner weston did not create socket within 5s"
    log "weston log:"
    sed 's/^/[weston] /' "$XDG_RUNTIME_DIR/tier2-weston-$TOKEN.log" >&2 || true
    kill "$WESTON_PID" 2>/dev/null || true
    exit 3
fi

log "inner weston up; exec'ing app: $*"

# Hand control to the app. The app uses the inner display; the
# inner weston is left as a background process that we don't trap —
# when the app exits, the container exits (this PID is 1), which
# tears down inner weston via SIGKILL.
WAYLAND_DISPLAY="$INNER_SOCKET" exec "$@"
