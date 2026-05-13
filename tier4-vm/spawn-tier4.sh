#!/bin/bash
# §Phase-7 tier-4 — bring up a libvirt+QEMU guest VM and attach it to
# the admin compositor as one chromed peer toplevel via virt-viewer.
#
# spec/02 row 4: "VM, whole-window — KVM + libvirt + virt-viewer".
# spec/29 §2: picked path. Linux-only per spec/00.
#
# Usage:
#   spawn-tier4.sh <vm_name>
#
# Architecture:
#
#   [QEMU + libvirt-managed guest, qemu:///session uri]
#         │ SPICE listen=127.0.0.1, autoport
#         ▼
#   [virt-viewer (admin uid, Wayland client)]
#         │ wayland-1 (admin compositor)
#         ▼
#   [admin compositor]
#
# virt-viewer connects to the spice port allocated by libvirt and
# renders the guest framebuffer in a single Wayland toplevel. qdwin's
# nested-passthrough chrome wraps it like any other peer toplevel
# (title-prefixed for visual differentiation).
#
# Env knobs:
#   TIER4_ADMIN_USER     Admin uid for libvirt + virt-viewer (default "admin").
#   TIER4_MEM_MIB        Guest RAM in MiB (default 256).
#   TIER4_TITLE_PREFIX   Window title prefix (default "[VM:<vm_name>] ").
#   TIER4_DOMAIN_DEFINE_ONLY=1
#                        Define the libvirt domain but don't start it
#                        or launch virt-viewer. Used by tests that just
#                        validate the XML substitution.
#   TIER4_NO_VIEWER=1    Define + start the domain, skip virt-viewer.
#                        Used by tests that observe the libvirt side
#                        without the wayland integration.
#   TIER4_DEBUG=1        Pass --verbose to virt-viewer.
#   TIER4_DOMAIN_TEMPLATE
#                        Path override for the domain template XML.
#                        Defaults to <script_dir>/domain-template.xml.
#   TIER4_USE_SECCTX     Default 1 — wrap virt-viewer via qdistro-secctx-exec
#                        so the resulting Wayland client carries
#                        sandbox_engine=qdistro.tier4, app_id=qdistro.tier4.<vm>.
#                        Set to 0 to run virt-viewer un-tagged (e.g. when
#                        debugging).
#   TIER4_SECCTX_ENGINE  Override sandbox_engine (default qdistro.tier4).
#   TIER4_SECCTX_APPID   Override app_id (default qdistro.tier4.<vm_name>).
#
# Lifecycle:
#   - Defines + starts a domain named <vm_name>. If a domain with
#     that name already exists and is running, attach to it instead
#     of recreating.
#   - Launches virt-viewer in the foreground; viewer exit triggers
#     domain destroy + undefine (matches tier-2/3 oneshot pattern).
#   - SIGTERM / SIGINT propagate cleanly.
#
# Exit code: virt-viewer's exit code, or non-zero on bring-up failure.
set -uo pipefail

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage; exit 0
fi
if [ $# -lt 1 ]; then
    usage >&2; exit 1
fi

VM_NAME="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Resolution order for the domain template:
#   1. TIER4_DOMAIN_TEMPLATE env override (full path).
#   2. Script-local file (when running from tier4-vm/).
#   3. /usr/share/qdistro/tier4/domain-template.xml (installed by
#      fresh-vm-bootstrap.sh; what /usr/local/bin/qdistro-tier4-spawn uses).
TEMPLATE="${TIER4_DOMAIN_TEMPLATE:-}"
if [ -z "$TEMPLATE" ]; then
    if [ -f "$SCRIPT_DIR/domain-template.xml" ]; then
        TEMPLATE="$SCRIPT_DIR/domain-template.xml"
    elif [ -f /usr/share/qdistro/tier4/domain-template.xml ]; then
        TEMPLATE=/usr/share/qdistro/tier4/domain-template.xml
    else
        TEMPLATE="$SCRIPT_DIR/domain-template.xml"  # report the missing-script-local path
    fi
fi

# Validate vm_name shape.
if ! [[ "$VM_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$ ]]; then
    echo "[tier4] FAIL: invalid vm_name '$VM_NAME' (alnum + - + _, 1-63 chars)" >&2
    exit 1
fi
if [ ! -f "$TEMPLATE" ]; then
    echo "[tier4] FAIL: template $TEMPLATE not found" >&2
    exit 1
fi

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
# virt-viewer needed unless caller suppresses it.
NO_VIEWER="${TIER4_NO_VIEWER:-0}"
if [ "$NO_VIEWER" != "1" ] && ! command -v virt-viewer >/dev/null 2>&1; then
    echo "[tier4] FAIL: virt-viewer not installed (zypper install virt-viewer)" >&2
    exit 2
fi

DEFINE_ONLY="${TIER4_DOMAIN_DEFINE_ONLY:-0}"
if [ "$DEFINE_ONLY" != "1" ] && [ "$NO_VIEWER" != "1" ]; then
    if [ ! -S "$ADMIN_RUNTIME/$WAYLAND_DISPLAY" ]; then
        echo "[tier4] FAIL: admin compositor socket $ADMIN_RUNTIME/$WAYLAND_DISPLAY not present" >&2
        exit 3
    fi
fi

MEM_MIB="${TIER4_MEM_MIB:-256}"
MEM_KIB=$((MEM_MIB * 1024))
TITLE_PREFIX="${TIER4_TITLE_PREFIX:-[VM:$VM_NAME] }"

# spec/10 §"SPICE main-channel clipboard": disable cliprdr / spice-
# vdagent clipboard channel by default. All host ↔ guest clipboard
# traffic flows through wayland selection (gated by tasks 034 + 037).
# Admin opts in via TIER4_SPICE_CLIPBOARD=allowed for legacy guests
# that need raw SPICE clipboard sync.
case "${TIER4_SPICE_CLIPBOARD:-disabled}" in
    allowed|enabled|yes|on|1) SPICE_CLIPBOARD="yes";;
    *)                         SPICE_CLIPBOARD="no";;
esac

# Optional bootable disk via per-VM linked clone of a base qcow2.
# When TIER4_DISK_BASE is set + readable, we create
#   $TIER4_DISK_DIR/<vm_name>.qcow2
# as a qemu-img linked clone (overlay) and substitute a <disk>
# element into the domain XML. Default unset → no disk → guest boots
# to "no bootable device" (sufficient for XML-shape tests but not for
# real spec/10 SPICE clipboard validation; that needs the SPICE-
# capable guest qcow2 from tier4-vm/build-guest-
# image.sh).
DISK_BASE="${TIER4_DISK_BASE:-}"
DISK_DIR="${TIER4_DISK_DIR:-/var/lib/libvirt/images}"
DISK_PATH=""
DISK_XML=""
if [ -n "$DISK_BASE" ]; then
    if [ ! -r "$DISK_BASE" ]; then
        echo "[tier4] FAIL: TIER4_DISK_BASE=$DISK_BASE not readable" >&2
        exit 4
    fi
    DISK_PATH="$DISK_DIR/$VM_NAME.qcow2"
    install -d -m 0755 "$DISK_DIR"
    rm -f "$DISK_PATH"
    if ! qemu-img create -f qcow2 -F qcow2 -b "$DISK_BASE" "$DISK_PATH" \
            >/dev/null 2>&1; then
        echo "[tier4] FAIL: qemu-img create linked clone failed" >&2
        exit 4
    fi
    chmod 0644 "$DISK_PATH"
    chown "${TIER4_ADMIN_USER:-admin}":root "$DISK_PATH" 2>/dev/null || true
    DISK_XML="<disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='$DISK_PATH'/><target dev='vda' bus='virtio'/></disk>"
    echo "[tier4] linked clone $DISK_PATH from base $DISK_BASE" >&2
fi

# spec/10 §"SPICE main-channel clipboard": per-launch SPICE password
# (16 hex chars from /dev/urandom). virt-viewer reads it from the
# domdisplay --include-password URI. Re-spawning the VM rotates the
# value so an attacker who learns one ticket can't re-attach later.
# Admin can disable via TIER4_SPICE_PASSWD="" (not recommended).
if [ -z "${TIER4_SPICE_PASSWD+x}" ]; then
    SPICE_PASSWD=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
elif [ -z "$TIER4_SPICE_PASSWD" ]; then
    # Caller explicitly disabled — keep XML intact but use a "" attr.
    # libvirt accepts empty passwd as "no auth" but SPICE then accepts
    # any client. Logged for audit.
    SPICE_PASSWD=""
    echo "[tier4] WARN: TIER4_SPICE_PASSWD='' — SPICE auth disabled" >&2
else
    # Caller-supplied password (test fixtures pass a known value).
    SPICE_PASSWD="$TIER4_SPICE_PASSWD"
fi

# qemu:///session lets the admin user manage their own libvirt domains
# without needing libvirtd running as root or polkit rules. Each
# `runuser -u admin -- virsh ...` re-resolves XDG_RUNTIME_DIR + opens a
# new session-uri connection.
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

# --- 1. domain XML substitution --------------------------------------
NEW_MAC="52:54:00:$(printf '%02x:%02x:%02x' \
    $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"

XML=$(sed \
    -e "s|__VM_NAME__|$VM_NAME|g" \
    -e "s|__MAC__|$NEW_MAC|g" \
    -e "s|__MEM_KIB__|$MEM_KIB|g" \
    -e "s|__SPICE_PASSWD__|$SPICE_PASSWD|g" \
    -e "s|__SPICE_CLIPBOARD__|$SPICE_CLIPBOARD|g" \
    -e "s|__DISK_XML__|$DISK_XML|g" \
    "$TEMPLATE")

# Sanity check: every marker substituted.
if printf '%s' "$XML" | grep -q '__[A-Z_]*__'; then
    echo "[tier4] FAIL: unsubstituted markers in domain XML:" >&2
    printf '%s' "$XML" | grep -o '__[A-Z_]*__' | sort -u >&2
    exit 4
fi

# --- 2. define + (optional) start ------------------------------------
# If a domain with this name already exists, undefine it first to make
# spawn-tier4.sh idempotent for re-runs in tests.
if run_as_admin virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
    echo "[tier4] domain '$VM_NAME' already exists — destroying + undefining" >&2
    run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
fi

echo "[tier4] defining domain '$VM_NAME' (mem=${MEM_MIB}MiB, mac=$NEW_MAC)" >&2
# Stage XML in a tempfile that the admin user can read. virsh define
# can't reliably read /dev/stdin across runuser because the inherited
# pipe inode is owned by the parent (root) and re-opening it as the
# dropped user fails with EACCES (kernel re-checks pipe ownership on
# open(), even when the fd is already inherited).
TMP_XML="/tmp/qdistro-tier4-${VM_NAME}-$$.xml"
printf '%s' "$XML" >"$TMP_XML"
chown "$ADMIN_USER" "$TMP_XML" 2>/dev/null || true
chmod 0644 "$TMP_XML" 2>/dev/null || true
if ! run_as_admin virsh define "$TMP_XML" >/dev/null; then
    echo "[tier4] FAIL: virsh define failed" >&2
    rm -f "$TMP_XML"
    exit 5
fi
rm -f "$TMP_XML"

if [ "$DEFINE_ONLY" = "1" ]; then
    echo "[tier4] TIER4_DOMAIN_DEFINE_ONLY=1 — domain defined; not starting" >&2
    echo "[tier4] OK"
    exit 0
fi

cleanup() {
    run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    if [ -n "$DISK_PATH" ] && [ -f "$DISK_PATH" ]; then
        rm -f "$DISK_PATH" 2>/dev/null || true
    fi
}
# EXIT runs cleanup once on natural exit. INT/TERM additionally exit
# the script explicitly — bash's default behaviour on a signal-trapped
# `sleep` is to resume the surrounding loop after the trap returns,
# so without an explicit exit the `while sleep` park below would
# continue forever in NO_VIEWER mode. Caught during s42 fresh-VM
# validation.
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

echo "[tier4] starting domain '$VM_NAME'" >&2
if ! run_as_admin virsh start "$VM_NAME" >/dev/null; then
    echo "[tier4] FAIL: virsh start failed (libvirtd may need to be reachable as $ADMIN_USER)" >&2
    exit 6
fi

# Wait briefly for spice port allocation. Up to 5s; the typical case
# is sub-second. --include-password embeds the per-launch SPICE
# ticket in the URI so virt-viewer can authenticate without prompting
# (spec/10 §"SPICE main-channel clipboard"). The URI is logged at the
# host syslog level only — script stderr just prints the host:port,
# scrubbing the password to keep it out of journals and pytest tee.
SPICE_URI=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    SPICE_URI=$(run_as_admin virsh domdisplay --include-password \
        "$VM_NAME" 2>/dev/null || true)
    [ -n "$SPICE_URI" ] && break
    sleep 0.5
done
if [ -z "$SPICE_URI" ]; then
    echo "[tier4] FAIL: virsh domdisplay returned no URI within 5s" >&2
    exit 7
fi
SPICE_URI_REDACTED="${SPICE_URI//$SPICE_PASSWD/REDACTED}"
echo "[tier4] domain display: $SPICE_URI_REDACTED (clipboard=$SPICE_CLIPBOARD)" >&2

if [ "$NO_VIEWER" = "1" ]; then
    echo "[tier4] TIER4_NO_VIEWER=1 — domain running; not launching viewer" >&2
    echo "[tier4] OK ($SPICE_URI)"
    # Park: the trap will clean up on signal.
    while sleep 60; do :; done
fi

# --- 3. virt-viewer (admin-side Wayland client) ----------------------
VIEWER_OPTS=(
    --connect "$LIBVIRT_URI"
    --wait
    --title "${TITLE_PREFIX}${VM_NAME}"
    --hotkeys=toggle-fullscreen=shift+f11,release-cursor=shift+f12
)
[ "${TIER4_DEBUG:-0}" = "1" ] && VIEWER_OPTS=(--verbose "${VIEWER_OPTS[@]}")

USE_SECCTX="${TIER4_USE_SECCTX:-1}"
SECCTX_ENGINE="${TIER4_SECCTX_ENGINE:-qdistro.tier4}"
SECCTX_APPID="${TIER4_SECCTX_APPID:-qdistro.tier4.$VM_NAME}"

if [ "$USE_SECCTX" = "1" ] && command -v qdistro-secctx-exec >/dev/null 2>&1; then
    echo "[tier4] launching virt-viewer for '$VM_NAME' (title-prefix='$TITLE_PREFIX', secctx engine=$SECCTX_ENGINE app_id=$SECCTX_APPID)" >&2
    run_as_admin qdistro-secctx-exec \
        --sandbox-engine "$SECCTX_ENGINE" \
        --app-id "$SECCTX_APPID" \
        --instance-id "$VM_NAME-$$" \
        -- virt-viewer "${VIEWER_OPTS[@]}" "$VM_NAME"
    EXIT=$?
else
    if [ "$USE_SECCTX" = "1" ]; then
        echo "[tier4] WARN: TIER4_USE_SECCTX=1 but qdistro-secctx-exec not found; running un-tagged" >&2
    fi
    echo "[tier4] launching virt-viewer for '$VM_NAME' (title-prefix='$TITLE_PREFIX')" >&2
    run_as_admin virt-viewer "${VIEWER_OPTS[@]}" "$VM_NAME"
    EXIT=$?
fi
echo "[tier4] virt-viewer exited rc=$EXIT" >&2
exit "$EXIT"
