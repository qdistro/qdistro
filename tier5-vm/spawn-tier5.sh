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
#   TIER5_MEM_KIB        (--vm only) guest RAM in KiB (default 2097152 = 2 GiB).
#   TIER5_SERIAL_LOG     (--vm only) path for a file-backed guest serial
#                        console. Default unset = pty serial (interactive
#                        `virsh console`). When set, the guest's boot
#                        console is appended to this file so a headless
#                        failure (kernel panic, mid-boot poweroff,
#                        firstboot stall) is diagnosable offline without a
#                        controlling TTY. The file is created/owned by the
#                        admin user (the qemu:///session domain runs as
#                        admin and must be able to open it for writing).
#   TIER5_QGA_TIMEOUT_SECS
#                        (--vm only) cold-boot deadline for qemu-guest-agent
#                        guest-ping (30..600, default 180). Nested CI guests can
#                        take longer than 90 seconds under parallel VM load.
#   TIER5_DOMAIN_TEMPLATE
#                        (--vm only) override domain XML template path.
#   TIER5_KEEP_DOMAIN=1  (--vm only) skip destroy+undefine on exit
#                        (useful for debug; normally one-VM-per-spawn).
#                        Bypasses ALL lifecycle policy below (the explicit
#                        "never auto-shutdown" switch).
#
#   Lifecycle policy (--vm only) is PER-APP. Precedence per value:
#     env var > [app:<policy-key>] section > global section > built-in default.
#   The config file ($ADMIN_HOME/.config/qdistro/tier5-lifecycle.conf) is written
#   by qdshell's "Sandboxed VM apps" settings panel and parsed (never sourced)
#   with literal section matching + numeric clamps; see conf_get/resolve_policy.
#   --policy-key KEY     stable per-app key for the lookup (qdshell passes its
#                        catalogue appId, e.g. "tier5/firefox"). Defaults to the
#                        app basename. Parsed before `--`; never reaches guest
#                        argv. TIER5_POLICY_KEY is the env equivalent (debug).
#   TIER5_SHUTDOWN_METHOD  graceful (default) | force. graceful =
#                        `virsh shutdown --mode acpi` (ACPI power button), qga
#                        agent shutdown after the grace, then `virsh destroy` as
#                        a last resort. force = immediate `virsh destroy`. A
#                        SIGTERM/SIGINT teardown is ALWAYS force (fast) regardless
#                        of this setting.
#   TIER5_SHUTDOWN_GRACE_SECS  seconds to wait for a graceful shutdown before
#                        escalating (0..300, default 15).
#   TIER5_IDLE_SHUTDOWN_SECS   after the app exits, hold the idle VM this long
#                        before shutting it down (0..86400, default 0 = reap
#                        immediately). A shutdown DELAY, not cross-spawn reuse.
#   TIER5_LOWMEM_MB      during the idle hold, if host MemAvailable drops below
#                        this many MiB, shut the idle VM down now (0 = disabled,
#                        default 0).
#   TIER5_LIFECYCLE_CONF override the GUI config file path.
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
    sed -n '2,159p' "$0" | sed 's/^# \{0,1\}//'
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
            # Stable per-app lifecycle-policy key (e.g. the qdshell catalogue
            # appId "tier5/firefox"). Parsed BEFORE `--` so it never reaches the
            # guest argv. qdshell passes row.appId here; absent it, the wrapper
            # falls back to the app basename. See the lifecycle-policy block.
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

# qemu_pids_for_domain [name] — print the pid(s) of the qemu process(es)
# backing a domain (default $VM_NAME). This is the ONLY reliable liveness
# oracle here: on a nested-virt host libvirt's domstate lies (reports
# "shut off" while qemu is still ALIVE — the failure mode commit 8ac0a39
# was written for). RETURN CODES are the contract (sol r6):
#   0 — pids printed: a backing qemu EXISTS
#   1 — clean empty scan: confirmed NO backing qemu
#   2 — scan unreliable: runuser/ps/proc lookup failed. NOT "dead" —
#       callers must fail closed, never treat it as proof of absence.
# libvirt launches the session qemu with `-name guest=<domain>,…`, so the
# pattern requires the discrete `-name guest=` token — no false match on
# another qemu carrying the string in an unrelated argument. The ps
# table runs through run_as_admin to see admin-owned session processes;
# candidates are narrowed to REAL qemu by /proc/<pid>/comm (truncated
# to 15 chars, e.g. "qemu-system-x86"), which also discards the
# run_as_admin wrapper chain (runuser/env/ps) carrying the pattern in
# its own argv — and further verified against /proc/<pid>/cmdline:
# the guest=<name>, token must be the argument right after `-name`,
# so a foreign qemu carrying the string inside another argument can
# never be reported (sol r18). A candidate whose comm cannot be READ
# (vanished or permission) is unverifiable — never silently
# discounted as "not qemu" (sol r7).
qemu_pids_for_domain() {
    local name="${1:-$VM_NAME}" p c out="" raw unread=""
    # The scan's own plumbing must work before "empty" can mean "dead":
    # runuser auth/exec failure (rc 1/126/127) is otherwise
    # indistinguishable from a clean no-match scan.
    run_as_admin true >/dev/null 2>&1 || return 2
    # Literal substring match: pgrep -f treats the name as ERE, so a
    # manual name like `t5.a` would also match `t5xa` — over-matching
    # hands the caller FOREIGN qemu pids it may signal (sol r15). awk
    # index() via ENVIRON needs no escaping at all.
    raw=$(run_as_admin ps -eo pid=,args= 2>/dev/null) || return 2
    raw=$(printf '%s\n' "$raw" \
        | T5N="-name guest=$name," \
            awk 'index($0, ENVIRON["T5N"]) {print $1}')
    [ -n "$raw" ] || return 1
    for p in $raw; do
        # An unreadable /proc/<pid>/comm CANNOT be discounted — the pid
        # might be our qemu; only a confirmed-qemu or a fully readable
        # candidate list may produce a verdict (sol r7). A candidate
        # that vanished between the ps scan and the read also lands
        # here, which fails closed — the next scan settles it.
        c=$(cat "/proc/$p/comm" 2>/dev/null) || { unread=1; continue; }
        case "$c" in qemu-system-*|qemu-kvm) ;; *) continue ;; esac
        # And the candidate's OWN argv must carry guest=<name>, as the
        # argument right after `-name` — the ps-line substring alone
        # also matches a foreign qemu carrying it inside another
        # argument (file=/tmp/-name guest=t5-x,backup.qcow2) (sol r18).
        # Parse on NUL — the only delimiter an argv entry cannot
        # contain: newline-folding via tr lets a single crafted arg
        # manufacture the fake "-name\nguest=..." pair (sol r19).
        T5NAME="$name" python3 - "$p" >/dev/null <<'PY'
import os, sys
pid = sys.argv[1]
needle = b"guest=" + os.environ["T5NAME"].encode() + b","
try:
    argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
except OSError:
    sys.exit(2)
sys.exit(0 if any(
    a == b"-name" and argv[i + 1].startswith(needle)
    for i, a in enumerate(argv[:-1])) else 1)
PY
        _avrc=$?
        case $_avrc in 0) out="$out $p" ;; 2) unread=1 ;; esac
    done
    [ -n "$out" ] || { [ -n "$unread" ] && return 2; return 1; }
    printf '%s' "$out"
}

# domain_is_live [name] — is the guest ACTUALLY alive? rc: 0 live,
# 1 confirmed dead, 2 cannot determine. The qemu-pid scan is the ONLY
# authority: domstate can lie in BOTH directions under nested virt —
# "shut off" for a live qemu, and stale "running" for a dead one —
# so it is not consulted here at all (a stale-running belt would wedge
# the session watch on a dead guest, sol r8). No backing qemu process
# IS death — libvirt cannot run a VM without one.
domain_is_live() {
    local name="${1:-$VM_NAME}"
    qemu_pids_for_domain "$name" >/dev/null
    case $? in 0) return 0 ;; 2) return 2 ;; esac
    return 1
}

