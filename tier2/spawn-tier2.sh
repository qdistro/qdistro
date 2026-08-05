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
#   QDISTRO_PROFILE        dev | daily-driver | release. Defaults to the
#                          hardened daily-driver posture. dev keeps direct
#                          admin/test launches available; hardened profiles
#                          require the root-launcher topology for secctx.
#
# Hardening knobs (defaults are the secure choice; relax for special
# workloads only):
#   TIER2_NETWORK          podman --network value. Default "none". Use
#                          "pasta" for workloads that need outbound
#                          (e.g. browser). Legacy "slirp4netns" is mapped
#                          to pasta with a WARN (Podman 6 removed slirp).
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

# --disposable: a throwaway tier-2 silo (07-disposables-plan P1). No
# container-name positional — the name is generated as disp-<workload>-<ts>;
# the home is tmpfs (no persist), the broker gate is qdistro.dispose.spawn:,
# and the secctx app_id is qdistro.disp.<token>.
DISPOSABLE=0
if [ "${1:-}" = "--disposable" ]; then
    DISPOSABLE=1; shift
fi

if [ "$DISPOSABLE" = 1 ]; then
    # disposable: <workload> -- <app> [args...]   (no container name)
    if [ "$#" -lt 3 ]; then usage >&2; exit 1; fi
    WORKLOAD="$1"; shift
    CONTAINER=""   # generated once WORKLOAD is validated (below)
else
    # persistent: <container> <workload> -- <app> [args...]
    if [ "$#" -lt 4 ]; then usage >&2; exit 1; fi
    CONTAINER="$1"; shift
    WORKLOAD="$1"; shift
fi
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

if [ -z "${QDISTRO_PROFILE:-}" ] && [ -r /etc/qdistro/profile ]; then
    # Disposable VM bootstraps persist QDISTRO_PROFILE=dev here so runuser-
    # launched integration probes keep the dev profile without test-specific
    # env plumbing. Real installs do not create this file and still default
    # to the hardened daily-driver profile.
    # shellcheck disable=SC1091
    . /etc/qdistro/profile
fi
QDISTRO_PROFILE="${QDISTRO_PROFILE:-daily-driver}"
case "$QDISTRO_PROFILE" in
    dev|daily-driver|release) ;;
    prod|production) QDISTRO_PROFILE=release ;;
    daily|dd) QDISTRO_PROFILE=daily-driver ;;
    *)
        echo "spawn-tier2: unknown QDISTRO_PROFILE=$QDISTRO_PROFILE (want dev|daily-driver|release)" >&2
        exit 2
        ;;
esac
export QDISTRO_PROFILE
is_hardened_profile() { [ "$QDISTRO_PROFILE" != "dev" ]; }

# --- root-launcher mode (secctx wire-tag provenance) ---------------------
# By default spawn-tier2 runs AS ADMIN (the qdistro-tier2-silo@.service unit
# uses User=admin, and qshell/PodApps launch it as admin too). In that
# topology qdistro-secctx-exec has no direct ROOT launcher parent, so qdwin's
# hardened secctx authorization refuses to bind the manager and the outer
# connection is UN-TAGGED on the wire (the wp_security_context_v1 app_id never
# reaches the compositor). See qdistro-tier2-silo@.service's "SECCTX
# PROVENANCE (known follow-up)" note.
#
# TIER2_ROOT_LAUNCHER=1 selects the proven tier-3 topology (see
# tier3/spawn-tier3.sh:442-463): a ROOT caller invokes spawn-tier2 as root,
# spawn-tier2 stays the root supervisor ONLY for the trusted-launcher
# parentage + the per-container dir bookkeeping, and EVERY rootless-podman
# touch (collision check, image-exists, orphan reaper, the final podman run)
# runs as admin via `runuser`. The secctx-exec helper is then a direct child
# of `runuser` (root) yet itself runs at the admin uid, satisfying BOTH
# qdistro-secctx-exec's trusted-launcher check AND qdwin's root-parent
# attestation — so the disposable's qdistro.disp.<token> app_id is stamped on
# the wire. Rootless podman keeps the admin --userns=keep-id state model
# intact (running podman as root would break it — that anti-pattern is
# explicitly forbidden).
ROOT_LAUNCHER=0
ADMIN_USER=""
if [ "${TIER2_ROOT_LAUNCHER:-0}" = "1" ]; then
    if [ "$(id -u)" -ne 0 ]; then
        echo "spawn-tier2: TIER2_ROOT_LAUNCHER=1 requires running as root" \
             "(it is the trusted launcher parent for qdistro-secctx-exec)" >&2
        exit 1
    fi
    # Resolve the admin uid we will drop podman to. Default 1000; never 0
    # (rootless podman + admin-owned state demand a non-root target).
    _root_admin_uid="${TIER2_ADMIN_UID:-1000}"
    if ! [[ "$_root_admin_uid" =~ ^[0-9]+$ ]] || [ "$_root_admin_uid" -eq 0 ]; then
        echo "spawn-tier2: TIER2_ROOT_LAUNCHER target uid '$_root_admin_uid'" \
             "is invalid (must be a non-root uid)" >&2
        exit 1
    fi
    ADMIN_USER="$(id -nu "$_root_admin_uid" 2>/dev/null || true)"
    [ -n "$ADMIN_USER" ] \
        || { echo "spawn-tier2: cannot resolve a user name for uid" \
                  "$_root_admin_uid (root-launcher mode)" >&2; exit 1; }
    ROOT_LAUNCHER=1
fi

if [ "$ROOT_LAUNCHER" = 1 ] && is_hardened_profile; then
    if [ "${TIER2_ALLOW_PRIVESC:-0}" = "1" ]; then
        echo "spawn-tier2: TIER2_ALLOW_PRIVESC=1 is not accepted in root-launcher hardened profile '$QDISTRO_PROFILE'" >&2
        exit 2
    fi
    if [ -n "${TIER2_KEEP_CAPS:-}" ]; then
        echo "spawn-tier2: TIER2_KEEP_CAPS is not accepted in root-launcher hardened profile '$QDISTRO_PROFILE'" >&2
        exit 2
    fi
    if [ -n "${TIER2_SECCOMP_PROFILE:-}" ]; then
        echo "spawn-tier2: TIER2_SECCOMP_PROFILE is not accepted from env in root-launcher hardened profile '$QDISTRO_PROFILE'" >&2
        exit 2
    fi
    case "${TIER2_NETWORK:-none}" in
        none|pasta) ;;
        slirp4netns)
            # Podman 6 removed slirp4netns; keep one-release compat for silos.yaml
            # / launch env that still say slirp4netns.
            echo "spawn-tier2: WARN: TIER2_NETWORK=slirp4netns is deprecated (Podman 6); mapping to pasta" >&2
            TIER2_NETWORK=pasta
            ;;
        *)
            echo "spawn-tier2: TIER2_NETWORK=${TIER2_NETWORK} is not an accepted hardened root-launcher network mode (want none|pasta; legacy slirp4netns maps to pasta)" >&2
            exit 2
            ;;
    esac
fi

# pm: route every podman invocation through the correct identity. In
# root-launcher mode podman MUST run rootless as admin (never as root — that
# would use root's empty image store and break --userns=keep-id state); in the
# default admin-direct mode it is bare `podman`. A single shim means a podman
# call can never accidentally run under the wrong uid.
if [ "$ROOT_LAUNCHER" = 1 ]; then
    pm() {
        runuser -u "$ADMIN_USER" -- env \
            XDG_RUNTIME_DIR="/run/user/${_root_admin_uid}" podman "$@"
    }
else
    pm() { podman "$@"; }
fi

# as_admin_run: route a NON-podman host helper through the admin identity in
# root-launcher mode, bare otherwise. Used for the two pre-wrapper steps that
# would otherwise change identity (admin -> root) under the root unit and break
# the admin-owned-state / admin-caller invariants the persistent silo path
# (TIER2_SILO) relies on (codex design review, fixes 2 + 3):
#   - qdistro-resolve-binding --record: writes the per-boot status file
#     /run/qdistro/silo-generation/<silo> + the activation marker under the
#     admin-owned 0700 /var/lib/qdistro/bindings; running it as root would
#     leave root-owned files in an admin-owned tree and emit the
#     template.binding.activated audit under the wrong identity.
#   - the broker CheckPermission gate: deployments may carry uid-scoped
#     qdistro.tier2.spawn rules keyed on the admin uid; a root caller would
#     miss them and be (fail-closed) DENIED. Dropping to admin keeps the
#     broker seeing the SAME caller uid the User=admin unit presented.
# The disposable path never sets TIER2_SILO and its broker rules are
# action-only, so this shim is a no-op-equivalent there (the gate still runs
# as admin, which is strictly more correct than the prior root caller).
if [ "$ROOT_LAUNCHER" = 1 ]; then
    as_admin_run() {
        runuser -u "$ADMIN_USER" -- env \
            XDG_RUNTIME_DIR="/run/user/${_root_admin_uid}" "$@"
    }
