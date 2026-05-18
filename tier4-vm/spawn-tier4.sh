#!/bin/bash
# §Phase-7 tier-4 — bring up a libvirt+QEMU guest VM and attach it to
# the admin compositor as one chromed peer toplevel.
#
# P11 (2026-05-17) introduced TIER4_DISPLAY=waypipe.
# P12 adds TIER4_DISPLAY=rdp as an explicit qdwin-to-qdwin per-window
# stream path:
#   - TIER4_DISPLAY=waypipe (default after P11):
#       Boots qdistro-tier4-guest.qcow2 (P10 artifact) and connects
#       host-side `waypipe --vsock client vsock://$CID:$PORT` against
#       the in-guest qdistro-tier4-publisher (a systemd service that
#       auto-starts at boot, binds vsock port 7879). The outer
#       xdg_toplevel rides waypipe-server; qdistro-secctx-exec stamps
#       it with engine=qdistro.tier4, app_id=qdistro.tier4.<vm>, so
#       P05a chrome + ClipboardGate light up unchanged.
#   - TIER4_DISPLAY=rdp:
#       Boots the same qdwin guest, starts the in-guest publisher in
#       RDP mode via qemu-guest-agent, subscribes one guest toplevel via
#       qdwin_shell_v1.subscribe_view_stream, bridges the guest-local
#       qdistro-forward RDP TCP endpoint over AF_VSOCK, and launches a
#       host FreeRDP wl_client through a local loopback bridge.
#
# Spec/reference:
#   plan2/tasks/P11-tier4-waypipe-migration.md
#   plan2/tasks/P11-tier4-waypipe-migration.md
#   plan2/tasks/P05b-tier5b-vm-app-windowed.md (canonical waypipe-vsock
#     pattern; mirrored here for tier-4)
#
# Usage:
#   spawn-tier4.sh <vm_name>
#   spawn-tier4.sh --source-only   (test mode — load helpers, no launch)
#
# Env knobs:
#   TIER4_CONFIG         Optional config file. Defaults load from
#                        /etc/qdistro/tier4.conf when present. Env vars
#                        override config-file values.
#   TIER4_STREAMING_METHOD
#                        Human-readable alias for TIER4_DISPLAY.
#   TIER4_DISPLAY        waypipe|rdp — display/streaming backend.
#                        Default 'waypipe' (P11 flip). Legacy viewer path removed in P13.
#   TIER4_ADMIN_USER     Admin uid for libvirt + waypipe-client (default "admin").
#   TIER4_MEM_MIB        Guest RAM in MiB (default 1024 for waypipe,
#                        since the guest runs nested qdwin).
#   TIER4_TITLE_PREFIX   Window title prefix (default "[VM:<vm_name>] ").
#   TIER4_DOMAIN_DEFINE_ONLY=1
#                        Define the libvirt domain but don't start it
#                        or launch the display. Used by tests that just
#                        validate the XML substitution.
#   TIER4_NO_VIEWER=1    Define + start the domain, skip the display
#                        client launch. Probes vsock readiness, then parks
#                        without launching any client).
#   TIER4_DEBUG=1        Verbose viewer / client.
#   TIER4_DOMAIN_TEMPLATE
#                        Path override for the domain template XML.
#                        Defaults to tier4-vm-guest/domain-template.xml for
#                        waypipe.
#   TIER4_GUEST_DISK     Path to the base qcow2 image (waypipe branch only).
#                        Default /var/lib/libvirt/images/qdistro-tier4-guest.qcow2.
#   TIER4_VIRTIOFS_DIR   Host dir exported as /host inside guest (waypipe).
#                        Default /var/lib/qdistro/tier4/<vm_name>/host.
#   TIER4_USE_SECCTX     Default 1 — wrap the display client via
#                        qdistro-secctx-exec so the resulting Wayland
#                        client carries sandbox_engine=qdistro.tier4,
#                        app_id=qdistro.tier4.<vm>.
#   QDISTRO_ALLOW_NO_SECCTX
#                        Set to 1 to allow USE_SECCTX=1 + missing
#                        qdistro-secctx-exec to fall back to a no-op
#                        wrap (dev-only opt-out; production must keep
#                        the default hard-fail). Mirrors tier5b.
#   TIER4_SECCTX_ENGINE  Override sandbox_engine (default qdistro.tier4).
#   TIER4_SECCTX_APPID   Override app_id (default qdistro.tier4.<vm_name>).
#   TIER4_CID            Override allocated CID (waypipe branch).
#   TIER4_PORT           Override vsock port. Defaults 7879 for waypipe,
#                        7880 for rdp.
#   TIER4_RDP_SUBSCRIBE  Guest toplevel handle to subscribe, or "last"
#                        (default). This is the per-window selector.
#   TIER4_RDP_LOCAL_PORT Host loopback port for FreeRDP. Default is
#                        33000 + (CID - TIER4_CID_MIN).
#   TIER4_RDP_CLIENT     FreeRDP client binary override.
#   TIER4_RDP_GUEST_APP  Guest app launched before subscribing.
#                        Currently only weston-terminal is accepted.
#   TIER4_KEEP_DOMAIN=1  Skip destroy+undefine on exit (debug, waypipe).
#
# Lifecycle:
#   - Defines + starts a domain named <vm_name>. Idempotent: an existing
#     domain is destroyed + undefined first.
#   - Waypipe branch: launches host-side waypipe-client (wrapped by
#     qdistro-secctx-exec, wrapped by tier4_control.py so Close() RPC
#     stays registered). The publisher is a systemd unit in the guest
#     (auto-started by P10's image bake); the host probes vsock
#     readiness via socat before declaring readiness.
#   - On viewer/client exit: domain destroy + undefine + overlay rm.
#   - SIGTERM / SIGINT propagate cleanly via the cleanup() trap.
#
# Exit code: viewer/client exit code, or non-zero on bring-up failure.
#
set -uo pipefail

