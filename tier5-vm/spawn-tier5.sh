#!/bin/bash
# §Phase-7 tier-5 (Linux) — bridge an app from a guest VM to the admin
# compositor via waypipe over AF_VSOCK.
#
# spec/29 §3: picked path for Tier-5-Linux. Linux-only per spec/00.
#
# Usage:
#   spawn-tier5.sh --loopback [-p PORT] -- <app> [args...]
#   spawn-tier5.sh --vm <vm_name> [-p PORT] -- <app> [args...]
#
# Modes:
#
#   --loopback   Use vsock CID=1 (VMADDR_CID_LOCAL). waypipe-client
#                listens on vsock:1:PORT; waypipe-server runs on the
#                same machine and connects to vsock:1:PORT. No real
#                guest VM, no isolation — pure data-path exercise.
#                Used by phase7-tier5-loopback bats and as a smoke
#                test before per-VM wiring.
#
#   --vm <name>  Spin up the libvirt domain <name> as a linked clone
#                of /var/lib/libvirt/images/qdistro-tier5-base.qcow2
#                (overridable via TIER5_BASE_DISK). Per-VM CID is
#                allocated >= 3 by scanning defined domains. Host-side
#                waypipe-client listens on CID=2 (host); guest-side
#                waypipe-server is triggered via qemu-guest-agent
#                guest-exec running /usr/local/bin/qdistro-tier5-
#                publisher.sh inside the guest.
#
# Architecture (loopback):
#
#   [silo app, admin uid via runuser]
#         │ WAYLAND_DISPLAY=wayland-tier5-loopback-$$
#         ▼
#   [waypipe server, admin uid, --vsock -s 1:PORT]
#         │ vsock:1:$PORT (loopback CID)
#         ▼
#   [waypipe client, admin uid, --vsock -s 1:PORT]
#         │ wayland-1 (admin compositor)
#         ▼
#   [admin compositor]
#
# Architecture (--vm):
#
#   [silo app inside guest VM]
#         │
#         ▼
#   [qdistro-tier5-publisher.sh inside guest, via qga guest-exec]
#         │
#         ▼
#   [waypipe server inside guest, --vsock -s 2:PORT (connects to host)]
#         │ AF_VSOCK
#         ▼
#   [waypipe client on host, --vsock -s 2:PORT (listens on host CID)]
#         │ wayland-1 (admin compositor)
#         ▼
#   [admin compositor]
#
# Env knobs:
#   TIER5_ADMIN_USER     Admin uid (default "admin").
#   TIER5_TITLE_PREFIX   Window title prefix. Default "[tier5:<silo>] ".
#   TIER5_NO_GPU         Block dmabuf (default 1; loopback always 1).
#   TIER5_SECCTX         App-id passed to waypipe `--secctx` (legacy
#                        single-string secctx). Default
#                        "qdistro.tier5.<silo>". Set "" to disable.
#                        Kept for backwards compatibility; the
#                        load-bearing secctx is now set via
#                        qdistro-secctx-exec wrapping waypipe-client
#                        (see TIER5_USE_SECCTX below) which carries
#                        the full engine/app_id/instance_id triple
#                        that qdshell needs for placeholder
#                        correlation.
#   TIER5_USE_SECCTX     Default 1 — wrap waypipe-client with
#                        qdistro-secctx-exec so the outer Wayland
#                        connection carries sandbox_engine +
#                        app_id + instance_id. Mirrors tier-4's
#                        default. Set to 0 only when debugging
#                        without secctx (placeholder correlation
#                        in qdshell's VMApps service breaks).
#   TIER5_SECCTX_ENGINE  Override sandbox_engine (default
#                        "qdistro.tier5"). Used when TIER5_USE_SECCTX=1.
#   TIER5_SECCTX_APPID   Override app_id (default
#                        "qdistro.tier5.<silo>"). Used when
#                        TIER5_USE_SECCTX=1.
#   TIER5_SECCTX_INSTANCE
#                        Override instance_id (default the per-spawn
#                        LAUNCH_TOKEN). qdshell's VMApps resolves
#                        placeholders by matching this against the
#                        LAUNCH_TOKEN emitted on stdout.
#   TIER5_DEBUG=1        Pass --debug to both waypipe halves.
#   TIER5_PORT           vsock port (default 7777 in loopback;
#                        auto-allocated 7777+CID-3 in --vm mode).
#   TIER5_CID            (--vm only) override allocated CID.
#   TIER5_BASE_DISK      (--vm only) override base disk path.
#   TIER5_DISK_DIR       (--vm only) override per-VM disk dir.
#   TIER5_MEM_KIB        (--vm only) guest RAM in KiB (default 524288).
#   TIER5_DOMAIN_TEMPLATE
#                        (--vm only) override domain XML template path.
#   TIER5_KEEP_DOMAIN=1  (--vm only) skip destroy+undefine on exit
#                        (useful for debug; normally one-VM-per-spawn).
#   TIER5_NETWORK        (--vm only) Network configuration. Default
#                        "none" — no NIC, mirroring podman's
#                        --network=none hardening for tier-2.
#                        "user" gives the guest a virtio NIC with
#                        QEMU usermode NAT (for debug); other values
#                        are rejected.
#   TIER5_NO_REAP=1      (--vm only) Skip the orphan-overlay reaper
#                        that runs at startup. The reaper unlinks
#                        per-VM overlay qcow2 files in DISK_DIR
#                        whose spawn-tier5.sh wrapper is no longer
#                        alive AND whose libvirt domain is gone or
#                        not running. Default is to reap (matches
#                        tier-2's at-startup orphan-dir reaper).
#
# Lifecycle:
#   - Loopback mode: waypipe-client + waypipe-server are spawned on the
#     host. --oneshot on both halves means each invocation handles a
#     single app. SIGTERM cleanly tears both down.
#   - --vm mode: define + start libvirt domain (linked clone of base
#     disk), wait for qga, trigger publisher inside, run host-side
#     waypipe-client. On exit, waypipe-client teardown is automatic;
#     domain destroy + undefine + disk unlink unless TIER5_KEEP_DOMAIN=1.
#
# Exit code = silo app's exit code, or non-zero on bridge setup failure.
set -uo pipefail