else
    as_admin_run() { "$@"; }
fi

# --- disposable identity (07-disposables-plan P1, D15) -------------------
if [ "$DISPOSABLE" = 1 ]; then
    # A disposable never mounts persistent state — refuse a silo binding.
    if [ -n "${TIER2_SILO:-}" ]; then
        echo "spawn-tier2: --disposable is incompatible with TIER2_SILO" \
             "(a throwaway silo has no persistent state)" >&2
        exit 1
    fi
    # Workload becomes part of a container name + a broker action: constrain.
    # A bash [[ =~ ]] test matches the WHOLE string (not line-by-line like
    # grep), so an embedded newline can't smuggle a second token past it.
    if ! [[ "$WORKLOAD" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
        echo "spawn-tier2: invalid disposable workload '$WORKLOAD'" \
             "(want ^[a-z0-9][a-z0-9-]{0,62}\$)" >&2
        exit 1
    fi
    DISP_TS="$(date +%Y%m%d-%H%M%S)"
    CONTAINER="disp-${WORKLOAD}-${DISP_TS}"
    # Same-second collision: append a short random hex suffix (D15).
    if command -v podman >/dev/null 2>&1 \
       && pm container exists "$CONTAINER" 2>/dev/null; then
        CONTAINER="${CONTAINER}-$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
    fi
    # Per-launch secctx token -> app_id qdistro.disp.<token>.
    DISP_TOKEN="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    TIER2_SECCTX_APPID="qdistro.disp.${DISP_TOKEN}"
    # Optional TTL lease (07-disposables-plan Lifecycle): a max-lifetime leak
    # backstop reaped by the session-manager periodic sweep. OFF by default --
    # interactive disposables rely on window-close + --rm and must NOT acquire a
    # surprise wall-clock kill. Opt in with QDISTRO_DISPOSABLE_TTL=<seconds> for
    # short-lived agent/workflow pods. When set to a positive integer we stamp
    # two immutable labels the sweep reads: the TTL (seconds) and the spawn
    # instant (unix epoch seconds, authored here rather than trusting the
    # podman version-volatile created-time field).
    DISP_LEASE_TTL=""
    DISP_LEASE_CREATED=""
    if [ -n "${QDISTRO_DISPOSABLE_TTL:-}" ]; then
        if [[ "$QDISTRO_DISPOSABLE_TTL" =~ ^[0-9]+$ ]] \
           && [ "$QDISTRO_DISPOSABLE_TTL" -gt 0 ]; then
            DISP_LEASE_TTL="$QDISTRO_DISPOSABLE_TTL"
        elif [ "$QDISTRO_DISPOSABLE_TTL" != "0" ]; then
            echo "spawn-tier2: ignoring invalid" \
                 "QDISTRO_DISPOSABLE_TTL=$QDISTRO_DISPOSABLE_TTL (want a" \
                 "positive integer of seconds; 0/empty = no lease)" >&2
        fi
    fi

    # Optional process-tree-empty lease (07-disposables-plan §Lifecycle "last
    # toplevel closed AND process tree empty"). OFF by default. Opt in with
    # QDISTRO_DISPOSABLE_LEASE_PROCTREE=1 for windowless/agent/workflow pods that
    # contract "no new client/workload will be launched once the inner tree has
    # collapsed to the compositor PID1 alone". The session-manager sweep then
    # reaps the disposable once it observes ONLY weston (PID1) running, past a
    # grace window. The grace seconds default to 30 (covers the normal "weston is
    # up before the inner client appears" startup race) and can be overridden
    # with QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE=<seconds>.
    DISP_LEASE_PROCTREE=""
    DISP_LEASE_PROCTREE_GRACE=""
    if [ "${QDISTRO_DISPOSABLE_LEASE_PROCTREE:-}" = "1" ]; then
        DISP_LEASE_PROCTREE="1"
        if [ -n "${QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE:-}" ]; then
            if [[ "$QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE" =~ ^[0-9]+$ ]]; then
                DISP_LEASE_PROCTREE_GRACE="$QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE"
            else
                echo "spawn-tier2: ignoring invalid" \
                     "QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE=$QDISTRO_DISPOSABLE_LEASE_PROCTREE_GRACE" \
                     "(want a non-negative integer of seconds)" >&2
            fi
        fi
    fi

    # The creation instant is the chain anchor for BOTH the TTL expiry and the
    # process-tree grace window, so stamp it whenever EITHER lease is opted in
    # (self-authored unix epoch, never podman's version-volatile created-time).
    if [ -n "$DISP_LEASE_TTL" ] || [ -n "$DISP_LEASE_PROCTREE" ]; then
        DISP_LEASE_CREATED="$(date +%s)"
    fi

    # Optional workflow-step lease (07-disposables-plan §Lifecycle "workflow step
    # completed"). A grouping id so a workflow runner can tear down EVERY
    # disposable a step spawned with one DisposeByWorkflow(id) call on step
    # completion. Opt in with QDISTRO_DISPOSABLE_WORKFLOW=<id>; the id rides in a
    # podman label and a --filter value, so constrain it to the same conservative
    # lowercase token shape as a workload (no arbitrary label bytes reach a
    # filter). This is an EXTERNAL teardown surface, not a periodic predicate — no
    # created/sweep machinery attaches to it.
    DISP_LEASE_WORKFLOW=""
    if [ -n "${QDISTRO_DISPOSABLE_WORKFLOW:-}" ]; then
        if [[ "$QDISTRO_DISPOSABLE_WORKFLOW" =~ ^[a-z0-9][a-z0-9-]{0,127}$ ]]; then
            DISP_LEASE_WORKFLOW="$QDISTRO_DISPOSABLE_WORKFLOW"
        else
            echo "spawn-tier2: ignoring invalid" \
                 "QDISTRO_DISPOSABLE_WORKFLOW=$QDISTRO_DISPOSABLE_WORKFLOW" \
                 "(want ^[a-z0-9][a-z0-9-]{0,127}\$)" >&2
        fi
    fi
fi

if [ "$ROOT_LAUNCHER" = 1 ]; then
    # Root supervisor: target the ADMIN uid + runtime dir, never root's own
    # (id -u would be 0 here and /run/user/0 is wrong for the rootless podman
    # we drop into). The wrapper/secctx chain runs under `runuser -u admin`,
    # which sets the admin XDG_RUNTIME_DIR itself.
    ADMIN_UID="$_root_admin_uid"
    RUNTIME_DIR="/run/user/$ADMIN_UID"
    # The admin runtime dir must already exist + be admin-owned (a logged-in
    # admin session creates it). Fail closed rather than spawn against a
    # bogus/absent runtime dir.
    if [ ! -d "$RUNTIME_DIR" ]; then
        echo "spawn-tier2: admin runtime dir $RUNTIME_DIR absent" \
             "(is the admin session up?) — refusing root-launcher spawn" >&2
        exit 1
    fi
    _rt_owner="$(stat -c '%u' "$RUNTIME_DIR" 2>/dev/null || echo -1)"
    if [ "$_rt_owner" != "$ADMIN_UID" ]; then
        echo "spawn-tier2: $RUNTIME_DIR is owned by uid $_rt_owner, not the" \
             "admin uid $ADMIN_UID — refusing (fail closed)" >&2
        exit 1
    fi
    # Do NOT export root's XDG_RUNTIME_DIR down the chain; the admin runuser
    # legs set their own. Keep it pointed at the admin dir for the host-side
    # checks below (outer wayland socket existence, etc).
    export XDG_RUNTIME_DIR="$RUNTIME_DIR"
else
    ADMIN_UID="${TIER2_ADMIN_UID:-$(id -u)}"
    RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$ADMIN_UID}"
    # Export so qdistro-secctx-exec (which checks the actual env, not just
    # our computed defaults) and podman both see it. Useful when this
    # script is invoked via `runuser -u admin -- bash …` which can strip
    # the parent shell's runtime-dir env.
    export XDG_RUNTIME_DIR="$RUNTIME_DIR"
fi
OUTER_DISPLAY="${TIER2_OUTER_DISPLAY:-${WAYLAND_DISPLAY:-wayland-1}}"
QDWIN_SHELL_SO="${TIER2_QDWIN_SHELL_SO:-/usr/lib64/weston/qdwin-shell.so}"
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
USE_SECCTX="${TIER2_USE_SECCTX:-1}"
ENGINE="${TIER2_SECCTX_ENGINE:-qdistro.tier2}"
SECCTX_APPID="${TIER2_SECCTX_APPID:-${CONTAINER}/${APP_NAME}}"
LAUNCHREC_PATH=""
LAUNCHREC_TOKEN=""
LAUNCHREC_FILE_ID=""

# Resolve this script's directory early — the open-in-disposable class resolver
# (below) and the spawn-common sourcing (further down) both need it.
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s\n' "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

# Broker spawn-gate action. Disposable spawn uses qdistro.dispose.spawn:
# (a rules-only, fail-closed namespace in the broker, same as tier2.spawn).
if [ "$DISPOSABLE" = 1 ]; then
    SPAWN_ACTION="qdistro.dispose.spawn:${WORKLOAD}"
else
    SPAWN_ACTION="qdistro.tier2.spawn:${WORKLOAD}/${APP_NAME}"
fi

# --- open-in-disposable: trusted-path class gate + RO input (07-plan P2) ---
# This is the LOAD-BEARING enforcement the codex design review made a hard
# condition: the qdistro.dispose.open:<class> gate and the read-only input
# attachment are bound TOGETHER in the trusted launch path, never in the SDK.
#
#   TIER2_OPEN_CLASS=<class>   the open class (registry key). When set we
#                              resolve it from the disposable-class registry,
#                              enforce its enablement (min_tier) gate, pin the
#                              workload + network to the class, and add a SECOND
#                              mandatory broker gate (qdistro.dispose.open:<class>).
#   TIER2_RO_INPUT=<path>      a single host file/dir to bind READ-ONLY into the
#                              disposable at /mnt/input/<basename> (D7
#                              mounts-not-copies). REQUIRES TIER2_OPEN_CLASS — an
#                              input may never be attached without a resolved,
#                              admin-authorized open class.
#
# Fail-closed everywhere: unknown/disabled class, malformed registry, a
# class→workload mismatch, a missing/invalid input path, or an unsupported
# bind shape all refuse BEFORE podman runs.
OPEN_CLASS="${TIER2_OPEN_CLASS:-}"
RO_INPUT="${TIER2_RO_INPUT:-}"
OPEN_ACTION=""
RO_INPUT_REAL=""
RO_INPUT_BASENAME=""
RO_INPUT_KIND=""

# --- export-back (07-disposables-plan P2 / D7 copy-exception) -------------
# When the resolved open class declares EXPORT=true, the disposable gets a
# per-launch host staging dir bound READ-WRITE at /mnt/output so the user/app
# can drop artifacts to be promoted back into the REQUESTING silo. This is a new
# persistent host-write surface, so it is gated TWICE by the broker
# (qdistro.dispose.export:<class>): once here at spawn (the surface) and again at
# import (the actual data crossing, in the session manager). The launcher writes
# meta.json OUTSIDE the bind (the container can never see/forge the request silo
# or class). The staging base is created root-controlled at install.
# Edit-round-trip (export-back follow-on): when the caller sets TIER2_REQUEST_EDIT=1
# (and the class is edit-capable, and a regular-FILE RO input is supplied), the
# single artifact dropped in /mnt/output is promoted back BESIDE its source as
# <name>.disp-edited at IMPORT (never overwriting in place). This is purely a
# launcher-stamped meta flag (edit_mode + input_realpath in meta.json, outside the
# bind) + a forensic label; it reuses the export staging, the /mnt/output RW bind,
# and the qdistro.dispose.export:<class> broker gate unchanged — the landing mode
# is chosen at import by the session manager from the meta.
REQUEST_EDIT="${TIER2_REQUEST_EDIT:-}"
EDIT_ENABLED=0
REQUEST_SILO="${TIER2_REQUEST_SILO:-}"
EXPORT_ENABLED=0
EXPORT_GATE_ACTION=""
EXPORT_STAGING_BASE="${TIER2_EXPORT_STAGING_BASE:-/var/lib/qdistro/disposable-export}"
EXPORT_STAGING_DIR=""
EXPORT_PAYLOAD_DIR=""
# Silo name shape mirrors templates.require_safe_name
# (^[A-Za-z0-9][A-Za-z0-9_.-]* with no '..'); it becomes a binding-file path
# component at import, so constrain it before it is ever trusted as routing.
_REQUEST_SILO_RE='^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$'

# An input with no class is refused: the class is the policy axis that
# authorizes routing untrusted bytes into a throwaway. (Reverse is allowed: an
# open class may legitimately have no input — e.g. an agent scratch pod.)
if [ -n "$RO_INPUT" ] && [ -z "$OPEN_CLASS" ]; then
    echo "spawn-tier2: TIER2_RO_INPUT set without TIER2_OPEN_CLASS —" \
         "an input may not be attached without an authorized open class" >&2
    exit 2
fi

if [ -n "$OPEN_CLASS" ]; then
    # open-in-disposable is a disposable-only flow.
    if [ "$DISPOSABLE" != 1 ]; then
        echo "spawn-tier2: TIER2_OPEN_CLASS requires --disposable" >&2
        exit 2
    fi
    # Locate the registry resolver module (dev tree, then installed layout).
    CLASSES_RESOLVER=()
    if [ -f "$SCRIPT_DIR/../session_manager/qdistro_disposable_classes.py" ]; then
        CLASSES_RESOLVER=(python3
            "$SCRIPT_DIR/../session_manager/qdistro_disposable_classes.py")
    elif [ -f /usr/libexec/qdistro/qdistro_disposable_classes.py ]; then
        CLASSES_RESOLVER=(python3
            /usr/libexec/qdistro/qdistro_disposable_classes.py)
    else
        echo "spawn-tier2: qdistro_disposable_classes.py not found" \
             "(open-in-disposable cannot resolve the class registry)" >&2
        exit 2
    fi
    # TRUSTED registry path (codex code-review MAJOR): the class registry is the
    # load-bearing source for the class->workload, class->network, and min_tier
    # decisions, so it MUST be the admin-owned installed file — never a
    # caller-selected one. We pass --registry explicitly so the resolver's
    # QDISTRO_DISPOSABLE_CLASSES env default (which an app could set to a forged
    # registry redefining agent-scratch to network=egress + a hostile workload)
    # is NOT honoured in the shipped path. A test-only override
    # (TIER2_DISPOSABLE_CLASSES_TEST) is honoured ONLY when explicitly set, so
    # the host unit tests + the VM probe can point at an in-tree registry without
    # opening the production path to env spoofing.
    CLASSES_REGISTRY="${TIER2_DISPOSABLE_CLASSES_TEST:-/etc/qdistro/disposable-classes.toml}"
    # Resolve + enablement-gate the class. Exit codes are authoritative:
    # 0 enabled, 3 unknown, 4 disabled (hostile-class/min_tier), 5 malformed.
    # (The script has no `set -e`, so the resolver's exit status is captured
    # directly — no set toggle that could leak `-e` into the rest of the run.)
    _OPEN_ERRF="$(mktemp 2>/dev/null || echo /tmp/.qd-openclass.$$)"
    OPEN_PLAN="$("${CLASSES_RESOLVER[@]}" --resolve "$OPEN_CLASS" \
        --registry "$CLASSES_REGISTRY" 2>"$_OPEN_ERRF")"
    OPEN_RC=$?
    OPEN_ERR="$(cat "$_OPEN_ERRF" 2>/dev/null)"; rm -f "$_OPEN_ERRF" 2>/dev/null
    case "$OPEN_RC" in
        0) : ;;
        3) echo "spawn-tier2: unknown open class '$OPEN_CLASS' — refusing ($OPEN_ERR)" >&2; exit 2 ;;
        4) echo "spawn-tier2: open class '$OPEN_CLASS' is DISABLED at this tier" \
                "(hostile-class / min_tier gate) — refusing ($OPEN_ERR)" >&2; exit 2 ;;
        5) echo "spawn-tier2: disposable-class registry is malformed — refusing all opens ($OPEN_ERR)" >&2; exit 2 ;;
        *) echo "spawn-tier2: open-class resolve failed (rc=$OPEN_RC) — refusing ($OPEN_ERR)" >&2; exit 2 ;;
    esac
    # Parse the KEY=VALUE plan the resolver printed.
    CLASS_WORKLOAD=""; CLASS_NETWORK=""; OPEN_ACTION=""
    CLASS_EXPORT=""; EXPORT_ACTION=""; CLASS_EDIT=""
    while IFS='=' read -r _k _v; do
        case "$_k" in
            WORKLOAD)      CLASS_WORKLOAD="$_v" ;;
            NETWORK)       CLASS_NETWORK="$_v" ;;
            OPEN_ACTION)   OPEN_ACTION="$_v" ;;
            EXPORT)        CLASS_EXPORT="$_v" ;;
            EXPORT_ACTION) EXPORT_ACTION="$_v" ;;
            EDIT)          CLASS_EDIT="$_v" ;;
        esac
    done <<< "$OPEN_PLAN"
    # The class pins the workload: the caller-supplied WORKLOAD must equal the
    # registry's. Otherwise an allow rule for one workload could be paired with
    # an unrelated open class (the codex class/workload-binding condition).
    if [ "$CLASS_WORKLOAD" != "$WORKLOAD" ]; then
        echo "spawn-tier2: open class '$OPEN_CLASS' maps to workload" \
             "'$CLASS_WORKLOAD' but the spawn workload is '$WORKLOAD' —" \
             "class/workload mismatch, refusing" >&2
        exit 2
    fi
    # The class also pins the app argv. This is load-bearing for open classes
    # whose workload script is the sanitizer/policy boundary (for example
    # url-preview validates URL shape, disables redirects, bounds curl, and
    # escapes terminal output). A caller may choose the workload position only
    # because the CLI shape needs it before the registry is resolved; it may NOT
    # pair an authorized class with arbitrary argv inside that egress/text image.
    # Direct custom argv remains available only when TIER2_OPEN_CLASS is absent.
    APP_ARGV=("$WORKLOAD")
    APP_NAME="$WORKLOAD"
    # The class pins the network mode: 'none' -> --network none, 'egress' ->
    # the pasta egress contract (Podman 6; replaces slirp4netns). The trusted
    # path SETS it from the class (a caller cannot widen a 'none' class to
    # egress via TIER2_NETWORK).
    case "$CLASS_NETWORK" in
        none)   TIER2_NETWORK="none" ;;
        egress) TIER2_NETWORK="pasta" ;;
        *) echo "spawn-tier2: open class '$OPEN_CLASS' has invalid network '$CLASS_NETWORK'" >&2; exit 2 ;;
    esac
    [ -n "$OPEN_ACTION" ] || { echo "spawn-tier2: resolver returned no OPEN_ACTION for '$OPEN_CLASS'" >&2; exit 2; }

    # --- export-back enablement (07-plan P2 / D7 copy-exception) ---------
    # Export-back is OPT-IN PER LAUNCH: a caller asks for it by supplying
    # TIER2_REQUEST_SILO (the silo to promote results into). An export-capable
    # class with NO request silo is just a normal disposable (no /mnt/output) — so
    # an ordinary agent-scratch open keeps working unchanged. When the caller DOES
    # opt in, the class must be export-capable (registry export=true) AND pass the
    # spawn-time broker export gate; otherwise refuse rather than silently drop the
    # caller's export intent. The request silo is shape-validated here; its
    # existence as a templated silo + its state_path are re-resolved (read-only) at
    # IMPORT — that is the routing trust anchor (a spawn-time string is not
    # authority on its own).
    if [ -n "$REQUEST_SILO" ]; then
        if [ "$CLASS_EXPORT" != "true" ]; then
            echo "spawn-tier2: TIER2_REQUEST_SILO set but open class '$OPEN_CLASS'" \
                 "is not export-capable (export=false) — refusing" >&2
            exit 2
        fi
        [ -n "$EXPORT_ACTION" ] || { echo "spawn-tier2: resolver returned no EXPORT_ACTION for '$OPEN_CLASS'" >&2; exit 2; }
        if ! [[ "$REQUEST_SILO" =~ $_REQUEST_SILO_RE ]] || [[ "$REQUEST_SILO" == *..* ]]; then
            echo "spawn-tier2: invalid TIER2_REQUEST_SILO '$REQUEST_SILO'" \
                 "(want ${_REQUEST_SILO_RE} with no '..')" >&2
            exit 2
        fi
        EXPORT_ENABLED=1
        EXPORT_GATE_ACTION="$EXPORT_ACTION"
    fi

    # --- validate + canonicalize the RO input (D7) -----------------------
    if [ -n "$RO_INPUT" ]; then
        case "$RO_INPUT" in
            /*) : ;;
            *) echo "spawn-tier2: TIER2_RO_INPUT must be an absolute path (got '$RO_INPUT')" >&2; exit 2 ;;
        esac
        # Canonicalize (resolve symlinks) so we mount a stable real path and a
        # stable basename — avoids a symlink-swap surprise at mount time.
        RO_INPUT_REAL="$(readlink -f -- "$RO_INPUT" 2>/dev/null || true)"
        [ -n "$RO_INPUT_REAL" ] && [ -e "$RO_INPUT_REAL" ] \
            || { echo "spawn-tier2: TIER2_RO_INPUT '$RO_INPUT' does not exist (after canonicalization)" >&2; exit 2; }
        # The canonical path becomes the SOURCE of a colon-delimited podman -v
        # spec (source:target:options), so a ':' or a control byte in it could
        # make the spec ambiguous (codex code-review minor). Reject both in the
        # source path AND the basename before building the bind. (Newlines also
        # can't survive a podman arg; reject all control bytes 0x00-0x1f.)
        case "$RO_INPUT_REAL" in
            *:*) echo "spawn-tier2: refusing input path containing ':' ('$RO_INPUT_REAL') — ambiguous podman volume spec" >&2; exit 2 ;;
        esac
        if printf '%s' "$RO_INPUT_REAL" | LC_ALL=C grep -q '[[:cntrl:]]'; then
            echo "spawn-tier2: refusing input path with control characters" >&2; exit 2
        fi
        if [ -f "$RO_INPUT_REAL" ]; then
            RO_INPUT_KIND="file"
        elif [ -d "$RO_INPUT_REAL" ]; then
            RO_INPUT_KIND="dir"
        else
            echo "spawn-tier2: TIER2_RO_INPUT '$RO_INPUT' is neither a regular file nor a directory — refusing" >&2
            exit 2
        fi
        RO_INPUT_BASENAME="$(basename -- "$RO_INPUT_REAL")"
        # Sanitize the basename: a single path component, no traversal, no
        # slash, not . or .., no ':' (colon-delimited spec), no control bytes —
        # it becomes the in-container mount target leaf.
        case "$RO_INPUT_BASENAME" in
            ""|"."|".."|*/*|*:*) echo "spawn-tier2: refusing unsafe input basename '$RO_INPUT_BASENAME'" >&2; exit 2 ;;
        esac
        if printf '%s' "$RO_INPUT_BASENAME" | LC_ALL=C grep -q '[[:cntrl:]]'; then
            echo "spawn-tier2: refusing input basename with control characters" >&2; exit 2
        fi
    fi

    # --- edit-round-trip enablement (export-back follow-on) --------------
    # Opt-in PER LAUNCH via TIER2_REQUEST_EDIT=1. It is a strict refinement of an
    # export launch: it needs the SAME preconditions (a request silo + an
    # export-capable, gated class -> EXPORT_ENABLED already set above) PLUS the
    # class must be edit-capable AND the RO input must be a single regular FILE
    # (an edit-round-trip returns ONE edited file beside ONE source; a directory
    # input has no single source to land beside). The chosen landing mode is
    # carried to import only as the meta `edit_mode` flag + `input_realpath`
    # (written outside the container bind); nothing about /mnt/output or the gate
    # changes. Refuse loudly rather than silently downgrade an edit request to a
    # plain export — the caller asked for the beside-source semantics.
    if [ -n "$REQUEST_EDIT" ]; then
        if [ "$REQUEST_EDIT" != "1" ]; then
            echo "spawn-tier2: TIER2_REQUEST_EDIT must be '1' if set (got '$REQUEST_EDIT')" >&2
            exit 2
        fi
        if [ "$EXPORT_ENABLED" != "1" ]; then
            echo "spawn-tier2: TIER2_REQUEST_EDIT=1 requires TIER2_REQUEST_SILO and an" \
                 "export-capable open class (no export surface to return the edit) — refusing" >&2
            exit 2
        fi
        if [ "$CLASS_EDIT" != "true" ]; then
            echo "spawn-tier2: TIER2_REQUEST_EDIT=1 but open class '$OPEN_CLASS' is not" \
                 "edit-capable (edit=false) — refusing" >&2
            exit 2
        fi
        if [ "$RO_INPUT_KIND" != "file" ]; then
            echo "spawn-tier2: TIER2_REQUEST_EDIT=1 requires a single regular-file" \
                 "TIER2_RO_INPUT to edit (got kind '${RO_INPUT_KIND:-none}') — refusing" >&2
            exit 2
        fi
        EDIT_ENABLED=1
    fi