# ---------------------------------------------------------------------
# Constants (waypipe branch)
# ---------------------------------------------------------------------
# CID allocation ceiling shared with tier5b. AF_VSOCK CIDs are uint32
# but a bounded ceiling makes ops debugging tractable and prevents a
# corrupt TAKEN list from spinning forever.
CID_MAX=4095

# Tier-4 CID range starts at 200, well clear of tier-5 (3..99) and
# tier-5b (100..199). The per-task flock file keeps the two tiers
# from competing for the same CID even though they use disjoint ranges
# (defense in depth + clearer ops trace).
TIER4_CID_MIN=200

# Fixed vsock port for tier-4 publishers. Each VM has a unique CID,
# so port collision within a host is impossible; tier-5b uses 7877+
# which is also clear.
# TODO P11-followup: if two concurrent tier-4 VMs ever need to coexist
# AND the publisher's listener semantics ever become per-port (vs
# per-CID-and-port), switch to PORT=7879+CID-200 to keep clear of
# tier5b.
TIER4_FIXED_PORT=7879
TIER4_RDP_FIXED_PORT=7880

# Lock file for atomic CID pick + virsh define (separate from tier5b's).
DOMAIN_LOCK_DIR="/run/qdistro"
if ! mkdir -p "$DOMAIN_LOCK_DIR" 2>/dev/null; then
    DOMAIN_LOCK_DIR="/tmp"
fi
DOMAIN_LOCK_FILE="$DOMAIN_LOCK_DIR/tier4.lock"

# ---------------------------------------------------------------------
# Configuration loader.
#
# /etc/qdistro/tier4.conf and $TIER4_CONFIG are simple KEY=VALUE files.
# Only known tier-4 keys are accepted, and existing environment values
# win over config-file defaults. This keeps operator config explicit
# while preserving one-shot env overrides in tests and launchers.
# ---------------------------------------------------------------------
tier4_config_key_allowed() {
    case "$1" in
        TIER4_STREAMING_METHOD|TIER4_DISPLAY|TIER4_ADMIN_USER|TIER4_MEM_MIB|\
        TIER4_TITLE_PREFIX|TIER4_DOMAIN_TEMPLATE|TIER4_GUEST_DISK|\
        TIER4_VIRTIOFS_DIR|TIER4_USE_SECCTX|TIER4_SECCTX_ENGINE|\
        TIER4_SECCTX_APPID|TIER4_SECCTX_INSTANCE|TIER4_CID|TIER4_PORT|\
        TIER4_RDP_SUBSCRIBE|TIER4_RDP_LOCAL_PORT|TIER4_RDP_CLIENT|\
        TIER4_RDP_GUEST_APP|TIER4_KEEP_DOMAIN|TIER4_DEBUG)
            return 0 ;;
        *) return 1 ;;
    esac
}