# domain_defined_state [name] — rc 0 = defined, 1 = CONFIRMED absent,
# 2 = cannot determine. Uses `virsh list --all --name`, not dominfo
# stderr: an enumeration that SUCCEEDS is authoritative (name in the
# list = defined; not in it = confirmed absent), while ANY failure is
# unverifiable. Dominfo absence detection would need error-text parsing
# whose shape drifts across libvirt versions — 12.x prints only
# "failed to get domain 'X'" while older releases print "Domain not
# found", and a loose "not found" substring also matches outage text
# like "libvirt socket not found" (sol r11). MUST stay defined before
# the startup reaper (~line 380), which is the first caller.
domain_defined_state() {
    local name="${1:-$VM_NAME}" list
    if ! list=$(run_as_admin virsh list --all --name 2>/dev/null); then
        return 2
    fi
    printf '%s\n' "$list" | grep -Fxq -- "$name" && return 0
    return 1
}

# manifest_app_state <appId> <base> — rc 0 = the CURRENT non-stale
# catalogue publishes this appId; rc 1 = a VALID catalogue for THIS
# base does not publish it (retired app — its disk is provably
# stale); rc 2 = cannot determine. "Valid" mirrors the scanner's own
# checks: parses, right schema, no "stale" marker (the reconciler
# publishes a READABLE empty stale manifest on scan failure — sol
# r13), well-formed rows, and the recorded base dev/ino/mtime/size
# matching the CURRENT base file — a base swapped between scans
# leaves a non-stale manifest that omits the new base's apps, and
# that must not read as retirement either (sol r14). Must stay
# defined before the startup reaper.
# tier5_derived_name <appId> — port of VMApps._vmNameForApp so a
# recorded binding/sidecar can be checked against the artifact's own
# name: a t5-shaped name is only a SHAPE (a hand-made overlay can
# match it); re-derivation proves the owner record names this very
# guest (sol r17). appIds are ASCII by the --policy-key charset, so
# JS charCodeAt == ord() here.
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

manifest_app_state() {
    python3 - "$VMAPPS_MANIFEST" "$1" "$2" <<'PY'
import json, os, sys
mpath, side, base = sys.argv[1], sys.argv[2], sys.argv[3]


def _row_ok(r):
    # Same strictness as the scanner's _row_ok — a catalogue that
    # fails validation proves nothing about retirement (sol r15).
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
# Identity is sampled by the scanner BEFORE reading base contents and
# pinned to nanosecond mtime, so a mid-scan rewrite cannot repeat the
# tuple. Write ORDER closes the residual hole — a base modified after
# the manifest was published must not authorize retirement either
# (sol r15/r16).
if mst.st_mtime_ns < st.st_mtime_ns:
    sys.exit(2)
apps = doc.get("apps")
# The scanner treats an empty catalogue as a transient state, not a
# verdict — mirror that: empty apps is cannot-determine, not "every
# app retired" (sol r15).
if not isinstance(apps, list) or not apps or not all(map(_row_ok, apps)):
    sys.exit(2)
sys.exit(0 if any(r["appId"] == side for r in apps) else 1)
PY
}

# domain_xml_tmpfile is provided by lib/spawn-common.sh (sourced below); it
# renders into a private 0700 dir under the admin runtime. (A local definition
# here would be shadowed by that source anyway.)

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
    for tool in virsh qemu-img flock; do
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
    # The published app catalogue — the reaper uses it to tell an OWNED
    # orphan (sidecar appId still published) from a retired app's disk.
    VMAPPS_MANIFEST="${VMAPPS_MANIFEST:-/usr/share/qdistro/vmapps/base.json}"
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
    # wrapper is no longer alive AND whose libvirt domain is CONFIRMED
    # ABSENT — a defined-but-stopped domain keeps its overlay: it has
    # an owner record (its bound app restarts it on next launch) that
    # the reaper cannot adjudicate (sol r11). Conservative: only
    # touches qcow2 files in the t5-<stub>-<hash> namespace whose
    # backing file is $DISK_BASE (won't
    # accidentally rm a user's unrelated qcow2 in the same dir).
    if [ "${TIER5_NO_REAP:-0}" != "1" ] && [ -d "$DISK_DIR" ]; then
        for overlay in "$DISK_DIR"/*.qcow2; do
            [ -f "$overlay" ] || continue
            base=$(basename "$overlay" .qcow2)
            # Don't touch our own per-spawn name even before it's created.
            [ "$base" = "$VM_NAME" ] && continue
            # Only names in OUR namespace — t5-<stub>-<64-bit hash> as
            # VMApps._vmNameForApp derives them. Backing-file match
            # alone does not prove tier-5 ownership: a separately
            # created overlay of our base in the same shared dir is
            # not ours to reap (sol r16).
            [[ "$base" =~ ^t5-[A-Za-z0-9_-]{1,43}-[0-9a-f]{16}$ ]] \
                || continue
            # Confirm the overlay is one of ours — EXACT backing-file
            # match from the JSON output, not a substring grep (sol r6).
            _backing=$(run_as_admin qemu-img info --output=json "$overlay" \
                2>/dev/null \
                | sed -n 's/.*"backing-filename": "\([^"]*\)".*/\1/p')
            [ "$_backing" = "$DISK_BASE" ] || continue
            # Live wrapper holding it? Skip.
            if pgrep -af "spawn-tier5\.sh.*--vm[[:space:]]+$base([[:space:]]|$)" \
                    >/dev/null 2>&1; then
                continue
            fi
            # Per-target hold: a wrapper spawning THIS domain right now
            # takes its fd9 SH on hold-$base, so our nonblocking EX
            # either loses (a live or in-flight decision owns it →
            # skip) or wins and serializes every later wrapper behind
            # our reap (sol r7). Creating the file when absent matches
            # the spawn's own `install` — hold files are never unlinked.
            _holddir="${TIER5_HOLD_DIR:-/run/qdistro-tier5}"
            install -d -m 0700 "$_holddir" 2>/dev/null || true
            _holdf="$_holddir/hold-$base"
            [ -f "$_holdf" ] || install -m 0600 /dev/null "$_holdf" 2>/dev/null
            exec {hfd}>>"$_holdf" 2>/dev/null || continue
            flock -n -x "$hfd" 2>/dev/null || { exec {hfd}>&-; continue; }
            # Domain still alive? Only a CONFIRMED-dead oracle verdict
            # permits reaping — live OR unverifiable skips, so a false
            # "shut off" can't cost a live guest its disk (sol r6).
            domain_is_live "$base"; _lv=$?
            if [ "$_lv" -ne 1 ]; then exec {hfd}>&-; continue; fi
            # Only a CONFIRMED-ABSENT domain makes this a true file
            # orphan. The reaper never undefines: a defined-but-dead
            # domain has an owner record — its bound app restarts it on
            # next launch, and deleting the disk under it is not ours
            # to do (sol r11). Unverifiable queries skip the same way.
            domain_defined_state "$base"; _ds=$?
            if [ "$_ds" -ne 1 ]; then exec {hfd}>&-; continue; fi
            # Owner record is REQUIRED and must bind THIS inode: the
            # t5-shaped name is only a shape, and the "<appId> <dev>
            # <ino>" sidecar must name the file it was stamped for —
            # a record copied beside a different overlay is not proof
            # (sol r17/r19). The appId must ALSO re-derive to this
            # name and be manifest-RETIRED — an owned orphan's app
            # relaunches under its own name. A missing/unreadable
            # sidecar, an inode mismatch, a foreign derivation, or an
            # unverifiable verdict all preserve (sol r12–r19).
            _sapp="" _sident=""
            [ -f "$overlay" ] && [ ! -L "$overlay" ] \
                && [ -f "$overlay.appid" ] && [ ! -L "$overlay.appid" ] \
                && read -r _sapp _sident 2>/dev/null \
                    < "$overlay.appid" || true
            [ -n "$_sapp" ] || { exec {hfd}>&-; continue; }
            [ "$_sident" = "$(stat -c '%d %i' "$overlay" 2>/dev/null)" ] \
                || { exec {hfd}>&-; continue; }
            [ "$(tier5_derived_name "$_sapp")" = "$base" ] \
                || { exec {hfd}>&-; continue; }
            manifest_app_state "$_sapp" "$DISK_BASE"; _ms=$?
            [ "$_ms" -eq 1 ] || { exec {hfd}>&-; continue; }
            # Settle + re-verify: an out-of-band `virsh create` could
            # still be exec'ing a qemu under this name. With no
            # definition AND the per-target hold held, no cooperating
            # start is possible, so a clean scan here is the last proof
            # the overlay needs (sol r7/r11).
            sleep 0.3
            domain_is_live "$base"; _lv=$?
            if [ "$_lv" -eq 0 ]; then
                echo "[tier5] orphan $base came alive during reap — keeping overlay" >&2
            fi
            if [ "$_lv" -ne 1 ]; then exec {hfd}>&-; continue; fi
            # The settle window can also invalidate the OTHER verdicts —
            # re-check defined-state and the owner record now: an
            # out-of-band define, a newly dropped sidecar, or a fresh
            # catalogue publication in the interval must all preserve
            # the overlay (sol r16).
            domain_defined_state "$base"; _ds=$?
            if [ "$_ds" -ne 1 ]; then exec {hfd}>&-; continue; fi
            # Same owner-record verdict, re-evaluated: the sidecar is
            # re-read (it may have appeared, changed, or vanished
            # during the settle) and must still bind this inode,
            # re-derive to $base, and still be manifest-retired
            # (sol r16/r17/r19).
            _sapp="" _sident=""
            [ -f "$overlay" ] && [ ! -L "$overlay" ] \
                && [ -f "$overlay.appid" ] && [ ! -L "$overlay.appid" ] \
                && read -r _sapp _sident 2>/dev/null \
                    < "$overlay.appid" || true
            [ -n "$_sapp" ] || { exec {hfd}>&-; continue; }
            [ "$_sident" = "$(stat -c '%d %i' "$overlay" 2>/dev/null)" ] \
                || { exec {hfd}>&-; continue; }
            [ "$(tier5_derived_name "$_sapp")" = "$base" ] \
                || { exec {hfd}>&-; continue; }
            manifest_app_state "$_sapp" "$DISK_BASE"; _ms=$?
            [ "$_ms" -eq 1 ] || { exec {hfd}>&-; continue; }
            rm -f "$overlay" "$overlay.appid" 2>/dev/null || true
            exec {hfd}>&-
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
    echo "[tier5] FAIL: qdistro-secctx-exec not in PATH; refusing untagged Tier-5 launch" >&2
    echo "        set TIER5_USE_SECCTX=0 only for explicit debug runs" >&2
    exit 3