fi

# Test/inspection hook: dump the resolved launch plan and exit 0 BEFORE the
# image/socket checks, the broker call, podman, or secctx — lets the host
# test suite assert the disposable identity + gate without a live broker,
# podman image, or wayland socket.
if [ "${TIER2_PRINT_PLAN:-0}" = "1" ]; then
    printf 'DISPOSABLE=%s\n' "$DISPOSABLE"
    printf 'CONTAINER=%s\n' "$CONTAINER"
    printf 'WORKLOAD=%s\n' "$WORKLOAD"
    printf 'APP_ID=%s\n' "$SECCTX_APPID"
    printf 'ENGINE=%s\n' "$ENGINE"
    printf 'SPAWN_ACTION=%s\n' "$SPAWN_ACTION"
    printf 'STATE=%s\n' "${TIER2_SILO:-none}"
    printf 'LEASE_TTL=%s\n' "${DISP_LEASE_TTL:-none}"
    printf 'LEASE_CREATED=%s\n' "${DISP_LEASE_CREATED:-none}"
    printf 'LEASE_PROCTREE=%s\n' "${DISP_LEASE_PROCTREE:-none}"
    printf 'LEASE_PROCTREE_GRACE=%s\n' "${DISP_LEASE_PROCTREE_GRACE:-none}"
    printf 'LEASE_WORKFLOW=%s\n' "${DISP_LEASE_WORKFLOW:-none}"
    printf 'OPEN_CLASS=%s\n' "${OPEN_CLASS:-none}"
    printf 'OPEN_ACTION=%s\n' "${OPEN_ACTION:-none}"
    printf 'EXPORT=%s\n' "$([ "$EXPORT_ENABLED" = 1 ] && printf 'true' || printf 'false')"
    printf 'EXPORT_ACTION=%s\n' "${EXPORT_GATE_ACTION:-none}"
    printf 'EDIT=%s\n' "$([ "$EDIT_ENABLED" = 1 ] && printf 'true' || printf 'false')"
    printf 'REQUEST_SILO=%s\n' "${REQUEST_SILO:-none}"
    printf 'OUTPUT_TARGET=%s\n' "$([ "$EXPORT_ENABLED" = 1 ] && printf '/mnt/output' || printf 'none')"
    printf 'NETWORK=%s\n' "${TIER2_NETWORK:-none}"
    printf 'RO_INPUT_REAL=%s\n' "${RO_INPUT_REAL:-none}"
    printf 'RO_INPUT_KIND=%s\n' "${RO_INPUT_KIND:-none}"
    printf 'RO_INPUT_TARGET=%s\n' \
        "$([ -n "${RO_INPUT_BASENAME:-}" ] && printf '/mnt/input/%s' "$RO_INPUT_BASENAME" || printf 'none')"
    exit 0
