#!/bin/bash
# §Phase-7 tier-5 (Linux) — bridge an app from a guest VM to the admin
# compositor via waypipe over AF_VSOCK.
#
# spec/29 §3: picked path for Tier-5-Linux. Linux-only per spec/00.
#
# Usage (as the admin user — root execution is refused):
#   spawn-tier5.sh --loopback [-p PORT] -- <app> [args...]
#   spawn-tier5.sh --vm <vm_name> [-p PORT] [--policy-key KEY] -- <app> [args...]
#
# Privilege split: the whole VM lifecycle (config, domain XML, overlay
# disks, sidecars, locks, logs, virsh, qemu-img) runs as admin against
# admin's own qemu:///session. The ONE privileged step is the host-side
# waypipe client: qdistro-secctx-exec and qdwin tag a Wayland connection
# only under a uid-0 direct launcher parent, and RegisterLaunch is
# root-helper-only. Both are done by the root bridge
#   pkexec /usr/libexec/qdistro/qdistro-tier5-bridge
# (polkit org.qdistro.tier5.bridge, auth_admin_keep). It takes only the VM
# name, port and launch token, derives engine qdistro.tier5, app-id
# qdistro.tier5.<name> and broker silo vm-<name> itself, prints
# "READY <pid>" once the waypipe client is verified and registered, and
# tears the client down when our end of its stdin pipe closes.
#
# Modes:
#
#   --loopback   vsock CID=1 (VMADDR_CID_LOCAL). The bridge's waypipe client
#                listens on vsock:1:PORT; a waypipe server runs here as
#                admin and connects to it. No guest VM, no isolation — a
#                data-path exercise. Needs the vsock_loopback module, which
#                installation/test setup provisions (loading it is
#                privileged).
#
#   --vm <name>  Run the per-app libvirt domain <name> (linked clone of
#                /var/lib/libvirt/images/qdistro-tier5-base.qcow2). The
#                guest gets a CID >= 3; the host-side client listens on
#                CID=2; the guest-side waypipe server is started through
#                qemu-guest-agent guest-exec of
#                /usr/local/bin/qdistro-tier5-publisher.sh.
#
# Architecture (--vm):
#
#   [silo app inside guest VM]
#         │
#   [waypipe server in guest, --vsock -s 2:PORT]
#         │ AF_VSOCK
#   [waypipe client on host, admin uid, under secctx-exec ← runuser ← bridge]
#         │ wayland-1
#   [admin compositor]
#
# Env knobs:
#   TIER5_DEBUG=1        Pass --debug to the loopback waypipe server.
#   TIER5_PORT           vsock port (default 7777 in loopback; random
#                        20000..39999 in --vm mode).
#   TIER5_CID            (--vm only) override the allocated guest CID.
#   TIER5_BASE_DISK      (--vm only) override base disk path.
#   TIER5_DISK_DIR       (--vm only) override per-VM disk dir (default
#                        ~/.local/share/libvirt/images).
#   TIER5_MEM_KIB        (--vm only) guest RAM in KiB (default 2097152).
#   TIER5_SERIAL_LOG     (--vm only) file-backed guest serial console;
#                        default unset = pty serial (`virsh console`).
#   TIER5_QGA_TIMEOUT_SECS
#                        (--vm only) cold-boot deadline for qemu-guest-agent
#                        guest-ping (30..600, default 180).
#   TIER5_DOMAIN_TEMPLATE
#                        (--vm only) override domain XML template path.
#   TIER5_KEEP_DOMAIN=1  (--vm only) never tear the domain down (debug).
#                        Bypasses ALL lifecycle policy below.
#   TIER5_NETWORK        (--vm only) "user" (default: virtio NIC with
#                        QEMU usermode NAT) or "none" (no NIC).
#   TIER5_NO_REAP=1      (--vm only) skip the startup orphan-overlay reaper.
#   TIER5_DECISION_WAIT_SECS
#                        (--vm only) bound on waiting for another wrapper's
#                        adopt/create decision (1..600, default 120). A
#                        relaunch waits this PLUS the worst-case graceful
#                        teardown budget for the session hold.
#
#   Lifecycle policy (--vm only) is PER-APP. Precedence per value:
#     env var > [app:<policy-key>] section > global section > built-in default.
#   The config file (~/.config/qdistro/tier5-lifecycle.conf) is written by
#   qdshell's "Sandboxed VM apps" settings panel and parsed (never sourced).
#   --policy-key KEY     stable per-app key (qdshell passes its catalogue
#                        appId, e.g. "tier5/firefox"); default: app basename.
#   TIER5_SHUTDOWN_METHOD  graceful (default) | force. graceful = ACPI power
#                        button, qga agent shutdown after the grace, then
#                        `virsh destroy`. A SIGTERM/SIGINT teardown is ALWAYS
#                        force.
#   TIER5_SHUTDOWN_GRACE_SECS  graceful-shutdown wait (0..300, default 15).
#   TIER5_IDLE_SHUTDOWN_SECS   keep the idle VM warm this long after the app
#                        exits, for a relaunch to adopt (0..86400, default 0).
#   TIER5_LOWMEM_MB      end the idle window early when host MemAvailable
#                        drops below this (0 = disabled).
#   TIER5_LIFECYCLE_CONF override the config file path.
#
# Exit code = silo app's exit code (loopback), or non-zero on bridge setup
# failure.
set -uo pipefail

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
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
EXPLICIT_POLICY_KEY=""

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
        --policy-key)
            # Parsed BEFORE `--` so it never reaches the guest argv.
            shift
            EXPLICIT_POLICY_KEY="${1:?--policy-key requires KEY}"; shift
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
# The lifecycle is admin's own business: everything below touches only
# admin-owned files and admin's libvirt session. Running it as root would
# reintroduce root file operations inside admin-writable directories.
if [ "$(id -u)" -eq 0 ]; then
    echo "[tier5] FAIL: run as the admin user, not root — the privileged" >&2
    echo "        step is the pkexec'd qdistro-tier5-bridge" >&2
    exit 2
fi
for tool in waypipe pkexec flock python3; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "[tier5] FAIL: $tool not installed" >&2
        exit 2
    }
done

TIER5_BRIDGE=/usr/libexec/qdistro/qdistro-tier5-bridge
ADMIN_UID=$(id -u)
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$ADMIN_UID}"
export XDG_RUNTIME_DIR="$RUNTIME_DIR"
export LIBVIRT_DEFAULT_URI="qemu:///session"
# The bridge always attaches its client to wayland-1.
if [ ! -S "$RUNTIME_DIR/wayland-1" ]; then
    echo "[tier5] FAIL: admin compositor socket $RUNTIME_DIR/wayland-1 not present" >&2
    exit 3
fi
HOLD_DIR="$RUNTIME_DIR/qdistro-tier5"
if ! { mkdir -m 0700 "$HOLD_DIR" 2>/dev/null || [ -d "$HOLD_DIR" ]; }; then
    echo "[tier5] FAIL: cannot create $HOLD_DIR" >&2
    exit 3
fi

clamp_int() {  # clamp_int VARNAME DEFAULT MIN MAX — validate/clamp an integer
    local name="$1" def="$2" lo="$3" hi="$4" cur
    cur="${!name}"
    printf '%s' "$cur" | grep -qE '^[0-9]+$' || cur="$def"
    [ "$cur" -lt "$lo" ] && cur="$lo"
    [ "$cur" -gt "$hi" ] && cur="$hi"
    printf -v "$name" '%s' "$cur"
}

