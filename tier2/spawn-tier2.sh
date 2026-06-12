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
#   TIER2_SILO             Silo name for template binding resolution. When
#                          set, the image is the silo's active generation
#                          DIGEST resolved from
#                          /var/lib/qdistro/bindings/<silo>.toml (never the
#                          :latest tag), and the silo's state_path is
#                          bind-mounted read-write at /home/admin so state
#                          survives container restarts and generation flips.
#                          A binding-resolved launch is the ONLY launch that
#                          mounts real state, and a missing/non-dir
#                          state_path is a hard error (no tmpfs fallback).
#                          A silo with no binding runs untemplated (tmpfs
#                          home, no state); a non-digest binding is a hard
#                          error.
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
#   TIER2_DETACH=1         Supervised detach: emit the stdout contract,
#                          hand the wait + per-container-dir cleanup to a
#                          setsid'd supervisor, and return 0 immediately
#                          (the named container keeps running for `podman
#                          exec`; the dir is cleaned only after it exits).
#                          NOT `podman run -d` (that tears the secctx tag
#                          down before the inner weston connects). Default
#                          foreground; container exit propagates to this
#                          script's exit code.
#   TIER2_DEBUG=1          Echo the resolved podman command before running.
#
# Hardening knobs (defaults are the secure choice; relax for special
# workloads only):
#   TIER2_NETWORK          podman --network value. Default "none". Use
#                          "slirp4netns" for workloads that need outbound
#                          (e.g. browser).
#   TIER2_PIDS_LIMIT       Default 512. Override for fork-heavy apps.
#                          (`pids` is the only cgroup v2 controller
#                          delegated to admin's user slice by default
#                          on Tumbleweed, so this is the only resource
#                          knob that works without root cooperation.)
#   TIER2_MEMORY           Default "" (no limit). Set to e.g. "512m"
#                          to cap. Requires the `memory` cgroup
#                          controller to be delegated to the user slice
#                          — it isn't by default on Tumbleweed. To
#                          enable, root needs to drop in:
#                              [Service]
#                              Delegate=memory cpu pids io
#                          on user@1000.service. Without delegation, podman
#                          errors on memory.swap.max. When you do set
#                          TIER2_MEMORY, we pair it with --memory-swap=<same>
#                          to disable swap accounting.
#   TIER2_CPUS             Default "" (no limit). Same delegation
#                          requirement as TIER2_MEMORY (cpu controller).
#   TIER2_KEEP_CAPS        Comma list of capabilities to KEEP. Default
#                          empty (--cap-drop=ALL). Avoid unless you
#                          really know what the workload needs.
#   TIER2_ALLOW_PRIVESC=1  Drop --security-opt=no-new-privileges. Almost
#                          never wanted; setuid binaries inside the image
#                          can re-gain privileges.
#
# Isolation model:
#   - Per-container runtime dir at $XDG_RUNTIME_DIR/qdistro-tier2/<token>/
#     is the only thing bound into the container's /run/user/<uid>.
#     The host's full /run/user is NOT exposed — sibling tier-2
#     containers can't see each other's wayland sockets and the
#     container has no path to user dbus, pulse, gpg-agent, ssh-agent,
#     etc. Trade-off: workloads that legitimately need pipewire audio
#     or org.freedesktop.portal.Desktop must explicitly opt in via a
#     future TIER2_PORTAL_* knob (not implemented).
#   - The resolved outer wayland socket (or wayland-secctx-NN if
#     wrapping with secctx-exec) is bind-mounted as a single file into
#     the per-container dir, so the inner weston publisher can connect
#     to qdwin and only that.
#   - Per-container dir is rm -rf'd on script exit (trap).
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
LAUNCHREC_PATH=""
LAUNCHREC_TOKEN=""
LAUNCHREC_FILE_ID=""

# Hardening defaults — secure, override via env for special workloads.
TIER2_NETWORK_VAL="${TIER2_NETWORK:-none}"
TIER2_PIDS_LIMIT_VAL="${TIER2_PIDS_LIMIT:-512}"
TIER2_MEMORY_VAL="${TIER2_MEMORY:-}"
TIER2_CPUS_VAL="${TIER2_CPUS:-}"
TIER2_KEEP_CAPS_VAL="${TIER2_KEEP_CAPS:-}"
TIER2_ALLOW_PRIVESC_VAL="${TIER2_ALLOW_PRIVESC:-0}"