root_owned_not_writable_by_others() {
    local path="$1" uid mode
    uid="$(stat -c '%u' "$path" 2>/dev/null)" || return 1
    mode="$(stat -c '%a' "$path" 2>/dev/null)" || return 1
    [ "$uid" = "0" ] || return 1
    # Last two octal digits must not have write bits.
    [ $(( 8#${mode: -2:1} & 2 )) -eq 0 ] || return 1
    [ $(( 8#${mode: -1:1} & 2 )) -eq 0 ] || return 1
}

require_trusted_root_file_if_privileged() {
    local path="$1" label="$2"
    if [ "$(id -u)" = "0" ] && [ "${QDISTRO_TIER4_DRY_RUN:-0}" != "1" ]; then
        root_owned_not_writable_by_others "$path" || {
            echo "[tier4] FAIL: $label '$path' must be root-owned and not group/world-writable" >&2
            return 1
        }
    fi
}

load_tier4_config_file() {
    local cfg="$1" line key value
    [ -n "$cfg" ] && [ -f "$cfg" ] || return 0
    require_trusted_root_file_if_privileged "$cfg" "config file" || return 4
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        key="${key%%[[:space:]]*}"
        tier4_config_key_allowed "$key" || {
            echo "[tier4] WARN: ignoring unknown config key '$key' in $cfg" >&2
            continue
        }
        if [ -z "${!key+x}" ]; then
            printf -v "$key" '%s' "$value"
            export "$key"
        fi
    done <"$cfg"
}

load_tier4_config() {
    if [ -n "${TIER4_CONFIG:-}" ]; then
        load_tier4_config_file "$TIER4_CONFIG"
    else
        load_tier4_config_file /etc/qdistro/tier4.conf
    fi
}

# ---------------------------------------------------------------------
# Helper: JSON-encode an arbitrary string via python3 json.dumps.
# Replicates tier5b's helper so any qga arg path is shell-injection-safe.
# Stdin = raw value; stdout = JSON-encoded literal (including quotes).
# ---------------------------------------------------------------------
json_encode_stdin() {
    python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))'
}

# ---------------------------------------------------------------------
# build_secctx_wrap USE_SECCTX ENGINE APPID INSTANCE
#
# Populates SECCTX_WRAP bash array with either:
#   (qdistro-secctx-exec --sandbox-engine E --app-id A --instance-id I --)
# or empty (when USE_SECCTX != 1). Mirrored from tier5b; exported so the
# unit test suite (--source-only) can verify the composition without
# launching a VM.
# ---------------------------------------------------------------------
build_secctx_wrap() {
    local use_secctx="$1"
    local engine="$2"
    local appid="$3"
    local instance="$4"
    SECCTX_WRAP=()
    if [ "$use_secctx" = "1" ]; then
        SECCTX_WRAP=(qdistro-secctx-exec
            --sandbox-engine "$engine"
            --app-id         "$appid"
            --instance-id    "$instance"
            --)
    fi
}

# ---------------------------------------------------------------------
# resolve_template DISPLAY SCRIPT_DIR
#
# Resolves the libvirt domain template path for the given display
# backend. Honours $TIER4_DOMAIN_TEMPLATE when set; otherwise searches
# branch-appropriate defaults. Echoes the path on stdout, exits non-
# zero on missing. Pure function so the unit tests can exercise the
# selection logic via --source-only without running anything.
# ---------------------------------------------------------------------
resolve_template() {
    local display="$1"
    local script_dir="$2"
    local override="${TIER4_DOMAIN_TEMPLATE:-}"
    if [ -n "$override" ]; then
        if [ ! -f "$override" ]; then
            echo "[tier4] FAIL: TIER4_DOMAIN_TEMPLATE=$override not readable" >&2
            return 4
        fi
        require_trusted_root_file_if_privileged "$override" "domain template" || return 4
        echo "$override"
        return 0
    fi
    if [ "$display" = "waypipe" ] || [ "$display" = "rdp" ]; then
        # P10's guest-image template (has vsock + virtio-snd
        # + virtiofs). Stored alongside tier4-vm-guest, not tier4-vm/.
        local guest_dir
        guest_dir="$(dirname "$script_dir")/tier4-vm-guest"
        if   [ -f "$guest_dir/domain-template.xml" ]; then
            echo "$guest_dir/domain-template.xml"; return 0
        elif [ -f /usr/share/qdistro/tier4-vm-guest/domain-template.xml ]; then
            echo /usr/share/qdistro/tier4-vm-guest/domain-template.xml; return 0
        else
            echo "[tier4] FAIL: tier4-vm-guest domain template not found (looked in $guest_dir and /usr/share/qdistro/tier4-vm-guest/)" >&2
            return 4
        fi
    fi
    echo "[tier4] FAIL: tier4-vm-guest domain template not found" >&2
    return 4
}

# ---------------------------------------------------------------------
# --source-only short-circuit. Unit tests source the script to exercise
# build_secctx_wrap / resolve_template / constants without executing
# the launch logic (no virsh, no waypipe, no root required).
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
if [ $# -lt 1 ]; then
    usage >&2; exit 1
fi

VM_NAME="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load operator defaults before resolving the streaming method. Existing
# environment variables keep precedence over config-file values.
load_tier4_config

# TIER4_STREAMING_METHOD is the readable config-file alias. TIER4_DISPLAY
# remains the historical env/API name used by tests and launchers.
if [ -n "${TIER4_STREAMING_METHOD:-}" ] && [ -z "${TIER4_DISPLAY:-}" ]; then
    TIER4_DISPLAY="$TIER4_STREAMING_METHOD"
fi

# TIER4_DISPLAY default — P11 flip: waypipe remains default. RDP is an
# explicit opt-in streaming method.
TIER4_DISPLAY="${TIER4_DISPLAY:-waypipe}"
case "$TIER4_DISPLAY" in
    waypipe|rdp) ;;
    *)
        echo "[tier4] FAIL: TIER4_DISPLAY='$TIER4_DISPLAY' invalid (expected: waypipe or rdp)" >&2
        exit 1
        ;;
esac

# Validate vm_name shape.
if ! [[ "$VM_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$ ]]; then
    echo "[tier4] FAIL: invalid vm_name '$VM_NAME' (alnum + - + _, 1-63 chars)" >&2
    exit 1
fi

# Resolve template (branch-aware).
TEMPLATE=$(resolve_template "$TIER4_DISPLAY" "$SCRIPT_DIR") || exit $?

# --- preflight --------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "[tier4] FAIL: must run as root (uses runuser to drop to admin uid)" >&2
    echo "        try: sudo $0 $VM_NAME" >&2
    exit 2
fi

ADMIN_USER="${TIER4_ADMIN_USER:-admin}"
if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
    echo "[tier4] FAIL: admin user '$ADMIN_USER' does not exist" >&2
    exit 2
fi

ADMIN_UID=$(id -u "$ADMIN_USER")
ADMIN_RUNTIME="/run/user/$ADMIN_UID"
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-1}"

if ! command -v virsh >/dev/null 2>&1; then
    echo "[tier4] FAIL: virsh not installed (zypper install libvirt-client)" >&2
    exit 2
fi
if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    echo "[tier4] FAIL: qemu-system-x86_64 not installed (zypper install qemu-x86)" >&2
    exit 2
fi

# Branch-specific preflight.
NO_VIEWER="${TIER4_NO_VIEWER:-0}"
DEFINE_ONLY="${TIER4_DOMAIN_DEFINE_ONLY:-0}"

for tool in flock qemu-img; do
    command -v "$tool" >/dev/null || {
        echo "[tier4] FAIL: TIER4_DISPLAY=$TIER4_DISPLAY needs $tool installed" >&2
        exit 2
    }
done
if [ "$TIER4_DISPLAY" = "waypipe" ]; then
    if ! command -v waypipe >/dev/null 2>&1; then
        echo "[tier4] FAIL: waypipe not installed (zypper install waypipe)" >&2
        exit 2
    fi
else
    command -v socat >/dev/null 2>&1 || {
        echo "[tier4] FAIL: TIER4_DISPLAY=rdp needs socat installed" >&2
        exit 2
    }
fi
# Base disk: P10's qdistro-tier4-guest.qcow2.
TIER4_GUEST_DISK="${TIER4_GUEST_DISK:-/var/lib/libvirt/images/qdistro-tier4-guest.qcow2}"
if [ "$DEFINE_ONLY" != "1" ] && [ ! -f "$TIER4_GUEST_DISK" ]; then
    echo "[tier4] FAIL: guest disk $TIER4_GUEST_DISK missing — bake via qdistro/tier4-vm-guest/build-guest-image.sh" >&2
    exit 3
fi

need_compositor=1
if [ "$DEFINE_ONLY" = "1" ] || [ "$NO_VIEWER" = "1" ]; then
    need_compositor=0
fi
if [ "${TIER4_TEST_NO_COMPOSITOR:-0}" = "1" ]; then
    need_compositor=0
fi
if [ "$need_compositor" = "1" ] && [ ! -S "$ADMIN_RUNTIME/$WAYLAND_DISPLAY" ]; then
    # waypipe branch spawns a wl_client into the admin compositor
    # (waypipe-client for waypipe). The socket
    # must exist before we can launch.
    echo "[tier4] FAIL: admin compositor socket $ADMIN_RUNTIME/$WAYLAND_DISPLAY not present" >&2
    exit 3
fi

# Default memory: waypipe branch needs more (nested qdwin + apps).
MEM_MIB="${TIER4_MEM_MIB:-1024}"
MEM_KIB=$((MEM_MIB * 1024))
TITLE_PREFIX="${TIER4_TITLE_PREFIX:-[VM:$VM_NAME] }"

# qemu:///session lets the admin user manage their own libvirt domains.
LIBVIRT_URI="qemu:///session"

run_as_admin() {
    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
        HOME="$ADMIN_HOME" \
        WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$ADMIN_RUNTIME/bus" \
        LIBVIRT_DEFAULT_URI="$LIBVIRT_URI" \
        "$@"
}

# qga wrapper with bounded timeout — mirrors tier5b's helper. Used by
# the waypipe branch to probe publisher health post-vsock-bind.
qga_cmd() {
    # $1 = vm, $2 = JSON request
    run_as_admin virsh qemu-agent-command --timeout 5 "$1" "$2"
}

qga_exec_shell_bg() {
    # $1 = shell command. Starts it through qemu-guest-agent and does
    # not wait for the child; command must background its long-lived
    # process if it should survive guest-exec's shell.
    local cmd_json
    cmd_json="$(printf '%s' "$1" | json_encode_stdin)"
    qga_cmd "$VM_NAME" "{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/bin/sh\",\"arg\":[\"-lc\",$cmd_json],\"capture-output\":true}}" >/dev/null
}

qga_exec_capture() {
    # $1 = shell command. Prints captured stdout, returns non-zero if
    # guest-exec itself fails or the guest command exits non-zero.
    local cmd_json start pid status
    cmd_json="$(printf '%s' "$1" | json_encode_stdin)"
    start="$(qga_cmd "$VM_NAME" "{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/bin/sh\",\"arg\":[\"-lc\",$cmd_json],\"capture-output\":true}}")" || return 1
    pid="$(printf '%s' "$start" | python3 -c 'import json,sys; print(json.load(sys.stdin)["return"]["pid"])')" || return 1
    for _ in $(seq 1 15); do
        status="$(qga_cmd "$VM_NAME" "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$pid}}")" || return 1
        if printf '%s' "$status" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin)["return"].get("exited") else 1)' >/dev/null 2>&1; then
            printf '%s' "$status" | python3 -c '
import base64, json, sys
r = json.load(sys.stdin)["return"]
if r.get("exitcode", 1) != 0:
    sys.exit(r.get("exitcode", 1))
data = r.get("out-data") or ""
if data:
    sys.stdout.write(base64.b64decode(data).decode("utf-8", "replace"))
'
            return $?
        fi
        sleep 1
    done
    return 1
}

