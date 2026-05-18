#!/bin/bash
# qdistro-tier4-publisher — runs inside the tier-4-guest VM.
#
# This file is the SOURCE-OF-TRUTH copy that lives in-tree under
# qdistro/tier4-vm-guest/. build-guest-image.sh copies it into the
# baked image at /usr/local/bin/. Shipping a free-standing copy here
# lets unit tests cover argv assembly + vsock-CID dispatch without
# booting a VM.
#
# Unlike tier-5 / tier-5b (which publish a single guest *app* via
# waypipe-server), tier-4-guest supports two qdwin-to-qdwin display
# publishers:
#
#   QDISTRO_TIER4_DISPLAY=waypipe (default):
#     Publish the nested qdwin's whole session as one outer toplevel
#     via waypipe-server.
#
#   QDISTRO_TIER4_DISPLAY=rdp:
#     Ask inner qdwin to subscribe one guest toplevel as a per-view RDP
#     stream, then bridge that guest-local RDP TCP port to the host over
#     AF_VSOCK. The host spawn-tier4.sh side connects a FreeRDP wl_client
#     to a local loopback forward, so the resulting host window is a
#     normal qdwin client with host-side chrome/secctx.
#
# Architecture: plan2/tasks/P10-tier4-guest-image-nested-qdwin.md
# §Phase C and plan2/tasks/P11-tier4-waypipe-migration.md.
#
# Invoked by the in-guest systemd unit qdistro-tier4-publisher.service
# *or* by the host via qemu-guest-agent guest-exec:
#
#   qdistro-tier4-publisher.sh <vsock_port>
#
# vsock_port is allocated by the host-side spawn-tier4 via the same
# flock+CID range mechanism tier5b uses.

set -uo pipefail

# Inner qdwin socket. Defaults to the standard wayland-0 under the
# admin runtime dir; can be overridden for tests.
WAYLAND_SOCKET="${QDISTRO_TIER4_WAYLAND_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/1000}/wayland-0}"

# Bystander binary path. Image bake places it at /usr/bin/qdwin-bystander
# via the qdwin-guest package; tests override.
BYSTANDER_BIN="${QDISTRO_TIER4_BYSTANDER_BIN:-/usr/bin/qdwin-bystander}"

load_guest_config() {
    local cfg="${QDISTRO_TIER4_CONFIG:-/etc/qdistro/tier4-publisher.conf}"
    local line key value
    [ -f "$cfg" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        key="${key%%[[:space:]]*}"
        case "$key" in
            QDISTRO_TIER4_STREAMING_METHOD|QDISTRO_TIER4_DISPLAY|\
            QDISTRO_TIER4_RDP_SUBSCRIBE|QDISTRO_TIER4_RDP_PEER_LABEL|\
            QDISTRO_TIER4_RDP_CREDS|QDISTRO_TIER4_WAYLAND_SOCKET|\
            QDISTRO_TIER4_BYSTANDER_BIN)
                if [ -z "${!key+x}" ]; then
                    printf -v "$key" '%s' "$value"
                    export "$key"
                fi
                ;;
            *)
                echo "[tier4-publisher] WARN: ignoring unknown config key '$key' in $cfg" >&2
                ;;
        esac
    done <"$cfg"
}

load_guest_config
WAYLAND_SOCKET="${QDISTRO_TIER4_WAYLAND_SOCKET:-${XDG_RUNTIME_DIR:-/run/user/1000}/wayland-0}"
BYSTANDER_BIN="${QDISTRO_TIER4_BYSTANDER_BIN:-/usr/bin/qdwin-bystander}"
if [ -n "${QDISTRO_TIER4_STREAMING_METHOD:-}" ] && [ -z "${QDISTRO_TIER4_DISPLAY:-}" ]; then
    QDISTRO_TIER4_DISPLAY="$QDISTRO_TIER4_STREAMING_METHOD"