fi

# Log names carry the launch token's short prefix: concurrent sessions of
# the SAME app share $SILO_TAG (the per-app domain), so silo+app alone no
# longer disambiguates two live wrappers' logs.
CLIENT_LOG="$ADMIN_RUNTIME/tier5-${SILO_TAG}-${APP_BASENAME}-${LAUNCH_TOKEN:0:8}-client.log"
SERVER_LOG="$ADMIN_RUNTIME/tier5-${SILO_TAG}-${APP_BASENAME}-${LAUNCH_TOKEN:0:8}-server.log"
mkdir -p "$ADMIN_RUNTIME"
: >"$CLIENT_LOG" >"$SERVER_LOG"
chown "$ADMIN_USER" "$CLIENT_LOG" "$SERVER_LOG" 2>/dev/null || true

# --- tier-5 VM lifecycle policy (graceful shutdown / idle timeout / low-mem) --
# How a finished/closed tier-5 VM is torn down. Policy is PER-APP: each value
# resolves with precedence
#   explicit env var  >  [app:$POLICY_KEY] section  >  global section  >  default
# so a per-app override can change just one field and inherit the rest. The
# config file is written by qdshell's "Sandboxed VM apps" settings panel (admin
# uid) and read here by ROOT. It only controls teardown TIMING + METHOD of a
# disposable VM (never a security boundary), so an admin-owned file is an
# acceptable trust posture — but it is PARSED (never sourced) with literal
# section matching + numeric clamps + a method allowlist, and only honored when
# it is a regular, admin/root-owned, non-symlink, non-group/other-writable file.
#
# POLICY_KEY is the STABLE per-app identity: qdshell passes its catalogue appId
# (e.g. "tier5/firefox") via --policy-key (argv survives pkexec; env does not).
# It is also the appId bound into the domain's <metadata> at define time —
# adoption requires an exact match, so the key IS an isolation boundary.
# An EXPLICIT key that fails the charset is a hard error, NOT a basename
# fallback: silently rebinding would let two distinct invalid appIds
# collapse onto one executable's binding and adopt one guest (sol r4).
# `[[ =~ ]]`, NOT grep — grep anchors line-by-line, so a newline-bearing
# key would slip past ^…$; `[[ =~ ]]` anchors against the whole string.
LIFECYCLE_CONF="${TIER5_LIFECYCLE_CONF:-$ADMIN_HOME/.config/qdistro/tier5-lifecycle.conf}"
POLICY_KEY="${EXPLICIT_POLICY_KEY:-${TIER5_POLICY_KEY:-}}"
if [ -n "$POLICY_KEY" ]; then
    if ! [[ "$POLICY_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$ ]]; then
        echo "[tier5] FAIL: invalid policy key '$POLICY_KEY'" >&2
        exit 3
    fi
else
    # Unkeyed direct/test use: derive from the app basename, sanitized to
    # the same charset so the recorded binding is always well-formed.
    POLICY_KEY=$(printf '%s' "$APP_BASENAME" | tr -c 'A-Za-z0-9._/-' '_')
    case "$POLICY_KEY" in [A-Za-z0-9]*) ;; *) POLICY_KEY="x$POLICY_KEY";; esac
    POLICY_KEY="${POLICY_KEY:0:128}"
fi

_conf_file_ok() {  # honor only a regular, admin/root-owned, sane-perm file
    local f="$LIFECYCLE_CONF" owner
    [ -f "$f" ] || return 1
    [ -L "$f" ] && return 1
    # Reject group- or world-writable files (-perm /022 matches either bit).
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

clamp_int() {  # clamp_int VARNAME DEFAULT MIN MAX — validate/clamp an integer
    local name="$1" def="$2" lo="$3" hi="$4" cur
    cur="${!name}"
    printf '%s' "$cur" | grep -qE '^[0-9]+$' || cur="$def"
    [ "$cur" -lt "$lo" ] && cur="$lo"
    [ "$cur" -gt "$hi" ] && cur="$hi"
    printf -v "$name" '%s' "$cur"
}

SHUTDOWN_METHOD="${TIER5_SHUTDOWN_METHOD:-$(resolve_policy SHUTDOWN_METHOD)}"
case "$SHUTDOWN_METHOD" in graceful|force) ;; *) SHUTDOWN_METHOD=graceful ;; esac
SHUTDOWN_GRACE_SECS="${TIER5_SHUTDOWN_GRACE_SECS:-$(resolve_policy SHUTDOWN_GRACE_SECS)}"
clamp_int SHUTDOWN_GRACE_SECS 15 0 300
IDLE_SHUTDOWN_SECS="${TIER5_IDLE_SHUTDOWN_SECS:-$(resolve_policy IDLE_SHUTDOWN_SECS)}"
clamp_int IDLE_SHUTDOWN_SECS 0 0 86400
LOWMEM_MB="${TIER5_LOWMEM_MB:-$(resolve_policy LOWMEM_MB)}"
clamp_int LOWMEM_MB 0 0 1048576
QGA_TIMEOUT_SECS="${TIER5_QGA_TIMEOUT_SECS:-180}"
clamp_int QGA_TIMEOUT_SECS 180 30 600

host_mem_avail_mb() {  # host MemAvailable in MiB, or "" if unreadable
    local kb
    kb=$(grep -E '^MemAvailable:' /proc/meminfo 2>/dev/null | grep -oE '[0-9]+' | head -1)
    [ -n "$kb" ] || return 0
    printf '%s' $((kb / 1024))
}