qga_exec_capture_quiet() {
    # Same as qga_exec_capture, but preserves stderr for caller handling.
    qga_exec_capture "$1" 2>/dev/null
}

find_rdp_client() {
    if [ -n "${TIER4_RDP_CLIENT:-}" ]; then
        command -v "$TIER4_RDP_CLIENT" >/dev/null 2>&1 || return 1
        echo "$TIER4_RDP_CLIENT"; return 0
    fi
    for c in wlfreerdp xfreerdp3 xfreerdp sdl-freerdp; do
        if command -v "$c" >/dev/null 2>&1; then
            echo "$c"; return 0
        fi
    done
    return 1
}

tcp_loopback_accepts() {
    local port="$1"
    python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
with socket.create_connection(("127.0.0.1", port), timeout=1):
    pass
PY
}

# ---------------------------------------------------------------------
# Waypipe branch.
#
# Uses idempotent domain-replacement + cleanup() trap
# with disk, XML markers, display launch, and teardown.
# ---------------------------------------------------------------------

# Common: idempotent destroy/undefine of any pre-existing same-named
# domain (so re-runs in tests don't trip on stale state).
maybe_overwrite_existing() {
    if run_as_admin virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
        local existing_state
        existing_state="$(run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | tr -d '\n' | tr -s ' ')"
        case "$existing_state" in
            running|paused)
                if [ "${TIER4_ALLOW_OVERWRITE_RUNNING:-0}" != "1" ]; then
                    echo "[tier4] WARN: domain '$VM_NAME' is $existing_state; destroying it (set TIER4_ALLOW_OVERWRITE_RUNNING=1 to silence)" >&2
                fi
                ;;
            *)
                echo "[tier4] domain '$VM_NAME' already exists ($existing_state) — destroying + undefining" >&2
                ;;
        esac
        run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
        run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    fi
}

# State for cleanup() trap.
DOMAIN_DEFINED=0
DISK_PATH=""
DISK_CREATED=0
VIRTIOFS_DIR=""
VIRTIOFS_OWNED=0
CLIENT_PID=
RDP_BRIDGE_PID=
CLEANUP_DONE=0