fi
DISPLAY_MODE="${QDISTRO_TIER4_DISPLAY:-waypipe}"
RDP_SUBSCRIBE="${QDISTRO_TIER4_RDP_SUBSCRIBE:-last}"
RDP_PEER_LABEL="${QDISTRO_TIER4_RDP_PEER_LABEL:-tier4-rdp}"
RDP_CREDS_PATH="${QDISTRO_TIER4_RDP_CREDS:-/run/qdistro-tier4-rdp.env}"
case "$RDP_SUBSCRIBE" in
    last|''|*[!0-9]*)
        if [ "$RDP_SUBSCRIBE" != "last" ]; then
            echo "[tier4-publisher] FAIL: QDISTRO_TIER4_RDP_SUBSCRIBE must be 'last' or a numeric qdwin toplevel handle" >&2
            exit 2
        fi
        ;;
esac

usage() {
    echo "usage: $0 <vsock_port>" >&2
    echo "  env: QDISTRO_TIER4_WAYLAND_SOCKET (default \$XDG_RUNTIME_DIR/wayland-0)" >&2
    echo "       QDISTRO_TIER4_BYSTANDER_BIN  (default /usr/bin/qdwin-bystander)" >&2
    echo "       QDISTRO_TIER4_DISPLAY        (waypipe|rdp; default waypipe)" >&2
}

if [ $# -lt 1 ]; then
    usage
    exit 2
fi

PORT="$1"
shift

case "$DISPLAY_MODE" in
    waypipe|rdp) ;;
    *)
        echo "[tier4-publisher] FAIL: QDISTRO_TIER4_DISPLAY='$DISPLAY_MODE' invalid (expected waypipe or rdp)" >&2
        exit 2
        ;;
esac

# Port sanity. 1..65535. Reject everything else loudly. (Same rule as
# tier5b-publisher; the lock-and-pick discipline lives in spawn-tier4,
# not here, so the publisher just validates the port it's handed.)
case "$PORT" in
    ''|*[!0-9]*)
        echo "[tier4-publisher] FAIL: port '$PORT' is not an integer" >&2
        exit 2
        ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "[tier4-publisher] FAIL: port '$PORT' out of range 1..65535" >&2
    exit 2
fi

# Wayland-socket sanity. Refuse path traversal / shell metacharacters
# even though we control the env: the systemd unit could in principle
# inherit a tampered XDG_RUNTIME_DIR via guest-agent guest-exec, and
# we'd rather fail-closed than glob-expand into something weird. The
# socket itself can be either an absolute path (we use it verbatim) or
# a basename (we resolve under $XDG_RUNTIME_DIR for the existence check
# but still pass the basename to --connect because that's what
# WAYLAND_DISPLAY expects).
case "$WAYLAND_SOCKET" in
    *\;*|*\|*|*\&*|*'$'*|*'`'*|*$'\n'*)
        echo "[tier4-publisher] FAIL: WAYLAND_SOCKET '$WAYLAND_SOCKET' contains forbidden chars" >&2
        exit 2
        ;;
esac