fi

# Root-launcher mode exists ONLY to stamp the secctx wire tag. If it CANNOT
# (secctx disabled, or the helper absent), it MUST fail closed — never silently
# fall through to the un-tagged wrapper, which would mint a disposable that
# looks tagged (correct name/app_id on stdout) yet carries NO
# wp_security_context on the wire. That false assurance is exactly what this
# mode removes, so it is a hard error. Checked HERE — before any podman /
# broker / per-container-dir work — so it fails fast and cannot be masked by an
# earlier "podman not in PATH" when a caller strips PATH to test this very gate.
if [ "$ROOT_LAUNCHER" = 1 ]; then
    if [ "$USE_SECCTX" != "1" ]; then
        echo "spawn-tier2: TIER2_ROOT_LAUNCHER=1 requires TIER2_USE_SECCTX=1" \
             "(the mode's sole purpose is the secctx wire tag) — refusing to" \
             "launch un-tagged" >&2
        exit 2
    fi
    if ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
        echo "spawn-tier2: TIER2_ROOT_LAUNCHER=1 but qdistro-secctx-exec is not" \
             "in PATH — cannot stamp the wire tag; refusing to launch un-tagged" \
             "(PACKAGING GAP)" >&2
        exit 2
    fi
fi

if [ "$ROOT_LAUNCHER" != 1 ] && is_hardened_profile; then
    if [ "$USE_SECCTX" != "1" ]; then
        echo "spawn-tier2: TIER2_USE_SECCTX=0 is dev/test-only; hardened profile '$QDISTRO_PROFILE' requires root-launcher secctx provenance" >&2
        exit 2
    fi
    if [ "${QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED:-0}" = "1" ]; then
        echo "spawn-tier2: QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1 is dev-only; hardened profile '$QDISTRO_PROFILE' refuses it" >&2
        exit 2
    fi
    if ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
        echo "spawn-tier2: qdistro-secctx-exec not in PATH; hardened profile '$QDISTRO_PROFILE' refuses untagged direct launch" >&2
        exit 2
    fi
    echo "spawn-tier2: direct Tier-2 launch has no trusted root launcher parent; hardened profile '$QDISTRO_PROFILE' refuses it" >&2
    echo "             Route interactive launches through qdistro-tier2-silo@.service / qdistro-tier2-silo-launch, or set QDISTRO_PROFILE=dev for local tests." >&2
    exit 2