# Try the ordinary undefine first. If libvirt reports retained per-domain state
# or metadata, remove only that ephemeral VM state and retry. Do not ask libvirt
# to remove storage: the wrapper owns the overlay and unlinks it only after the
# domain definition is confirmed gone.
undefine_domain() {
    local err
    if err=$(run_as_admin virsh undefine "$VM_NAME" 2>&1); then
        return 0
    fi
    echo "[tier5] WARN: undefine $VM_NAME failed: $err" >&2
    domain_defined_state; local ds=$?
    if [ "$ds" -eq 1 ]; then
        return 0
    fi
    # rc 2 (query outage) falls through to the metadata-aware retry —
    # its own failure then reports honestly instead of faking success.
    [ "$ds" -eq 0 ] || return 1
    if err=$(run_as_admin virsh undefine "$VM_NAME" \
        --managed-save --snapshots-metadata --checkpoints-metadata \
        --nvram --tpm 2>&1); then
        echo "[tier5] undefine $VM_NAME succeeded after removing retained metadata" >&2
        return 0
    fi
    echo "[tier5] WARN: metadata-aware undefine $VM_NAME failed: $err" >&2
    return 1
}

# reap_domain [force] — stop the domain, undefine it, unlink the overlay.
# Graceful (default): ACPI power-button (`virsh shutdown --mode acpi`, the owner-
# requested mechanism). If the guest is still up after SHUTDOWN_GRACE_SECS, try
# ONE bounded qga agent shutdown (qemu-guest-agent is already a hard tier-5
# dependency, so this is the natural fallback when the guest lacks a power-button
# handler), and only then force `virsh destroy`. force=1 (a signal teardown, or
# SHUTDOWN_METHOD=force) skips straight to destroy — keeping the SIGTERM path
# fast (s48's 10s budget). The undefine retry afterwards is load-bearing: virsh
# destroy/shutdown can return before libvirt finishes teardown, so a lone
# undefine races it and leaves a stopped-but-DEFINED domain (a leak, perm-gui/21
# S4). NOTE: `domstate` output has its whitespace stripped, so "shut off"
# compares as "shutoff".
reap_domain() {
    local force="${1:-0}" method="$SHUTDOWN_METHOD" waited=0 st
    [ "$force" = "1" ] && method=force
    if [ "$method" = "graceful" ]; then
        echo "[tier5] graceful shutdown (ACPI) $VM_NAME (grace=${SHUTDOWN_GRACE_SECS}s)" >&2
        run_as_admin virsh shutdown --mode acpi "$VM_NAME" >/dev/null 2>&1 || true
        while [ "$waited" -lt "$SHUTDOWN_GRACE_SECS" ]; do
            st=$(run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | tr -d '[:space:]')
            case "$st" in shutoff|crashed|"") break ;; esac
            sleep 1; waited=$((waited + 1))
        done
        st=$(run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | tr -d '[:space:]')
        case "$st" in
            shutoff|crashed|"") : ;;
            *)
                echo "[tier5] ACPI grace expired; trying qga agent shutdown $VM_NAME" >&2
                run_as_admin virsh shutdown --mode agent "$VM_NAME" >/dev/null 2>&1 || true
                waited=0
                while [ "$waited" -lt 8 ]; do
                    st=$(run_as_admin virsh domstate "$VM_NAME" 2>/dev/null | tr -d '[:space:]')
                    case "$st" in shutoff|crashed|"") break ;; esac
                    sleep 1; waited=$((waited + 1))
                done
                ;;
        esac
    fi
    # Force-destroy if still running (or method=force). Harmless if already off.
    run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    # Nested-KVM safety net: domstate can falsely report "shut off" while qemu is
    # still ALIVE, so `virsh destroy` no-ops and the undefine below then drops only
    # the persistent config — leaving a live TRANSIENT domain that dominfo keeps
    # reporting, so it never undefines (perm-gui/21 S4). If qemu survived destroy,
    # signal it directly. A pure FALLBACK: when destroy already worked there is no
    # qemu pid and we fall straight through to undefine.
    local qpids
    qpids=$(qemu_pids_for_domain)
    if [ -n "$qpids" ]; then
        echo "[tier5] destroy left qemu alive for $VM_NAME (pids:$qpids); killing" >&2
        run_as_admin kill $qpids 2>/dev/null || true
        for _ in 1 2 3 4 5 6; do
            qpids=$(qemu_pids_for_domain)
            [ -n "$qpids" ] || break
            sleep 0.5
        done
        qpids=$(qemu_pids_for_domain)
        [ -n "$qpids" ] && run_as_admin kill -9 $qpids 2>/dev/null || true
    fi
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        undefine_domain || true
        domain_defined_state; _ds=$?
        [ "$_ds" -eq 1 ] && return 0
        # rc 0 (still defined) or rc 2 (cannot verify): retry, then
        # report honestly — either way the caller keeps the overlay.
        sleep 0.5
    done
    echo "[tier5] ERROR: domain $VM_NAME still defined or state unverifiable" >&2
    run_as_admin virsh dominfo "$VM_NAME" >&2 || true
    return 1
}

# domain_appid — the appId this domain was bound to at define time (the
# POLICY_KEY baked into its <metadata>). Empty when the domain is
# undefined, undumpable, or carries no binding (hand-defined or
# pre-binding domains): anything but an exact match is FOREIGN.
domain_appid() {
    run_as_admin virsh dumpxml "$VM_NAME" 2>/dev/null \
        | tr -d '\n' \
        | sed -n 's|.*<qdistro:appId[^>]*>\([^<]*\)</qdistro:appId>.*|\1|p'
}

# --- teardown ownership -------------------------------------------------
# REAP is the teardown decision: unset = undecided, 1 = this wrapper is the
# last lock holder and must destroy+undefine+unlink, 0 = a live session (or
# an in-flight decision) still owns the VM — leave it alone.
REAP=

# release_hold — drop the fd9 session hold so a relaunch's adoption probe
# (or a teardown claim) is no longer blocked by this exiting wrapper.
release_hold() {
    flock -u 9 2>/dev/null || true
}

# decide_teardown — claim or relinquish teardown ownership via a BOUNDED
# EX claim on fd10. Winning means NO wrapper holds the domain: every
# actor either exited or is still waiting for the lock, so the winner
# sees the final state of the world and may destroy it. Losing means
# someone adopted (or is deciding) — leaving the VM is always safe then
# because that holder runs this same decision at its own exit. The wait
# is bounded rather than -n: a transient hold (e.g. a foreign spawn's
# brief SH before its binding refusal) must not starve the last real
# owner's claim and leak a running guest (sol r4). 2s outlasts any
# refuse-path exit by orders of magnitude; a genuine session holds far
# longer, so a losing claim just delays this exit slightly.
decide_teardown() {
    [ -n "$REAP" ] && return 0
    release_hold
    if flock -w 2 -x 10 2>/dev/null; then
        REAP=1
    else
        REAP=0
    fi
}

# idle_keep_warm — after the published app exits, release the session hold
# and wait up to IDLE_SHUTDOWN_SECS for a RELAUNCH to adopt the still-running
# VM (the per-app guest: qdshell derives the domain name from the catalogue
# appId, so a relaunch of the same app names the same domain and attaches
# under its own session hold). Any lock contention during the window means
# an adoption is under way — leave the VM to it. The window still ends
# early on guest self-stop or host memory pressure; at the deadline the
# last holder claims teardown exclusively. A SIGTERM during the wait hits
# the force trap, whose cleanup runs decide_teardown on the same protocol.
idle_keep_warm() {
    [ "${TIER5_KEEP_DOMAIN:-0}" = "1" ] && { REAP=0; return 0; }
    release_hold
    [ "$IDLE_SHUTDOWN_SECS" -gt 0 ] || return 0
    echo "[tier5] app exited; holding idle $VM_NAME up to ${IDLE_SHUTDOWN_SECS}s for relaunch" >&2
    local waited=0 step avail
    while [ "$waited" -lt "$IDLE_SHUTDOWN_SECS" ]; do
        # End the window early only on a CONFIRMED-dead guest — a false
        # "shut off" domstate must not cut the adoption window short and
        # hand a live guest to the teardown claim (sol r6). Live or
        # unverifiable keep watching (adoption probe + low-mem below);
        # the deadline bounds the wait.
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
        # Adoption probe: an exclusive grab on fd10 can only succeed while
        # NO session/decision lock exists. Take and drop it immediately so
        # the adoption window stays open between polls.
        if ! flock -n -x 10 2>/dev/null; then
            echo "[tier5] $VM_NAME adopted by a relaunch; leaving it running" >&2
            REAP=0
            return 0
        fi
        flock -u 10
        step=5; [ $((IDLE_SHUTDOWN_SECS - waited)) -lt 5 ] && step=$((IDLE_SHUTDOWN_SECS - waited))
        sleep "$step"; waited=$((waited + step))
    done
    echo "[tier5] idle window (${IDLE_SHUTDOWN_SECS}s) elapsed without relaunch" >&2
}