cleanup() {
    # Re-entrancy guard (mirrors tier5b correctness fix): a chained
    # INT→TERM must not rerun teardown twice.
    if [ "$CLEANUP_DONE" = "1" ]; then
        return 0
    fi
    CLEANUP_DONE=1
    if [ -n "$CLIENT_PID" ]; then
        kill -- "-$CLIENT_PID" 2>/dev/null || kill "$CLIENT_PID" 2>/dev/null || true
        wait "$CLIENT_PID" 2>/dev/null || true
    fi
    CLIENT_PID=
    if [ -n "$RDP_BRIDGE_PID" ]; then
        kill -- "-$RDP_BRIDGE_PID" 2>/dev/null || kill "$RDP_BRIDGE_PID" 2>/dev/null || true
        wait "$RDP_BRIDGE_PID" 2>/dev/null || true
    fi
    RDP_BRIDGE_PID=
    [ -n "${RDP_PASSWORD_FILE:-}" ] && rm -f "$RDP_PASSWORD_FILE" 2>/dev/null || true
    if [ "$DOMAIN_DEFINED" = "1" ] && [ "${TIER4_KEEP_DOMAIN:-0}" != "1" ]; then
        run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
        run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    fi
    if [ "$DISK_CREATED" = "1" ] && [ -n "$DISK_PATH" ] && [ -f "$DISK_PATH" ] \
       && [ "${TIER4_KEEP_DOMAIN:-0}" != "1" ]; then
        rm -f "$DISK_PATH" 2>/dev/null || true
    fi
    if [ "$VIRTIOFS_OWNED" = "1" ] && [ -n "$VIRTIOFS_DIR" ] \
       && [ -d "$VIRTIOFS_DIR" ] && [ "${TIER4_KEEP_DOMAIN:-0}" != "1" ]; then
        rm -rf "$VIRTIOFS_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# =====================================================================
# Waypipe branch.
# =====================================================================

# Secctx triple — same engine/appid prefix so
# P05a chrome wire fires unchanged.
USE_SECCTX="${TIER4_USE_SECCTX:-1}"
SECCTX_ENGINE="${TIER4_SECCTX_ENGINE:-qdistro.tier4}"
SECCTX_APPID="${TIER4_SECCTX_APPID:-qdistro.tier4.$VM_NAME}"

# Hard-fail by default if USE_SECCTX=1 but the tagger is missing. Dev
# workflows can opt out via QDISTRO_ALLOW_NO_SECCTX=1 (mirrors tier5b).
if [ "$USE_SECCTX" = "1" ] && ! command -v qdistro-secctx-exec >/dev/null 2>&1; then
    if [ "${QDISTRO_ALLOW_NO_SECCTX:-0}" = "1" ]; then
        echo "[tier4] WARN: qdistro-secctx-exec not in PATH; QDISTRO_ALLOW_NO_SECCTX=1, continuing without stamp" >&2
        USE_SECCTX=0
    else
        echo "[tier4] FAIL: qdistro-secctx-exec not in PATH" >&2
        echo "        Set QDISTRO_ALLOW_NO_SECCTX=1 for dev-only opt-out." >&2
        exit 2
    fi
fi

# Per-spawn instance token (loud and unique).
LAUNCH_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
if [ "${#LAUNCH_TOKEN}" -ne 32 ]; then
    echo "[tier4] FAIL: could not generate launch token from /dev/urandom" >&2
    exit 5
fi
SECCTX_INSTANCE="${TIER4_SECCTX_INSTANCE:-$VM_NAME-$LAUNCH_TOKEN}"

# Echo the resolved triple BEFORE the vsock dance so s107's chrome
# probe (and any future placeholder correlator) can grep stderr for
# the actual triple, not just for evidence that the spawn script ran.
# Mirrors tier5b's stdout SECCTX_APPID=... line.
echo "[tier4] waypipe-branch resolved (secctx engine=$SECCTX_ENGINE app_id=$SECCTX_APPID instance=$SECCTX_INSTANCE)" >&2

# --- atomic CID pick under tier4.lock ---------------------------------
touch "$DOMAIN_LOCK_FILE" 2>/dev/null || true
chmod 0644 "$DOMAIN_LOCK_FILE" 2>/dev/null || true
exec 9>"$DOMAIN_LOCK_FILE"
if ! flock -w 30 9; then
    echo "[tier4] FAIL: could not acquire $DOMAIN_LOCK_FILE within 30s" >&2
    exit 5
fi

if [ -n "${TIER4_CID:-}" ]; then
    CID="$TIER4_CID"
    [[ "$TIER4_CID" =~ ^[0-9]+$ ]] || { echo "[tier4] FAIL: CID='$TIER4_CID' not numeric" >&2; exit 1; }
    if [ "$TIER4_CID" -lt "$TIER4_CID_MIN" ] || [ "$TIER4_CID" -gt "$CID_MAX" ]; then
        echo "[tier4] FAIL: CID '$TIER4_CID' out of valid range $TIER4_CID_MIN-$CID_MAX" >&2; exit 1
    fi
else
    TAKEN=$(run_as_admin sh -c '
        for d in $(virsh list --all --name); do
            [ -z "$d" ] && continue
            virsh dumpxml "$d" 2>/dev/null
        done' | \
        grep -oE "<cid[^>]*address='[0-9]+'" | \
        grep -oE "[0-9]+" | sort -un || true)
    CID="$TIER4_CID_MIN"
    while echo "$TAKEN" | grep -qx "$CID"; do
        CID=$((CID + 1))
        if [ "$CID" -gt "$CID_MAX" ]; then
            echo "[tier4] FAIL: CID range exhausted (next=$CID > CID_MAX=$CID_MAX)" >&2
            exit 5
        fi
    done
fi

if [ "$TIER4_DISPLAY" = "rdp" ]; then
    PORT="${TIER4_PORT:-$TIER4_RDP_FIXED_PORT}"
else
    PORT="${TIER4_PORT:-$TIER4_FIXED_PORT}"
fi
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "[tier4] FAIL: PORT='$PORT' not numeric" >&2; exit 1; }

maybe_overwrite_existing

# --- per-VM overlay (linked clone of P10 guest disk) ------------------
# --- disk dir (waypipe) — only honor override in dry-run mode ---
if [ "${QDISTRO_TIER4_DRY_RUN:-0}" = "1" ]; then
    DISK_DIR="${TIER4_DISK_DIR:-/var/lib/libvirt/images}"
else
    DISK_DIR="/var/lib/libvirt/images"
fi
DISK_PATH="$DISK_DIR/$VM_NAME.qcow2"
install -d -m 0755 "$DISK_DIR"

if [ "$DEFINE_ONLY" != "1" ]; then
    rm -f "$DISK_PATH"
    if ! qemu-img create -f qcow2 -F qcow2 -b "$TIER4_GUEST_DISK" "$DISK_PATH" \
            >/dev/null 2>&1; then
        echo "[tier4] FAIL: qemu-img create linked clone from $TIER4_GUEST_DISK failed" >&2
        exit 4
    fi
    chmod 0644 "$DISK_PATH"
    chown "$ADMIN_USER":root "$DISK_PATH" 2>/dev/null || true
    DISK_CREATED=1
    echo "[tier4] linked clone $DISK_PATH from base $TIER4_GUEST_DISK" >&2
fi

# --- virtiofs share ---------------------------------------------------
VIRTIOFS_DIR="${TIER4_VIRTIOFS_DIR:-/var/lib/qdistro/tier4/$VM_NAME/host}"
if [ ! -d "$VIRTIOFS_DIR" ]; then
    install -d -m 0755 "$VIRTIOFS_DIR"
    VIRTIOFS_OWNED=1
fi

# --- domain XML substitution -----------------------------------------
NEW_MAC="52:54:00:$(printf '%02x:%02x:%02x' \
    $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"

# P10's tier4-vm-guest template uses: __VM_NAME__, __MEM_KIB__, __CID__,
# __DISK_PATH__, __VIRTIOFS_DIR__. (__MAC__ is documented but the actual
# P10 template doesn't currently carry a NIC, so __MAC__ may be absent;
# substituting it as a no-op is harmless.)
TMP_XML="/tmp/qdistro-tier4-${VM_NAME}-$$.xml"
sed \
    -e "s|__VM_NAME__|$VM_NAME|g" \
    -e "s|__MAC__|$NEW_MAC|g" \
    -e "s|__MEM_KIB__|$MEM_KIB|g" \
    -e "s|__CID__|$CID|g" \
    -e "s|__DISK_PATH__|$DISK_PATH|g" \
    -e "s|__VIRTIOFS_DIR__|$VIRTIOFS_DIR|g" \
    "$TEMPLATE" >"$TMP_XML"

if grep -q '__[A-Z_]*__' "$TMP_XML"; then
    echo "[tier4] FAIL: unsubstituted markers in domain XML:" >&2
    grep -o '__[A-Z_]*__' "$TMP_XML" | sort -u >&2
    rm -f "$TMP_XML"
    exit 4
fi

# Template validation would go here if needed
# Currently only waypipe templates are expected

maybe_overwrite_existing

echo "[tier4] defining domain '$VM_NAME' (mem=${MEM_MIB}MiB, mac=$NEW_MAC, display=$TIER4_DISPLAY, cid=$CID, port=$PORT)" >&2
chown "$ADMIN_USER" "$TMP_XML"; chmod 0644 "$TMP_XML"
if ! run_as_admin virsh define "$TMP_XML" >/dev/null; then
    echo "[tier4] FAIL: virsh define failed" >&2
    rm -f "$TMP_XML"
    exit 5
fi
rm -f "$TMP_XML"
DOMAIN_DEFINED=1

# Release flock now that CID is committed to libvirt's XML.
flock -u 9 2>/dev/null || true
exec 9>&- 2>/dev/null || true

if [ "$DEFINE_ONLY" = "1" ]; then
    echo "[tier4] TIER4_DOMAIN_DEFINE_ONLY=1 — domain defined; not starting" >&2
    echo "[tier4] OK"
    exit 0
fi

# --- start domain -----------------------------------------------------
if ! run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | grep -qw running; then
    if ! run_as_admin virsh start "$VM_NAME" >/dev/null; then
        echo "[tier4] FAIL: virsh start failed" >&2
        exit 6
    fi
fi
echo "[tier4] domain $VM_NAME running (cid=$CID port=$PORT)" >&2

# --- wait for qga (publisher startup signal proxy) -------------------
QGA_OK=0
for i in $(seq 1 90); do
    if qga_cmd "$VM_NAME" \
        '{"execute":"guest-ping"}' >/dev/null 2>&1; then
        QGA_OK=1; break
    fi
    sleep 1
done
if [ "$QGA_OK" != "1" ]; then
    echo "[tier4] FAIL: qemu-guest-agent never responded after 90s" >&2
    exit 7
fi
echo "[tier4] qga ready" >&2

if [ "$TIER4_DISPLAY" = "rdp" ]; then
    RDP_SUBSCRIBE="${TIER4_RDP_SUBSCRIBE:-last}"
    RDP_GUEST_APP="${TIER4_RDP_GUEST_APP:-weston-terminal}"
    case "$RDP_GUEST_APP" in
        weston-terminal) ;;
        *)
            echo "[tier4] FAIL: TIER4_RDP_GUEST_APP='$RDP_GUEST_APP' unsupported (expected: weston-terminal)" >&2
            exit 1
            ;;
    esac
    case "$RDP_SUBSCRIBE" in
        last|''|*[!0-9]*)
            if [ "$RDP_SUBSCRIBE" != "last" ]; then
                echo "[tier4] FAIL: TIER4_RDP_SUBSCRIBE must be 'last' or a numeric qdwin toplevel handle" >&2
                exit 1
            fi
            ;;
    esac
    RDP_CREDS_PATH="/run/qdistro-tier4-rdp.env"
    RDP_GUEST_PREFLIGHT='
