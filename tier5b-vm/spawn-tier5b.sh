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
#
# Env knobs (mirrors spawn-tier5.sh; --vm uses TIER5B_* names):
#   TIER5B_ADMIN_USER    Admin uid (default "admin").
#   TIER5B_TITLE_PREFIX  Window title prefix.
#                        Default "[tier5b:<app>:<silo>] ".
#   TIER5B_NO_GPU        Block dmabuf (default 1).
#   TIER5B_USE_SECCTX    Default 1 — wrap waypipe-client with
#                        qdistro-secctx-exec. Set to 0 only for
#                        debugging without secctx.
#   TIER5B_SECCTX_ENGINE Default "qdistro.tier5b".
#   TIER5B_SECCTX_APPID  Default "qdistro.tier5b.<silo>".
#   TIER5B_SECCTX_INSTANCE
#                        Default the per-spawn LAUNCH_TOKEN.
#   TIER5B_PORT          vsock port (default auto-allocated from CID).
#   TIER5B_CID           override allocated CID.
#   TIER5B_BASE_DISK     override base disk path.
#   TIER5B_DISK_DIR      override per-VM disk dir.
#   TIER5B_MEM_KIB       guest RAM in KiB (default 1048576 = 1 GiB;
#                        bigger than tier-5's 512 MiB because Firefox
#                        + content process + GPU process is hungrier).
#   TIER5B_DOMAIN_TEMPLATE override domain XML template path.
#   TIER5B_KEEP_DOMAIN=1 skip destroy+undefine on exit (debug).
#   TIER5B_NETWORK       "none" (default; hardening) or "user" (debug).
#   TIER5B_NO_REAP=1     skip orphan-overlay reaper at startup.
#   TIER5B_APP           Target app name; default "firefox". Used to
#                        derive the default base disk and the launcher
#                        metadata.
#
# Lifecycle:
#   - define + start libvirt domain (linked clone of per-app base disk)
#   - wait for qga
#   - host-side: spawn qdistro-secctx-exec -- waypipe-client (listens on
#     vsock CID=2:PORT)
#   - guest-side: trigger qdistro-tier5b-publisher.sh via qga guest-exec
#     which exec's waypipe-server connecting to host CID=2:PORT
#   - close-button / SIGTERM teardown:
#       1. SIGTERM the in-guest publisher via qga guest-exec
#       2. reap host-side waypipe-client (was --oneshot; exits when the
#          waypipe-server side closes)
#       3. virsh destroy + undefine + rm overlay  (unless KEEP_DOMAIN)
#
# Exit code = waypipe-client's exit code (== 0 on clean app shutdown).
set -uo pipefail

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

# --- preflight --------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
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