CLIENT_PID=
SERVER_PID=
LAUNCHREC_PATH=""
CLEANUP_STARTED=0
cleanup() {
    # force=1 (signal teardown) skips graceful and ALWAYS runs, even if a
    # graceful EXIT-path cleanup was already mid-flight — so a SIGTERM during a
    # graceful shutdown escalates to an immediate destroy instead of stalling.
    local force="${1:-0}"
    if [ "$CLEANUP_STARTED" = "1" ] && [ "$force" != "1" ]; then return 0; fi
    CLEANUP_STARTED=1
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
    [ -n "$CLIENT_PID" ] && kill "$CLIENT_PID" 2>/dev/null || true
    # CLIENT_PID is the backgrounded runuser/secctx-exec PARENT. When we wrap
    # waypipe with qdistro-secctx-exec, the load-bearing waypipe is secctx-exec's
    # exec'd fork CHILD, whose pid secctx-exec published to LAUNCHREC_PATH. Killing
    # only CLIENT_PID can orphan that inner waypipe (perm-gui/s48). secctx-exec now
    # forwards SIGTERM to it, but reap the published pid directly too so teardown is
    # robust even against an older/uncrebuilt secctx-exec binary in the image.
    if [ -n "$LAUNCHREC_PATH" ] && [ -r "$LAUNCHREC_PATH" ]; then
        _inner_pid="$(awk 'NR==1{print $1}' "$LAUNCHREC_PATH" 2>/dev/null)"
        if [ -n "$_inner_pid" ] && printf '%s' "$_inner_pid" | grep -qE '^[0-9]+$'; then
            # Guard against pid reuse: only kill if it is still a waypipe. The
            # inner client may already be gone (C-side SIGTERM-forward above), in
            # which case the pid could be recycled by an unrelated process.
            if grep -qx waypipe "/proc/$_inner_pid/comm" 2>/dev/null; then
                kill "$_inner_pid" 2>/dev/null || true
            fi
        fi
    fi
    local domain_reaped=1
    if [ "$MODE" = "vm" ] && [ "${TIER5_KEEP_DOMAIN:-0}" != "1" ]; then
        # Decide teardown ownership if idle_keep_warm didn't already: only
        # the last lock holder may destroy the shared per-app domain — an
        # earlier wrapper's exit must never reap a VM a relaunch adopted.
        decide_teardown
        if [ "$REAP" = "1" ]; then
            # The claim only proves no wrapper holds the domain NOW —
            # not that it is still OURS. The original guest could have
            # died during the idle window and the name been re-bound to
            # another app; destroying that foreign replacement is
            # exactly the cross-app kill the binding exists to prevent
            # (sol r9). Re-verify before reaping: a DEFINED domain must
            # carry our binding, confirmed-absent has no owner to
            # protect, and an unverifiable state keeps everything.
            domain_defined_state; _ds=$?
            _bound=""
            [ "$_ds" -eq 0 ] && _bound=$(domain_appid)
            if [ "$_ds" -eq 0 ] && [ "$_bound" != "$POLICY_KEY" ]; then
                echo "[tier5] $VM_NAME re-bound to '${_bound:-<unbound>}'" >&2
                echo "        — leaving domain and overlay alone" >&2
                domain_reaped=0
            elif [ "$_ds" -eq 2 ]; then
                echo "[tier5] cannot verify $VM_NAME state —" >&2
                echo "        keeping domain and overlay" >&2
                domain_reaped=0
            else
                # reap_domain is idempotent on absent/stopped domains,
                # so it runs unconditionally under an exclusive claim:
                # any defined leftover (a crashed predecessor) is ours.
                reap_domain "$force" || domain_reaped=0
                if [ "$domain_reaped" = "1" ]; then
                    # The unlink needs its own proof of death: a destroy
                    # that raced or a kill that missed can leave a qemu
                    # alive after the definition is gone. Only a
                    # confirmed-dead verdict lets the disk go; anything
                    # else leaves it for the next spawn's orphan reaper
                    # (sol r7).
                    domain_is_live; _lv=$?
                    if [ "$_lv" -eq 1 ]; then
                        # The disk unlink needs its own owner record in
                        # EVERY case: the domain's binding vouched for
                        # the path, not the inode — a file swapped in
                        # while the domain was stopped is not ours to
                        # delete (sol r21). And with the disk absent, a
                        # sidecar still gets the same check — only OUR
                        # regular-file record goes; a foreign,
                        # symlinked, or unreadable one is an
                        # unrecognized record and is preserved
                        # (sol r22).
                        _sapp="" _sident=""
                        [ -f "$DISK.appid" ] && [ ! -L "$DISK.appid" ] \
                            && read -r _sapp _sident 2>/dev/null \
                                < "$DISK.appid" || true
                        if [ -f "$DISK" ]; then
                            if [ "$_sapp" != "$POLICY_KEY" ] \
                                || [ "$_sident" != "$(stat -c '%d %i' "$DISK" 2>/dev/null)" ]; then
                                domain_reaped=0
                                echo "[tier5] WARN: keeping $DISK — overlay owner unproven" >&2
                            else
                                rm -f "$DISK" "$DISK.appid" 2>/dev/null || true
                            fi
                        elif [ -e "$DISK.appid" ] || [ -L "$DISK.appid" ]; then
                            if [ "$_sapp" = "$POLICY_KEY" ] \
                                && [ -f "$DISK.appid" ] \
                                && [ ! -L "$DISK.appid" ]; then
                                rm -f "$DISK.appid" 2>/dev/null || true
                            else
                                domain_reaped=0
                                echo "[tier5] WARN: keeping $DISK.appid —" >&2
                                echo "        foreign or unreadable record" >&2
                            fi
                        fi
                    else
                        domain_reaped=0
                        echo "[tier5] WARN: keeping $DISK — $VM_NAME live or unverifiable" >&2
                    fi
                fi
            fi
        fi
    fi
    if [ "$domain_reaped" != "1" ]; then
        echo "[tier5] ERROR: retaining overlay $DISK — $VM_NAME still defined or unverifiably live" >&2
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
# Normal exit (incl. the wait loop's app-exit path) -> graceful teardown. A
# successful app must not hide an incomplete domain reap: promote cleanup
# failure to a setup/bridge failure while preserving any earlier nonzero code.
# INT/TERM -> force (fast) teardown; drop the EXIT trap so it can't double-run.
trap cleanup_on_exit EXIT
trap 'trap - EXIT INT TERM; cleanup 1; exit 130' INT
trap 'trap - EXIT INT TERM; cleanup 1; exit 143' TERM

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

# Build the secctx-exec wrap if enabled. When wrapping, also hand secctx-exec a
# launch-record path + token so it publishes the inner waypipe-client pid for the
# permission-lineage register below (mirrors tier-1/2/3). The record is what lets
# enforce mode resolve this tier-5 silo's source pid to its silo instead of
# failing closed; without it tier-5 was the only tier whose spawn never published
# a launch record. The file is written by secctx-exec AS ADMIN (runuser below),
# so it must live in admin's runtime dir; root reads it back to register.
SECCTX_WRAP=()
LAUNCHREC_PATH=""
LAUNCHREC_TOKEN=""
if [ "$USE_SECCTX" = "1" ]; then
    LAUNCHREC_TOKEN="$(gen_launch_token "[tier5] FAIL")"
    LAUNCHREC_FILE_ID="$(gen_launch_token "[tier5] FAIL")"
    LAUNCHREC_PATH="$ADMIN_RUNTIME/qdistro-tier5-launchrec-$LAUNCHREC_FILE_ID.pid"
    rm -f "$LAUNCHREC_PATH" 2>/dev/null || true
    SECCTX_WRAP=(env QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1
        QDISTRO_LAUNCH_RECORD_PATH="$LAUNCHREC_PATH"
        QDISTRO_LAUNCH_RECORD_TOKEN="$LAUNCHREC_TOKEN"
        qdistro-secctx-exec
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

# Permission-lineage: register the inner waypipe-client pid (published by
# secctx-exec into LAUNCHREC_PATH) with the broker, binding (pid,starttime) ->
# silo so a later cross-silo gate resolves this tier-5 source pid to its record
# instead of failing closed. No-op when secctx-exec wasn't wrapped
# (LAUNCHREC_PATH empty, i.e. TIER5_USE_SECCTX=0) or dbus-send is absent.
qd_register_secctx_launch_record \
    "$SILO_TAG" "$SECCTX_ENGINE" "$SECCTX_APPID" "$SECCTX_INSTANCE" "tier5" \
    "$LAUNCHREC_PATH" "$LAUNCHREC_TOKEN" "tier5"

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
# Adopt-or-create under the per-VM hold lock. We already carry the fd9
# SHARED session hold, so once a running domain is observed it cannot be
# torn out from under us — a teardown claim needs an exclusive lock that
# every SH holder conflicts with. A running domain is adopted as-is (the
# guest is per-APP: qdshell derives the name from the catalogue appId, so
# a relaunch of the same app names the same domain); a missing or stopped
# one is (re)created under a short EXCLUSIVE upgrade that serializes
# against a racing creator or an exiting wrapper's teardown claim.
# _create_overlay_disk — create the linked clone under a PARTIAL name,
# stamp its bound owner record "<appId> <dev> <ino>", then link it
# into place. Crash residue is then always provable: a partial plus a
# sidecar binding its inode is recoverable (finish the create), and a
# bare partial is a harmless leak — while a file AT $DISK without a
# matching record is NEVER our residue, so the caller refuses it
# instead of deleting an unproven file (sol r19/r20). ln, not mv:
# it atomically refuses to clobber a $DISK that appeared after the
# caller's absence check.
_create_overlay_disk() {
    # mktemp claims the partial name with O_EXCL — a foreign file at a
    # predictable $DISK.partial.$$ can then never be overwritten by our
    # qemu-img or unlinked by our failure cleanup (sol r21). Run it as
    # admin so the partial is admin-owned like every overlay here.
    _tmp=$(run_as_admin mktemp "$DISK.partial.XXXXXX" 2>/dev/null) \
        || { echo "[tier5] FAIL: cannot create partial under $DISK_DIR" >&2; exit 4; }
    if ! run_as_admin qemu-img create -f qcow2 -F qcow2 -b "$DISK_BASE" "$_tmp" >/dev/null 2>&1; then
        rm -f "$_tmp" 2>/dev/null
        echo "[tier5] FAIL: qemu-img create -b $DISK_BASE failed at $DISK" >&2
        exit 4
    fi
    _di=$(run_as_admin stat -c '%d %i' "$_tmp" 2>/dev/null)
    # The sidecar temp gets the same O_EXCL claim — a predictable
    # $DISK.appid.$$ path could otherwise overwrite (or on failure
    # unlink) a foreign file/symlink squatting there (sol r22).
    _stmp=$(run_as_admin mktemp "$DISK.appid.tmp.XXXXXX" 2>/dev/null)
    if [ -z "$_di" ] || [ -z "$_stmp" ] \
            || ! printf '%s %s\n' "$POLICY_KEY" "$_di" > "$_stmp"; then
        rm -f "$_tmp" ${_stmp:+"$_stmp"} 2>/dev/null
        echo "[tier5] FAIL: could not stamp binding sidecar $DISK.appid" >&2
        exit 4
    fi
    # Install the record first — but never clobber one that is not
    # ours: the caller gates readable foreign records, yet a record
    # could still appear in the gap (sol r22). Our OWN stale record
    # (same appId) is removed so the new binding can land.
    if [ -e "$DISK.appid" ] || [ -L "$DISK.appid" ]; then
        _ca=""
        [ -f "$DISK.appid" ] && [ ! -L "$DISK.appid" ] \
            && read -r _ca _ci 2>/dev/null < "$DISK.appid" || true
        if [ "$_ca" != "$POLICY_KEY" ]; then
            rm -f "$_tmp" "$_stmp" 2>/dev/null
            echo "[tier5] FAIL: $DISK.appid holds a foreign or" >&2
            echo "        unreadable record — preserving it" >&2
            exit 4
        fi
        rm -f "$DISK.appid" 2>/dev/null
    fi
    # ln -T: the destination is a filename, never a directory sink —
    # link(2) also refuses any existing path, so nothing can be
    # clobbered between the check and the install (sol r23).
    if ! ln -T "$_stmp" "$DISK.appid" 2>/dev/null \
            || [ ! "$DISK.appid" -ef "$_stmp" ]; then
        rm -f "$_tmp" "$_stmp" 2>/dev/null
        echo "[tier5] FAIL: $DISK.appid appeared during create" >&2
        echo "        — refusing to clobber an unproven record" >&2
        exit 4
    fi
    rm -f "$_stmp" 2>/dev/null
    if ! ln -T "$_tmp" "$DISK" 2>/dev/null \
            || [ ! "$DISK" -ef "$_tmp" ]; then
        rm -f "$_tmp" 2>/dev/null
        echo "[tier5] FAIL: $DISK appeared during create — refusing" >&2
        echo "        to clobber an unproven file" >&2
        exit 4
    fi
    rm -f "$_tmp" 2>/dev/null
}

create_domain() {
    # Defined-state FIRST: it decides both overlay provenance and
    # whether a define is needed. An already-defined domain must be
    # OURS before we may start it — the decision layer checked the
    # binding, but a transient query outage could have hidden a
    # foreign domain then (sol r8); an unverifiable query refuses.
    domain_defined_state; _ds=$?
    case "$_ds" in
        0)  if [ "$(domain_appid)" != "$POLICY_KEY" ]; then
                echo "[tier5] FAIL: existing domain $VM_NAME is bound to" >&2
                echo "        another app — refusing to start it" >&2
                REAP=0; exit 3
            fi ;;
        1)  ;;  # confirmed absent — define below, disk needs its own record
        *)  echo "[tier5] FAIL: cannot determine state of $VM_NAME — refusing" >&2
            REAP=0; exit 3 ;;
    esac

    # Linked-clone disk per VM. An EXISTING overlay is reusable only
    # with a provable owner bound to THIS inode — in EVERY state: a
    # defined-ours domain's binding vouches for the path, not the
    # inode, and a file swapped in while the domain was stopped would
    # otherwise boot under our binding (sol r21). The <disk>.appid
    # sidecar must name our appId, record the disk's own dev/ino, and
    # the backing must be our base; anything less — missing sidecar,
    # foreign appId, inode mismatch, wrong provenance — is refused AND
    # preserved: our own crash residue never lands at $DISK without
    # its record (the create goes through .partial), so an unproven
    # file here is not ours to delete either (sol r12/r19/r20/r21).
    if [ -f "$DISK" ] && [ ! -L "$DISK" ]; then
        _sapp="" _sident=""
        [ -f "$DISK.appid" ] && [ ! -L "$DISK.appid" ] \
            && read -r _sapp _sident 2>/dev/null < "$DISK.appid" || true
        _back=$(run_as_admin qemu-img info --output=json "$DISK" \
            2>/dev/null \
            | sed -n 's/.*"backing-filename": "\([^"]*\)".*/\1/p')
        if [ "$_sapp" != "$POLICY_KEY" ] \
                || [ "$_sident" != "$(stat -c '%d %i' "$DISK" 2>/dev/null)" ] \
                || [ "$_back" != "$DISK_BASE" ]; then
            echo "[tier5] FAIL: overlay $DISK bound to" >&2
            echo "        '${_sapp:-<unbound>}' / backing '${_back:-<unknown>}'" >&2
            echo "        — refusing to boot an unproven disk" >&2
            REAP=0; exit 3
        fi
    else
        # $DISK absent: a crash between the sidecar stamp and the ln
        # leaves a provable pair — the sidecar binds a $DISK.partial.*
        # file's inode, and dev+ino cannot be forged by a planted file.
        # Finish that create rather than leaking the partial. Only a
        # REGULAR sidecar naming OUR appId may reach the create path:
        # a foreign record, a symlink, or an unreadable/empty file is
        # an unrecognized owner record — preserve it and refuse, never
        # overwrite it (sol r21/r22).
        _sapp="" _sident="" _recover=""
        if [ -e "$DISK.appid" ] || [ -L "$DISK.appid" ]; then
            [ -f "$DISK.appid" ] && [ ! -L "$DISK.appid" ] \
                && read -r _sapp _sident 2>/dev/null < "$DISK.appid" || true
            if [ "$_sapp" != "$POLICY_KEY" ]; then
                echo "[tier5] FAIL: $DISK.appid holds a foreign or" >&2
                echo "        unreadable record — preserving it" >&2
                REAP=0; exit 3
            fi
        fi
        if [ "$_sapp" = "$POLICY_KEY" ] && [ -n "$_sident" ]; then
            for _cand in "$DISK".partial.*; do
                [ -f "$_cand" ] && [ ! -L "$_cand" ] || continue
                [ "$(stat -c '%d %i' "$_cand" 2>/dev/null)" = "$_sident" ] \
                    && { _recover="$_cand"; break; }
            done
        fi
        if [ -n "$_recover" ]; then
            if ln -T "$_recover" "$DISK" 2>/dev/null \
                    && [ "$DISK" -ef "$_recover" ]; then
                rm -f "$_recover" 2>/dev/null
                echo "[tier5] recovered crashed create at $DISK" >&2
            else
                rm -f "$_recover" 2>/dev/null
                echo "[tier5] FAIL: $DISK appeared during create" >&2
                echo "        recovery — refusing to clobber it" >&2
                exit 4
            fi
        else
            _create_overlay_disk
        fi
    fi

    # Define only on CONFIRMED absence (the same verdict that gated the
    # overlay provenance above).
    [ "$_ds" -eq 1 ] && _create_domain_define

    # Start if not live. The qemu oracle decides — a false "shut off"
    # domstate must not push `virsh start` at an already-live domain,
    # and an unverifiable scan must not guess either (sol r7): rc 0
    # means a racer started it (proceed — the guest is ours), rc 1
    # starts it, rc 2 fails closed.
    domain_is_live; _lv=$?
    if [ "$_lv" -eq 1 ]; then
        if ! run_as_admin virsh start "$VM_NAME" >/dev/null; then
            echo "[tier5] FAIL: virsh start failed" >&2
            exit 6
        fi
    elif [ "$_lv" -eq 2 ]; then
        echo "[tier5] FAIL: cannot determine liveness of $VM_NAME before start" >&2
        REAP=0; exit 3
    fi
}