fi

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
# SCRIPT_PATH / SCRIPT_DIR were resolved early (above the open-class gate).
SPAWN_COMMON="$SCRIPT_DIR/../lib/spawn-common.sh"
if [ ! -r "$SPAWN_COMMON" ] && [ -r /usr/lib/qdistro/spawn-common.sh ]; then
    SPAWN_COMMON=/usr/lib/qdistro/spawn-common.sh
fi
if [ ! -r "$SPAWN_COMMON" ]; then
    echo "spawn-tier2: spawn-common.sh not found (looked near $SCRIPT_DIR and /usr/lib/qdistro)" >&2
    exit 5
fi
. "$SPAWN_COMMON"
# TIER2_LAUNCH_TOKEN lets a TRUSTED launcher pre-commit the token instead of
# reading it back off our stdout. The root-launcher topology needs this: when
# the launch runs inside a systemd unit our stdout is the journal, not the
# caller's pipe, so the D-Bus caller (qdshell, via
# SessionManager1.LaunchPodApp) could not otherwise learn the token it has to
# match against the toplevel's secctx instance-id to resolve its placeholder.
# The token is correlation, not authorisation — it is the secctx instance-id,
# the per-container dir name and a podman label, so a MALFORMED one would
# corrupt those namespaces (path traversal via the per-container dir most of
# all). Validate the exact shape the generator guarantees and fail closed;
# never fall back to generating one, or a caller typo would silently produce a
# token the caller is not watching for.
if [ -n "${TIER2_LAUNCH_TOKEN:-}" ]; then
    if ! [[ "$TIER2_LAUNCH_TOKEN" =~ ^[0-9a-f]{32}$ ]]; then
        echo "spawn-tier2: TIER2_LAUNCH_TOKEN must be 32 lowercase hex digits" >&2
        exit 2
    fi
    LAUNCH_TOKEN="$TIER2_LAUNCH_TOKEN"
else
    LAUNCH_TOKEN="$(gen_launch_token "spawn-tier2")"
fi

# Custom seccomp profile — deny-by-default, allows only the ~145
# syscalls needed by bash/weston/weston-terminal/coreutils. Hardened
# profiles fail closed if no workload profile is found. The podman-default
# fallback is dev-only for ad-hoc checkout runs. If TIER2_SECCOMP_PROFILE
# was explicitly set via env but the file is missing, we fail closed rather
# than silently dropping to the default.
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
        if is_hardened_profile; then
            echo "spawn-tier2: FATAL: no seccomp profile found for workload '$WORKLOAD' in hardened profile '$QDISTRO_PROFILE'" >&2
            exit 2
        fi
        echo "spawn-tier2: WARN: no seccomp profile found for workload '$WORKLOAD'; dev profile using podman default" >&2
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
    # In root-launcher mode this MUST run as admin (see as_admin_run): it
    # writes /run/qdistro/silo-generation/<silo> + the activation marker into
    # the admin-owned binding tree and emits the activation audit. Running it
    # as root would leave root-owned files in an admin-owned 0700 dir and
    # mis-attribute the audit.
    launch_env="$(as_admin_run "${RESOLVER[@]}" "$TIER2_SILO" --record --launch-env)"
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

if ! pm image exists "$IMAGE" 2>/dev/null; then
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

