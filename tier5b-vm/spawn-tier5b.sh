#!/bin/bash
# §Phase-7 tier-5b (per-app VM-windowed) — bring up one VM that
# publishes ONE GUI app to the admin compositor via waypipe over
# AF_VSOCK, in `direct` publisher shape.
#
# Architecture (probe-verdict shape `direct`):
#
#   [target app inside guest VM]
#         │ wayland-1 (waypipe-server's emulated display in guest)
#         ▼
#   [qdistro-tier5b-publisher.sh inside guest, via qga guest-exec]
#         │
#         ▼
#   [waypipe-server inside guest, --vsock -s 2:PORT (connects to host)]
#         │ AF_VSOCK
#         ▼
#   [waypipe-client on host, --vsock -s 2:PORT, wrapped by
#    qdistro-secctx-exec so the outer wl_client carries the secctx
#    tag (engine=qdistro.tier5b, app_id=qdistro.tier5b.<silo>,
#    instance_id=<launch_token>)]
#         │ wayland-1 (admin compositor)
#         ▼
#   [admin compositor — chrome painted host-side from the secctx
#    triple (SSD titlebar in silo colour, ClipboardGate gated on the
#    same triple)]
#
# Why secctx is host-side: AF_VSOCK lacks SCM_RIGHTS, so the two-fd
# wp_security_context_v1.create_listener cannot traverse vsock. The
# host stamps the outer wl_client; this is the only architecturally
# possible path. See plan2/research/qdwin-nested-over-vsock/
# 02-waypipe-secctx-passthrough.md Resolution A.
#
# Usage:
#   spawn-tier5b.sh --vm <vm_name> [--app APP] [-p PORT]
#   spawn-tier5b.sh --loopback [--app APP] [-p PORT]
#   spawn-tier5b.sh --source-only  (test mode — load helpers, no launch)
#
# Env knobs (mirrors spawn-tier5.sh; --vm uses TIER5B_* names):
#   TIER5B_ADMIN_USER    Admin uid (default "admin").
#   TIER5B_TITLE_PREFIX  Window title prefix.
#                        Default "[tier5b:<app>:<silo>] ".
#   TIER5B_NO_GPU        Block dmabuf (default 1).
#   TIER5B_USE_SECCTX    Default 1 — wrap waypipe-client with
#                        qdistro-secctx-exec. Set to 0 only for
#                        debugging without secctx.
#   QDISTRO_ALLOW_NO_SECCTX
#                        Set to 1 to allow USE_SECCTX=1 + missing
#                        qdistro-secctx-exec to fall back to a no-op
#                        wrap (dev-only opt-out; production must
#                        keep the default hard-fail).
#   TIER5B_SECCTX_ENGINE Default "qdistro.tier5b".
#   TIER5B_SECCTX_APPID  Default "qdistro.tier5b.<silo>".
#   TIER5B_SECCTX_INSTANCE
#                        Default the per-spawn LAUNCH_TOKEN.
#   TIER5B_PORT          vsock port (default auto-allocated from CID).
#   TIER5B_CID           override allocated CID.
#   TIER5B_BASE_DISK     override base disk path. NOTE: ignored unless
#                        QDISTRO_TIER5B_DRY_RUN=1, to keep caller-
#                        controlled file paths out of the prod path.
#   TIER5B_DISK_DIR      override per-VM disk dir. (Same dry-run restriction.)
#   TIER5B_MEM_KIB       guest RAM in KiB (default 1048576 = 1 GiB;
#                        bigger than tier-5's 512 MiB because Firefox
#                        + content process + GPU process is hungrier).
#   TIER5B_DOMAIN_TEMPLATE override domain XML template path.
#                        (Same dry-run restriction.)
#   TIER5B_KEEP_DOMAIN=1 skip destroy+undefine on exit (debug).
#   TIER5B_NETWORK       "none" (default; hardening) or "user" (debug).
#   TIER5B_NO_REAP=1     skip orphan-overlay reaper at startup.
#   TIER5B_APP           Target app name; default "firefox". Used to
#                        derive the default base disk and the launcher
#                        metadata.
#   QDISTRO_TIER5B_DRY_RUN=1
#                        Dry-run mode for tests: relax caller-controlled
#                        path restrictions.
#   TIER5B_OVERLAY_HEADROOM_BYTES
#                        Free-space headroom (bytes) required ON TOP OF the
#                        base image's virtual size before creating the
#                        linked-clone overlay. Default 2 GiB. The budget is
#                        `base virtual-size + headroom`; if the overlay
#                        target filesystem has less free than that, the
#                        spawn FAILS CLOSED (exit 4 + QDISTRO_EVENT=error
#                        ERROR_CODE=enospc) instead of launching into a
#                        guaranteed mid-session ENOSPC.
#   QDISTRO_EVENT_SINK   (test hook) argv prefix the structured event
#                        MESSAGE is appended to instead of `logger`.
#   QDISTRO_FREE_BYTES_CMD / QDISTRO_BASE_VSIZE_CMD
#                        (test hooks) override the free-bytes / base
#                        virtual-size queries so the precheck can be driven
#                        both above and below budget without a live disk.
#
# Structured journald events (machine-readable; see qd_emit_event in
# lib/spawn-common.sh). Each is a single MESSAGE line of space-separated
# KEY=VALUE tokens, tagged SYSLOG_IDENTIFIER=qdistro-tier5b, so ops can
# `journalctl -t qdistro-tier5b -o json` and filter on QDISTRO_EVENT=:
#   launch  QDISTRO_EVENT=launch TIER=5b VM_NAME=<vm|loopback-PID>
#           APP_ID=<app> ADMIN_UID=<uid> MODE=<vm|loopback>
#           LAUNCH_TOKEN=<tok>
#   close   QDISTRO_EVENT=close  TIER=5b VM_NAME=<...> APP_ID=<app>
#           ADMIN_UID=<uid> EXIT_REASON=<app-exit|cleanup> RC=<rc>
#   error   QDISTRO_EVENT=error  TIER=5b VM_NAME=<...> APP_ID=<app>
#           ADMIN_UID=<uid> ERROR_CODE=<stable-code> EXIT=<exit-status>
# Stable ERROR_CODE values: enospc (overlay free-space budget),
#   overlay-create (qemu-img create failed), define (virsh define),
#   start (virsh start), qga-timeout (guest agent never answered),
#   guest-exec (publisher guest-exec returned no pid),
#   client-died (waypipe-client died during readiness poll).
#
# Lifecycle:
#   - define + start libvirt domain (linked clone of per-app base disk)
#   - wait for qga
#   - host-side: spawn qdistro-secctx-exec -- waypipe-client (listens on
#     vsock CID=2:PORT) under setsid so a qdshell crash doesn't reap it
#   - guest-side: trigger qdistro-tier5b-publisher.sh via qga guest-exec
#     which exec's waypipe-server connecting to host CID=2:PORT
#   - close-button / SIGTERM teardown (cleanup() is re-entrant-guarded
#     so a chained INT-then-TERM doesn't run the qga path twice):
#       1. SIGTERM the in-guest publisher via qga guest-exec
#       2. reap host-side waypipe-client (was --oneshot; exits when the
#          waypipe-server side closes)
#       3. virsh destroy + undefine + rm overlay  (unless KEEP_DOMAIN)
#
# Exit code = waypipe-client's exit code (== 0 on clean app shutdown).
set -uo pipefail

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
# CID allocation ceiling. AF_VSOCK CIDs are uint32, but libvirt and
# practical sysctl scenarios make it useful to bound this for ops
# debugging — a TAKEN list with junk shouldn't loop forever.
CID_MAX=4095