_create_domain_define() {
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

    MEM_KIB="${TIER5_MEM_KIB:-2097152}"
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

    # __SERIAL_XML__: default to a pty serial (interactive `virsh console`).
    # When TIER5_SERIAL_LOG is set, use a file-backed serial so the guest's
    # boot console is captured headlessly (virsh console needs a controlling
    # TTY and can't be redirected to a file). The qemu:///session domain runs
    # as admin, so the log file must be admin-writable; create it as admin.
    if [ -n "${TIER5_SERIAL_LOG:-}" ]; then
        run_as_admin sh -c ': >>"$1" 2>/dev/null || true' _ "$TIER5_SERIAL_LOG"
        SERIAL_XML="<serial type='file'><source path='$TIER5_SERIAL_LOG'/><target type='isa-serial' port='0'/></serial>"
    else
        SERIAL_XML="<serial type='pty'><target type='isa-serial' port='0'/></serial>"
    fi
    SERIAL_XML_ESCAPED=$(printf '%s' "$SERIAL_XML" | sed 's|[\\/&]|\\&|g')

    sed \
        -e "s|__NIC_XML__|$NIC_XML_ESCAPED|g" \
        -e "s|__SERIAL_XML__|$SERIAL_XML_ESCAPED|g" \
        -e "s|__VM_NAME__|$VM_NAME|g" \
        -e "s|__APP_ID__|$POLICY_KEY|g" \
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
}