# Mandatory broker gate(s). Tier-2 launch is a security boundary, so only
# explicit admin-authored rules may authorize it. Cache rows and hook verdicts
# are ignored by the broker for the qdistro.{tier2,dispose}.spawn: AND
# qdistro.dispose.open: namespaces — all in the same rules-only, fail-closed
# set in the broker. A non-allow verdict (deny / unknown / empty / unsupported /
# dbus error) fails closed.
if ! command -v dbus-send >/dev/null 2>&1; then
    fail "dbus-send not found; broker authorization required"
fi
# broker_gate <action> <human-label> — refuses unless the broker says "allow".
# The script runs without `set -e`, so the dbus exit status is captured directly
# (no set +e/-e toggle that could leak `-e` into the rest of the script).
broker_gate() {
    local _action="$1" _label="$2" _out _status _reply
    # In root-launcher mode the gate runs as admin (as_admin_run): the broker
    # authorizes on the CALLER uid, and deployments may carry uid-scoped
    # qdistro.tier2.spawn rules keyed on the admin uid. A root caller would
    # miss them and be fail-closed DENIED, so we present the admin uid the
    # User=admin unit used to. In the default path as_admin_run is a no-op.
    _out=$(as_admin_run dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$_action" \
        "dict:string:string:" 2>&1)
    _status=$?
    _reply=$(printf '%s' "$_out" | tr -d ' \t\n')
    if [ "$_status" -ne 0 ]; then
        fail "broker authorization failed for $_label (action='$_action')"
    fi
    case "$_reply" in
        allow|string\"allow\")
            if [ "${TIER2_DEBUG:-0}" = "1" ]; then
                echo "spawn-tier2: broker allowed $_label (action='$_action')" >&2
            fi
            ;;
        deny|string\"deny\")
            fail "broker denied $_label (action='$_action' decision=deny)" ;;
        unknown|string\"unknown\"|"")
            fail "broker has no allow rule for $_label (action='$_action' decision=unknown)" ;;
        *)
            fail "broker returned unsupported verdict for $_label (action='$_action' reply='$_reply')" ;;
    esac
}

# Gate 1: the spawn gate (always). SPAWN_ACTION resolved with the launch
# identity above.
broker_gate "$SPAWN_ACTION" "${WORKLOAD}/${APP_NAME}"

# Gate 2: the OPEN gate (only for open-in-disposable). This is the class-level
# policy axis the codex review required be enforced in the trusted path — a
# spawn-gate allow for the workload does NOT by itself authorize routing an
# untrusted input into the throwaway; the admin must ALSO allow
# qdistro.dispose.open:<class>. Both gates must pass.
if [ -n "$OPEN_ACTION" ]; then
    broker_gate "$OPEN_ACTION" "open-class ${OPEN_CLASS}"
fi