if [ ! -S "$ADMIN_RUNTIME/$WAYLAND_DISPLAY" ]; then
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
    for tool in virsh qemu-img; do
        command -v "$tool" >/dev/null || {
            echo "[tier5b] FAIL: --vm needs $tool installed" >&2
            exit 2
        }
    done
    DISK_BASE="${TIER5B_BASE_DISK:-/var/lib/libvirt/images/qdistro-tier5b-${APP_NAME}-base.qcow2}"
    DISK_DIR="${TIER5B_DISK_DIR:-$ADMIN_HOME/.local/share/libvirt/images}"
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
            run_as_admin virsh undefine "$base" >/dev/null 2>&1 || true
            rm -f "$overlay" 2>/dev/null || true
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

    if [ -n "${TIER5B_CID:-}" ]; then
        CID="$TIER5B_CID"
    else
        TAKEN=$(run_as_admin sh -c '
            for d in $(virsh list --all --name); do
                [ -z "$d" ] && continue
                virsh dumpxml "$d" 2>/dev/null
            done' | \
            grep -oE "cid[[:space:]]+address='[0-9]+'" | \
            grep -oE "[0-9]+" | sort -un || true)
        # Pick a per-app CID range that doesn't collide with tier-5 (3..).
        # Tier-5b starts at 100 to keep allocations cleanly separated in
        # `virsh list` for ops debugging. Caller can override via TIER5B_CID.
        CID=100
        while echo "$TAKEN" | grep -qx "$CID"; do
            CID=$((CID + 1))
        done
    fi
    PORT="${EXPLICIT_PORT:-${TIER5B_PORT:-$((7877 + CID - 100))}}"
fi

TITLE_PREFIX="${TIER5B_TITLE_PREFIX:-[tier5b:$APP_NAME:$SILO_TAG] }"

LAUNCH_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
if [ "${#LAUNCH_TOKEN}" -ne 32 ]; then
    echo "[tier5b] FAIL: could not generate launch token from /dev/urandom" >&2
    exit 5
fi
echo "LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "VM_NAME=$VM_NAME"
echo "APP_ID=$APP_NAME"
echo "TIER=5b"

USE_SECCTX="${TIER5B_USE_SECCTX:-1}"
SECCTX_ENGINE="${TIER5B_SECCTX_ENGINE:-qdistro.tier5b}"
SECCTX_APPID="${TIER5B_SECCTX_APPID:-qdistro.tier5b.$SILO_TAG}"
SECCTX_INSTANCE="${TIER5B_SECCTX_INSTANCE:-$LAUNCH_TOKEN}"
if [ "$USE_SECCTX" = "1" ] && ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
    echo "[tier5b] WARN: qdistro-secctx-exec not in PATH; placeholder correlation will not work" >&2
    USE_SECCTX=0
fi

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
cleanup() {
    # 1. Try to SIGTERM the guest publisher gracefully (lets Firefox
    #    write its session), then reap the waypipe-server inside guest.
    if [ "$MODE" = "vm" ] && [ -n "$GUEST_PID" ]; then
        run_as_admin virsh qemu-agent-command "$VM_NAME" \
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
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# --- 1. host-side waypipe client (creates vsock listener) -----------
HOST_LISTEN_CID=1
[ "$MODE" = "vm" ] && HOST_LISTEN_CID=2

CLIENT_OPTS=(-s "$HOST_LISTEN_CID:$PORT" --vsock -o)
[ "$NO_GPU" = "1" ] && CLIENT_OPTS+=(--no-gpu)
[ -n "$TITLE_PREFIX" ] && CLIENT_OPTS+=(--title-prefix "$TITLE_PREFIX")
[ "$DEBUG" = "1" ] && CLIENT_OPTS+=(--debug)
# Keep waypipe's --secctx for tools that inspect waypipe's own secctx.
# Load-bearing secctx comes from qdistro-secctx-exec wrap below.
CLIENT_OPTS+=(--secctx "qdistro.tier5b.$SILO_TAG")

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
echo "[tier5b] vsock listener ready cid=$HOST_LISTEN_CID port=$PORT" >&2

# --- 2. guest-side waypipe server ------------------------------------
if [ "$MODE" = "loopback" ]; then
    # Loopback path: run the in-tree publisher script directly on the
    # host (via QDISTRO_TIER5B_DRY_RUN for unit tests, or for real for
    # bats integration with a real Firefox if present).
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PUB="$SCRIPT_DIR/qdistro-tier5b-publisher.sh"
    if [ ! -x "$PUB" ]; then
        echo "[tier5b] FAIL: publisher script not executable at $PUB" >&2
        exit 5
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
    exit "$EXIT"
fi

# --- 2'. --vm mode --------------------------------------------------
if [ ! -f "$DISK" ]; then
    if ! run_as_admin qemu-img create -f qcow2 -F qcow2 -b "$DISK_BASE" "$DISK" >/dev/null 2>&1; then
        echo "[tier5b] FAIL: qemu-img create -b $DISK_BASE failed at $DISK" >&2
        exit 4
    fi
    DISK_CREATED=1
fi

if ! run_as_admin virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    TMPL="${TIER5B_DOMAIN_TEMPLATE:-}"
    if [ -z "$TMPL" ]; then
        if   [ -f "$SCRIPT_DIR/domain-template.xml" ]; then
            TMPL="$SCRIPT_DIR/domain-template.xml"
        elif [ -f /usr/share/qdistro/tier5b/domain-template.xml ]; then
            TMPL=/usr/share/qdistro/tier5b/domain-template.xml
        else
            echo "[tier5b] FAIL: domain template not found" >&2
            exit 4
        fi
    fi

    MEM_KIB="${TIER5B_MEM_KIB:-1048576}"
    NEW_MAC="52:54:00:$(printf '%02x:%02x:%02x' \
        $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"

    TMP_XML="/tmp/qdistro-tier5b-${VM_NAME}-$$.xml"
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
        exit 4
    fi
    chown "$ADMIN_USER" "$TMP_XML"; chmod 0644 "$TMP_XML"
    if ! run_as_admin virsh define "$TMP_XML" >/dev/null; then
        echo "[tier5b] FAIL: virsh define failed" >&2
        rm -f "$TMP_XML"
        exit 5
    fi
    rm -f "$TMP_XML"
    DOMAIN_DEFINED=1
fi

if ! run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | grep -qw running; then
    if ! run_as_admin virsh start "$VM_NAME" >/dev/null; then
        echo "[tier5b] FAIL: virsh start failed" >&2
        exit 6
    fi
fi
DOMAIN_DEFINED=1
echo "[tier5b] domain $VM_NAME running (cid=$CID)" >&2

QGA_OK=0
for i in $(seq 1 90); do
    if run_as_admin virsh qemu-agent-command "$VM_NAME" \
        '{"execute":"guest-ping"}' >/dev/null 2>&1; then
        QGA_OK=1; break
    fi
    sleep 1
done
if [ "$QGA_OK" != "1" ]; then
    echo "[tier5b] FAIL: qemu-guest-agent never responded after 90s" >&2
    exit 7
fi
echo "[tier5b] qga ready" >&2

# Build guest-exec arguments. First arg is the port for the publisher;
# subsequent args (anything after `--` on the command line) become
# extra args passed to the baked app binary.
QGA_ARGS_JSON='"'"$PORT"'"'
for a in "$@"; do
    esc=${a//\\/\\\\}
    esc=${esc//\"/\\\"}
    QGA_ARGS_JSON="$QGA_ARGS_JSON,\"$esc\""
done

QGA_REQ="{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/usr/local/bin/qdistro-tier5b-publisher.sh\",\"arg\":[$QGA_ARGS_JSON],\"capture-output\":true}}"

QGA_REPLY=$(run_as_admin virsh qemu-agent-command "$VM_NAME" "$QGA_REQ" 2>"$SERVER_LOG" || true)
if [ -z "$QGA_REPLY" ] || ! echo "$QGA_REPLY" | grep -q '"pid"'; then
    echo "[tier5b] FAIL: guest-exec didn't return a pid" >&2
    echo "[tier5b] reply: $QGA_REPLY" >&2
    cat "$SERVER_LOG" >&2 || true
    exit 8
fi
GUEST_PID=$(echo "$QGA_REPLY" | grep -oE '"pid"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1)
echo "[tier5b] guest publisher pid=$GUEST_PID running" >&2
echo "[tier5b] $SILO_TAG (vm) → vsock:$CID(guest) → $HOST_LISTEN_CID:$PORT → wayland-1" >&2
echo "[tier5b] app: $APP_NAME" >&2

# waypipe-client (--oneshot) exits when the connection is torn down.
wait "$CLIENT_PID" 2>/dev/null
EXIT=$?
echo "[tier5b] tier-5b spawn exited rc=$EXIT" >&2
exit "$EXIT"