# qemu_pids_for_domain [name] — print the pid(s) of the qemu process(es)
# backing a domain (default $VM_NAME). This is the ONLY reliable liveness
# oracle here: on a nested-virt host libvirt's domstate lies (reports
# "shut off" while qemu is still ALIVE — the failure mode commit 8ac0a39
# was written for). RETURN CODES are the contract:
#   0 — pids printed: a backing qemu EXISTS
#   1 — clean scan: confirmed NO backing qemu
#   2 — scan unreliable (/proc unlistable, a process of ours unreadable
#       for any reason but disappearance, interpreter failure). NOT
#       "dead" — callers must fail closed.
# Session qemus run as this uid with `-name guest=<domain>,…`: a candidate
# must be ours, have a qemu comm, and carry `guest=<name>,` as the argument
# right after `-name` (argv split on NUL, so no crafted argument can fake
# the pair). A process that vanishes mid-scan (ENOENT/ESRCH) is dead, not
# ambiguous; EACCES/EIO on one is ambiguous — it may be the live guest.
qemu_pids_for_domain() {
    local name="${1:-$VM_NAME}" out rc
    out=$(T5NAME="$name" python3 - <<'PY'
import errno, os, sys
needle = b"guest=" + os.environ["T5NAME"].encode() + b","
me = os.getuid()
# Only these prove the process is gone; any other failure (EACCES, EIO,
# ...) on a candidate means it may be a live qemu we could not read.
GONE = (errno.ENOENT, errno.ESRCH)
try:
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
except OSError:
    sys.exit(2)
found = []
unsure = False
for p in pids:
    try:
        if os.stat(f"/proc/{p}").st_uid != me:
            continue
        with open(f"/proc/{p}/comm", "rb") as f:
            comm = f.read().strip()
        if not (comm.startswith(b"qemu-system-") or comm == b"qemu-kvm"):
            continue
        with open(f"/proc/{p}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError as e:
        if e.errno not in GONE:
            unsure = True
        continue
    if any(a == b"-name" and argv[i + 1].startswith(needle)
           for i, a in enumerate(argv[:-1])):
        found.append(p)
if found:
    print(" ".join(found))
    sys.exit(0)
sys.exit(2 if unsure else 1)
PY
)
    rc=$?
    case $rc in
        0) printf '%s' "$out"; return 0 ;;
        1) return 1 ;;
        *) return 2 ;;
    esac
}

# domain_is_live [name] — is the guest ACTUALLY alive? rc: 0 live,
# 1 confirmed dead, 2 cannot determine. The qemu scan is the only
# authority: domstate lies in BOTH directions under nested virt ("shut
# off" for a live qemu, stale "running" for a dead one).
domain_is_live() {
    qemu_pids_for_domain "${1:-$VM_NAME}" >/dev/null
}

# domain_defined_state [name] — rc 0 = defined, 1 = CONFIRMED absent,
# 2 = cannot determine. An enumeration that SUCCEEDS is authoritative;
# any failure is unverifiable (dominfo error text drifts across libvirt
# versions and also matches outage text).
domain_defined_state() {
    local name="${1:-$VM_NAME}" list
    list=$(virsh list --all --name 2>/dev/null) || return 2
    printf '%s\n' "$list" | grep -Fxq -- "$name" && return 0
    return 1
}

# tier5_derived_name <appId> — port of VMApps._vmNameForApp: an owner
# record's appId must re-derive to the artifact's own name before the
# reaper treats the artifact as that app's. appIds are ASCII by the
# --policy-key charset, so JS charCodeAt == ord() here.
tier5_derived_name() {
    python3 - "$1" <<'PY'
import re, sys
s = sys.argv[1]
fnv, djb = 0x811c9dc5, 0x00001505
for c in (ord(ch) for ch in s):
    fnv = ((fnv ^ c) * 0x01000193) & 0xFFFFFFFF
    djb = ((djb * 33) ^ c) & 0xFFFFFFFF
stub = re.sub(r"[^a-zA-Z0-9_-]", "-", s)
stub = re.sub(r"-{2,}", "-", stub).strip("-")[:43]
print("t5-" + (stub or "vmapp") + f"-{fnv:08x}{djb:08x}")
PY
}

# manifest_app_state <appId> <base> — rc 0 = the CURRENT non-stale
# catalogue publishes this appId; rc 1 = a VALID catalogue for THIS base
# does not publish it (retired app — its disk is provably stale); rc 2 =
# cannot determine. "Valid" mirrors the scanner: parses, schema 1, no
# "stale" marker, well-formed nonempty rows, the recorded base identity
# matching the current base file, and the manifest written after the base.
manifest_app_state() {
    python3 - "$VMAPPS_MANIFEST" "$1" "$2" <<'PY'
import json, os, sys
mpath, side, base = sys.argv[1], sys.argv[2], sys.argv[3]


def _row_ok(r):
    if not isinstance(r, dict):
        return False
    for k in ("appId", "desktopId", "name", "iconName", "comment"):
        if not isinstance(r.get(k), str):
            return False
    a = r.get("execArgv")
    if not isinstance(a, str):
        return False
    try:
        argv = json.loads(a)
    except Exception:
        return False
    return (isinstance(argv, list) and len(argv) > 0
            and all(isinstance(x, str) for x in argv) and bool(argv[0]))


try:
    with open(mpath) as f:
        doc = json.load(f)
    st = os.stat(base)
    mst = os.stat(mpath)
except Exception:
    sys.exit(2)
if not isinstance(doc, dict) or doc.get("stale") or doc.get("schema") != 1:
    sys.exit(2)
ident = (doc.get("baseDev"), doc.get("baseIno"),
         doc.get("baseMtime"), doc.get("baseMtimeNs"),
         doc.get("baseSize"))
if ident != (st.st_dev, st.st_ino, int(st.st_mtime), st.st_mtime_ns,
             st.st_size):
    sys.exit(2)
if mst.st_mtime_ns < st.st_mtime_ns:
    sys.exit(2)
apps = doc.get("apps")
if not isinstance(apps, list) or not apps or not all(map(_row_ok, apps)):
    sys.exit(2)
sys.exit(0 if any(r["appId"] == side for r in apps) else 1)
PY
}

# _read_owner_record <path> — read the "<appId> <dev> <ino>" sidecar into
# _sapp / _sident ("<dev> <ino>"). Empty _sapp = no readable record. The
# sidecar binds an overlay to the app it was created for, and to the very
# inode created — a record copied beside another disk does not match.
_read_owner_record() {
    _sapp="" _sident=""
    local line=""
    [ -f "$1" ] && [ ! -L "$1" ] || return 0
    IFS= read -r line <"$1" 2>/dev/null || [ -n "$line" ] || return 0
    case "$line" in
        *" "*) _sapp=${line%% *}; _sident=${line#* } ;;
        *) _sapp=$line ;;
    esac
}

# overlay_backing <path> — the overlay's backing file, "" when unreadable.
# -U: a running guest holds the image lock.
overlay_backing() {
    qemu-img info -U --output=json "$1" 2>/dev/null \
        | sed -n 's/.*"backing-filename": "\([^"]*\)".*/\1/p'
}

DEBUG="${TIER5_DEBUG:-0}"