# The decision: adopt a running domain under our shared session hold, or
# take EXCLUSIVE for the create path. The session SH is RELEASED before
# the exclusive grab — an SH->EX upgrade on one fd deadlocks against a
# concurrent upgrader (each waits for the other's SH), and an EX on a
# second fd would deadlock against our own SH. The release window is
# safe: a teardown that claims the gap destroys a VM with no holders,
# and a relaunch that adopts in the gap either sees the domain running
# (keeps it, our EX then waits) or ends up at this same create decision
# behind us. The re-check under exclusivity always sees a final state.
# Early FOREIGN check, BEFORE we join the lock session: a LIVE domain
# bound to a different appId means we refuse here — a mismatched spawn
# that never takes fd9 can never starve the true owner's teardown claim
# (sol r4). Liveness uses the qemu oracle, not domstate alone — a false
# "shut off" must not slip a live foreign guest past this check either
# (sol r6). An unverifiable scan falls through to the authoritative
# locked checks below, which re-verify under the holds.
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

# --- per-VM ownership hold (relaunch reuse) ------------------------
# flock(2) protocol on $HOLD_FILE, used by every wrapper invocation
# that touches this domain:
#   fd9  — session hold: SHARED from the adopt/create decision onward,
#          so concurrent launches of the same app coexist on one guest.
#          The create decision RELEASES then retakes it EXCLUSIVELY
#          (an upgrade would deadlock against a concurrent upgrader),
#          serializing against a teardown claim or a racing create —
#          but never against plain SH adopters.
#   fd10 — teardown claim: a BOUNDED EX claim (see decide_teardown). If
#          it fails, a live session (or an in-flight decision) still
#          owns the VM and this wrapper must leave it alone; if it wins,
#          we are the last holder and own teardown.
# Locks are fd-bound, so a crashed wrapper (SIGKILL, OOM) releases
# everything automatically — a leftover running/defined domain is
# then adopted or reaped by the next spawn. The hold FILES are never
# unlinked while in use: unlink+recreate would split the flock
# namespace across two inodes and lose mutual exclusion.
HOLD_DIR="${TIER5_HOLD_DIR:-/run/qdistro-tier5}"
install -d -m 0700 "$HOLD_DIR"
HOLD_FILE="$HOLD_DIR/hold-$VM_NAME"
[ -f "$HOLD_FILE" ] || install -m 0600 /dev/null "$HOLD_FILE"
exec 9>>"$HOLD_FILE"
exec 10>>"$HOLD_FILE"
if ! flock -w 120 -s 9; then
    echo "[tier5] FAIL: timed out waiting for the $VM_NAME session hold" >&2
    exit 3
fi

# Bound identity: a domain is OURS only when its recorded appId equals
# our POLICY_KEY exactly. The name alone is not identity — catalogue
# reloads can retire one appId and publish another that derives the same
# name, and the running guest outlives the manifest it came from. A
# RUNNING foreign domain is refused (its sessions may still be live);
# a DEFINED foreign domain is refused outright even when its qemu is
# confirmed dead — death proves we would not kill a live guest, but it
# does not make the foreign definition or disk ours (sol r11). Refusals
# pin REAP=0: a wrapper that never established ownership must never
# tear down someone else's guest.
ADOPTED=0
domain_is_live; _lv=$?
if [ "$_lv" -eq 0 ]; then
    _bound=$(domain_appid)
    if [ "$_bound" != "$POLICY_KEY" ]; then
        echo "[tier5] FAIL: domain $VM_NAME is bound to app '${_bound:-<unbound>}'," >&2
        echo "        not '$POLICY_KEY' — refusing cross-app adoption" >&2
        REAP=0; exit 3
    fi
    ADOPTED=1
elif [ "$_lv" -eq 2 ]; then
    # Cannot prove the domain is dead OR alive — never guess on an
    # isolation boundary; leaving is always safe (sol r6).
    echo "[tier5] FAIL: cannot determine liveness of $VM_NAME — refusing" >&2
    REAP=0; exit 3