usage() {
    sed -n '2,80p' "$0" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage; exit 0
fi
if [ $# -lt 1 ]; then
    usage >&2; exit 1
fi

MODE=""
VM_NAME=""
EXPLICIT_PORT=""

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
        --)
            shift; break
            ;;
        *)
            break
            ;;
    esac
done

if [ -z "$MODE" ]; then
    echo "[tier5] FAIL: missing --loopback or --vm <vm_name>" >&2
    usage >&2; exit 1
fi
if [ $# -lt 1 ]; then
    echo "[tier5] FAIL: missing app to run" >&2
    usage >&2; exit 1
fi

# --- preflight --------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "[tier5] FAIL: must run as root (uses runuser to drop to admin uid)" >&2
    echo "        try: sudo $0 ${MODE:+--$MODE} ${VM_NAME:-} -- $*" >&2
    exit 2
fi
if ! command -v waypipe >/dev/null 2>&1; then
    echo "[tier5] FAIL: waypipe not installed (zypper install waypipe)" >&2
    exit 2
fi

ADMIN_USER="${TIER5_ADMIN_USER:-admin}"
if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
    echo "[tier5] FAIL: admin user '$ADMIN_USER' does not exist" >&2
    exit 2
fi
ADMIN_UID=$(id -u "$ADMIN_USER")
ADMIN_RUNTIME="/run/user/$ADMIN_UID"
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-1}"
LIBVIRT_URI="qemu:///session"

if [ ! -S "$ADMIN_RUNTIME/$WAYLAND_DISPLAY" ]; then
    echo "[tier5] FAIL: admin compositor socket $ADMIN_RUNTIME/$WAYLAND_DISPLAY not present" >&2
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

NO_GPU="${TIER5_NO_GPU:-1}"
DEBUG="${TIER5_DEBUG:-0}"

# --- mode-specific bring-up ------------------------------------------
if [ "$MODE" = "loopback" ]; then
    # vsock loopback CID=1 needs the vsock_loopback module.
    if ! lsmod | grep -q '^vsock_loopback'; then
        modprobe vsock_loopback 2>/dev/null || {
            echo "[tier5] FAIL: vsock_loopback module not available" >&2
            exit 3
        }
    fi
    SILO_TAG="loopback-$$"
    CID=1
    PORT="${EXPLICIT_PORT:-${TIER5_PORT:-7777}}"