# Lock file for atomic CID/PORT pick + virsh define. /run/qdistro is
# preferred (tmpfs, root-owned); fall back to /tmp if /run is read-only.
DOMAIN_LOCK_DIR="/run/qdistro"
if ! mkdir -p "$DOMAIN_LOCK_DIR" 2>/dev/null; then
    DOMAIN_LOCK_DIR="/tmp"
fi
DOMAIN_LOCK_FILE="$DOMAIN_LOCK_DIR/tier5b.lock"

# ---------------------------------------------------------------------
# Helper: JSON-encode an arbitrary string via python3 json.dumps.
# Replaces the prior hand-rolled bash parameter-expansion escape, which
# only handled backslash and double-quote (security HIGH-2).
# Stdin = raw value; stdout = JSON-encoded literal (including quotes).
# ---------------------------------------------------------------------
json_encode_stdin() {
    python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))'
}

# ---------------------------------------------------------------------
# build_secctx_wrap USE_SECCTX ENGINE APPID INSTANCE
#
# Populates the SECCTX_WRAP bash array with either:
#   (qdistro-secctx-exec --sandbox-engine E --app-id A --instance-id I --)
# or empty (when USE_SECCTX != 1). Exported so the test suite can source
# this file in --source-only mode and verify the composition.
# ---------------------------------------------------------------------
build_secctx_wrap() {
    local use_secctx="$1"
    local engine="$2"
    local appid="$3"
    local instance="$4"
    SECCTX_WRAP=()
    if [ "$use_secctx" = "1" ]; then
        SECCTX_WRAP=(env QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1
            qdistro-secctx-exec
            --sandbox-engine "$engine"
            --app-id         "$appid"
            --instance-id    "$instance"
            --)
    fi
}

