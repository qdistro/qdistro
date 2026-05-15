#!/bin/bash
# §Phase-7 tier-2 — start a rootless podman container that hosts a
# nested weston + qdwin-shell.so publisher, and exec a guest app
# inside it. Each inner xdg_toplevel is advertised to the outer qdwin
# via qdwin_nested_manager_v1, where it becomes a regular peer
# toplevel (chrome, focus, broker gates — same as any other tier).
#
# Usage:
#   spawn-tier2.sh <container_name> <workload> -- <app> [app-args...]
#
# Example:
#   spawn-tier2.sh tier2-c1 weston-terminal -- weston-terminal
#
# <workload> selects the image: qdistro/tier2-<workload>:latest, built
# by tier2/make-tier2-image.sh.
#
# Env knobs:
#   TIER2_ADMIN_UID        Admin uid; default $(id -u) (usually 1000).
#   TIER2_OUTER_DISPLAY    Outer Wayland socket basename in
#                          $XDG_RUNTIME_DIR. Default $WAYLAND_DISPLAY,
#                          else "wayland-1". Overridden in-process when
#                          wrapping with qdistro-secctx-exec.
#   TIER2_USE_SECCTX       Default 1 — wrap podman with
#                          qdistro-secctx-exec so the nested weston's
#                          outer connection carries
#                          sandbox_engine=qdistro.tier2,
#                          app_id=<container>/<app>,
#                          instance_id=<launch-token>.
#   TIER2_SECCTX_ENGINE    Override sandbox_engine (default qdistro.tier2).
#   TIER2_SECCTX_APPID     Override app_id (default <container>/<app>).
#   TIER2_QDWIN_SHELL_SO   Host path to the qdwin-shell.so to bind-mount
#                          into the container at /usr/lib64/weston/.
#                          Default /usr/lib64/weston/qdwin-shell.so.
#   TIER2_DETACH=1         Run podman with -d (detached). Default
#                          foreground; container exit propagates to
#                          this script's exit code.
#   TIER2_DEBUG=1          Echo the resolved podman command before running.
#
# stdout (machine-parseable; one key=value per line, emitted before
# exec so qdshell can correlate the eventual toplevel_added):
#   LAUNCH_TOKEN=<32hex>
#   CONTAINER=<container_name>
#   IMAGE=<image>
#   APP_ID=<sandbox-app-id>
set -uo pipefail