# Launch token: stable identifier the outer qdwin sees in
# wp_security_context_v1.instance_id. qdshell uses this to swap its
# placeholder taskbar entry for the real one when toplevel_added
# arrives. Cheap entropy is fine; this is correlation, not auth.
# Always 32 lowercase hex chars — the orphan-dir reaper filters on
# `^[0-9a-f]{32}$` to ignore podman's "<no value>" sentinel and any
# other label noise.
# shellcheck source=../lib/spawn-common.sh
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s\n' "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
SPAWN_COMMON="$SCRIPT_DIR/../lib/spawn-common.sh"
if [ ! -r "$SPAWN_COMMON" ] && [ -r /usr/lib/qdistro/spawn-common.sh ]; then
    SPAWN_COMMON=/usr/lib/qdistro/spawn-common.sh
fi
if [ ! -r "$SPAWN_COMMON" ]; then
    echo "spawn-tier2: spawn-common.sh not found (looked near $SCRIPT_DIR and /usr/lib/qdistro)" >&2
    exit 5
fi
. "$SPAWN_COMMON"
LAUNCH_TOKEN="$(gen_launch_token "spawn-tier2")"

# Custom seccomp profile — deny-by-default, allows only the ~145
# syscalls needed by bash/weston/weston-terminal/coreutils. Falls back
# to podman's default profile if no profile found on the auto-detection
# path (e.g. dev runs from a checkout that hasn't been installed). If
# TIER2_SECCOMP_PROFILE was explicitly set via env but the file is
# missing, we fail closed rather than silently dropping to the default.
_seccomp_explicit="${TIER2_SECCOMP_PROFILE:-}"
TIER2_SECCOMP_PROFILE="${TIER2_SECCOMP_PROFILE:-}"
if [ -z "$TIER2_SECCOMP_PROFILE" ]; then
    # Search order: dev-tree adjacent, installed /usr/lib, legacy /usr/local/share.
    for _seccomp_dir in \
        "$SCRIPT_DIR/seccomp" \
        "/usr/lib/qdistro/seccomp" \
        "/usr/local/share/qdistro/tier2/seccomp"; do
        if [ -f "$_seccomp_dir/${WORKLOAD}.json" ]; then
            TIER2_SECCOMP_PROFILE="$_seccomp_dir/${WORKLOAD}.json"
            break
        fi
    done
    if [ -z "$TIER2_SECCOMP_PROFILE" ]; then
        echo "spawn-tier2: WARN: no seccomp profile found for workload '$WORKLOAD'; using podman default" >&2
    fi
elif [ ! -f "$TIER2_SECCOMP_PROFILE" ]; then
    echo "spawn-tier2: FATAL: TIER2_SECCOMP_PROFILE=$_seccomp_explicit does not exist" >&2
    exit 2
fi

# --- pre-flight ---------------------------------------------------------
fail() { echo "spawn-tier2: $*" >&2; exit 2; }

command -v podman >/dev/null 2>&1 \
    || fail "podman not in PATH"