# ---------------------------------------------------------------------
# --source-only short-circuit. Used by unit tests that need to call
# build_secctx_wrap (and other helpers) without executing the top-level
# launch logic (no virsh, no waypipe, no root required).
# ---------------------------------------------------------------------
if [ "${1:-}" = "--source-only" ]; then
    return 0 2>/dev/null || exit 0
fi

usage() {
    sed -n '2,80p' "$0" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage; exit 0
fi

MODE=""
VM_NAME=""
EXPLICIT_PORT=""
APP_OVERRIDE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --loopback)
            MODE=loopback; shift
            ;;
        --vm)
            MODE=vm; shift
            VM_NAME="${1:?--vm requires <vm_name>}"; shift
            ;;
        -p|--port)
            shift
            EXPLICIT_PORT="${1:?-p requires PORT}"; shift
            ;;
        --app)
            shift
            APP_OVERRIDE="${1:?--app requires APP}"; shift
            ;;
        --)
            shift; break
            ;;
        *)
            break
            ;;
    esac
done

if [ -z "$MODE" ]; then
    echo "[tier5b] FAIL: missing --loopback or --vm <vm_name>" >&2
    usage >&2; exit 1
fi

APP_NAME="${APP_OVERRIDE:-${TIER5B_APP:-firefox}}"

DRY_RUN="${QDISTRO_TIER5B_DRY_RUN:-0}"

# --- preflight --------------------------------------------------------
if [ "$DRY_RUN" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    echo "[tier5b] FAIL: must run as root (uses runuser to drop to admin uid)" >&2
    exit 2
fi
if ! command -v waypipe >/dev/null 2>&1; then
    echo "[tier5b] FAIL: waypipe not installed (zypper install waypipe)" >&2
    exit 2
fi

ADMIN_USER="${TIER5B_ADMIN_USER:-admin}"
if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
    echo "[tier5b] FAIL: admin user '$ADMIN_USER' does not exist" >&2
    exit 2
fi
ADMIN_UID=$(id -u "$ADMIN_USER")
ADMIN_RUNTIME="/run/user/$ADMIN_UID"
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-1}"
LIBVIRT_URI="qemu:///session"

if [ "$DRY_RUN" != "1" ] && [ ! -S "$ADMIN_RUNTIME/$WAYLAND_DISPLAY" ]; then
    echo "[tier5b] FAIL: admin compositor socket $ADMIN_RUNTIME/$WAYLAND_DISPLAY not present" >&2
    exit 3
fi

run_as_admin() {
    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
        HOME="$ADMIN_HOME" \
        LIBVIRT_DEFAULT_URI="$LIBVIRT_URI" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$ADMIN_RUNTIME/bus" \
        "$@"
}

# qga wrapper with bounded timeout (correctness S1 / operational F3).
# `virsh qemu-agent-command --timeout 5` caps the guest-agent wait at
# 5 s so a wedged qga can't stall cleanup or trip the spawn watchdog.
qga_cmd() {
    # $1 = vm, $2 = JSON request
    run_as_admin virsh qemu-agent-command --timeout 5 "$1" "$2"
}

NO_GPU="${TIER5B_NO_GPU:-1}"
DEBUG="${TIER5B_DEBUG:-0}"

# --- mode-specific bring-up ------------------------------------------
if [ "$MODE" = "loopback" ]; then
    if ! lsmod | grep -q '^vsock_loopback'; then
        modprobe vsock_loopback 2>/dev/null || {
            echo "[tier5b] FAIL: vsock_loopback module not available" >&2
            exit 3
        }
    fi
    SILO_TAG="loopback-$$"
    CID=1
    PORT="${EXPLICIT_PORT:-${TIER5B_PORT:-7877}}"