else
    flock -u 9 2>/dev/null || true
    if ! flock -w 120 -x 9; then
        echo "[tier5] FAIL: timed out waiting for the $VM_NAME decision hold" >&2
        exit 3
    fi
    domain_is_live; _lv=$?
    if [ "$_lv" -eq 0 ]; then
        # A racing creator finished while we waited for exclusivity.
        _bound=$(domain_appid)
        if [ "$_bound" != "$POLICY_KEY" ]; then
            echo "[tier5] FAIL: domain $VM_NAME is bound to app '${_bound:-<unbound>}'," >&2
            echo "        not '$POLICY_KEY' — refusing cross-app adoption" >&2
            REAP=0; exit 3
        fi
        ADOPTED=1
    elif [ "$_lv" -eq 2 ]; then
        echo "[tier5] FAIL: cannot determine liveness of $VM_NAME — refusing" >&2
        REAP=0; exit 3
    else
        # Foreign-binding check needs a PRECISE defined-state: a query
        # outage must not be read as "no domain" — create_domain would
        # then skip define and `virsh start` a stopped foreign guest the
        # moment the query recovers (sol r8).
        domain_defined_state; _ds=$?
        if [ "$_ds" -eq 2 ]; then
            echo "[tier5] FAIL: cannot determine whether $VM_NAME" >&2
            echo "        is defined — refusing to create over it" >&2
            REAP=0; exit 3
        fi
        if [ "$_ds" -eq 0 ] && [ "$(domain_appid)" != "$POLICY_KEY" ]; then
            # A defined domain that is NOT ours squats our name — bound
            # to another app or to nothing. Refuse rather than reclaim:
            # confirmed-dead proves no live guest is killed, but it does
            # NOT make the foreign definition or the foreign disk ours —
            # destroying another app's stopped guest crosses the same
            # boundary the binding exists to protect (sol r11). The
            # owner relaunches under its own name; an admin removes a
            # genuinely stale squatting domain deliberately.
            _fb=$(domain_appid)
            echo "[tier5] FAIL: domain $VM_NAME is bound to app" >&2
            echo "        '${_fb:-<unbound>}', not '$POLICY_KEY' —" >&2
            echo "        refusing to reclaim another app's guest" >&2
            REAP=0; exit 3
        fi
        create_domain
    fi
    flock -s 9
fi
if [ "$ADOPTED" = "1" ]; then
    echo "[tier5] adopted running domain $VM_NAME (per-app guest shared across relaunches)" >&2
else
    echo "[tier5] domain $VM_NAME running (cid=$CID)" >&2
fi

# Wait for qga. A deadline (rather than a fixed iteration count) keeps the
# contract honest if individual virsh calls stall briefly under nested VM load.
# qd_deadline_pending clamps a host suspend/resume jump: the nested guest gets
# no CPU while its host is asleep, so that interval must not consume its boot
# budget.  This is the same accounting contract used by scripts/vm/vm-exec.
QGA_OK=0
if ! qd_deadline_start "$QGA_TIMEOUT_SECS" "[tier5] WARN: qga wait"; then
    echo "[tier5] FAIL: cannot initialize qga deadline clock" >&2
    exit 7
fi
while qd_deadline_pending; do
    if run_as_admin virsh qemu-agent-command --timeout 5 "$VM_NAME" \
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

# Build guest-exec arguments via python3 json.dumps so every byte
# (control characters included) is encoded, matching spawn-tier5b's
# HIGH-2 fix (iso2 `15` E3). First arg is the port for the publisher.
json_encode_stdin() {
    python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))'
}
QGA_ARGS_JSON="$(printf '%s' "$PORT" | json_encode_stdin)"
for a in "$@"; do
    QGA_ARGS_JSON="$QGA_ARGS_JSON,$(printf '%s' "$a" | json_encode_stdin)"
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
echo "[tier5] lifecycle[$POLICY_KEY]: method=$SHUTDOWN_METHOD grace=${SHUTDOWN_GRACE_SECS}s idle=${IDLE_SHUTDOWN_SECS}s lowmem=${LOWMEM_MB}MB" >&2

# In VM mode (no --oneshot), waypipe-client stays alive as a listener.
# Poll the domain state — when it stops, tear down the bridge.
# In loopback mode, waypipe-client has --oneshot and exits when the
# server disconnects; wait for it directly.
if [ "$MODE" = "vm" ]; then
    # Session watch: the loop ends when the guest is confirmed dead OR
    # the published app exits. A transient runuser/virsh query failure
    # (e.g. virtqemud "End of file ... I/O error" under audit-backlog
    # churn from this very poll) must NOT be read as "domain stopped":
    # the old `grep -qw running` shape exited on ANY non-"running"
    # domstate output, so a single blip declared a HEALTHY guest
    # stopped and tore it down (perm-gui 20/21 cold-start failed every
    # run for this reason).
    exec_gone=0
    while :; do
        # Liveness comes ONLY from the qemu oracle — domstate is not
        # consulted: it can lie in both directions under nested virt
        # ("shut off" for a live qemu — the original perm-gui 20/21
        # cold-start killer — and stale "running" for a dead one, which
        # would wedge this watch). Only a CONFIRMED-dead verdict ends
        # the session watch: breaking on ambiguity would hand a
        # possibly-live guest to teardown's destroy+kill (sol r7). A
        # truly dead guest yields confirmed-dead as soon as the scan
        # can run; a persistently broken oracle just keeps this
        # harmless watch going — the app-exit watch below is the
        # primary session-end signal anyway (sol r8).
        domain_is_live; _lv=$?
        [ "$_lv" -eq 1 ] && break
        # Session-on-done: each session publishes ONE app; once that app exits
        # (e.g. its last window was closed -> request_close -> the app quit)
        # this wrapper's work is done. Watch the published process via the
        # guest agent; an explicit "exited":true ends the wait. We then run
        # idle_keep_warm — the relaunch window (IDLE_SHUTDOWN_SECS/low-mem
        # policy plus last-holder teardown claim) — and break; the EXIT-trap
        # cleanup() tears the domain down ONLY if no session adopted it in
        # the meantime (graceful ACPI shutdown by default — see reap_domain/
        # SHUTDOWN_METHOD — then undefine + overlay unlink). Before the
        # app-process watch, the loop ended ONLY on a guest self-poweroff, so
        # closing the app window leaked the whole nested VM + overlay
        # (perm-gui/21 S3-S5). Keying on OUR app process (GUEST_PID), not a
        # single toplevel or the whole guest, is deliberate: a multi-window
        # app ends its session when it FULLY exits, and a guest may outlive
        # us because a relaunched session of the same app adopted it.
        # --timeout is load-bearing: `qemu-agent-command` BLOCKS by default, so a
        # guest whose qga has gone unresponsive (a dying/OOMing guest is exactly
        # the case that matters) would hang this loop forever and the
        # domstate-based reap above could never fire. Bounding it lets the next
        # iteration's domstate check catch a stopped/gone guest instead.
        es=$(run_as_admin virsh qemu-agent-command --timeout 5 "$VM_NAME" \
             "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$GUEST_PID}}" 2>&1)
        if printf '%s' "$es" | grep -qE '"exited"[[:space:]]*:[[:space:]]*true'; then
            echo "[tier5] published app (pid=$GUEST_PID) exited; session done for $VM_NAME" >&2
            idle_keep_warm
            break
        elif printf '%s' "$es" | grep -qiE 'PID [0-9]+ does not exist|does not exist'; then
            # The guest agent REAPS a finished guest-exec entry the first time its
            # terminal status is read, after which status calls for that PID return
            # "PID N does not exist". So a missing-PID for OUR known GUEST_PID also
            # means the app exited — covering the race where we miss the single
            # "exited":true (e.g. a --timeout firing exactly as qga reaped it).
            # Require 2 CONSECUTIVE so a one-off blip can't reap a live app; a
            # generic qga timeout/outage is NOT this error, resets the counter,
            # and is left to the domstate path above (guest death) to handle.
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
    kill "$CLIENT_PID" 2>/dev/null; wait "$CLIENT_PID" 2>/dev/null
    exit 0
else
    wait "$CLIENT_PID" 2>/dev/null
    EXIT=$?
    echo "[tier5] tier-5 spawn exited rc=$EXIT" >&2
    exit "$EXIT"
fi