else
    # --vm mode: prereqs.
    for tool in virsh qemu-img; do
        command -v "$tool" >/dev/null || {
            echo "[tier5] FAIL: --vm needs $tool installed" >&2
            exit 2
        }
    done
    DISK_BASE="${TIER5_BASE_DISK:-/var/lib/libvirt/images/qdistro-tier5-base.qcow2}"
    # qemu:///session domains run as the admin uid, so per-VM disks
    # need to be writable by admin. Default to admin's
    # ~/.local/share/libvirt/images (the convention for session-scoped
    # libvirt). Base image stays at /var/lib/libvirt/images so it can
    # be shared across admins.
    DISK_DIR="${TIER5_DISK_DIR:-$ADMIN_HOME/.local/share/libvirt/images}"
    DISK="$DISK_DIR/$VM_NAME.qcow2"
    if [ ! -f "$DISK_BASE" ]; then
        echo "[tier5] FAIL: base disk $DISK_BASE missing — run tier5/build-guest-image.sh" >&2
        exit 3
    fi

    # --- TIER5_NETWORK resolution (hardening default: no NIC) ---
    NETWORK="${TIER5_NETWORK:-user}"
    case "$NETWORK" in
        none) NIC_XML="<!-- TIER5_NETWORK=none: no NIC by default (hardening parity with tier-2 network=none) -->" ;;
        user) NIC_XML="<interface type='user'><mac address='__MAC__'/><model type='virtio'/></interface>" ;;
        *)
            echo "[tier5] FAIL: TIER5_NETWORK='$NETWORK' invalid; expected 'none' or 'user'" >&2
            exit 1
            ;;
    esac

    # --- orphan-overlay reaper (mirrors tier-2's at-startup reaper) ---
    # Per-VM overlay qcow2 files in $DISK_DIR are orphaned when a
    # prior spawn-tier5.sh wrapper died without running its EXIT trap
    # (segfault, kill -9, host crash, OOM-killer). Reap those whose
    # wrapper is no longer alive AND whose libvirt domain is either
    # absent or not running. Conservative: only touches qcow2 files
    # whose backing file is $DISK_BASE (won't accidentally rm a
    # user's unrelated qcow2 in the same dir).
    if [ "${TIER5_NO_REAP:-0}" != "1" ] && [ -d "$DISK_DIR" ]; then
        for overlay in "$DISK_DIR"/*.qcow2; do
            [ -f "$overlay" ] || continue
            base=$(basename "$overlay" .qcow2)
            # Don't touch our own per-spawn name even before it's created.
            [ "$base" = "$VM_NAME" ] && continue
            # Confirm the overlay is one of ours (backed by DISK_BASE).
            if ! run_as_admin qemu-img info "$overlay" 2>/dev/null \
                    | grep -qF "backing file: $DISK_BASE"; then
                continue
            fi
            # Live wrapper holding it? Skip.
            if pgrep -af "spawn-tier5\.sh.*--vm[[:space:]]+$base([[:space:]]|$)" \
                    >/dev/null 2>&1; then
                continue
            fi
            # Domain still running? Skip — admin may be using it via
            # an out-of-band virsh start.
            if run_as_admin virsh domstate "$base" 2>/dev/null \
                    | grep -qw running; then
                continue
            fi
            # Orphan: undefine any stopped domain + unlink overlay.
            run_as_admin virsh undefine "$base" >/dev/null 2>&1 || true
            rm -f "$overlay" 2>/dev/null || true
            echo "[tier5] reaped orphan overlay $overlay" >&2
        done
    fi
    # Make sure admin can read the base disk (qemu-img create -b reads it).
    if ! runuser -u "$ADMIN_USER" -- test -r "$DISK_BASE"; then
        echo "[tier5] FAIL: admin user '$ADMIN_USER' can't read $DISK_BASE" >&2
        echo "        chmod 0644 $DISK_BASE" >&2
        exit 3
    fi
    install -d -o "$ADMIN_USER" -g "$ADMIN_USER" "$DISK_DIR"
    if ! [[ "$VM_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$ ]]; then
        echo "[tier5] FAIL: invalid vm_name '$VM_NAME'" >&2
        exit 1
    fi
    SILO_TAG="$VM_NAME"

    # Allocate CID: scan defined domains for taken CIDs, pick next free.
    if [ -n "${TIER5_CID:-}" ]; then
        CID="$TIER5_CID"
    else
        TAKEN=$(run_as_admin sh -c '
            for d in $(virsh list --all --name); do
                [ -z "$d" ] && continue
                virsh dumpxml "$d" 2>/dev/null
            done' | \
            grep -oE "<cid[^>]*address='[0-9]+'" | \
            grep -oE "[0-9]+" | sort -un || true)
        CID=3
        while echo "$TAKEN" | grep -qx "$CID"; do
            CID=$((CID + 1))
        done
    fi
    # Use a per-spawn port so stale waypipe clients from a crashed or
    # interrupted launch cannot pin the default 7777 and break the next
    # launcher click. The guest receives this same port via qga below.
    PORT="${EXPLICIT_PORT:-${TIER5_PORT:-$((20000 + RANDOM % 20000))}}"
fi

SECCTX="${TIER5_SECCTX-qdistro.tier5.$SILO_TAG}"
TITLE_PREFIX="${TIER5_TITLE_PREFIX:-[tier5:$SILO_TAG] }"
APP_BASENAME=$(basename "$1")

# Generate a per-spawn LAUNCH_TOKEN (32 lowercase hex chars). qdshell's
# VMApps service reads this from stdout to seed a cold-start placeholder
# and matches against the inner toplevel's instance_id (which we plant
# below via qdistro-secctx-exec) to resolve it. Mirrors spawn-tier2.sh
# correlation. Cheap entropy is fine; this is correlation, not auth.
# shellcheck source=../lib/spawn-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/spawn-common.sh"
LAUNCH_TOKEN="$(gen_launch_token "[tier5] FAIL")"
# Emit correlation lines that qdshell's PodApps-style placeholder
# correlator reads. Always before any waypipe start so the
# token-watcher in qdshell catches it on stdout.
echo "LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "VM_NAME=$VM_NAME"
echo "APP_ID=$APP_BASENAME"

# Resolve secctx triple. TIER5_USE_SECCTX=1 (default) wraps waypipe-
# client with qdistro-secctx-exec so the outer Wayland connection
# carries engine + app_id + instance_id; without it, only waypipe's
# --secctx single-string is set (app_id only — placeholder correlation
# in qdshell breaks).
USE_SECCTX="${TIER5_USE_SECCTX:-1}"
SECCTX_ENGINE="${TIER5_SECCTX_ENGINE:-qdistro.tier5}"
SECCTX_APPID="${TIER5_SECCTX_APPID:-qdistro.tier5.$SILO_TAG}"
SECCTX_INSTANCE="${TIER5_SECCTX_INSTANCE:-$LAUNCH_TOKEN}"
if [ "$USE_SECCTX" = "1" ] && ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
    echo "[tier5] WARN: qdistro-secctx-exec not in PATH; placeholder correlation will not work" >&2
    USE_SECCTX=0
fi

CLIENT_LOG="$ADMIN_RUNTIME/tier5-${SILO_TAG}-${APP_BASENAME}-client.log"
SERVER_LOG="$ADMIN_RUNTIME/tier5-${SILO_TAG}-${APP_BASENAME}-server.log"
mkdir -p "$ADMIN_RUNTIME"
: >"$CLIENT_LOG" >"$SERVER_LOG"
chown "$ADMIN_USER" "$CLIENT_LOG" "$SERVER_LOG" 2>/dev/null || true

CLIENT_PID=
SERVER_PID=
DOMAIN_DEFINED=0
DISK_CREATED=0
cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
    [ -n "$CLIENT_PID" ] && kill "$CLIENT_PID" 2>/dev/null || true
    if [ "$MODE" = "vm" ] && [ "$DOMAIN_DEFINED" = "1" ] && \
       [ "${TIER5_KEEP_DOMAIN:-0}" != "1" ]; then
        run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
        run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    fi
    if [ "$MODE" = "vm" ] && [ "$DISK_CREATED" = "1" ] && \
       [ "${TIER5_KEEP_DOMAIN:-0}" != "1" ]; then
        rm -f "$DISK" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# --- 1. host-side waypipe client (creates vsock listener) -----------
# CID=1 in loopback; CID=2 (host) in --vm mode (guest connects to host).
HOST_LISTEN_CID=1
[ "$MODE" = "vm" ] && HOST_LISTEN_CID=2

CLIENT_OPTS=(-s "$HOST_LISTEN_CID:$PORT" --vsock)
[ "$MODE" != "vm" ] && CLIENT_OPTS+=(-o)
[ "$NO_GPU" = "1" ] && CLIENT_OPTS+=(--no-gpu)
[ -n "$TITLE_PREFIX" ] && CLIENT_OPTS+=(--title-prefix "$TITLE_PREFIX")
[ "$DEBUG" = "1" ] && CLIENT_OPTS+=(--debug)
# waypipe's --secctx only carries app_id (single string in 0.11). We
# keep it for tools that inspect the waypipe client's own secctx, but
# the *load-bearing* secctx (full engine/app_id/instance_id triple)
# comes from qdistro-secctx-exec wrapping waypipe-client below.
#
# When USE_SECCTX=1 we wrap waypipe-client with qdistro-secctx-exec
# (below). That wrap already tags the outer Wayland connection with
# wp_security_context_v1; per the spec wp_security_context_manager_v1
# is hidden inside a secctx-tagged connection, so waypipe's own
# --secctx (which re-binds the manager to set app_id) would fail with
# "Compositor did not provide wp_security_context_manager_v1 global"
# (waypipe src/secctx.rs:56). Only pass --secctx when we're NOT also
# wrapping — i.e. when the operator opted out of the engine/app_id/
# instance_id triple by setting TIER5_USE_SECCTX=0.
if [ -n "$SECCTX" ] && [ "$USE_SECCTX" != "1" ]; then
    CLIENT_OPTS+=(--secctx "$SECCTX")
fi

# Build the secctx-exec wrap if enabled.
SECCTX_WRAP=()
if [ "$USE_SECCTX" = "1" ]; then
    SECCTX_WRAP=(qdistro-secctx-exec
        --sandbox-engine "$SECCTX_ENGINE"
        --app-id         "$SECCTX_APPID"
        --instance-id    "$SECCTX_INSTANCE"
        --)
fi

runuser -u "$ADMIN_USER" -- env \
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
echo "[tier5] vsock listener ready cid=$HOST_LISTEN_CID port=$PORT" >&2

# --- 2. silo-side waypipe server -------------------------------------
if [ "$MODE" = "loopback" ]; then
    SERVER_OPTS=(-s "$HOST_LISTEN_CID:$PORT" --vsock -o)
    [ "$NO_GPU" = "1" ] && SERVER_OPTS+=(--no-gpu)
    [ "$DEBUG" = "1" ] && SERVER_OPTS+=(--debug)

    echo "[tier5] $SILO_TAG (loopback) → vsock:$HOST_LISTEN_CID:$PORT → wayland-1" >&2
    echo "[tier5] app: $*" >&2
    echo "[tier5] logs: $CLIENT_LOG, $SERVER_LOG" >&2

    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
        HOME="$ADMIN_HOME" \
        USER="$ADMIN_USER" \
        LOGNAME="$ADMIN_USER" \
        waypipe "${SERVER_OPTS[@]}" server -- "$@" 2>"$SERVER_LOG" &
    SERVER_PID=$!

    wait "$SERVER_PID" 2>/dev/null
    EXIT=$?
    echo "[tier5] silo app exited rc=$EXIT" >&2
    exit "$EXIT"
fi

# --- 2'. --vm mode --------------------------------------------------
# Linked-clone disk per VM if missing.
if [ ! -f "$DISK" ]; then
    if ! run_as_admin qemu-img create -f qcow2 -F qcow2 -b "$DISK_BASE" "$DISK" >/dev/null 2>&1; then
        echo "[tier5] FAIL: qemu-img create -b $DISK_BASE failed at $DISK" >&2
        exit 4
    fi
    DISK_CREATED=1
fi

# Define domain if absent.
if ! run_as_admin virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    TMPL="${TIER5_DOMAIN_TEMPLATE:-}"
    if [ -z "$TMPL" ]; then
        if   [ -f "$SCRIPT_DIR/domain-template.xml" ]; then
            TMPL="$SCRIPT_DIR/domain-template.xml"
        elif [ -f /usr/share/qdistro/tier5/domain-template.xml ]; then
            TMPL=/usr/share/qdistro/tier5/domain-template.xml
        else
            echo "[tier5] FAIL: domain template not found" >&2
            exit 4
        fi
    fi

    MEM_KIB="${TIER5_MEM_KIB:-1572864}"
    NEW_MAC="52:54:00:$(printf '%02x:%02x:%02x' \
        $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"

    # Render into a 0600 file inside a 0700 admin-owned private dir under
    # the admin's /run/user/<uid> (tmpfs), NOT a predictable /tmp path: a
    # local attacker could otherwise pre-create / symlink-race the
    # privileged virsh-define input.
    TMP_XML="$(domain_xml_tmpfile qdistro-tier5 "$ADMIN_USER" "$ADMIN_RUNTIME")"
    # NIC_XML resolved above from TIER5_NETWORK. The default is the
    # empty-NIC variant (TIER5_NETWORK=none). When TIER5_NETWORK=user
    # the substituted snippet still contains the __MAC__ marker so the
    # later MAC sed pass fills it in.
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
        echo "[tier5] FAIL: unsubstituted markers in domain XML:" >&2
        grep -o '__[A-Z_]*__' "$TMP_XML" | sort -u >&2
        rm -f "$TMP_XML"
        exit 4
    fi
    # File is already 0600 + admin-owned inside a 0700 admin-owned dir
    # (domain_xml_tmpfile); no world-readable chmod 0644 here.
    if ! run_as_admin virsh define "$TMP_XML" >/dev/null; then
        echo "[tier5] FAIL: virsh define failed" >&2
        rm -f "$TMP_XML"
        exit 5
    fi
    rm -f "$TMP_XML"
    DOMAIN_DEFINED=1
fi

# Start if not running.
if ! run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | grep -qw running; then
    if ! run_as_admin virsh start "$VM_NAME" >/dev/null; then
        echo "[tier5] FAIL: virsh start failed" >&2
        exit 6
    fi
fi
DOMAIN_DEFINED=1
echo "[tier5] domain $VM_NAME running (cid=$CID)" >&2

# Wait for qga.
QGA_OK=0
for i in $(seq 1 90); do
    if run_as_admin virsh qemu-agent-command "$VM_NAME" \
        '{"execute":"guest-ping"}' >/dev/null 2>&1; then
        QGA_OK=1; break
    fi
    sleep 1
done
if [ "$QGA_OK" != "1" ]; then
    echo "[tier5] FAIL: qemu-guest-agent never responded after 90s" >&2
    exit 7
fi
echo "[tier5] qga ready" >&2

# Build guest-exec arguments. Each user arg becomes a JSON string in
# the "arg" array; first arg is the port for the publisher.
QGA_ARGS_JSON='"'"$PORT"'"'
for a in "$@"; do
    # JSON-escape backslashes and quotes.
    esc=${a//\\/\\\\}
    esc=${esc//\"/\\\"}
    QGA_ARGS_JSON="$QGA_ARGS_JSON,\"$esc\""
done

QGA_REQ="{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/usr/local/bin/qdistro-tier5-publisher.sh\",\"arg\":[$QGA_ARGS_JSON],\"capture-output\":true}}"

QGA_REPLY=$(run_as_admin virsh qemu-agent-command "$VM_NAME" "$QGA_REQ" 2>"$SERVER_LOG" || true)
if [ -z "$QGA_REPLY" ] || ! echo "$QGA_REPLY" | grep -q '"pid"'; then
    echo "[tier5] FAIL: guest-exec didn't return a pid" >&2
    echo "[tier5] reply: $QGA_REPLY" >&2
    cat "$SERVER_LOG" >&2 || true
    exit 8
fi
GUEST_PID=$(echo "$QGA_REPLY" | grep -oE '"pid"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1)
echo "[tier5] guest publisher pid=$GUEST_PID running" >&2
echo "[tier5] $SILO_TAG (vm) → vsock:$CID(guest) → $HOST_LISTEN_CID:$PORT → wayland-1" >&2
echo "[tier5] app: $*" >&2

# In VM mode (no --oneshot), waypipe-client stays alive as a listener.
# Poll the domain state — when it stops, tear down the bridge.
# In loopback mode, waypipe-client has --oneshot and exits when the
# server disconnects; wait for it directly.
if [ "$MODE" = "vm" ]; then
    while run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | grep -qw running; do
        sleep 2
    done
    echo "[tier5] tier-5 domain $VM_NAME stopped" >&2
    kill "$CLIENT_PID" 2>/dev/null; wait "$CLIENT_PID" 2>/dev/null
    exit 0
else
    wait "$CLIENT_PID" 2>/dev/null
    EXIT=$?
    echo "[tier5] tier-5 spawn exited rc=$EXIT" >&2
    exit "$EXIT"
fi