# Gate 3: the EXPORT gate (only when the open class is export-capable). Binding a
# writable /mnt/output surface into a disposable that may return bytes to a real
# silo is a class-level decision an admin must explicitly allow — registry
# export=true alone does not grant it (07-disposables-plan P2 / D7 copy-exception).
# Re-checked at IMPORT time (the actual data crossing) by the session manager.
if [ "$EXPORT_ENABLED" = 1 ]; then
    broker_gate "$EXPORT_GATE_ACTION" "export-class ${OPEN_CLASS}"
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
    live_tokens=$(pm ps -a --format '{{.Labels.qdistro_tier2_token}}' 2>/dev/null \
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

# In root-launcher mode the per-container dir lives under the ADMIN runtime
# dir and is written by the admin podman wrapper (socket stubs), so it MUST be
# admin-owned, not root-owned. Create it as admin via runuser; the root
# supervisor still owns the EXIT-trap cleanup (root can rm an admin-owned
# subtree). In the default admin-direct mode this is a plain mkdir as admin.
if [ "$ROOT_LAUNCHER" = 1 ]; then
    runuser -u "$ADMIN_USER" -- mkdir -p -m 0700 "$PERCONT_DIR" \
        || fail "could not create admin-owned per-container dir $PERCONT_DIR"
else
    mkdir -p "$PERCONT_DIR"
    chmod 0700 "$PERCONT_DIR"
fi

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

# --- export-back staging tree (07-plan P2 / D7 copy-exception) -----------
# Created here (LAUNCH_TOKEN now exists) and DELIBERATELY NOT in cleanup_percont:
# the payload must survive the disposable's teardown so the requesting silo can
# import it afterwards. The session-manager importer removes it on a successful
# one-shot import; a boot/session-stop sweep reaps an orphan whose token has no
# live container. Layout per token:
#   <base>/<token>/meta.json   launcher-written, OUTSIDE the bind (the container
#                              can never see/forge the request silo or class)
#   <base>/<token>/payload/    the ONLY path bound RW into the container
# The base is root-controlled and created at install (a PACKAGING GAP if absent —
# fail closed rather than auto-create, which could race a symlink in).
if [ "$EXPORT_ENABLED" = 1 ]; then
    if [ ! -d "$EXPORT_STAGING_BASE" ] || [ -L "$EXPORT_STAGING_BASE" ]; then
        fail "export staging base $EXPORT_STAGING_BASE missing or a symlink — refusing export (PACKAGING GAP: install-session-manager.sh creates it root/admin-owned)"
    fi
    EXPORT_STAGING_DIR="$EXPORT_STAGING_BASE/$LAUNCH_TOKEN"
    EXPORT_PAYLOAD_DIR="$EXPORT_STAGING_DIR/payload"
    # Owned by admin so the --userns=keep-id container (inner admin == host admin)
    # can write payload/. In root-launcher mode the script is root; create as
    # admin via runuser. meta.json is written by the same identity, one level
    # ABOVE payload/, so it is never inside the container's RW bind.
    if [ "$ROOT_LAUNCHER" = 1 ]; then stg_run() { runuser -u "$ADMIN_USER" -- "$@"; }
    else stg_run() { "$@"; }; fi
    stg_run mkdir -p -m 0700 "$EXPORT_PAYLOAD_DIR" \
        || fail "could not create export staging dir $EXPORT_PAYLOAD_DIR"
    # meta.json: written by python (json.dumps) so an odd input basename cannot
    # break JSON. python opens+writes the file itself (the redirect would be done
    # by the root parent shell and mis-own it), so it lands owned by the staging
    # identity. input_basename is null when there was no RO input.
    # edit_mode + input_realpath ride the meta ONLY for an edit-round-trip launch;
    # they select the beside-source landing at import. input_realpath is the
    # launcher's own canonical source path (the importer re-canonicalizes it and
    # requires it strictly under the request silo's state — a spawn-time string is
    # not authority). For a plain export they are absent/null and the importer
    # takes the Incoming/ landing as before.
    stg_run python3 - "$EXPORT_STAGING_DIR/meta.json" "$LAUNCH_TOKEN" \
        "$REQUEST_SILO" "$OPEN_CLASS" "$WORKLOAD" "$CONTAINER" \
        "$(date +%s)" "${RO_INPUT_BASENAME:-}" "$EDIT_ENABLED" \
        "${RO_INPUT_REAL:-}" <<'PYMETA'
import json, os, sys
(path, token, silo, oc, workload, container, created, inp,
 edit_enabled, inp_real) = sys.argv[1:11]
edit_mode = (edit_enabled == "1")
meta = {
    "version": 1,
    "launch_token": token,
    "request_silo": silo,
    "open_class": oc,
    "workload": workload,
    "container": container,
    "created": int(created),
    "input_basename": inp if inp else None,
    "edit_mode": edit_mode,
    # Only meaningful (and only trusted) when edit_mode; null otherwise.
    "input_realpath": (inp_real if (edit_mode and inp_real) else None),
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(meta, f, sort_keys=True)
PYMETA
    [ -f "$EXPORT_STAGING_DIR/meta.json" ] \
        || fail "could not write export meta.json for token $LAUNCH_TOKEN"
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
export TIER2_PERCONT_DIR="$PERCONT_DIR"
export TIER2_ADMIN_UID_RESOLVED="$ADMIN_UID"
export TIER2_IMAGE="$IMAGE"
export TIER2_CONTAINER="$CONTAINER"
export TIER2_DISPOSABLE_RESOLVED="$DISPOSABLE"
# Lease labels (all optional, all opt-in in the disposable identity block):
# empty unless the corresponding QDISTRO_DISPOSABLE_* knob was set. Exported so
# the WRAPPER_BODY shell — which only sees exported TIER2_*_RESOLVED vars, not
# this script's locals — can stamp them on the podman run. TTL + proctree are
# in-session sweep leases; created anchors BOTH their windows; workflow is the
# external DisposeByWorkflow grouping id.
export TIER2_LEASE_TTL_RESOLVED="${DISP_LEASE_TTL:-}"
export TIER2_LEASE_CREATED_RESOLVED="${DISP_LEASE_CREATED:-}"
export TIER2_LEASE_PROCTREE_RESOLVED="${DISP_LEASE_PROCTREE:-}"
export TIER2_LEASE_PROCTREE_GRACE_RESOLVED="${DISP_LEASE_PROCTREE_GRACE:-}"
export TIER2_LEASE_WORKFLOW_RESOLVED="${DISP_LEASE_WORKFLOW:-}"
export TIER2_QDWIN_SHELL_SO_RESOLVED="$QDWIN_SHELL_SO"
export TIER2_STATE_PATH_RESOLVED="$STATE_PATH"
export TIER2_NETWORK_RESOLVED="$TIER2_NETWORK_VAL"
export TIER2_PIDS_LIMIT_RESOLVED="$TIER2_PIDS_LIMIT_VAL"
export TIER2_MEMORY_RESOLVED="$TIER2_MEMORY_VAL"
export TIER2_CPUS_RESOLVED="$TIER2_CPUS_VAL"
export TIER2_KEEP_CAPS_RESOLVED="$TIER2_KEEP_CAPS_VAL"
export TIER2_ALLOW_PRIVESC_RESOLVED="$TIER2_ALLOW_PRIVESC_VAL"
export TIER2_SECCOMP_PROFILE_RESOLVED="$TIER2_SECCOMP_PROFILE"
# Open-in-disposable RO input (07-plan P2 / D7): the canonicalized host path,
# its kind, and the in-container basename, all validated + admin-gated above.
# Empty unless TIER2_OPEN_CLASS+TIER2_RO_INPUT opted in. The wrapper binds it
# READ-ONLY (nosuid/nodev/noexec) under /mnt/input.
export TIER2_RO_INPUT_REAL_RESOLVED="${RO_INPUT_REAL:-}"
export TIER2_RO_INPUT_KIND_RESOLVED="${RO_INPUT_KIND:-}"
export TIER2_RO_INPUT_BASENAME_RESOLVED="${RO_INPUT_BASENAME:-}"
# Export-back staging (07-plan P2 / D7 copy-exception): the host payload dir bound
# READ-WRITE under /mnt/output, plus the request silo + open class stamped as
# immutable container labels (forensics; meta.json remains the authoritative
# source the importer reads). Empty unless an export-capable open class opted in.
export TIER2_EXPORT_ENABLED_RESOLVED="$EXPORT_ENABLED"
export TIER2_EDIT_ENABLED_RESOLVED="$EDIT_ENABLED"
export TIER2_EXPORT_PAYLOAD_DIR_RESOLVED="${EXPORT_PAYLOAD_DIR:-}"
export TIER2_REQUEST_SILO_RESOLVED="${REQUEST_SILO:-}"
export TIER2_OPEN_CLASS_RESOLVED="${OPEN_CLASS:-}"
APP_ARGV_JOINED="$(printf '%q ' "${APP_ARGV[@]}")"
export TIER2_APP_ARGV_JOINED="$APP_ARGV_JOINED"

# The admin-direct secctx path forks qdistro-secctx-exec in-process, so the
# WRAPPER_BODY shell inherits all the exported TIER2_*_RESOLVED / QDWIN_* /
# WAYLAND_DISPLAY / XDG_RUNTIME_DIR vars above by plain process inheritance.
# The ROOT-launcher path goes through `runuser -u admin`, which runs PAM and
# RESETS the environment to admin's login env — silently stripping every one of
# those exports, so the wrapper would run mis-configured (empty image, no
# per-container dir, no socket name). Re-pass them EXPLICITLY through `env` in
# the runuser leg. Built here (right after the exports) as NAME=VALUE pairs so
# the list cannot drift from the exports. NB secctx-exec REWRITES
# WAYLAND_DISPLAY for its child, so we pass the pre-secctx value and let the
# rewrite happen as usual; XDG_RUNTIME_DIR/HOME are also set by the runuser leg.
SECCTX_ENV_PASS=(
    "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
    "QDWIN_OUTER_DISPLAY=$QDWIN_OUTER_DISPLAY"
    "QDWIN_NESTED_MODE=$QDWIN_NESTED_MODE"
    "QDWIN_LAUNCH_TOKEN=$QDWIN_LAUNCH_TOKEN"
    "TIER2_INNER_SOCKET=$TIER2_INNER_SOCKET"
    "TIER2_PERCONT_DIR=$TIER2_PERCONT_DIR"
    "TIER2_ADMIN_UID_RESOLVED=$TIER2_ADMIN_UID_RESOLVED"
    "TIER2_IMAGE=$TIER2_IMAGE"
    "TIER2_CONTAINER=$TIER2_CONTAINER"
    "TIER2_DISPOSABLE_RESOLVED=$TIER2_DISPOSABLE_RESOLVED"
    "TIER2_LEASE_TTL_RESOLVED=$TIER2_LEASE_TTL_RESOLVED"
    "TIER2_LEASE_CREATED_RESOLVED=$TIER2_LEASE_CREATED_RESOLVED"
    "TIER2_LEASE_PROCTREE_RESOLVED=$TIER2_LEASE_PROCTREE_RESOLVED"
    "TIER2_LEASE_PROCTREE_GRACE_RESOLVED=$TIER2_LEASE_PROCTREE_GRACE_RESOLVED"
    "TIER2_LEASE_WORKFLOW_RESOLVED=$TIER2_LEASE_WORKFLOW_RESOLVED"
    "TIER2_QDWIN_SHELL_SO_RESOLVED=$TIER2_QDWIN_SHELL_SO_RESOLVED"
    "TIER2_STATE_PATH_RESOLVED=$TIER2_STATE_PATH_RESOLVED"
    "TIER2_NETWORK_RESOLVED=$TIER2_NETWORK_RESOLVED"
    "TIER2_PIDS_LIMIT_RESOLVED=$TIER2_PIDS_LIMIT_RESOLVED"
    "TIER2_MEMORY_RESOLVED=$TIER2_MEMORY_RESOLVED"
    "TIER2_CPUS_RESOLVED=$TIER2_CPUS_RESOLVED"
    "TIER2_KEEP_CAPS_RESOLVED=$TIER2_KEEP_CAPS_RESOLVED"
    "TIER2_ALLOW_PRIVESC_RESOLVED=$TIER2_ALLOW_PRIVESC_RESOLVED"
    "TIER2_SECCOMP_PROFILE_RESOLVED=$TIER2_SECCOMP_PROFILE_RESOLVED"
    "TIER2_EXPORT_ENABLED_RESOLVED=$TIER2_EXPORT_ENABLED_RESOLVED"
    "TIER2_EDIT_ENABLED_RESOLVED=$TIER2_EDIT_ENABLED_RESOLVED"
    "TIER2_EXPORT_PAYLOAD_DIR_RESOLVED=$TIER2_EXPORT_PAYLOAD_DIR_RESOLVED"
    "TIER2_REQUEST_SILO_RESOLVED=$TIER2_REQUEST_SILO_RESOLVED"
    "TIER2_OPEN_CLASS_RESOLVED=$TIER2_OPEN_CLASS_RESOLVED"
    "TIER2_APP_ARGV_JOINED=$TIER2_APP_ARGV_JOINED"
    "TIER2_DEBUG=${TIER2_DEBUG:-0}"
)

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
elif [ "${TIER2_DISPOSABLE_RESOLVED:-0}" = 1 ]; then
    # Disposable home is a WRITABLE tmpfs (07-plan "tmpfs home"): the app can
    # run, but every byte lives in RAM and is discarded on teardown by
    # construction (tmpfs + --rm — never the persistent host fs). This is the
    # enumerated writable surface; persistent volume attachments are denied
    # to disp-* silos by broker policy.
    PODMAN_HARDENING+=(
        --mount type=tmpfs,destination=/home/admin,tmpfs-size=256m,tmpfs-mode=0700,U
        # Authoritative disposable marker: the session-manager reaper lists +
        # filters by THIS label (not the name shape), so it can never reap an
        # admin container that merely happens to be named disp-*. The name
        # regex stays as defence-in-depth on the remove path.
        --label qdistro_disposable=1
    )
    # Optional lease labels (each only when the matching knob was opted in). The
    # session-manager periodic sweep reaps a TTL disposable once now-created>ttl,
    # and a proctree disposable once its inner tree is PID1-only past grace;
    # absent labels mean no lease (the default). The workflow id is the grouping
    # key DisposeByWorkflow filters on. Read from the exported TIER2_LEASE_*
    # _RESOLVED env (this is the WRAPPER_BODY shell, which does not see the parent
    # script non-exported DISP_LEASE_* locals). The created label is shared by the
    # TTL and proctree windows, so emit it whenever EITHER is present.
    if [ -n "${TIER2_LEASE_TTL_RESOLVED:-}" ]; then
        PODMAN_HARDENING+=( --label "qdistro_lease_ttl=${TIER2_LEASE_TTL_RESOLVED}" )
    fi
    if [ "${TIER2_LEASE_PROCTREE_RESOLVED:-}" = "1" ]; then
        PODMAN_HARDENING+=( --label "qdistro_lease_proctree=1" )
        if [ -n "${TIER2_LEASE_PROCTREE_GRACE_RESOLVED:-}" ]; then
            PODMAN_HARDENING+=(
                --label "qdistro_lease_proctree_grace=${TIER2_LEASE_PROCTREE_GRACE_RESOLVED}"
            )
        fi
    fi
    if [ -n "${TIER2_LEASE_TTL_RESOLVED:-}" ] \
       || [ "${TIER2_LEASE_PROCTREE_RESOLVED:-}" = "1" ]; then
        PODMAN_HARDENING+=( --label "qdistro_lease_created=${TIER2_LEASE_CREATED_RESOLVED}" )
    fi
    if [ -n "${TIER2_LEASE_WORKFLOW_RESOLVED:-}" ]; then
        PODMAN_HARDENING+=( --label "qdistro_lease_workflow=${TIER2_LEASE_WORKFLOW_RESOLVED}" )
    fi
    # Export-back labels (07-plan P2 / D7 copy-exception): forensics + a
    # defence-in-depth join. The IMPORTER does NOT trust these for routing — it
    # reads the launcher-written meta.json (outside the container) — but they let
    # a forensic reader correlate a container with its request silo / open class,
    # and the boot sweep uses qdistro_export to find staging to reap.
    if [ "${TIER2_EXPORT_ENABLED_RESOLVED:-0}" = "1" ]; then
        PODMAN_HARDENING+=(
            --label "qdistro_export=1"
            --label "qdistro_request_silo=${TIER2_REQUEST_SILO_RESOLVED}"
            --label "qdistro_open_class=${TIER2_OPEN_CLASS_RESOLVED}"
        )
        # Edit-round-trip: a forensic marker only (the landing mode is decided at
        # import from meta.json, never from this label).
        if [ "${TIER2_EDIT_ENABLED_RESOLVED:-0}" = "1" ]; then
            PODMAN_HARDENING+=( --label "qdistro_edit=1" )
        fi
    fi
fi
PODMAN_HARDENING+=(
    --mount type=tmpfs,destination=/home/admin/.cache,tmpfs-size=32m,tmpfs-mode=0700,U
    --tmpfs=/run:size=4m,mode=0755
)
# Open-in-disposable RO input (07-plan P2 / D7 mounts-not-copies). A single
# host file/dir, validated + admin-gated by the parent before podman, bound
# READ-ONLY under /mnt/input/<basename>. Hardening on the bind itself:
#   ro       — the disposable cannot write back (D7: output leaves only via a
#              future brokered export, never an in-place edit of the source).
#   nosuid   — a setuid bit on the (untrusted) input cannot escalate.
#   nodev    — no device nodes honoured from the input.
#   noexec   — the input bytes cannot be executed in-place.
#   rprivate — the bind is private (mount events do not propagate either way).
# The container already runs --read-only rootfs + no-new-privileges; this is
# the only writable-shaped path into it from the host and it is read-only.
if [ -n "${TIER2_RO_INPUT_REAL_RESOLVED:-}" ]; then
    _ro_target="/mnt/input/${TIER2_RO_INPUT_BASENAME_RESOLVED}"
    PODMAN_HARDENING+=(
        -v "${TIER2_RO_INPUT_REAL_RESOLVED}:${_ro_target}:ro,nosuid,nodev,noexec,rprivate"
    )
fi
# Export-back output (07-plan P2 / D7 copy-exception): the per-launch host
# staging payload dir bound READ-WRITE under /mnt/output so the disposable can
# drop artifacts to be promoted back to the requesting silo. Only present when an
# export-capable open class opted in (triple-gated: registry export=true + the
# spawn-time AND import-time qdistro.dispose.export:<class> broker rules). The
# bind is the ONLY writable host path into the container besides the tmpfs home:
#   nosuid/nodev/noexec — the disposable cannot stage a setuid/device/executable
#                         payload that means anything on the host side.
#   rprivate            — mount events do not propagate either way.
# The importer treats everything written here as HOSTILE (regular-files-only,
# all-or-nothing, caps, O_NOFOLLOW) and only reads it after the container is gone.
if [ "${TIER2_EXPORT_ENABLED_RESOLVED:-0}" = "1" ] \
   && [ -n "${TIER2_EXPORT_PAYLOAD_DIR_RESOLVED:-}" ]; then
    PODMAN_HARDENING+=(
        -v "${TIER2_EXPORT_PAYLOAD_DIR_RESOLVED}:/mnt/output:rw,nosuid,nodev,noexec,rprivate"
    )
fi
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
        echo "spawn-tier2-wrapper: FATAL: seccomp profile $TIER2_SECCOMP_PROFILE_RESOLVED disappeared before podman run" >&2
        exit 4
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
    if [ "$ROOT_LAUNCHER" = 1 ]; then
        # Root-launcher topology (mirrors tier3/spawn-tier3.sh:442-463): we are
        # root, and we run qdistro-secctx-exec via `runuser -u admin` so the
        # helper runs at the ADMIN uid (introspectable by the unprivileged
        # admin compositor) while its DIRECT launcher parent is `runuser`
        # (root). That satisfies BOTH qdistro-secctx-exec's trusted-launcher
        # check AND qdwin's root-parent attestation, so the disposable's
        # qdistro.disp.<token> app_id is stamped on the wire. The WRAPPER_BODY
        # (which execs the rootless `podman run`) runs as admin too, keeping
        # the --userns=keep-id state model intact.
        LAUNCHREC_TOKEN="$(gen_launch_token "spawn-tier2")"
        LAUNCHREC_FILE_ID="$(gen_launch_token "spawn-tier2")"
        # The launch record is written by secctx-exec AS ADMIN under the admin
        # runtime dir, so it must live there (root reads it back below to
        # register the launch with the broker).
        LAUNCHREC_PATH="$RUNTIME_DIR/qdistro-tier2-launchrec-$LAUNCHREC_FILE_ID.pid"
        runuser -u "$ADMIN_USER" -- rm -f "$LAUNCHREC_PATH" 2>/dev/null || true
        # `runuser` resets the env (PAM), so re-pass EVERYTHING the chain needs
        # explicitly via `env`: the secctx-exec controls + the WRAPPER_BODY's
        # full TIER2_*_RESOLVED set (SECCTX_ENV_PASS). Without the latter the
        # wrapper would see empty vars and mis-launch.
        # runuser runs PAM and sets HOME/USER/LOGNAME for admin itself; we only
        # need to re-assert XDG_RUNTIME_DIR (the admin runtime dir) + the secctx
        # controls + the wrapper's TIER2_*_RESOLVED env on top.
        runuser -u "$ADMIN_USER" -- env \
            XDG_RUNTIME_DIR="$RUNTIME_DIR" \
            QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
            QDISTRO_LAUNCH_RECORD_PATH="$LAUNCHREC_PATH" \
            QDISTRO_LAUNCH_RECORD_TOKEN="$LAUNCHREC_TOKEN" \
            "${SECCTX_ENV_PASS[@]}" \
            qdistro-secctx-exec \
                --sandbox-engine "$ENGINE" \
                --app-id "$SECCTX_APPID" \
                --instance-id "$LAUNCH_TOKEN" \
                -- bash -c "$WRAPPER_BODY" &
    elif [ "$(id -u)" -ne 0 ] && [ "${QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED:-0}" != "1" ]; then
        echo "spawn-tier2: WARN: secctx stamping requires a direct root launcher parent;" >&2
        echo "             dev profile running un-tagged. Use the root launcher path (TIER2_ROOT_LAUNCHER=1)," >&2
        echo "             or set QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1 only with QDWIN_SECCTX_OPEN=1 for dev tests." >&2
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
        echo "spawn-tier2: WARN: qdistro-secctx-exec not in PATH; dev profile running un-tagged" >&2
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