# Resolve absolute vs basename. --connect takes a basename to match
# wl_display_connect's lookup rules; if the operator handed us an
# absolute path, derive the basename and the runtime dir from it.
case "$WAYLAND_SOCKET" in
    /*)
        SOCK_DIR="$(dirname "$WAYLAND_SOCKET")"
        SOCK_NAME="$(basename "$WAYLAND_SOCKET")"
        ;;
    *)
        SOCK_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
        SOCK_NAME="$WAYLAND_SOCKET"
        ;;
esac

LOG="${QDISTRO_TIER4_LOG:-/var/log/qdistro-tier4-publisher.log}"
if ! touch "$LOG" 2>/dev/null; then
    LOG="$(mktemp -t qdistro-tier4-publisher.XXXXXX.log)"
fi
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds): tier4-publisher port=$PORT sock_dir=$SOCK_DIR sock_name=$SOCK_NAME ==="

# Wait briefly for the vsock module on first boot (idempotent).
modprobe vhost_vsock 2>/dev/null || true
modprobe vsock 2>/dev/null || true
for _ in 1 2 3 4 5; do
    [ -e /dev/vsock ] && break
    sleep 0.5
done

# Wait for inner qdwin's wayland-0 to appear. The systemd unit
# qdistro-tier4-publisher.service has After=qdwin-guest-session.service
# but bring-up isn't synchronous on the Wayland-socket-ready event; a
# bounded poll plugs the race.
WAIT_BUDGET="${QDISTRO_TIER4_WAYLAND_WAIT:-30}"
if [ "${QDISTRO_TIER4_DRY_RUN:-0}" != "1" ]; then
    for _ in $(seq 1 "$WAIT_BUDGET"); do
        [ -S "$SOCK_DIR/$SOCK_NAME" ] && break
        sleep 1
    done
    if [ ! -S "$SOCK_DIR/$SOCK_NAME" ]; then
        echo "[tier4-publisher] FAIL: inner Wayland socket $SOCK_DIR/$SOCK_NAME never appeared (waited ${WAIT_BUDGET}s)" >&2
        exit 3
    fi
fi

# qdwin-bystander needs XDG_RUNTIME_DIR to point at the socket dir
# (libwayland resolves wayland-0 under $XDG_RUNTIME_DIR).
export XDG_RUNTIME_DIR="$SOCK_DIR"

tcp_loopback_accepts() {
    local port="$1"
    timeout 1 bash -c ":</dev/tcp/127.0.0.1/$port" >/dev/null 2>&1
}

# Dry-run hook for unit tests: write the resolved argv to a tmpfile
# and exit 0 instead of execing waypipe (which isn't installed in the
# unit-test sandbox). Mirrors tier5b's QDISTRO_TIER5B_DRY_RUN.
if [ "${QDISTRO_TIER4_DRY_RUN:-0}" = "1" ]; then
    DRY_OUT="${QDISTRO_TIER4_DRY_OUT:-/tmp/qdistro-tier4-dry.log}"
    {
        echo "display=$DISPLAY_MODE"
        echo "port=$PORT"
        echo "sock_dir=$SOCK_DIR"
        echo "sock_name=$SOCK_NAME"
        echo "bystander_bin=$BYSTANDER_BIN"
        echo "xdg_runtime_dir=$XDG_RUNTIME_DIR"
        if [ "$DISPLAY_MODE" = "rdp" ]; then
            echo "rdp_subscribe=$RDP_SUBSCRIBE"
            echo "rdp_creds_path=$RDP_CREDS_PATH"
            echo "argv=$BYSTANDER_BIN --inner-display $SOCK_NAME --subscribe $RDP_SUBSCRIBE --peer-label $RDP_PEER_LABEL"
            echo "bridge=socat VSOCK-LISTEN:$PORT,reuseaddr,fork TCP:127.0.0.1:\$RDP_PORT"
        else
            echo "argv=waypipe --vsock -s 2:$PORT server -- $BYSTANDER_BIN --inner-display $SOCK_NAME --forward-session"
        fi
    } >"$DRY_OUT"
    exit 0
fi

if [ "$DISPLAY_MODE" = "rdp" ]; then
    command -v socat >/dev/null 2>&1 || {
        echo "[tier4-publisher] FAIL: QDISTRO_TIER4_DISPLAY=rdp needs socat in the guest" >&2
        exit 2
    }

    CREDS_TMP="$(mktemp -t qdistro-tier4-rdp.XXXXXX.env)"
    BYSTANDER_OUT="$(mktemp -t qdistro-tier4-rdp-bystander.XXXXXX.out)"
    BYSTANDER_PID=
    SOCAT_PID=
    cleanup_rdp() {
        if [ -n "$SOCAT_PID" ]; then
            kill -- "-$SOCAT_PID" 2>/dev/null || kill "$SOCAT_PID" 2>/dev/null || true
            wait "$SOCAT_PID" 2>/dev/null || true
        fi
        [ -n "$BYSTANDER_PID" ] && kill "$BYSTANDER_PID" 2>/dev/null || true
        rm -f "$CREDS_TMP" "$BYSTANDER_OUT" 2>/dev/null || true
    }
    trap cleanup_rdp EXIT INT TERM

    "$BYSTANDER_BIN" --inner-display "$SOCK_NAME" \
        --subscribe "$RDP_SUBSCRIBE" \
        --peer-label "$RDP_PEER_LABEL" >"$BYSTANDER_OUT" 2>>"$LOG" &
    BYSTANDER_PID=$!

    RDP_PORT=""
    RDP_PASSWORD=""
    RDP_CERT_PATH=""
    for _ in $(seq 1 "${QDISTRO_TIER4_RDP_WAIT:-45}"); do
        if ! kill -0 "$BYSTANDER_PID" 2>/dev/null; then
            echo "[tier4-publisher] FAIL: qdwin-bystander exited before RDP approval" >&2
            cat "$BYSTANDER_OUT" >&2 || true
            exit 4
        fi
        RDP_PORT="$(awk -F= '/^RDP_PORT=/{print $2; exit}' "$BYSTANDER_OUT" 2>/dev/null || true)"
        if [ -n "$RDP_PORT" ] && [ "$RDP_PORT" != "0" ]; then
            RDP_PASSWORD="$(awk -F= '/^RDP_PASSWORD=/{print $2; exit}' "$BYSTANDER_OUT" 2>/dev/null || true)"
            RDP_CERT_PATH="$(awk -F= '/^RDP_CERT_PATH=/{print $2; exit}' "$BYSTANDER_OUT" 2>/dev/null || true)"
            break
        fi
        sleep 1
    done
    case "$RDP_PORT" in
        ''|*[!0-9]*|0)
            echo "[tier4-publisher] FAIL: no usable RDP_PORT from subscribe_view_stream" >&2
            cat "$BYSTANDER_OUT" >&2 || true
            exit 4
            ;;
    esac
    if [ "$RDP_PORT" -lt 1 ] || [ "$RDP_PORT" -gt 65535 ]; then
        echo "[tier4-publisher] FAIL: RDP_PORT '$RDP_PORT' out of range 1..65535" >&2
        exit 4
    fi
    case "$RDP_PASSWORD$RDP_CERT_PATH" in
        *$'\n'*|*$'\r'*|*[[:cntrl:]]*)
            echo "[tier4-publisher] FAIL: RDP credentials contain control characters" >&2
            exit 4
            ;;
    esac
    if [ -z "$RDP_PASSWORD" ]; then
        echo "[tier4-publisher] FAIL: qdwin returned an empty RDP password" >&2
        exit 4
    fi

    RDP_READY=0
    for _ in $(seq 1 "${QDISTRO_TIER4_RDP_READY_WAIT:-20}"); do
        if tcp_loopback_accepts "$RDP_PORT" >/dev/null 2>&1; then
            RDP_READY=1
            break
        fi
        sleep 1
    done
    if [ "$RDP_READY" != "1" ]; then
        echo "[tier4-publisher] FAIL: qdistro-forward did not accept TCP on 127.0.0.1:$RDP_PORT" >&2
        cat "$BYSTANDER_OUT" >&2 || true
        exit 4
    fi

    {
        echo "RDP_PORT=$RDP_PORT"
        echo "RDP_PASSWORD=$RDP_PASSWORD"
        echo "RDP_CERT_PATH=$RDP_CERT_PATH"
        echo "RDP_VSOCK_PORT=$PORT"
    } >"$CREDS_TMP"
    install -m 0600 "$CREDS_TMP" "$RDP_CREDS_PATH"

    echo "[tier4-publisher] RDP stream ready tcp=127.0.0.1:$RDP_PORT vsock=2:$PORT creds=$RDP_CREDS_PATH"
    setsid socat "VSOCK-LISTEN:$PORT,reuseaddr,fork" "TCP:127.0.0.1:$RDP_PORT" &
    SOCAT_PID=$!
    wait "$SOCAT_PID"
    exit $?
fi

# Real path: connect out to host CID=2 on $PORT via waypipe-server,
# which exec's the bystander as its only wl_client. The bystander
# binds inner qdwin via --inner-display (a SEPARATE wl_display from
# the ambient $WAYLAND_DISPLAY which waypipe-server has set to its
# own intercept socket); waypipe-server then ferries the bystander's
# outer xdg_toplevel allocation over vsock to the host.
#
# Shape A: --forward-session asks the bystander to create exactly one
# outer xdg_toplevel for the whole VM session (one window per VM on
# the host compositor), regardless of how many inner toplevels exist.
# Inner enumeration is still logged as structured FORWARD lines on
# stdout (so P11 / the host-side parser can correlate), but each inner
# toplevel does NOT spawn its own outer window. Pixel-perfect mirroring
# of inner surfaces onto the outer toplevel is P11's job; for P10 the
# outer toplevel attaches a placeholder SHM buffer.
exec waypipe --vsock -s "2:$PORT" server -- \
    "$BYSTANDER_BIN" --inner-display "$SOCK_NAME" --forward-session