set -eu
command -v socat >/dev/null
command -v qdistro-forward >/dev/null
test -x /usr/bin/qdwin-bystander
test -x /usr/local/bin/qdistro-tier4-publisher.sh
test -S /run/user/1000/wayland-0
'
    if ! qga_exec_capture "$RDP_GUEST_PREFLIGHT" >/dev/null; then
        echo "[tier4] FAIL: guest RDP preflight failed (need socat, qdistro-forward, qdwin-bystander, publisher, and /run/user/1000/wayland-0)" >&2
        exit 7
    fi

    # The baked waypipe publisher owns qdwin_shell_v1 by default. RDP
    # mode needs its own bystander subscriber, so stop the default unit.
    qga_exec_capture "systemctl stop qdistro-tier4-publisher.service 2>/dev/null || true" >/dev/null || true
    qga_exec_capture "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 $RDP_GUEST_APP >/tmp/qdistro-tier4-rdp-app.log 2>&1 &" >/dev/null || true

    RDP_PUBLISHER_CMD="rm -f '$RDP_CREDS_PATH'; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 QDISTRO_TIER4_DISPLAY=rdp QDISTRO_TIER4_RDP_SUBSCRIBE='$RDP_SUBSCRIBE' QDISTRO_TIER4_RDP_CREDS='$RDP_CREDS_PATH' /usr/local/bin/qdistro-tier4-publisher.sh '$PORT' >/var/log/qdistro-tier4-rdp-publisher.launch.log 2>&1 &"
    echo "[tier4] starting guest RDP publisher (subscribe=$RDP_SUBSCRIBE vsock=$PORT)" >&2
    if ! qga_exec_shell_bg "$RDP_PUBLISHER_CMD"; then
        echo "[tier4] FAIL: qga failed to start RDP publisher" >&2
        exit 7
    fi