else
    for tool in virsh qemu-img flock; do
        command -v "$tool" >/dev/null || {
            echo "[tier5b] FAIL: --vm needs $tool installed" >&2
            exit 2
        }
    done

    # Caller-controlled path knobs (security F4): only honour the env
    # overrides for TIER5B_BASE_DISK, TIER5B_DISK_DIR, TIER5B_DOMAIN_TEMPLATE
    # when QDISTRO_TIER5B_DRY_RUN=1. In prod, defaults are forced.
    if [ "$DRY_RUN" = "1" ]; then
        DISK_BASE="${TIER5B_BASE_DISK:-/var/lib/libvirt/images/qdistro-tier5b-${APP_NAME}-base.qcow2}"
        DISK_DIR="${TIER5B_DISK_DIR:-$ADMIN_HOME/.local/share/libvirt/images}"
    else
        DISK_BASE="/var/lib/libvirt/images/qdistro-tier5b-${APP_NAME}-base.qcow2"
        DISK_DIR="$ADMIN_HOME/.local/share/libvirt/images"
    fi
    DISK="$DISK_DIR/$VM_NAME.qcow2"
    if [ ! -f "$DISK_BASE" ]; then
        echo "[tier5b] FAIL: base disk $DISK_BASE missing — run tier5b/build-guest-image.sh --app $APP_NAME" >&2
        exit 3
    fi

    NETWORK="${TIER5B_NETWORK:-none}"
    case "$NETWORK" in
        none) NIC_XML="<!-- TIER5B_NETWORK=none: no NIC by default (hardening parity with tier-2 / tier-5 network=none) -->" ;;
        user) NIC_XML="<interface type='user'><mac address='__MAC__'/><model type='virtio'/></interface>" ;;
        *)
            echo "[tier5b] FAIL: TIER5B_NETWORK='$NETWORK' invalid; expected 'none' or 'user'" >&2
            exit 1
            ;;
    esac

    # --- orphan-overlay reaper (mirrors tier-5 / tier-2) ---
    # L10 TOCTOU fix: atomic check-then-rm. We `mv` the overlay to a
    # tmp name first (single rename = atomic); only if rename succeeds
    # do we follow up with undefine + rm. A concurrent spawn racing to
    # claim the same VM name will see the rename and bail.
    if [ "${TIER5B_NO_REAP:-0}" != "1" ] && [ -d "$DISK_DIR" ]; then
        for overlay in "$DISK_DIR"/*.qcow2; do
            [ -f "$overlay" ] || continue
            base=$(basename "$overlay" .qcow2)
            [ "$base" = "$VM_NAME" ] && continue
            if ! run_as_admin qemu-img info "$overlay" 2>/dev/null \
                    | grep -qF "backing file: $DISK_BASE"; then
                continue
            fi
            if pgrep -af "spawn-tier5b\.sh.*--vm[[:space:]]+$base([[:space:]]|$)" \
                    >/dev/null 2>&1; then
                continue
            fi
            if run_as_admin virsh domstate "$base" 2>/dev/null \
                    | grep -qw running; then
                continue
            fi
            # Atomic claim: rename overlay → .reaping. If another process
            # already moved it, mv fails and we skip.
            reap_target="${overlay}.reaping.$$"
            if ! mv -n "$overlay" "$reap_target" 2>/dev/null; then
                continue
            fi
            run_as_admin virsh undefine "$base" >/dev/null 2>&1 || true
            rm -f "$reap_target" 2>/dev/null || true
            echo "[tier5b] reaped orphan overlay $overlay" >&2
        done
    fi
    if ! runuser -u "$ADMIN_USER" -- test -r "$DISK_BASE"; then
        echo "[tier5b] FAIL: admin user '$ADMIN_USER' can't read $DISK_BASE" >&2
        echo "        chmod 0644 $DISK_BASE" >&2
        exit 3
    fi
    install -d -o "$ADMIN_USER" -g "$ADMIN_USER" "$DISK_DIR"
    if ! [[ "$VM_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$ ]]; then
        echo "[tier5b] FAIL: invalid vm_name '$VM_NAME'" >&2
        exit 1
    fi
    SILO_TAG="$VM_NAME"

    # --- atomic CID/PORT allocation under DOMAIN_LOCK_FILE ---------
    # HIGH H1/H2 fix: take a flock so two concurrent spawns can't pick
    # the same CID, and bound the while-loop at CID_MAX so a corrupt
    # TAKEN list can't spin forever.
    touch "$DOMAIN_LOCK_FILE" 2>/dev/null || true
    chmod 0644 "$DOMAIN_LOCK_FILE" 2>/dev/null || true
    exec 9>"$DOMAIN_LOCK_FILE"
    if ! flock -w 30 9; then
        echo "[tier5b] FAIL: could not acquire $DOMAIN_LOCK_FILE within 30s" >&2
        exit 5
    fi

    if [ -n "${TIER5B_CID:-}" ]; then
        CID="$TIER5B_CID"
    else
        TAKEN=$(run_as_admin sh -c '
            for d in $(virsh list --all --name); do
                [ -z "$d" ] && continue
                virsh dumpxml "$d" 2>/dev/null
            done' | \
            grep -oE "<cid[^>]*address='[0-9]+'" | \
            grep -oE "[0-9]+" | sort -un || true)
        # Pick a per-app CID range that doesn't collide with tier-5 (3..).
        # Tier-5b starts at 100 to keep allocations cleanly separated in
        # `virsh list` for ops debugging. Caller can override via TIER5B_CID.
        CID=100
        while echo "$TAKEN" | grep -qx "$CID"; do
            CID=$((CID + 1))
            if [ "$CID" -gt "$CID_MAX" ]; then
                echo "[tier5b] FAIL: CID range exhausted (next=$CID > CID_MAX=$CID_MAX)" >&2
                exit 5
            fi
        done
    fi
    PORT="${EXPLICIT_PORT:-${TIER5B_PORT:-$((7877 + CID - 100))}}"
fi

TITLE_PREFIX="${TIER5B_TITLE_PREFIX:-[tier5b:$APP_NAME:$SILO_TAG] }"

# shellcheck source=../lib/spawn-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/spawn-common.sh"
LAUNCH_TOKEN="$(gen_launch_token "[tier5b] FAIL")"
echo "LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "VM_NAME=$VM_NAME"
echo "APP_ID=$APP_NAME"
echo "TIER=5b"

# Structured-event context. For loopback VM_NAME is empty, so the event
# VM_NAME field uses SILO_TAG (loopback-<pid>) — a stable per-spawn id.
EVENT_TAG="qdistro-tier5b"
EVENT_VM="${VM_NAME:-$SILO_TAG}"
emit_launch_event() {
    qd_emit_event "$EVENT_TAG" launch \
        "TIER=5b" "VM_NAME=$EVENT_VM" "APP_ID=$APP_NAME" \
        "ADMIN_UID=$ADMIN_UID" "MODE=$MODE" "LAUNCH_TOKEN=$LAUNCH_TOKEN"
}
emit_close_event() {
    # $1 = exit reason (single token), $2 = rc
    qd_emit_event "$EVENT_TAG" close \
        "TIER=5b" "VM_NAME=$EVENT_VM" "APP_ID=$APP_NAME" \
        "ADMIN_UID=$ADMIN_UID" "EXIT_REASON=${1:-cleanup}" "RC=${2:-0}"
}
emit_error_event() {
    # $1 = stable error code, $2 = exit status
    qd_emit_event "$EVENT_TAG" error \
        "TIER=5b" "VM_NAME=$EVENT_VM" "APP_ID=$APP_NAME" \
        "ADMIN_UID=$ADMIN_UID" "ERROR_CODE=${1:-unknown}" "EXIT=${2:-1}"
}
# die_event CODE EXIT_STATUS MESSAGE...: emit a structured error event,
# print the human message to stderr, and exit fail-closed.
die_event() {
    local code="$1" status="$2"; shift 2
    echo "[tier5b] FAIL: $*" >&2
    emit_error_event "$code" "$status"
    # Make the paired close event (emitted by the EXIT-trap cleanup)
    # record the real failure reason/status instead of the default 0.
    CLOSE_REASON=error; CLOSE_RC="$status"
    exit "$status"
}

emit_launch_event

USE_SECCTX="${TIER5B_USE_SECCTX:-1}"
SECCTX_ENGINE="${TIER5B_SECCTX_ENGINE:-qdistro.tier5b}"
SECCTX_APPID="${TIER5B_SECCTX_APPID:-qdistro.tier5b.$SILO_TAG}"
SECCTX_INSTANCE="${TIER5B_SECCTX_INSTANCE:-$LAUNCH_TOKEN}"
# Hard-fail by default if USE_SECCTX=1 but the tagger is missing; the
# silent downgrade in P05b regressed the load-bearing security
# invariant. Dev workflows can opt out via QDISTRO_ALLOW_NO_SECCTX=1.
if [ "$USE_SECCTX" = "1" ] && ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
    if [ "${QDISTRO_ALLOW_NO_SECCTX:-0}" = "1" ]; then
        echo "[tier5b] WARN: qdistro-secctx-exec not in PATH; QDISTRO_ALLOW_NO_SECCTX=1, continuing without stamp" >&2
        USE_SECCTX=0
    else
        echo "[tier5b] FAIL: qdistro-secctx-exec not in PATH" >&2
        echo "        Set QDISTRO_ALLOW_NO_SECCTX=1 for dev-only opt-out." >&2
        exit 2
    fi
fi

# Echo the resolved triple so s108 stage 6 / qdshell PodApps
# placeholder correlator can grep for it (test-integrity F2).
echo "SECCTX_APPID=$SECCTX_APPID"
echo "SECCTX_ENGINE=$SECCTX_ENGINE"
echo "SECCTX_INSTANCE=$SECCTX_INSTANCE"

CLIENT_LOG="$ADMIN_RUNTIME/tier5b-${SILO_TAG}-${APP_NAME}-client.log"
SERVER_LOG="$ADMIN_RUNTIME/tier5b-${SILO_TAG}-${APP_NAME}-server.log"
mkdir -p "$ADMIN_RUNTIME"
: >"$CLIENT_LOG" >"$SERVER_LOG"
chown "$ADMIN_USER" "$CLIENT_LOG" "$SERVER_LOG" 2>/dev/null || true

CLIENT_PID=
SERVER_PID=
DOMAIN_DEFINED=0
DISK_CREATED=0
GUEST_PID=
CLEANUP_DONE=0
# Close-event bookkeeping. The normal app-exit paths set these to
# (app-exit, <rc>) just before they exit; a signal-driven teardown leaves
# the default (cleanup, <signal-rc>) so the close event still records why.
CLOSE_REASON=cleanup
CLOSE_RC=0
cleanup() {
    # Re-entrancy guard (correctness S1): a chained INT→TERM must not
    # rerun the qga path twice.
    if [ "$CLEANUP_DONE" = "1" ]; then
        return 0
    fi
    CLEANUP_DONE=1
    # Structured close event: emitted exactly once (re-entrancy guarded),
    # so ops can correlate the launch event with its teardown + reason.
    emit_close_event "$CLOSE_REASON" "$CLOSE_RC"
    # 1. Try to SIGTERM the guest publisher gracefully (lets Firefox
    #    write its session), then reap the waypipe-server inside guest.
    if [ "$MODE" = "vm" ] && [ -n "$GUEST_PID" ]; then
        qga_cmd "$VM_NAME" \
            "{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/bin/kill\",\"arg\":[\"-TERM\",\"$GUEST_PID\"]}}" \
            >/dev/null 2>&1 || true
    fi
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
    [ -n "$CLIENT_PID" ] && kill "$CLIENT_PID" 2>/dev/null || true
    if [ "$MODE" = "vm" ] && [ "$DOMAIN_DEFINED" = "1" ] && \
       [ "${TIER5B_KEEP_DOMAIN:-0}" != "1" ]; then
        run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
        run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    fi
    if [ "$MODE" = "vm" ] && [ "$DISK_CREATED" = "1" ] && \
       [ "${TIER5B_KEEP_DOMAIN:-0}" != "1" ]; then
        rm -f "$DISK" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'CLOSE_REASON=signal-int; CLOSE_RC=130; cleanup; exit 130' INT
trap 'CLOSE_REASON=signal-term; CLOSE_RC=143; cleanup; exit 143' TERM

# --- 1. host-side waypipe client (creates vsock listener) -----------
HOST_LISTEN_CID=1
[ "$MODE" = "vm" ] && HOST_LISTEN_CID=2

CLIENT_OPTS=(-s "$HOST_LISTEN_CID:$PORT" --vsock -o)
[ "$NO_GPU" = "1" ] && CLIENT_OPTS+=(--no-gpu)
[ -n "$TITLE_PREFIX" ] && CLIENT_OPTS+=(--title-prefix "$TITLE_PREFIX")
[ "$DEBUG" = "1" ] && CLIENT_OPTS+=(--debug)
# Note: the redundant `waypipe --secctx` flag was removed (security S5).
# Load-bearing secctx comes from qdistro-secctx-exec wrap; the inner
# waypipe-client flag was advisory and confused ops about which is
# authoritative.

build_secctx_wrap "$USE_SECCTX" "$SECCTX_ENGINE" "$SECCTX_APPID" "$SECCTX_INSTANCE"

# setsid so a qdshell crash (the parent of this script in production)
# doesn't propagate SIGHUP to waypipe-client. The client must outlive
# the shell — s108 stage 8 exercises this.
setsid runuser -u "$ADMIN_USER" -- env \
    WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
    XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
    HOME="$ADMIN_HOME" \
    "${SECCTX_WRAP[@]}" waypipe "${CLIENT_OPTS[@]}" client >"$CLIENT_LOG" 2>&1 &
CLIENT_PID=$!

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if grep -q 'Starting client main process' "$CLIENT_LOG" 2>/dev/null; then
        break
    fi
    sleep 0.2
done

# Operational S2: confirm the client is actually alive after the poll
# loop. If it crashed during startup, surface the failure here instead
# of waiting until the guest publisher times out.
if ! kill -0 "$CLIENT_PID" 2>/dev/null; then
    cat "$CLIENT_LOG" >&2 || true
    die_event client-died 6 "waypipe-client died during readiness poll"
fi
echo "[tier5b] vsock listener ready cid=$HOST_LISTEN_CID port=$PORT" >&2

# --- 2. guest-side waypipe server ------------------------------------
if [ "$MODE" = "loopback" ]; then
    # Loopback path: run the in-tree publisher script directly on the
    # host (via QDISTRO_TIER5B_DRY_RUN for unit tests, or for real for
    # bats integration with a real Firefox if present).
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PUB="$SCRIPT_DIR/qdistro-tier5b-publisher.sh"
    if [ ! -x "$PUB" ]; then
        die_event publisher-missing 5 "publisher script not executable at $PUB"
    fi
    echo "[tier5b] $SILO_TAG (loopback) → vsock:$HOST_LISTEN_CID:$PORT → wayland-1" >&2
    echo "[tier5b] app: $APP_NAME" >&2
    QDISTRO_TIER5B_APP_BIN="$APP_NAME" \
        runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
        HOME="$ADMIN_HOME" \
        bash "$PUB" "$PORT" "$@" 2>"$SERVER_LOG" &
    SERVER_PID=$!
    wait "$SERVER_PID" 2>/dev/null
    EXIT=$?
    echo "[tier5b] guest app exited rc=$EXIT" >&2
    CLOSE_REASON=app-exit; CLOSE_RC=$EXIT
    exit "$EXIT"
fi

# --- 2'. --vm mode --------------------------------------------------
# Free-space budget precheck (fail-closed): before creating the overlay,
# confirm the overlay target filesystem can hold the base image's full
# copy-on-write growth (its virtual size) plus a configurable headroom.
# Without this, a near-full disk launches and then dies mid-session with
# ENOSPC, corrupting whatever the guest was writing. Skip only when the
# overlay already exists (re-attach: we are not allocating new space).
if [ ! -f "$DISK" ]; then
    OVERLAY_HEADROOM="${TIER5B_OVERLAY_HEADROOM_BYTES:-2147483648}"  # 2 GiB
    if ! qd_free_space_check "$DISK_BASE" "$DISK_DIR" "$OVERLAY_HEADROOM"; then
        die_event enospc 4 "insufficient free space for overlay in $DISK_DIR (see precheck diagnostic above)"
    fi
    if ! run_as_admin qemu-img create -f qcow2 -F qcow2 -b "$DISK_BASE" "$DISK" >/dev/null 2>&1; then
        die_event overlay-create 4 "qemu-img create -b $DISK_BASE failed at $DISK"
    fi
    DISK_CREATED=1
fi

if ! run_as_admin virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    # Same dry-run gating as the disk paths (security F4).
    if [ "$DRY_RUN" = "1" ]; then
        TMPL="${TIER5B_DOMAIN_TEMPLATE:-}"
    else
        TMPL=""
    fi
    if [ -z "$TMPL" ]; then
        if   [ -f "$SCRIPT_DIR/domain-template.xml" ]; then
            TMPL="$SCRIPT_DIR/domain-template.xml"
        elif [ -f /usr/share/qdistro/tier5b/domain-template.xml ]; then
            TMPL=/usr/share/qdistro/tier5b/domain-template.xml
        else
            die_event template-missing 4 "domain template not found"
        fi
    fi

    MEM_KIB="${TIER5B_MEM_KIB:-1048576}"
    NEW_MAC="52:54:00:$(printf '%02x:%02x:%02x' \
        $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"

    # Private 0600 file in a 0700 admin-owned dir under /run/user/<uid>,
    # not a predictable /tmp path (privileged virsh-define input).
    TMP_XML="$(domain_xml_tmpfile qdistro-tier5b "$ADMIN_USER" "$ADMIN_RUNTIME")"
    NIC_XML_ESCAPED=$(printf '%s' "$NIC_XML" | sed 's|[\\/&]|\\&|g')
    sed \
        -e "s|__NIC_XML__|$NIC_XML_ESCAPED|g" \
        -e "s|__VM_NAME__|$VM_NAME|g" \
        -e "s|__MAC__|$NEW_MAC|g" \
        -e "s|__MEM_KIB__|$MEM_KIB|g" \
        -e "s|__CID__|$CID|g" \
        -e "s|__DISK_PATH__|$DISK|g" \
        "$TMPL" >"$TMP_XML"

    if grep -q '__[A-Z_]*__' "$TMP_XML"; then
        echo "[tier5b] FAIL: unsubstituted markers in domain XML:" >&2
        grep -o '__[A-Z_]*__' "$TMP_XML" | sort -u >&2
        rm -f "$TMP_XML"
        die_event domain-render 4 "unsubstituted markers in domain XML"
    fi
    # Already 0600 + admin-owned inside a 0700 admin-owned dir.
    if ! run_as_admin virsh define "$TMP_XML" >/dev/null; then
        rm -f "$TMP_XML"
        die_event define 5 "virsh define failed"
    fi
    rm -f "$TMP_XML"
    DOMAIN_DEFINED=1
fi

# Release the flock now that the domain is defined; the CID is
# committed to libvirt's domain XML, so other spawns scanning TAKEN
# will see it.
flock -u 9 2>/dev/null || true
exec 9>&- 2>/dev/null || true

if ! run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | grep -qw running; then
    if ! run_as_admin virsh start "$VM_NAME" >/dev/null; then
        die_event start 6 "virsh start failed"
    fi
fi
DOMAIN_DEFINED=1
echo "[tier5b] domain $VM_NAME running (cid=$CID)" >&2

QGA_OK=0
for i in $(seq 1 90); do
    if qga_cmd "$VM_NAME" \
        '{"execute":"guest-ping"}' >/dev/null 2>&1; then
        QGA_OK=1; break
    fi
    sleep 1
done
if [ "$QGA_OK" != "1" ]; then
    die_event qga-timeout 7 "qemu-guest-agent never responded after 90s"
fi
echo "[tier5b] qga ready" >&2

# Build guest-exec arguments via python3 json.dumps so every byte is
# encoded safely (security HIGH-2 fix). First arg is the port for the
# publisher; subsequent args (anything after `--` on the command line)
# become extra args passed to the baked app binary.
QGA_ARG_PARTS=()
QGA_ARG_PARTS+=("$(printf '%s' "$PORT" | json_encode_stdin)")
for a in "$@"; do
    QGA_ARG_PARTS+=("$(printf '%s' "$a" | json_encode_stdin)")
done
QGA_ARGS_JSON=$(IFS=,; echo "${QGA_ARG_PARTS[*]}")

QGA_REQ="{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/usr/local/bin/qdistro-tier5b-publisher.sh\",\"arg\":[$QGA_ARGS_JSON],\"capture-output\":true}}"

QGA_REPLY=$(qga_cmd "$VM_NAME" "$QGA_REQ" 2>"$SERVER_LOG" || true)
if [ -z "$QGA_REPLY" ] || ! echo "$QGA_REPLY" | grep -q '"pid"'; then
    echo "[tier5b] reply: $QGA_REPLY" >&2
    cat "$SERVER_LOG" >&2 || true
    die_event guest-exec 8 "guest-exec didn't return a pid"
fi
GUEST_PID=$(echo "$QGA_REPLY" | grep -oE '"pid"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1)
echo "[tier5b] guest publisher pid=$GUEST_PID running" >&2
echo "[tier5b] $SILO_TAG (vm) → vsock:$CID(guest) → $HOST_LISTEN_CID:$PORT → wayland-1" >&2
echo "[tier5b] app: $APP_NAME" >&2

# waypipe-client (--oneshot) exits when the connection is torn down.
wait "$CLIENT_PID" 2>/dev/null
EXIT=$?
echo "[tier5b] tier-5b spawn exited rc=$EXIT" >&2
CLOSE_REASON=app-exit; CLOSE_RC=$EXIT
exit "$EXIT"