# --- mode-specific bring-up ------------------------------------------
if [ "$MODE" = "loopback" ]; then
    if [ ! -d /sys/module/vsock_loopback ]; then
        echo "[tier5] FAIL: vsock_loopback module not loaded (provisioned" >&2
        echo "        by installation/test setup: modprobe vsock_loopback)" >&2
        exit 3
    fi
    SILO_TAG="loopback-$$"
    PORT="${EXPLICIT_PORT:-${TIER5_PORT:-7777}}"
else
    for tool in virsh qemu-img; do
        command -v "$tool" >/dev/null || {
            echo "[tier5] FAIL: --vm needs $tool installed" >&2
            exit 2
        }
    done
    if ! [[ "$VM_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$ ]] \
            || [[ "$VM_NAME" == loopback-* ]]; then
        echo "[tier5] FAIL: invalid vm_name '$VM_NAME'" >&2
        exit 1
    fi
    SILO_TAG="$VM_NAME"
    DISK_BASE="${TIER5_BASE_DISK:-/var/lib/libvirt/images/qdistro-tier5-base.qcow2}"
    # qemu:///session domains run as admin, so per-VM disks live in admin's
    # session image dir; the base stays shared under /var/lib/libvirt/images.
    DISK_DIR="${TIER5_DISK_DIR:-$HOME/.local/share/libvirt/images}"
    DISK="$DISK_DIR/$VM_NAME.qcow2"
    VMAPPS_MANIFEST="${VMAPPS_MANIFEST:-/usr/share/qdistro/vmapps/base.json}"
    if [ ! -r "$DISK_BASE" ]; then
        echo "[tier5] FAIL: base disk $DISK_BASE missing or unreadable" >&2
        echo "        — run tier5/build-guest-image.sh (needs mode 0644)" >&2
        exit 3
    fi
    if ! mkdir -p -- "$DISK_DIR"; then
        echo "[tier5] FAIL: cannot create $DISK_DIR" >&2
        exit 3
    fi

    NETWORK="${TIER5_NETWORK:-user}"
    case "$NETWORK" in
        none) NIC_XML="<!-- TIER5_NETWORK=none: no NIC (hardening parity with tier-2 network=none) -->" ;;
        user) NIC_XML="<interface type='user'><mac address='__MAC__'/><model type='virtio'/></interface>" ;;
        *)
            echo "[tier5] FAIL: TIER5_NETWORK='$NETWORK' invalid; expected 'none' or 'user'" >&2
            exit 1
            ;;
    esac

    # --- orphan-overlay reaper -----------------------------------------
    # Overlays are orphaned when a wrapper died without its EXIT trap
    # (kill -9, host crash, OOM). Reap only provable leftovers: a
    # t5-<stub>-<hash> overlay of OUR base whose hold nobody holds, whose
    # qemu is confirmed dead and whose domain is confirmed absent (a
    # defined-but-stopped domain is relaunched by its app, not reaped),
    # with a sidecar that binds this inode to an appId deriving this very
    # name — and that app is retired from a valid catalogue (an owned
    # orphan's app relaunches under its own name). Any unverifiable verdict
    # keeps the overlay.
    if [ "${TIER5_NO_REAP:-0}" != "1" ] && [ -d "$DISK_DIR" ]; then
        for overlay in "$DISK_DIR"/*.qcow2; do
            [ -f "$overlay" ] && [ ! -L "$overlay" ] || continue
            base=$(basename "$overlay" .qcow2)
            [ "$base" = "$VM_NAME" ] && continue
            [[ "$base" =~ ^t5-[A-Za-z0-9_-]{1,43}-[0-9a-f]{16}$ ]] || continue
            [ "$(overlay_backing "$overlay")" = "$DISK_BASE" ] || continue
            ( umask 077; : >>"$HOLD_DIR/hold-$base" ) 2>/dev/null || continue
            { exec {hfd}>>"$HOLD_DIR/hold-$base"; } 2>/dev/null || continue
            if flock -n -x "$hfd" 2>/dev/null \
                    && { domain_is_live "$base"; [ $? -eq 1 ]; } \
                    && { domain_defined_state "$base"; [ $? -eq 1 ]; }; then
                _read_owner_record "$overlay.appid"
                if [ -n "$_sapp" ] \
                        && [ "$_sident" = "$(stat -c '%d %i' "$overlay" 2>/dev/null)" ] \
                        && [ "$(tier5_derived_name "$_sapp")" = "$base" ] \
                        && { manifest_app_state "$_sapp" "$DISK_BASE"; [ $? -eq 1 ]; }; then
                    rm -f -- "$overlay" "$overlay.appid"
                    echo "[tier5] reaped orphan overlay $overlay ($_sapp retired)" >&2
                fi
            fi
            exec {hfd}>&-
        done
    fi

    # Allocate CID: scan defined domains for taken CIDs, pick next free.
    if [ -n "${TIER5_CID:-}" ]; then
        CID="$TIER5_CID"
    else
        TAKEN=$(for d in $(virsh list --all --name 2>/dev/null); do
                    virsh dumpxml "$d" 2>/dev/null
                done | grep -oE "<cid[^>]*address='[0-9]+'" \
                     | grep -oE "[0-9]+" | sort -un || true)
        CID=3
        while echo "$TAKEN" | grep -qx "$CID"; do
            CID=$((CID + 1))
        done
    fi
    # A per-spawn port: a stale client from a crashed launch cannot pin the
    # next one. The guest receives the same port via qga below.
    PORT="${EXPLICIT_PORT:-${TIER5_PORT:-$((20000 + RANDOM % 20000))}}"
fi

APP_BASENAME=$(basename "$1")

# The LAUNCH_TOKEN is the secctx instance_id the bridge stamps on the
# waypipe client: qdshell's VMApps reads it from stdout to seed a
# cold-start placeholder and resolves it when a toplevel carries it.
# shellcheck source=../lib/spawn-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/spawn-common.sh"
LAUNCH_TOKEN="$(gen_launch_token "[tier5] FAIL")"
echo "LAUNCH_TOKEN=$LAUNCH_TOKEN"
echo "VM_NAME=$VM_NAME"
echo "APP_ID=$APP_BASENAME"

# Concurrent sessions of the same app share $SILO_TAG (the per-app
# domain); the token prefix keeps their logs apart.
CLIENT_LOG="$RUNTIME_DIR/tier5-${SILO_TAG}-${APP_BASENAME}-${LAUNCH_TOKEN:0:8}-client.log"
SERVER_LOG="$RUNTIME_DIR/tier5-${SILO_TAG}-${APP_BASENAME}-${LAUNCH_TOKEN:0:8}-server.log"
if ! { : >"$CLIENT_LOG" && : >"$SERVER_LOG"; }; then
    echo "[tier5] FAIL: cannot create waypipe logs under $RUNTIME_DIR" >&2
    exit 4
fi

# --- tier-5 VM lifecycle policy (graceful shutdown / idle timeout / low-mem) --
# POLICY_KEY is the STABLE per-app identity (qdshell's catalogue appId).
# It is also the appId bound into the domain's <metadata> and the overlay
# sidecar — adoption requires an exact match, so two apps never share a
# guest. An EXPLICIT key failing the charset is a hard error, never a
# basename fallback. `[[ =~ ]]` anchors against the whole string (grep
# would let a newline-bearing key through line by line).
LIFECYCLE_CONF="${TIER5_LIFECYCLE_CONF:-$HOME/.config/qdistro/tier5-lifecycle.conf}"
POLICY_KEY="${EXPLICIT_POLICY_KEY:-${TIER5_POLICY_KEY:-}}"
if [ -n "$POLICY_KEY" ]; then
    if ! [[ "$POLICY_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$ ]]; then
        echo "[tier5] FAIL: invalid policy key '$POLICY_KEY'" >&2
        exit 3
    fi
else
    POLICY_KEY=$(printf '%s' "$APP_BASENAME" | tr -c 'A-Za-z0-9._/-' '_')
    case "$POLICY_KEY" in [A-Za-z0-9]*) ;; *) POLICY_KEY="x$POLICY_KEY";; esac
    POLICY_KEY="${POLICY_KEY:0:128}"
fi

_conf_file_ok() {  # honor only a regular, own/root-owned, sane-perm file
    local f="$LIFECYCLE_CONF" owner
    [ -f "$f" ] || return 1
    [ -L "$f" ] && return 1
    find "$f" -maxdepth 0 -perm /022 2>/dev/null | grep -q . && return 1
    owner=$(stat -c '%u' "$f" 2>/dev/null) || return 1
    case "$owner" in 0|"$ADMIN_UID") return 0 ;; *) return 1 ;; esac
}

conf_get() {  # conf_get SECTION KEY -> value ("" SECTION = global/top), else ""
    _conf_file_ok || return 0
    awk -v want="$1" -v key="$2" '
        /^[[:space:]]*[#;]/ { next }
        /^[[:space:]]*\[.*\][[:space:]]*$/ {                 # section header
            s=$0; sub(/^[[:space:]]*\[/,"",s); sub(/\][[:space:]]*$/,"",s)
            cur=s; next                                      # literal, not regex
        }
        {
            if (cur != want) next                            # string equality
            line=$0; sub(/^[[:space:]]+/,"",line)
            eq=index(line,"="); if (eq==0) next              # split at first =
            k=substr(line,1,eq-1); sub(/[[:space:]]+$/,"",k)
            if (k != key) next
            v=substr(line,eq+1); gsub(/[[:space:]]/,"",v); val=v   # last wins
        }
        END { if (val != "") print val }
    ' "$LIFECYCLE_CONF"
}

resolve_policy() {  # resolve_policy KEY -> per-app override else global else ""
    local v
    v=$(conf_get "app:$POLICY_KEY" "$1")
    [ -n "$v" ] && { printf '%s' "$v"; return 0; }
    conf_get "" "$1"
}

SHUTDOWN_GRACE_MAX=300
SHUTDOWN_AGENT_SECS=8
SHUTDOWN_METHOD="${TIER5_SHUTDOWN_METHOD:-$(resolve_policy SHUTDOWN_METHOD)}"
case "$SHUTDOWN_METHOD" in graceful|force) ;; *) SHUTDOWN_METHOD=graceful ;; esac
SHUTDOWN_GRACE_SECS="${TIER5_SHUTDOWN_GRACE_SECS:-$(resolve_policy SHUTDOWN_GRACE_SECS)}"
clamp_int SHUTDOWN_GRACE_SECS 15 0 "$SHUTDOWN_GRACE_MAX"
IDLE_SHUTDOWN_SECS="${TIER5_IDLE_SHUTDOWN_SECS:-$(resolve_policy IDLE_SHUTDOWN_SECS)}"
clamp_int IDLE_SHUTDOWN_SECS 0 0 86400
LOWMEM_MB="${TIER5_LOWMEM_MB:-$(resolve_policy LOWMEM_MB)}"
clamp_int LOWMEM_MB 0 0 1048576
QGA_TIMEOUT_SECS="${TIER5_QGA_TIMEOUT_SECS:-180}"
clamp_int QGA_TIMEOUT_SECS 180 30 600
DECISION_WAIT_SECS="${TIER5_DECISION_WAIT_SECS:-120}"
clamp_int DECISION_WAIT_SECS 120 1 600
# A wrapper tearing the guest down holds the hold EXCLUSIVELY through its
# whole graceful shutdown — up to the maximum grace (another app session's
# policy may differ from ours), the agent fallback, then destroy and the
# undefine retries. A relaunch waits that out and cold-starts after the
# teardown instead of failing (an interim defect waited a fixed 120 s).
TEARDOWN_BUDGET_SECS=$((SHUTDOWN_GRACE_MAX + SHUTDOWN_AGENT_SECS + 60))
HOLD_WAIT_SECS=$((DECISION_WAIT_SECS + TEARDOWN_BUDGET_SECS))

host_mem_avail_mb() {  # host MemAvailable in MiB, or "" if unreadable
    local kb
    kb=$(grep -E '^MemAvailable:' /proc/meminfo 2>/dev/null | grep -oE '[0-9]+' | head -1)
    [ -n "$kb" ] || return 0
    printf '%s' $((kb / 1024))
}

# Try the ordinary undefine first. If libvirt reports retained per-domain state
# or metadata, remove only that ephemeral VM state and retry. Storage is never
# libvirt's to remove: the wrapper unlinks the overlay itself, and only after
# the definition is confirmed gone.
undefine_domain() {
    local err
    if err=$(virsh undefine "$VM_NAME" 2>&1); then
        return 0
    fi
    echo "[tier5] WARN: undefine $VM_NAME failed: $err" >&2
    domain_defined_state; local ds=$?
    [ "$ds" -eq 1 ] && return 0
    [ "$ds" -eq 0 ] || return 1
    if err=$(virsh undefine "$VM_NAME" \
        --managed-save --snapshots-metadata --checkpoints-metadata \
        --nvram --tpm 2>&1); then
        echo "[tier5] undefine $VM_NAME succeeded after removing retained metadata" >&2
        return 0
    fi
    echo "[tier5] WARN: metadata-aware undefine $VM_NAME failed: $err" >&2
    return 1
}

# reap_domain [force] — stop and undefine the domain. Graceful (default):
# ACPI power button; after SHUTDOWN_GRACE_SECS one bounded qga agent
# shutdown; then `virsh destroy`. force=1 (a signal teardown, or
# SHUTDOWN_METHOD=force) goes straight to destroy. The undefine retry is
# load-bearing: destroy/shutdown can return before libvirt finishes, and a
# lone undefine races it into a stopped-but-DEFINED leak. `domstate` output
# has its whitespace stripped, so "shut off" compares as "shutoff". Under
# nested virt domstate can claim "shut off" for a live qemu, making destroy
# a no-op — there is no pid-signalling fallback: the confirmed-dead gate in
# cleanup() then keeps the overlay and the next launch adopts the guest.
reap_domain() {
    local force="${1:-0}" method="$SHUTDOWN_METHOD" waited=0 st
    [ "$force" = "1" ] && method=force
    if [ "$method" = "graceful" ]; then
        echo "[tier5] graceful shutdown (ACPI) $VM_NAME (grace=${SHUTDOWN_GRACE_SECS}s)" >&2
        virsh shutdown --mode acpi "$VM_NAME" >/dev/null 2>&1 || true
        while [ "$waited" -lt "$SHUTDOWN_GRACE_SECS" ]; do
            st=$(virsh domstate "$VM_NAME" 2>/dev/null | tr -d '[:space:]')
            case "$st" in shutoff|crashed|"") break ;; esac
            sleep 1; waited=$((waited + 1))
        done
        st=$(virsh domstate "$VM_NAME" 2>/dev/null | tr -d '[:space:]')
        case "$st" in
            shutoff|crashed|"") : ;;
            *)
                echo "[tier5] ACPI grace expired; trying qga agent shutdown $VM_NAME" >&2
                virsh shutdown --mode agent "$VM_NAME" >/dev/null 2>&1 || true
                waited=0
                while [ "$waited" -lt "$SHUTDOWN_AGENT_SECS" ]; do
                    st=$(virsh domstate "$VM_NAME" 2>/dev/null | tr -d '[:space:]')
                    case "$st" in shutoff|crashed|"") break ;; esac
                    sleep 1; waited=$((waited + 1))
                done
                ;;
        esac
    fi
    virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        undefine_domain || true
        domain_defined_state; _ds=$?
        [ "$_ds" -eq 1 ] && return 0
        sleep 0.5
    done
    echo "[tier5] ERROR: domain $VM_NAME still defined or state unverifiable" >&2
    virsh dominfo "$VM_NAME" >&2 || true
    return 1
}

# domain_appid — the appId this domain was bound to at define time (the
# POLICY_KEY baked into its <metadata>). Empty when the domain is
# undefined, undumpable, or unbound: anything but an exact match is FOREIGN.
domain_appid() {
    virsh dumpxml "$VM_NAME" 2>/dev/null \
        | tr -d '\n' \
        | sed -n 's|.*<qdistro:appId[^>]*>\([^<]*\)</qdistro:appId>.*|\1|p'
}

# --- teardown ownership -------------------------------------------------
# flock(2) protocol, all files under $HOLD_DIR (admin's runtime dir):
#   HOLD_FD   hold-$VM    SHARED for a wrapper's whole session — from
#                         before its adopt/create decision to its exit. Every
#                         exclusive actor (teardown claim, orphan reaper,
#                         cleanup tool) conflicts with it.
#   CLAIM_FD  hold-$VM    a second open of the same file, for EXCLUSIVE claims.
#   CREATE_FD create-$VM  EXCLUSIVE around the dead-guest re-check + create, so
#                         racing creators serialize; the loser re-checks and
#                         adopts.
#   LIVE_FD   live-$VM    SHARED once a session is ESTABLISHED (adopted or
#                         created a guest bound to its app) — the positive
#                         adoption signal. Contention on the hold alone may be
#                         a wrapper still deciding, one that may yet refuse.
# Locks are fd-bound, so a crashed wrapper releases everything; the files
# are never unlinked (unlink+recreate would split the lock namespace).
# REAP: unset = undecided, 1 = this wrapper owns teardown, 0 = leave the VM.
REAP=
JOINED=0

HOLD_FD="" CLAIM_FD="" CREATE_FD="" LIVE_FD=""

release_hold() {
    [ -n "$LIVE_FD" ] && flock -u "$LIVE_FD" 2>/dev/null
    [ -n "$HOLD_FD" ] && flock -u "$HOLD_FD" 2>/dev/null
    return 0
}

# session_established — rc 0 when some OTHER wrapper holds an established
# session on the domain. Our own LIVE_FD lock must already be released.
session_established() {
    if flock -n -x "$LIVE_FD" 2>/dev/null; then
        flock -u "$LIVE_FD"
        return 1
    fi
    return 0
}

# decide_teardown — claim teardown, or hand it to an established session.
# Winning the exclusive claim means no wrapper holds the domain, so the
# winner sees the final state and may destroy it. Losing it is NOT proof
# of adoption: the holder may be a relaunch that is still deciding and
# will refuse (a transient two-pid qemu scan, a binding mismatch). Keep
# responsibility until either the claim wins or an established session
# exists — every decision's own waits are bounded, so this terminates.
decide_teardown() {
    [ -n "$REAP" ] && return 0
    release_hold
    local tries=0
    while :; do
        if flock -w 2 -x "$CLAIM_FD" 2>/dev/null; then
            REAP=1; return 0
        fi
        if session_established; then
            echo "[tier5] $VM_NAME adopted by another session; leaving it running" >&2
            REAP=0; return 0
        fi
        [ $((tries % 15)) -eq 0 ] \
            && echo "[tier5] waiting for another wrapper's decision on $VM_NAME" >&2
        tries=$((tries + 1))
    done
}

# idle_keep_warm — after the published app exits, release the session hold
# and wait up to IDLE_SHUTDOWN_SECS for a relaunch (same app ⇒ same domain
# name) to ESTABLISH a session on the still-running VM. Only an established
# session ends our responsibility; contention from a wrapper still deciding
# keeps us waiting. The window ends early on a confirmed-dead guest or host
# memory pressure; then decide_teardown settles ownership.
idle_keep_warm() {
    [ "${TIER5_KEEP_DOMAIN:-0}" = "1" ] && { REAP=0; return 0; }
    release_hold
    [ "$IDLE_SHUTDOWN_SECS" -gt 0 ] || return 0
    echo "[tier5] app exited; holding idle $VM_NAME up to ${IDLE_SHUTDOWN_SECS}s for relaunch" >&2
    local waited=0 step avail
    while [ "$waited" -lt "$IDLE_SHUTDOWN_SECS" ]; do
        domain_is_live; _lv=$?
        [ "$_lv" -eq 1 ] && {
            echo "[tier5] guest stopped on its own during idle window" >&2; return 0
        }
        if [ "$LOWMEM_MB" -gt 0 ]; then
            avail=$(host_mem_avail_mb)
            if [ -n "$avail" ] && [ "$avail" -lt "$LOWMEM_MB" ]; then
                echo "[tier5] host low memory (${avail}MB < ${LOWMEM_MB}MB); ending idle window for $VM_NAME" >&2
                return 0
            fi
        fi
        if session_established; then
            echo "[tier5] $VM_NAME adopted by a relaunch; leaving it running" >&2
            REAP=0
            return 0
        fi
        step=1; [ $((IDLE_SHUTDOWN_SECS - waited)) -lt 1 ] && step=$((IDLE_SHUTDOWN_SECS - waited))
        sleep "$step"; waited=$((waited + step))
    done
    echo "[tier5] idle window (${IDLE_SHUTDOWN_SECS}s) elapsed without relaunch" >&2
}

# --- the bridge -----------------------------------------------------------
# Bash marks coproc and {var} descriptors close-on-exec, so no virsh/qemu-img
# child keeps the bridge's stdin open: when this wrapper exits — even by
# SIGKILL — the bridge sees EOF and tears its waypipe client down.
BRIDGE_PID=""
BRIDGE_IN=""
BRIDGE_OUT=""
start_bridge() {
    local mode_arg=$1 name=$2 line=""
    coproc TIER5_BRIDGE_PROC {
        exec pkexec "$TIER5_BRIDGE" "$mode_arg" "$name" \
            --port "$PORT" --token "$LAUNCH_TOKEN" 2>>"$CLIENT_LOG"
    }
    BRIDGE_PID=$TIER5_BRIDGE_PROC_PID
    exec {BRIDGE_OUT}<&"${TIER5_BRIDGE_PROC[0]}" {BRIDGE_IN}>&"${TIER5_BRIDGE_PROC[1]}"
    # Our dups are the only ends left, so closing BRIDGE_IN is a real EOF.
    eval "exec ${TIER5_BRIDGE_PROC[0]}<&- ${TIER5_BRIDGE_PROC[1]}>&-"
    # The wait covers an authentication prompt (auth_admin_keep).
    if ! read -r -t 300 -u "$BRIDGE_OUT" line \
            || ! [[ "$line" =~ ^READY\ [0-9]+$ ]]; then
        stop_bridge
        echo "[tier5] FAIL: tier-5 bridge did not start the waypipe client" >&2
        echo "        (authentication cancelled/denied or launch refused)" >&2
        sed 's/^/[tier5]   /' "$CLIENT_LOG" >&2 2>/dev/null || true
        exit 9
    fi
    echo "[tier5] bridge ready: waypipe client pid=${line#READY } silo=vm-$name" >&2
}

# bridge_alive — rc 0 while the bridge (and so the waypipe client) runs.
# It writes nothing after READY, so EOF on its stdout means it exited.
bridge_alive() {
    [ -n "$BRIDGE_OUT" ] || return 1
    local _x
    read -r -t 0.05 -u "$BRIDGE_OUT" _x
    [ $? -gt 128 ]
}

stop_bridge() {
    [ -n "$BRIDGE_PID" ] || return 0
    [ -n "$BRIDGE_IN" ] && exec {BRIDGE_IN}>&-
    BRIDGE_IN=""
    wait "$BRIDGE_PID" 2>/dev/null
    local rc=$?
    BRIDGE_PID=""
    [ -n "$BRIDGE_OUT" ] && exec {BRIDGE_OUT}<&-
    BRIDGE_OUT=""
    return "$rc"
}

SERVER_PID=
CLEANUP_STARTED=0
cleanup() {
    # force=1 (signal teardown) skips graceful and ALWAYS runs, even if a
    # graceful EXIT-path cleanup was already mid-flight.
    local force="${1:-0}"
    if [ "$CLEANUP_STARTED" = "1" ] && [ "$force" != "1" ]; then return 0; fi
    CLEANUP_STARTED=1
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
    stop_bridge || true
    local domain_reaped=1
    if [ "$MODE" = "vm" ] && [ "$JOINED" = "1" ] \
            && [ "${TIER5_KEEP_DOMAIN:-0}" != "1" ]; then
        decide_teardown
        if [ "$REAP" = "1" ]; then
            # The claim proves no wrapper holds the domain NOW — not that
            # it is still OURS: the guest may have died and the name been
            # re-bound by another app. A defined domain must carry our
            # binding; an unverifiable state keeps everything.
            domain_defined_state; _ds=$?
            if [ "$_ds" -eq 0 ] && [ "$(domain_appid)" != "$POLICY_KEY" ]; then
                echo "[tier5] $VM_NAME is bound to another app — leaving domain and overlay alone" >&2
                domain_reaped=0
            elif [ "$_ds" -eq 2 ]; then
                echo "[tier5] cannot verify $VM_NAME state — keeping domain and overlay" >&2
                domain_reaped=0
            elif ! reap_domain "$force"; then
                domain_reaped=0
            else
                # The unlink needs its own proof of death: a destroy that
                # raced or no-op'd can leave a qemu alive after the
                # definition is gone. Anything but confirmed-dead keeps
                # the disk for the next spawn to adopt or reap.
                domain_is_live; _lv=$?
                if [ "$_lv" -ne 1 ]; then
                    domain_reaped=0
                    echo "[tier5] WARN: keeping $DISK — $VM_NAME live or unverifiable" >&2
                elif [ -e "$DISK" ] || [ -L "$DISK" ]; then
                    _read_owner_record "$DISK.appid"
                    if [ -f "$DISK" ] && [ ! -L "$DISK" ] \
                            && [ "$_sapp" = "$POLICY_KEY" ] \
                            && [ "$_sident" = "$(stat -c '%d %i' "$DISK" 2>/dev/null)" ]; then
                        rm -f -- "$DISK" "$DISK.appid"
                    else
                        domain_reaped=0
                        echo "[tier5] WARN: keeping $DISK — its sidecar does not bind it to $POLICY_KEY" >&2
                    fi
                elif [ -e "$DISK.appid" ]; then
                    _read_owner_record "$DISK.appid"
                    [ "$_sapp" = "$POLICY_KEY" ] && rm -f -- "$DISK.appid"
                fi
            fi
        fi
    fi
    if [ "$domain_reaped" != "1" ]; then
        echo "[tier5] ERROR: retaining overlay ${DISK:-} — ${VM_NAME:-} still defined or unverifiably live" >&2
        return 1
    fi
    return 0
}
cleanup_on_exit() {
    local rc=$?
    trap - EXIT
    cleanup 0 || { [ "$rc" -ne 0 ] || rc=5; }
    exit "$rc"
}
# Normal exit -> graceful teardown; a successful app must not hide an
# incomplete reap. INT/TERM -> force (fast) teardown.
trap cleanup_on_exit EXIT
trap 'trap - EXIT INT TERM; cleanup 1; exit 130' INT
trap 'trap - EXIT INT TERM; cleanup 1; exit 143' TERM

# --- 1. host-side waypipe client, via the root bridge -----------------
if [ "$MODE" = "loopback" ]; then
    start_bridge --loopback "$SILO_TAG"
else
    start_bridge --vm "$VM_NAME"
fi
for _ in $(seq 1 50); do
    grep -q 'Starting client main process' "$CLIENT_LOG" 2>/dev/null && break
    sleep 0.2
done
echo "[tier5] vsock listener ready cid=$([ "$MODE" = loopback ] && echo 1 || echo 2) port=$PORT" >&2

# --- 2. silo-side waypipe server (loopback) ---------------------------
if [ "$MODE" = "loopback" ]; then
    SERVER_OPTS=(-s "1:$PORT" --vsock -o --no-gpu)
    [ "$DEBUG" = "1" ] && SERVER_OPTS+=(--debug)

    echo "[tier5] $SILO_TAG (loopback) → vsock:1:$PORT → wayland-1" >&2
    echo "[tier5] app: $*" >&2
    echo "[tier5] logs: $CLIENT_LOG, $SERVER_LOG" >&2

    waypipe "${SERVER_OPTS[@]}" server -- "$@" 2>"$SERVER_LOG" &
    SERVER_PID=$!
    wait "$SERVER_PID" 2>/dev/null
    EXIT=$?
    SERVER_PID=
    echo "[tier5] silo app exited rc=$EXIT" >&2
    exit "$EXIT"
fi

# --- 2'. --vm mode --------------------------------------------------
# create_domain — under the create lock with the session hold kept:
# ensure our overlay exists, define the domain from a fresh render, start it.
create_domain() {
    domain_defined_state; _ds=$?
    case "$_ds" in
        0)  if [ "$(domain_appid)" != "$POLICY_KEY" ]; then
                echo "[tier5] FAIL: existing domain $VM_NAME is bound to" >&2
                echo "        another app — refusing to start it" >&2
                REAP=0; exit 3
            fi ;;
        1)  ;;
        *)  echo "[tier5] FAIL: cannot determine state of $VM_NAME — refusing" >&2
            REAP=0; exit 3 ;;
    esac

    # An existing overlay is reused only when its sidecar binds it — this
    # inode — to our appId; anything else is preserved and refused.
    if [ -e "$DISK" ] || [ -L "$DISK" ]; then
        _read_owner_record "$DISK.appid"
        if [ ! -f "$DISK" ] || [ -L "$DISK" ] \
                || [ "$_sapp" != "$POLICY_KEY" ] \
                || [ "$_sident" != "$(stat -c '%d %i' "$DISK" 2>/dev/null)" ]; then
            echo "[tier5] FAIL: overlay $DISK is not bound to '$POLICY_KEY'" >&2
            echo "        by its sidecar — refusing to boot it" >&2
            REAP=0; exit 3
        fi
    else
        # The sidecar is written before the disk is linked into place, so a
        # crash between the two leaves OUR record without a disk — residue
        # this create (holding the create lock) may replace. A foreign
        # record is refused.
        if [ -e "$DISK.appid" ] || [ -L "$DISK.appid" ]; then
            _read_owner_record "$DISK.appid"
            if [ "$_sapp" != "$POLICY_KEY" ]; then
                echo "[tier5] FAIL: $DISK.appid names '${_sapp:-<unreadable>}'" >&2
                echo "        — refusing to replace another app's record" >&2
                REAP=0; exit 3
            fi
            rm -f -- "$DISK.appid"
        fi
        rm -f -- "$DISK".partial.*
        local tmp ident
        if ! tmp=$(mktemp "$DISK.partial.XXXXXX") \
                || ! qemu-img create -f qcow2 -F qcow2 -b "$DISK_BASE" "$tmp" >/dev/null 2>&1 \
                || ! ident=$(stat -c '%d %i' "$tmp") \
                || ! printf '%s %s\n' "$POLICY_KEY" "$ident" >"$DISK.appid" \
                || ! ln -T "$tmp" "$DISK"; then
            rm -f -- "${tmp:-}" "$DISK.appid"
            echo "[tier5] FAIL: cannot create overlay $DISK on $DISK_BASE" >&2
            exit 4
        fi
        rm -f -- "$tmp"
    fi
    if [ "$(overlay_backing "$DISK")" != "$DISK_BASE" ]; then
        echo "[tier5] FAIL: $DISK does not back onto $DISK_BASE — refusing to boot it" >&2
        REAP=0; exit 3
    fi

    # Define unconditionally so the started domain always runs OUR render
    # (disk, CID, binding), even over a leftover stopped definition.
    _create_domain_define

    # Start if not live. The qemu oracle decides: a false "shut off" must
    # not push `virsh start` at a live domain, and ambiguity fails closed.
    domain_is_live; _lv=$?
    if [ "$_lv" -eq 1 ]; then
        if ! virsh start "$VM_NAME" >/dev/null; then
            echo "[tier5] FAIL: virsh start failed" >&2
            exit 6
        fi
    elif [ "$_lv" -eq 2 ]; then
        echo "[tier5] FAIL: cannot determine liveness of $VM_NAME before start" >&2
        REAP=0; exit 3
    fi
}

_create_domain_define() {
    local script_dir tmpl mem_kib new_mac tmp_xml nic_esc serial_xml serial_esc
    script_dir="$(cd "$(dirname "$0")" && pwd)"
    tmpl="${TIER5_DOMAIN_TEMPLATE:-}"
    if [ -z "$tmpl" ]; then
        if   [ -f "$script_dir/domain-template.xml" ]; then
            tmpl="$script_dir/domain-template.xml"
        elif [ -f /usr/share/qdistro/tier5/domain-template.xml ]; then
            tmpl=/usr/share/qdistro/tier5/domain-template.xml
        else
            echo "[tier5] FAIL: domain template not found" >&2
            exit 4
        fi
    fi
    mem_kib="${TIER5_MEM_KIB:-2097152}"
    new_mac="52:54:00:$(printf '%02x:%02x:%02x' \
        $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))"
    tmp_xml="$(domain_xml_tmpfile qdistro-tier5 "" "$RUNTIME_DIR")"
    nic_esc=$(printf '%s' "$NIC_XML" | sed 's|[\\/&]|\\&|g')
    # TIER5_SERIAL_LOG: file-backed serial captures the boot console
    # headlessly (virsh console needs a TTY); default is a pty serial.
    if [ -n "${TIER5_SERIAL_LOG:-}" ]; then
        : >>"$TIER5_SERIAL_LOG" 2>/dev/null || true
        serial_xml="<serial type='file'><source path='$TIER5_SERIAL_LOG'/><target type='isa-serial' port='0'/></serial>"
    else
        serial_xml="<serial type='pty'><target type='isa-serial' port='0'/></serial>"
    fi
    serial_esc=$(printf '%s' "$serial_xml" | sed 's|[\\/&]|\\&|g')
    sed \
        -e "s|__NIC_XML__|$nic_esc|g" \
        -e "s|__SERIAL_XML__|$serial_esc|g" \
        -e "s|__VM_NAME__|$VM_NAME|g" \
        -e "s|__APP_ID__|$POLICY_KEY|g" \
        -e "s|__MAC__|$new_mac|g" \
        -e "s|__MEM_KIB__|$mem_kib|g" \
        -e "s|__CID__|$CID|g" \
        -e "s|__DISK_PATH__|$DISK|g" \
        "$tmpl" >"$tmp_xml"
    if grep -q '__[A-Z_]*__' "$tmp_xml"; then
        echo "[tier5] FAIL: unsubstituted markers in domain XML:" >&2
        grep -o '__[A-Z_]*__' "$tmp_xml" | sort -u >&2
        rm -f -- "$tmp_xml"
        exit 4
    fi
    if ! virsh define "$tmp_xml" >/dev/null; then
        echo "[tier5] FAIL: virsh define failed" >&2
        rm -f -- "$tmp_xml"
        exit 5
    fi
    rm -f -- "$tmp_xml"
}

# Early FOREIGN check, BEFORE joining the lock session: a live domain bound
# to another appId is refused here, so a mismatched spawn never takes the hold
# and can never delay the true owner's teardown claim. An unverifiable scan
# falls through to the authoritative checks under the hold.
domain_is_live; _early_lv=$?
if [ "$_early_lv" -eq 0 ]; then
    _early_bound=$(domain_appid)
    if [ "$_early_bound" != "$POLICY_KEY" ]; then
        echo "[tier5] FAIL: domain $VM_NAME is bound to app" >&2
        echo "        '${_early_bound:-<unbound>}', not '$POLICY_KEY'" >&2
        echo "        — refusing cross-app adoption" >&2
        REAP=0; exit 3
    fi
fi

HOLD_FILE="$HOLD_DIR/hold-$VM_NAME"
# `>>` creates without ever replacing an existing (possibly locked) inode.
( umask 077; : >>"$HOLD_FILE"; : >>"$HOLD_DIR/create-$VM_NAME"; : >>"$HOLD_DIR/live-$VM_NAME" )
exec {HOLD_FD}>>"$HOLD_FILE" {CLAIM_FD}>>"$HOLD_FILE" \
    {LIVE_FD}>>"$HOLD_DIR/live-$VM_NAME"
if ! flock -w "$HOLD_WAIT_SECS" -s "$HOLD_FD"; then
    echo "[tier5] FAIL: timed out waiting ${HOLD_WAIT_SECS}s for the $VM_NAME session hold" >&2
    REAP=0; exit 3
fi
JOINED=1

# refuse_foreign_live — a running domain is ours only when its recorded
# appId equals our POLICY_KEY exactly (the name alone is not identity: a
# guest outlives the catalogue it came from).
refuse_foreign_live() {
    local bound
    bound=$(domain_appid)
    if [ "$bound" != "$POLICY_KEY" ]; then
        echo "[tier5] FAIL: domain $VM_NAME is bound to app '${bound:-<unbound>}'," >&2
        echo "        not '$POLICY_KEY' — refusing cross-app adoption" >&2
        REAP=0; exit 3
    fi
}

ADOPTED=0
domain_is_live; _lv=$?
if [ "$_lv" -eq 0 ]; then
    refuse_foreign_live
    ADOPTED=1
elif [ "$_lv" -eq 2 ]; then
    echo "[tier5] FAIL: cannot determine liveness of $VM_NAME — refusing" >&2
    REAP=0; exit 3
else
    # Creators serialize on the create lock while KEEPING the session hold:
    # waiting for an exclusive session hold would wait out the winner's
    # whole session instead of adopting its guest.
    exec {CREATE_FD}>>"$HOLD_DIR/create-$VM_NAME"
    if ! flock -w "$DECISION_WAIT_SECS" -x "$CREATE_FD"; then
        echo "[tier5] FAIL: timed out waiting for the $VM_NAME create lock" >&2
        REAP=0; exit 3
    fi
    domain_is_live; _lv=$?
    if [ "$_lv" -eq 0 ]; then
        refuse_foreign_live
        ADOPTED=1
    elif [ "$_lv" -eq 2 ]; then
        echo "[tier5] FAIL: cannot determine liveness of $VM_NAME — refusing" >&2
        REAP=0; exit 3
    else
        # A query outage must not read as "no domain": create_domain would
        # then define over (and start) a stopped foreign guest.
        domain_defined_state; _ds=$?
        if [ "$_ds" -eq 2 ]; then
            echo "[tier5] FAIL: cannot determine whether $VM_NAME is defined — refusing" >&2
            REAP=0; exit 3
        fi
        if [ "$_ds" -eq 0 ] && [ "$(domain_appid)" != "$POLICY_KEY" ]; then
            # Confirmed death proves no live guest is killed — it does not
            # make another app's definition or disk ours.
            _fb=$(domain_appid)
            echo "[tier5] FAIL: domain $VM_NAME is bound to app" >&2
            echo "        '${_fb:-<unbound>}', not '$POLICY_KEY' —" >&2
            echo "        refusing to reclaim another app's guest" >&2
            REAP=0; exit 3
        fi
        create_domain
    fi
fi
# Established: from here this session is the positive adoption signal an
# exiting wrapper waits for (see decide_teardown / idle_keep_warm).
if ! flock -w "$DECISION_WAIT_SECS" -s "$LIVE_FD"; then
    echo "[tier5] FAIL: cannot mark the $VM_NAME session established" >&2
    exit 3
fi
[ -n "$CREATE_FD" ] && exec {CREATE_FD}>&-
if [ "$ADOPTED" = "1" ]; then
    echo "[tier5] adopted running domain $VM_NAME (per-app guest shared across relaunches)" >&2
else
    echo "[tier5] domain $VM_NAME running (cid=$CID)" >&2
fi

# Wait for qga. qd_deadline_pending clamps a host suspend/resume jump: the
# nested guest gets no CPU while its host sleeps, so that interval must not
# consume its boot budget (same contract as scripts/vm/vm-exec).
QGA_OK=0
if ! qd_deadline_start "$QGA_TIMEOUT_SECS" "[tier5] WARN: qga wait"; then
    echo "[tier5] FAIL: cannot initialize qga deadline clock" >&2
    exit 7
fi
while qd_deadline_pending; do
    if virsh qemu-agent-command --timeout 5 "$VM_NAME" \
        '{"execute":"guest-ping"}' >/dev/null 2>&1; then
        QGA_OK=1; break
    fi
    sleep 1
done
if [ "$QGA_OK" != "1" ]; then
    echo "[tier5] FAIL: qemu-guest-agent never responded after ${QGA_TIMEOUT_SECS}s" >&2
    exit 7
fi
echo "[tier5] qga ready" >&2

# guest-exec arguments are json.dumps-encoded byte for byte (control
# characters included). The first arg is the port for the publisher.
json_encode_stdin() {
    python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))'
}
QGA_ARGS_JSON="$(printf '%s' "$PORT" | json_encode_stdin)"
for a in "$@"; do
    QGA_ARGS_JSON="$QGA_ARGS_JSON,$(printf '%s' "$a" | json_encode_stdin)"
done

QGA_REQ="{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/usr/local/bin/qdistro-tier5-publisher.sh\",\"arg\":[$QGA_ARGS_JSON],\"capture-output\":true}}"

QGA_REPLY=$(virsh qemu-agent-command "$VM_NAME" "$QGA_REQ" 2>>"$SERVER_LOG" || true)
if [ -z "$QGA_REPLY" ] || ! echo "$QGA_REPLY" | grep -q '"pid"'; then
    echo "[tier5] FAIL: guest-exec didn't return a pid" >&2
    echo "[tier5] reply: $QGA_REPLY" >&2
    cat -- "$SERVER_LOG" >&2 || true
    exit 8
fi
GUEST_PID=$(echo "$QGA_REPLY" | grep -oE '"pid"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' | head -1)
echo "[tier5] guest publisher pid=$GUEST_PID running" >&2
echo "[tier5] $SILO_TAG (vm) → vsock:$CID(guest) → 2:$PORT → wayland-1" >&2
echo "[tier5] app: $*" >&2
echo "[tier5] lifecycle[$POLICY_KEY]: method=$SHUTDOWN_METHOD grace=${SHUTDOWN_GRACE_SECS}s idle=${IDLE_SHUTDOWN_SECS}s lowmem=${LOWMEM_MB}MB" >&2

# Session watch: ends when the guest is confirmed dead, the published app
# exits, or the bridge's waypipe client is gone (no window can reach the
# compositor any more). Liveness comes only from the qemu oracle; ambiguity
# keeps watching. `--timeout` is load-bearing: qemu-agent-command blocks by
# default, and a dying guest's unresponsive qga would hang the loop.
exec_gone=0
while :; do
    domain_is_live; _lv=$?
    [ "$_lv" -eq 1 ] && break
    if ! bridge_alive; then
        echo "[tier5] waypipe client bridge exited; session done for $VM_NAME" >&2
        idle_keep_warm
        break
    fi
    es=$(virsh qemu-agent-command --timeout 5 "$VM_NAME" \
         "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$GUEST_PID}}" 2>&1)
    if printf '%s' "$es" | grep -qE '"exited"[[:space:]]*:[[:space:]]*true'; then
        echo "[tier5] published app (pid=$GUEST_PID) exited; session done for $VM_NAME" >&2
        idle_keep_warm
        break
    elif printf '%s' "$es" | grep -qiE 'PID [0-9]+ does not exist|does not exist'; then
        # qga reaps a finished guest-exec entry on its first terminal status
        # read; later reads say "PID N does not exist". Two in a row means
        # the app exited; a generic timeout/outage resets the count.
        exec_gone=$((exec_gone + 1))
        if [ "$exec_gone" -ge 2 ]; then
            echo "[tier5] published app (pid=$GUEST_PID) reaped by qga (no longer tracked); session done for $VM_NAME" >&2
            idle_keep_warm
            break
        fi
    else
        exec_gone=0
    fi
    sleep 2
done
echo "[tier5] $VM_NAME wait loop ended; last-holder teardown claim decides teardown" >&2
exit 0