fi

# --- probe in-guest publisher vsock readiness ------------------------
# The publisher is a systemd service auto-started at boot (P10's image
# bake); we don't trigger it via qga. We probe vsock CONNECT until it
# accepts — proves waypipe-server has bound, not merely that the
# script started (banner-grepping a log file is the anti-pattern P10
# fix-pass corrected).
VSOCK_OK=0
echo "[tier4] waiting for publisher on vsock://$CID:$PORT (up to 30s) ..." >&2
if command -v socat >/dev/null 2>&1; then
    deadline=$(( $(date +%s) + 30 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if timeout 2 socat -T1 - "VSOCK-CONNECT:$CID:$PORT" </dev/null \
                >/dev/null 2>&1; then
            VSOCK_OK=1
            break
        fi
        sleep 1
    done
elif command -v python3 >/dev/null 2>&1; then
    # plan-too-thin fallback: probe via python3 socket.AF_VSOCK.
    deadline=$(( $(date +%s) + 30 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if python3 -c "
import socket, sys
try:
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(($CID, $PORT))
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            VSOCK_OK=1
            break
        fi
        sleep 1
    done
else
    echo "[tier4] WARN: neither socat nor python3 available for vsock probe; assuming publisher is ready (degraded)" >&2
    VSOCK_OK=1
fi
if [ "$VSOCK_OK" != "1" ]; then
    echo "[tier4] FAIL: publisher never bound vsock://$CID:$PORT within 30s" >&2
    # Diagnostic dump.
    if command -v socat >/dev/null 2>&1; then
        echo "[tier4] last vsock probe stderr:" >&2
        timeout 2 socat -T1 - "VSOCK-CONNECT:$CID:$PORT" </dev/null 2>&1 | head -5 >&2 || true
    fi
    exit 7
fi
echo "[tier4] publisher vsock bind confirmed on vsock://$CID:$PORT" >&2

if [ "$NO_VIEWER" = "1" ]; then
    echo "[tier4] TIER4_NO_VIEWER=1 — domain running; not launching display client" >&2
    echo "[tier4] OK (vsock://$CID:$PORT)"
    while sleep 60; do :; done
fi

# --- host-side waypipe-client (wrapped by tier4_control + secctx) -----
CONTROL_SCRIPT="${TIER4_CONTROL_SCRIPT:-}"
if [ -z "$CONTROL_SCRIPT" ]; then
    if [ -f "$SCRIPT_DIR/tier4_control.py" ]; then
        CONTROL_SCRIPT="$SCRIPT_DIR/tier4_control.py"
    elif [ -f /usr/share/qdistro/tier4-vm/tier4_control.py ]; then
        CONTROL_SCRIPT=/usr/share/qdistro/tier4-vm/tier4_control.py
    fi
fi

build_secctx_wrap "$USE_SECCTX" "$SECCTX_ENGINE" "$SECCTX_APPID" "$SECCTX_INSTANCE"

CLIENT_LOG="$ADMIN_RUNTIME/tier4-${VM_NAME}-client.log"
: >"$CLIENT_LOG"
chown "$ADMIN_USER" "$CLIENT_LOG" 2>/dev/null || true

if [ "$TIER4_DISPLAY" = "rdp" ]; then
    RDP_CREDS=""
    for _ in $(seq 1 30); do
        RDP_CREDS="$(qga_exec_capture "cat /run/qdistro-tier4-rdp.env 2>/dev/null" 2>/dev/null || true)"
        if printf '%s\n' "$RDP_CREDS" | grep -q '^RDP_PORT='; then
            break
        fi
        sleep 1
    done
    if ! printf '%s\n' "$RDP_CREDS" | grep -q '^RDP_PORT='; then
        echo "[tier4] FAIL: guest RDP publisher did not write /run/qdistro-tier4-rdp.env" >&2
        echo "[tier4] guest publisher log:" >&2
        qga_exec_capture_quiet "cat /var/log/qdistro-tier4-rdp-publisher.launch.log 2>/dev/null || true" >&2 || true
        exit 7
    fi
    RDP_PASSWORD="$(printf '%s\n' "$RDP_CREDS" | awk -F= '/^RDP_PASSWORD=/{print $2; exit}')"
    case "$RDP_PASSWORD" in
        *$'\n'*|*$'\r'*|*[[:cntrl:]]*)
            echo "[tier4] FAIL: guest RDP password contains control characters" >&2
            exit 7
            ;;
    esac
    if [ -z "$RDP_PASSWORD" ]; then
        echo "[tier4] FAIL: guest RDP publisher returned an empty password" >&2
        exit 7
    fi
    RDP_CLIENT="$(find_rdp_client)" || {
        echo "[tier4] FAIL: no FreeRDP client found (tried wlfreerdp, xfreerdp3, xfreerdp, sdl-freerdp)" >&2
        exit 2
    }
    if [ -n "${TIER4_RDP_LOCAL_PORT:-}" ]; then
        RDP_LOCAL_PORT="$TIER4_RDP_LOCAL_PORT"
    else
        RDP_LOCAL_PORT=$((33000 + CID - TIER4_CID_MIN))
    fi
    [[ "$RDP_LOCAL_PORT" =~ ^[0-9]+$ ]] || { echo "[tier4] FAIL: TIER4_RDP_LOCAL_PORT='$RDP_LOCAL_PORT' not numeric" >&2; exit 1; }
    if [ "$RDP_LOCAL_PORT" -lt 1 ] || [ "$RDP_LOCAL_PORT" -gt 65535 ]; then
        echo "[tier4] FAIL: TIER4_RDP_LOCAL_PORT '$RDP_LOCAL_PORT' out of range 1..65535" >&2
        exit 1
    fi
    RDP_BRIDGE_LOG="$ADMIN_RUNTIME/tier4-${VM_NAME}-rdp-bridge.log"
    : >"$RDP_BRIDGE_LOG"
    chown "$ADMIN_USER" "$RDP_BRIDGE_LOG" 2>/dev/null || true
    setsid runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="$ADMIN_RUNTIME" HOME="$ADMIN_HOME" \
        socat "TCP-LISTEN:$RDP_LOCAL_PORT,bind=127.0.0.1,reuseaddr,fork" \
              "VSOCK-CONNECT:$CID:$PORT" >>"$RDP_BRIDGE_LOG" 2>&1 &
    RDP_BRIDGE_PID=$!
    RDP_BRIDGE_READY=0
    for _ in $(seq 1 20); do
        if ! kill -0 "$RDP_BRIDGE_PID" 2>/dev/null; then
            break
        fi
        if tcp_loopback_accepts "$RDP_LOCAL_PORT" >/dev/null 2>&1; then
            RDP_BRIDGE_READY=1
            break
        fi
        sleep 0.1
    done
    if [ "$RDP_BRIDGE_READY" != "1" ]; then
        echo "[tier4] FAIL: RDP vsock bridge died during startup" >&2
        cat "$RDP_BRIDGE_LOG" >&2 || true
        exit 6
    fi

    RDP_PASSWORD_FILE="$ADMIN_RUNTIME/tier4-${VM_NAME}-rdp-password"
    (umask 077 && printf '%s\n' "$RDP_PASSWORD" >"$RDP_PASSWORD_FILE")
    chown "$ADMIN_USER" "$RDP_PASSWORD_FILE" 2>/dev/null || true
    RDP_ARGV=("$RDP_CLIENT" "/v:127.0.0.1:$RDP_LOCAL_PORT" "/u:qdistro" "/cert:ignore" "/from-stdin")
    [ "${TIER4_DEBUG:-0}" = "1" ] && RDP_ARGV+=("/log-level:DEBUG")
    DISPLAY_ARGV=("${RDP_ARGV[@]}")
else
    # Argv shape mirrors tier5b's `waypipe --vsock client vsock://CID:PORT`
    # pattern.
    WAYPIPE_ARGV=(waypipe --vsock client "vsock://$CID:$PORT")
    [ "${TIER4_DEBUG:-0}" = "1" ] && WAYPIPE_ARGV+=(--debug)
    DISPLAY_ARGV=("${WAYPIPE_ARGV[@]}")
fi

if [ -n "$CONTROL_SCRIPT" ] && [ -f "$CONTROL_SCRIPT" ] \
        && command -v python3 >/dev/null 2>&1; then
    # The control script exec's the rest of argv as the child process.
    # With the display client as the child, control's Close() RPC kills
    # the client process tree on close-button (then cleanup() reaps virsh).
    CLIENT_CMD=(python3 "$CONTROL_SCRIPT"
                --vm-name "$VM_NAME"
                -- "${DISPLAY_ARGV[@]}")
else
    if [ "$TIER4_DISPLAY" = "rdp" ]; then
        echo "[tier4] FAIL: TIER4_DISPLAY=rdp requires tier4_control.py so the RDP password can be passed over stdin, not argv" >&2
        exit 2
    fi
    if [ -n "$CONTROL_SCRIPT" ]; then
        echo "[tier4] WARN: control script $CONTROL_SCRIPT or python3 unavailable; close button will not destroy the domain (degraded)" >&2
    else
        echo "[tier4] WARN: tier4_control.py not found; close button will not destroy the domain (degraded)" >&2
    fi
    CLIENT_CMD=("${DISPLAY_ARGV[@]}")
fi

if [ "$USE_SECCTX" = "1" ]; then
    echo "[tier4] launching $TIER4_DISPLAY client for '$VM_NAME' (secctx engine=$SECCTX_ENGINE app_id=$SECCTX_APPID)" >&2
else
    echo "[tier4] launching $TIER4_DISPLAY client for '$VM_NAME' (un-tagged)" >&2
fi
echo "[tier4] display-client log: $CLIENT_LOG" >&2

# setsid: a qdshell crash (the parent of this script in production)
# must not propagate SIGHUP to waypipe-client. The client outlives the
# shell for the duration of the VM session.
setsid runuser -u "$ADMIN_USER" -- env \
    XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
    HOME="$ADMIN_HOME" \
    WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$ADMIN_RUNTIME/bus" \
    QDISTRO_TIER4_RDP_PASSWORD_FILE="${RDP_PASSWORD_FILE:-}" \
    "${SECCTX_WRAP[@]}" "${CLIENT_CMD[@]}" >>"$CLIENT_LOG" 2>&1 &
CLIENT_PID=$!

# Poll the client log for readiness banner; bail loud if the client
# crashed during startup (so the operator doesn't wait for the cleanup
# trap to fire on a zombie session).
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [ "$TIER4_DISPLAY" = "rdp" ]; then
        if kill -0 "$CLIENT_PID" 2>/dev/null; then
            break
        fi
    elif grep -q 'Starting client main process\|Setting up vsock' "$CLIENT_LOG" 2>/dev/null; then
        break
    fi
    sleep 0.2
done

if ! kill -0 "$CLIENT_PID" 2>/dev/null; then
    echo "[tier4] FAIL: $TIER4_DISPLAY client died during readiness poll" >&2
    cat "$CLIENT_LOG" >&2 || true
    exit 6
fi

wait "$CLIENT_PID" 2>/dev/null
EXIT=$?
echo "[tier4] $TIER4_DISPLAY client exited rc=$EXIT" >&2
exit "$EXIT"