# --- template binding resolution (fableplan task 05) --------------------
# When this launch represents a templated silo (TIER2_SILO set), the image
# is the silo's active generation DIGEST resolved from its binding file —
# never the mutable :latest tag. This is the enforcement point for "a
# candidate is mechanically unable to launch against real state": the only
# image a binding can name is a promoted generation (qdistro-template-promote
# is the only writer of bindings), and a non-digest reference is a hard
# error with no tag fallback. A silo with no binding runs untemplated
# (today's tag-based behaviour), logged so coverage is visible.
TIER2_SILO="${TIER2_SILO:-}"
STATE_PATH=""
if [ -n "$TIER2_SILO" ]; then
    RESOLVER=()
    if command -v qdistro-resolve-binding >/dev/null 2>&1; then
        RESOLVER=(qdistro-resolve-binding)
    elif [ -f /usr/libexec/qdistro/qdistro-resolve-binding ]; then
        RESOLVER=(/usr/libexec/qdistro/qdistro-resolve-binding)
    elif [ -f "$SCRIPT_DIR/../templates/qdistro_resolve_binding.py" ]; then
        RESOLVER=(env "PYTHONPATH=$SCRIPT_DIR/../templates" python3 \
                  "$SCRIPT_DIR/../templates/qdistro_resolve_binding.py")
    fi
    [ "${#RESOLVER[@]}" -gt 0 ] \
        || fail "TIER2_SILO=$TIER2_SILO set but qdistro-resolve-binding not found"
    # ONE binding read: --launch-env emits GENERATION/TEMPLATE/STATE_PATH/
    # FIRST_ACTIVATION as KEY=VALUE lines (no TOML parsing in bash, no second
    # read racing a concurrent promote). --record commits the per-boot status
    # + activation marker under the current ordering.
    launch_env="$("${RESOLVER[@]}" "$TIER2_SILO" --record --launch-env)"
    resolve_rc=$?
    case "$resolve_rc" in
        0)  resolved_gen=""
            while IFS='=' read -r _k _v; do
                case "$_k" in
                    GENERATION)  resolved_gen="$_v" ;;
                    STATE_PATH)  STATE_PATH="$_v" ;;
                esac
            done <<< "$launch_env"
            # Defence in depth: never trust resolver stdout shape — only an
            # exact sha256 digest may become the launch image.
            if [[ ! "$resolved_gen" =~ ^sha256:[0-9a-f]{64}$ ]]; then
                fail "resolver returned a non-digest for silo $TIER2_SILO: '$resolved_gen'"
            fi
            IMAGE="$resolved_gen"
            # A binding-resolved launch is the ONLY launch that mounts real
            # state, and it must mount it: a missing/non-dir state_path is a
            # hard error, never a silent tmpfs home (losing a session's state
            # is worse than refusing). The state tree is created by promote;
            # spawn-tier2 only verifies.
            [ -n "$STATE_PATH" ] \
                || fail "resolver returned no STATE_PATH for templated silo $TIER2_SILO"
            [ -d "$STATE_PATH" ] \
                || fail "state_path $STATE_PATH for silo $TIER2_SILO is missing or not a directory — refusing to launch a templated silo without its state"
            echo "spawn-tier2: silo $TIER2_SILO resolved to generation $IMAGE (state=$STATE_PATH)" >&2 ;;
        3)  echo "spawn-tier2: silo $TIER2_SILO runs UNTEMPLATED (no binding); using $IMAGE" >&2 ;;
        *)  fail "binding resolution failed for silo $TIER2_SILO (rc=$resolve_rc) — refusing to launch (no tag fallback)" ;;
    esac
fi

if ! podman image exists "$IMAGE" 2>/dev/null; then
    if [ -n "$TIER2_SILO" ]; then
        fail "resolved generation $IMAGE for silo $TIER2_SILO is not present in the image store"
    fi
    fail "image $IMAGE not present; run tier2/make-tier2-image.sh $WORKLOAD"
fi

if [ ! -S "$RUNTIME_DIR/$OUTER_DISPLAY" ]; then
    fail "outer wayland socket not found at $RUNTIME_DIR/$OUTER_DISPLAY"
fi

if [ ! -f "$QDWIN_SHELL_SO" ]; then
    fail "qdwin-shell.so not found at $QDWIN_SHELL_SO (set TIER2_QDWIN_SHELL_SO)"
fi

# --- per-container runtime dir + cleanup trap ----------------------------
# This is the load-bearing isolation step: the container only sees an
# initially-empty /run/user/<uid>, so dbus, pulse, gpg-agent, ssh-agent
# and sibling tier-2 wayland sockets are all invisible to it. The single
# resolved outer wayland socket is bind-mounted on top by the post-secctx
# wrapper below.
PARENT_DIR="$RUNTIME_DIR/qdistro-tier2"
PERCONT_DIR="$PARENT_DIR/$LAUNCH_TOKEN"