usage() {
    sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage; exit 0
fi
if [ "$#" -lt 4 ]; then
    usage >&2; exit 1
fi

CONTAINER="$1"; shift
WORKLOAD="$1"; shift
if [ "$1" != "--" ]; then
    echo "spawn-tier2: expected '--' before app argv, got '$1'" >&2
    usage >&2; exit 1
fi
shift
if [ "$#" -lt 1 ]; then
    echo "spawn-tier2: app argv missing after '--'" >&2
    usage >&2; exit 1
fi
APP_ARGV=("$@")
APP_NAME="${APP_ARGV[0]##*/}"

ADMIN_UID="${TIER2_ADMIN_UID:-$(id -u)}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$ADMIN_UID}"
# Export so qdistro-secctx-exec (which checks the actual env, not just
# our computed defaults) and podman both see it. Useful when this
# script is invoked via `runuser -u admin -- bash …` which can strip
# the parent shell's runtime-dir env.
export XDG_RUNTIME_DIR="$RUNTIME_DIR"
OUTER_DISPLAY="${TIER2_OUTER_DISPLAY:-${WAYLAND_DISPLAY:-wayland-1}}"
QDWIN_SHELL_SO="${TIER2_QDWIN_SHELL_SO:-/usr/lib64/weston/qdwin-shell.so}"
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
USE_SECCTX="${TIER2_USE_SECCTX:-1}"
ENGINE="${TIER2_SECCTX_ENGINE:-qdistro.tier2}"
SECCTX_APPID="${TIER2_SECCTX_APPID:-${CONTAINER}/${APP_NAME}}"

# Launch token: stable identifier the outer qdwin sees in
# wp_security_context_v1.instance_id. qdshell uses this to swap its
# placeholder taskbar entry for the real one when toplevel_added
# arrives. Cheap entropy is fine; this is correlation, not auth.
LAUNCH_TOKEN="$(head -c 16 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' || echo $$-$(date +%s%N))"

# --- pre-flight ---------------------------------------------------------
fail() { echo "spawn-tier2: $*" >&2; exit 2; }

command -v podman >/dev/null 2>&1 \
    || fail "podman not in PATH"

if ! podman image exists "$IMAGE" 2>/dev/null; then
    fail "image $IMAGE not present; run tier2/make-tier2-image.sh $WORKLOAD"
fi

if [ ! -S "$RUNTIME_DIR/$OUTER_DISPLAY" ]; then
    fail "outer wayland socket not found at $RUNTIME_DIR/$OUTER_DISPLAY"
fi

if [ ! -f "$QDWIN_SHELL_SO" ]; then
    fail "qdwin-shell.so not found at $QDWIN_SHELL_SO (set TIER2_QDWIN_SHELL_SO)"
fi

# Make WAYLAND_DISPLAY visible to podman's `-e WAYLAND_DISPLAY` (no
# value) forwarding. If we go through qdistro-secctx-exec next, the
# wrapper rewrites WAYLAND_DISPLAY in our child env to wayland-secctx-NN
# before exec'ing the rest of the chain — podman then forwards that
# rewritten value into the container, so the inner weston's
# nested-mode publisher connects via the tagged listener.
export WAYLAND_DISPLAY="$OUTER_DISPLAY"
export QDWIN_OUTER_DISPLAY="$OUTER_DISPLAY"
export QDWIN_NESTED_MODE=1
export QDWIN_LAUNCH_TOKEN="$LAUNCH_TOKEN"
export TIER2_INNER_SOCKET="wayland-tier2"

# `podman -e VAR` (no `=`) forwards VAR from the host env into the
# container. The host-env values for WAYLAND_DISPLAY / QDWIN_*
# are set above and may be rewritten by qdistro-secctx-exec.
PODMAN_ARGS=(
    run
    --name "$CONTAINER"
    --rm
    --userns=keep-id
    --user "${ADMIN_UID}:${ADMIN_UID}"
    -v "${RUNTIME_DIR}:/run/user/${ADMIN_UID}:rw"
    -v "${QDWIN_SHELL_SO}:/usr/lib64/weston/qdwin-shell.so:ro"
    -e "XDG_RUNTIME_DIR=/run/user/${ADMIN_UID}"
    -e WAYLAND_DISPLAY
    -e QDWIN_OUTER_DISPLAY
    -e QDWIN_NESTED_MODE
    -e QDWIN_LAUNCH_TOKEN
    -e TIER2_INNER_SOCKET
    "$IMAGE"
    "${APP_ARGV[@]}"
)

# NOTE: we don't add `podman -d`. The secctx wrapper around podman
# closes its close_fd as soon as podman returns; `podman run -d`
# returns immediately, which tears the wp_security_context_v1 tag
# down before the inner weston has even connected to the outer.
# Callers that want non-blocking semantics should background the
# whole script instead (`bash spawn-tier2.sh ... &`).

# --- emit correlation metadata to stdout BEFORE exec --------------------
echo "LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "CONTAINER=$CONTAINER"
echo "IMAGE=$IMAGE"
echo "APP_ID=$SECCTX_APPID"

[ "${TIER2_DEBUG:-0}" = "1" ] && \
    echo "+ podman ${PODMAN_ARGS[*]}" >&2

# --- exec --------------------------------------------------------------
if [ "$USE_SECCTX" = "1" ] && command -v qdistro-secctx-exec >/dev/null 2>&1; then
    exec qdistro-secctx-exec \
        --sandbox-engine "$ENGINE" \
        --app-id "$SECCTX_APPID" \
        --instance-id "$LAUNCH_TOKEN" \
        -- podman "${PODMAN_ARGS[@]}"
else
    if [ "$USE_SECCTX" = "1" ]; then
        echo "spawn-tier2: WARN: qdistro-secctx-exec not in PATH; running un-tagged" >&2
    fi
    exec podman "${PODMAN_ARGS[@]}"
fi