# Reap orphan per-container dirs from prior spawns that died without
# running their EXIT trap (segfault, kill -9, host crash). Use `podman
# ps -a` so containers in Exited / Created / Stopping that haven't been
# auto-removed yet still count as "live" — we don't want to rm a dir
# while podman still has a record of the container. Filter the label
# set to 32-hex-char tokens to ignore podman's "<no value>" sentinel
# for unlabeled containers.
if [ -d "$PARENT_DIR" ]; then
    live_tokens=$(podman ps -a --format '{{.Labels.qdistro_tier2_token}}' 2>/dev/null \
                    | grep -E '^[0-9a-f]{32}$' \
                    | sort -u || true)
    for d in "$PARENT_DIR"/*/; do
        [ -d "$d" ] || continue
        token=$(basename "$d")
        case " $live_tokens " in
            *" $token "*) ;;
            *) rm -rf "$d" 2>/dev/null || true ;;
        esac
    done
fi

mkdir -p "$PERCONT_DIR"
chmod 0700 "$PERCONT_DIR"

# Cleanup runs from both the EXIT trap (covers pre-flight `fail`s and
# the explicit call after the wrapper returns below) and the orphan-
# reaper on the next spawn (covers `kill -9` of this script and any
# crash that bypasses the trap). Keep cleanup_percont idempotent so
# both paths can fire safely.
cleanup_percont() {
    rm -rf "$PERCONT_DIR" 2>/dev/null || true
    rmdir "$PARENT_DIR" 2>/dev/null || true
}
trap cleanup_percont EXIT

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
export TIER2_PERCONT_DIR="$PERCONT_DIR"
export TIER2_ADMIN_UID_RESOLVED="$ADMIN_UID"
export TIER2_IMAGE="$IMAGE"
export TIER2_CONTAINER="$CONTAINER"
export TIER2_QDWIN_SHELL_SO_RESOLVED="$QDWIN_SHELL_SO"
export TIER2_STATE_PATH_RESOLVED="$STATE_PATH"
export TIER2_NETWORK_RESOLVED="$TIER2_NETWORK_VAL"
export TIER2_PIDS_LIMIT_RESOLVED="$TIER2_PIDS_LIMIT_VAL"
export TIER2_MEMORY_RESOLVED="$TIER2_MEMORY_VAL"
export TIER2_CPUS_RESOLVED="$TIER2_CPUS_VAL"
export TIER2_KEEP_CAPS_RESOLVED="$TIER2_KEEP_CAPS_VAL"
export TIER2_ALLOW_PRIVESC_RESOLVED="$TIER2_ALLOW_PRIVESC_VAL"
export TIER2_SECCOMP_PROFILE_RESOLVED="$TIER2_SECCOMP_PROFILE"
APP_ARGV_JOINED="$(printf '%q ' "${APP_ARGV[@]}")"
export TIER2_APP_ARGV_JOINED="$APP_ARGV_JOINED"

# --- emit correlation metadata to stdout BEFORE exec --------------------
echo "LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "CONTAINER=$CONTAINER"
echo "IMAGE=$IMAGE"
echo "APP_ID=$SECCTX_APPID"

# --- post-secctx wrapper -------------------------------------------------
# qdistro-secctx-exec rewrites WAYLAND_DISPLAY in the child env BEFORE
# the inner command runs. We need that rewritten value to construct the
# single-socket bind, so the bind args are computed inside this bash -c
# block (not at script-prepare time). All TIER2_*_RESOLVED vars are
# exported above so the inner shell sees them through the secctx-exec
# fork without needing argv passthrough.
WRAPPER_BODY='
set -euo pipefail
RUNTIME="$XDG_RUNTIME_DIR"
DISPLAY_NAME="$WAYLAND_DISPLAY"
OUTER_SOCKET_PATH="$RUNTIME/$DISPLAY_NAME"
if [ ! -S "$OUTER_SOCKET_PATH" ]; then
    echo "spawn-tier2-wrapper: outer socket $OUTER_SOCKET_PATH missing" >&2
    exit 4
fi

# Prepare stub socket files inside the per-container dir; podman binds
# the host socket OVER each stub. Without the stub the bind target
# doesn'"'"'t exist in the per-container dir tree the container sees.
INNER_SOCK="$TIER2_PERCONT_DIR/$DISPLAY_NAME"
: > "$INNER_SOCK"
chmod 0600 "$INNER_SOCK"

# pipewire socket: the inner weston'"'"'s pipewire-backend connects to
# the host pipewire daemon to publish per-toplevel pw_streams (the
# pixel feed the outer qdwin consumes via qdistro-nested-pixelfeed).
# Optional — workloads that don'"'"'t need pixel output (e.g. headless
# CLI) work without it. We bind whichever pipewire-N sockets exist
# at spawn time.
PIPEWIRE_BINDS=()
for pw in "$RUNTIME"/pipewire-[0-9]*; do
    [ -e "$pw" ] || continue
    base=$(basename "$pw")
    stub="$TIER2_PERCONT_DIR/$base"
    : > "$stub"
    chmod 0600 "$stub"
    PIPEWIRE_BINDS+=( -v "$pw:/run/user/${TIER2_ADMIN_UID_RESOLVED}/$base:rw" )
done

# Build cap/no-new-privs/network/limits args. Rationale per option in
# the script header'"'"'s "Hardening knobs" section.
PODMAN_HARDENING=(
    --cap-drop=ALL
    --network="$TIER2_NETWORK_RESOLVED"
    --pids-limit="$TIER2_PIDS_LIMIT_RESOLVED"
    # IPC and PID namespaces are private by podman default for a fresh
    # `podman run`, but stating them keeps intent explicit and survives
    # podman default changes.
    --ipc=private
    --pid=private
    # Block setuid escalation inside the container.
    # Read-only image rootfs + small tmpfs scratch dirs. Writes inside
    # the container land in tmpfs (lost on container exit) or in the
    # per-container runtime dir (cleaned by the host trap). Any
    # attempt to persist into the image rootfs returns ENOSPC, which
    # is the security property we want.
    --read-only
    --tmpfs=/tmp:size=64m,mode=1777
    --mount type=tmpfs,destination=/var/cache,tmpfs-size=16m,tmpfs-mode=0755,U
)
# Templated-silo persistent state. ONLY a binding-resolved launch mounts
# real state (TIER2_STATE_PATH_RESOLVED is empty for candidate/validate/
# untemplated launches — they keep the tmpfs home). The state bind is
# emitted BEFORE the /home/admin/.cache tmpfs so .cache layers on top of it
# (podman applies overlapping mounts parent-first). No `:U` and no chown:
# state_path is admin-owned on the host and the container runs
# --userns=keep-id, so the uids already line up; `:U` on persistent state
# would rewrite ownership of a real home and is forbidden.
if [ -n "${TIER2_STATE_PATH_RESOLVED:-}" ]; then
    PODMAN_HARDENING+=( -v "$TIER2_STATE_PATH_RESOLVED:/home/admin:rw" )
fi
PODMAN_HARDENING+=(
    --mount type=tmpfs,destination=/home/admin/.cache,tmpfs-size=32m,tmpfs-mode=0700,U
    --tmpfs=/run:size=4m,mode=0755
)
# --memory and --cpus only when explicitly requested — both require
# delegation of the corresponding cgroup v2 controller to admin'"'"'s
# user slice (see header). Without it the container fails to start.
# --memory pairs with --memory-swap=<same> to disable swap accounting.
if [ -n "$TIER2_MEMORY_RESOLVED" ]; then
    PODMAN_HARDENING+=( --memory="$TIER2_MEMORY_RESOLVED" )
    PODMAN_HARDENING+=( --memory-swap="$TIER2_MEMORY_RESOLVED" )
fi
if [ -n "$TIER2_CPUS_RESOLVED" ]; then
    PODMAN_HARDENING+=( --cpus="$TIER2_CPUS_RESOLVED" )
fi
if [ "$TIER2_ALLOW_PRIVESC_RESOLVED" != "1" ]; then
    PODMAN_HARDENING+=( --security-opt=no-new-privileges )
fi
if [ -n "$TIER2_SECCOMP_PROFILE_RESOLVED" ]; then
    if [ -f "$TIER2_SECCOMP_PROFILE_RESOLVED" ]; then
        PODMAN_HARDENING+=( "--security-opt=seccomp=$TIER2_SECCOMP_PROFILE_RESOLVED" )
    else
        echo "spawn-tier2-wrapper: WARN: seccomp profile $TIER2_SECCOMP_PROFILE_RESOLVED disappeared; using podman default" >&2
    fi
fi
if [ -n "$TIER2_KEEP_CAPS_RESOLVED" ]; then
    IFS="," read -ra _caps <<< "$TIER2_KEEP_CAPS_RESOLVED"
    for c in "${_caps[@]}"; do
        PODMAN_HARDENING+=( --cap-add="$c" )
    done
fi

PODMAN_ARGS=(
    run
    --name "$TIER2_CONTAINER"
    --rm
    --userns=keep-id
    --user "${TIER2_ADMIN_UID_RESOLVED}:${TIER2_ADMIN_UID_RESOLVED}"
    # Label so the orphan-dir reaper in the next spawn can tell which
    # per-container dirs still belong to a live container.
    --label "qdistro_tier2_token=$QDWIN_LAUNCH_TOKEN"
    "${PODMAN_HARDENING[@]}"
    -v "$TIER2_PERCONT_DIR:/run/user/${TIER2_ADMIN_UID_RESOLVED}:rw"
    -v "$OUTER_SOCKET_PATH:/run/user/${TIER2_ADMIN_UID_RESOLVED}/$DISPLAY_NAME:rw"
    "${PIPEWIRE_BINDS[@]}"
    -v "$TIER2_QDWIN_SHELL_SO_RESOLVED:/usr/lib64/weston/qdwin-shell.so:ro"
    -e "XDG_RUNTIME_DIR=/run/user/${TIER2_ADMIN_UID_RESOLVED}"
    -e WAYLAND_DISPLAY
    # qdwin-shell.so reads QDWIN_OUTER_DISPLAY to know which outer
    # wayland to dial. spawn-tier2.sh set it to the pre-secctx name,
    # but the only socket we bind into the container is the secctx-
    # rewritten one ($WAYLAND_DISPLAY); align them.
    -e "QDWIN_OUTER_DISPLAY=$DISPLAY_NAME"
    -e QDWIN_NESTED_MODE
    -e QDWIN_LAUNCH_TOKEN
    -e TIER2_INNER_SOCKET
    "$TIER2_IMAGE"
)

# Re-tokenise app argv from the printf %q joined string the parent
# emitted. eval is safe here: every token went through %q.
eval "set -- $TIER2_APP_ARGV_JOINED"

[ "${TIER2_DEBUG:-0}" = "1" ] && \
    echo "+ podman ${PODMAN_ARGS[*]} $*" >&2

exec podman "${PODMAN_ARGS[@]}" "$@"
'

# NOTE: we don'"'"'t add `podman -d`. The secctx wrapper around podman
# closes its close_fd as soon as podman returns; `podman run -d`
# returns immediately, which tears the wp_security_context_v1 tag
# down before the inner weston has even connected to the outer.
# Callers that want non-blocking semantics should background the
# whole script instead (`bash spawn-tier2.sh ... &`).

# --- run --------------------------------------------------------------
# Run as a child (not exec) so the EXIT trap above runs cleanup_percont
# on every normal exit path. Without this, exec'ing replaces the
# script and the trap evaporates — the per-container dir would only
# get cleaned by the next spawn's orphan reaper, which is fine for
# crashes but not for one-shot single-spawn runs.
#
# Forward TERM/INT to the child so non-tty callers (systemd unit,
# qdshell launcher via Quickshell.execDetached) can still stop the
# container by signaling the script PID — without exec, script-PID
# is not container-PID and the signal otherwise dies in the bash
# parent. SIGINT from a TTY already broadcasts to the whole
# foreground process group, so this matters for the non-tty path.
forward_signal() {
    [ -n "${child_pid:-}" ] && kill -"$1" "$child_pid" 2>/dev/null || true
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT'  INT

if [ "$USE_SECCTX" = "1" ] && command -v qdistro-secctx-exec >/dev/null 2>&1; then
    if [ "$(id -u)" -ne 0 ] && [ "${QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED:-0}" != "1" ]; then
        echo "spawn-tier2: WARN: secctx stamping requires a direct root launcher parent;" >&2
        echo "             running un-tagged. Use the root launcher/broker path, or set" >&2
        echo "             QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1 only with QDWIN_SECCTX_OPEN=1 for dev tests." >&2
        bash -c "$WRAPPER_BODY" &
    else
        LAUNCHREC_TOKEN="$(gen_launch_token "spawn-tier2")"
        LAUNCHREC_FILE_ID="$(gen_launch_token "spawn-tier2")"
        LAUNCHREC_PATH="$RUNTIME_DIR/qdistro-tier2-launchrec-$LAUNCHREC_FILE_ID.pid"
        rm -f "$LAUNCHREC_PATH"
        QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
        QDISTRO_LAUNCH_RECORD_PATH="$LAUNCHREC_PATH" \
        QDISTRO_LAUNCH_RECORD_TOKEN="$LAUNCHREC_TOKEN" \
        qdistro-secctx-exec \
            --sandbox-engine "$ENGINE" \
            --app-id "$SECCTX_APPID" \
            --instance-id "$LAUNCH_TOKEN" \
            -- bash -c "$WRAPPER_BODY" &
    fi
else
    if [ "$USE_SECCTX" = "1" ]; then
        echo "spawn-tier2: WARN: qdistro-secctx-exec not in PATH; running un-tagged" >&2
    fi
    bash -c "$WRAPPER_BODY" &
fi
child_pid=$!

if [ -n "$LAUNCHREC_PATH" ]; then
    qd_register_secctx_launch_record \
        "${TIER2_SILO:-$CONTAINER}" "$ENGINE" "$SECCTX_APPID" "$LAUNCH_TOKEN" "tier2" \
        "$LAUNCHREC_PATH" "$LAUNCHREC_TOKEN" "tier2"
fi

# --- detached test mode (TIER2_DETACH=1) -------------------------------
# Supervised detach (codex r1/r2): NOT `podman run -d` — under secctx-exec
# that returns before the inner weston connects (tearing down the
# wp_security_context_v1 tag), and the EXIT trap would rm the per-container
# dir while the container still needs it. Instead, the stdout contract
# (LAUNCH_TOKEN/CONTAINER/IMAGE/APP_ID) has ALREADY been emitted above, the
# wrapper chain is running as $child_pid, and here we hand the wait +
# deferred cleanup to a setsid'd supervisor and return now. The supervisor
# removes the per-container dir ONLY after the container (child) exits, so a
# `podman exec`-ing test harness sees a stable, live container. The container
# name is the stable silo-derived $CONTAINER, and the secctx tag on the outer
# connection is unchanged (it was stamped by the still-running wrapper chain).
if [ "${TIER2_DETACH:-0}" = "1" ]; then
    trap - EXIT TERM INT   # the supervisor owns cleanup now, not this shell
    setsid bash -c '
        cpid="'"$child_pid"'"
        # Poll, not wait: $cpid is this process'"'"'s parent-shell child, not
        # ours, so wait(2) cannot reap it; kill -0 sees it until it exits.
        while kill -0 "$cpid" 2>/dev/null; do sleep 0.5; done
        rm -rf "'"$PERCONT_DIR"'" 2>/dev/null || true
        rmdir "'"$PARENT_DIR"'" 2>/dev/null || true
    ' >/dev/null 2>&1 &
    disown
    echo "spawn-tier2: detached (container=$CONTAINER token=$LAUNCH_TOKEN)" >&2
    exit 0
fi

# `wait` returns 128+signo when a trap interrupts it — the child may
# still be alive (mid-podman-teardown). Loop until the child actually
# exits so cleanup_percont (EXIT trap) can't rm the per-container dir
# while podman still has bind-mounts into it.
while kill -0 "$child_pid" 2>/dev/null; do
    wait "$child_pid" 2>/dev/null || true
done
wait "$child_pid" 2>/dev/null
rc=$?
exit "$rc"
