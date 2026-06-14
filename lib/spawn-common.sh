# spawn-common.sh — shared bash helpers for the tier-N spawn scripts.
#
# Sourced (never executed) by qdistro/tier*/spawn-tier*.sh. Replaces
# the inline copies of these helpers that each spawn script carried
# until 2026-05-21; those copies had already drifted slightly (tier3
# validated the launch token with a regex while every other tier
# used a string-length check, which would have missed a /dev/urandom
# or od malfunction that produced non-hex bytes of the right length).
#
# Source via:
#   _SC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$_SC_DIR/../lib/spawn-common.sh"
#
# This file expects bash 4+ (for [[ =~ ]]).

# gen_launch_token <log-prefix>
#
# Print a 32-lowercase-hex per-spawn launch token on stdout. Exits the
# calling script with status 5 if /dev/urandom or od produce something
# that doesn't match ^[0-9a-f]{32}$ — qdshell's orphan-dir reaper and
# secctx-instance correlators all filter on that shape.
#
# The token is correlation, not auth; entropy quality from /dev/urandom
# is more than enough.
gen_launch_token() {
    local prefix="${1:-spawn}" tok
    tok="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    if ! [[ "$tok" =~ ^[0-9a-f]{32}$ ]]; then
        echo "${prefix}: failed to generate launch token from /dev/urandom" >&2
        exit 5
    fi
    printf '%s' "$tok"
}

# domain_xml_tmpfile <log-prefix>
#
# Print the path of a fresh, 0600, private temp file to render a privileged
# libvirt domain XML into. Replaces the predictable
# /tmp/qdistro-tierN-<vm>-$$.xml paths the spawn scripts used until
# 2026-05-29: a predictable name under world-writable /tmp let a local
# attacker pre-create or symlink-race the file before `virsh define` read
# it (the render runs privileged via run_as_admin). The replacement is a
# mktemp'd file inside a 0700 process-private directory under $XDG_RUNTIME_DIR
# (tmpfs) or /run — both per-user and not world-traversable, so neither the
# file nor its parent can be raced by another uid.
#
# In the common case the per-user 0700 runtime dir IS the private base and the
# file is mktemp'd directly in it; the world-writable /run and /tmp fallbacks
# get an extra unguessable 0700 mktemp -d subdir so the race is closed there
# too. Cleanup is the caller's existing `rm -f "$TMP_XML"` — this function sets
# NO EXIT trap (it is invoked via command substitution; see the body NOTE).
#
# The spawn scripts run privileged (root, via pkexec) but hand the rendered
# XML to `virsh` running as the unprivileged admin user (run_as_admin).
# Pass the admin user/runtime as $2/$3 so the private dir is created under
# the admin's own /run/user/<uid> (tmpfs, 0700) and chowned to admin: the
# admin can then read the file, while no OTHER uid can traverse the dir.
#   $1 = log/file prefix (e.g. qdistro-tier5)
#   $2 = owner user that will read the file via virsh (default: current user)
#   $3 = that owner's runtime dir, used as the private base (default:
#        $XDG_RUNTIME_DIR -> /run -> $TMPDIR)
#
# Exits the calling script with status 5 if a private dir/file cannot be
# created — a privileged render must never fall back to a predictable path.
# _qd_dir_is_private <dir> <expected-reader-user|empty> — 0 iff <dir> is owned
# by the expected reader (default: the current user) AND carries NO group/other
# permission bits (mode ?00), i.e. a genuine per-user private directory we may
# drop a 0600 file into directly. Gates the no-subdir fast path below so a
# stale/misconfigured/non-private base never skips the 0700 race-isolation dir.
_qd_dir_is_private() {
    local d="$1" want="${2:-}" meta own perms
    [ -d "$d" ] || return 1
    meta="$(stat -c '%U %a' "$d" 2>/dev/null)" || return 1
    own="${meta%% *}"; perms="${meta##* }"
    [ -n "$want" ] || want="$(id -un)"
    [ "$own" = "$want" ] || return 1
    case "$perms" in *00) return 0 ;; *) return 1 ;; esac
}

domain_xml_tmpfile() {
    local prefix="${1:-qdistro}" owner="${2:-}" owner_rt="${3:-}" base dir f
    # NOTE: this function is invoked via command substitution
    # (TMP_XML="$(domain_xml_tmpfile ...)"), i.e. in a SUBSHELL. It must NOT set
    # an EXIT trap to clean the private dir: a trap set here fires when the
    # substitution subshell returns — deleting the dir before the caller writes
    # the XML, which was the tier-5/tier-4 "virsh define: No such file or
    # directory" bug. Cleanup is the caller's existing `rm -f "$TMP_XML"`.
    #
    # Pick the most private writable base, preferring the owner's per-user
    # runtime. The file is dropped DIRECTLY in base only when base is verified
    # private (owner-owned, no group/other bits) — then mktemp's O_EXCL + the
    # 0700 parent close the pre-create/symlink race and the caller's rm -f leaves
    # nothing. Any non-private base (world-writable /run|/tmp, or an
    # unexpectedly-permissive owner_rt) instead gets an unguessable 0700
    # mktemp -d subdir for the same protection.
    if [ -n "$owner_rt" ] && [ -d "$owner_rt" ]; then
        base="$owner_rt"
    elif [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "${XDG_RUNTIME_DIR}" ] && [ -w "${XDG_RUNTIME_DIR}" ]; then
        base="$XDG_RUNTIME_DIR"
    elif [ -d /run ] && [ -w /run ]; then
        base="/run"
    else
        base="${TMPDIR:-/tmp}"
    fi
    local need_subdir=1
    _qd_dir_is_private "$base" "$owner" && need_subdir=0
    if [ "$need_subdir" = 1 ]; then
        dir="$(mktemp -d "$base/qdistro-domxml.XXXXXX")" || {
            echo "${prefix}: failed to create private domain-XML dir under $base" >&2
            exit 5
        }
        chmod 0700 "$dir"
        # Owned by the reader (admin) so it can traverse its own 0700 dir.
        if [ -n "$owner" ] && ! chown "$owner" "$dir" 2>/dev/null; then
            echo "${prefix}: failed to chown private domain-XML dir to $owner" >&2
            rm -rf "$dir"; exit 5
        fi
        base="$dir"
    fi
    f="$(mktemp "$base/${prefix}.XXXXXX.xml")" || {
        echo "${prefix}: failed to create private domain-XML file under $base" >&2
        exit 5
    }
    chmod 0600 "$f"
    # The render runs as root but virsh reads the file as the admin owner — fail
    # closed if we cannot hand ownership over, rather than defer to a confusing
    # "virsh define: permission denied".
    if [ -n "$owner" ] && ! chown "$owner" "$f" 2>/dev/null; then
        echo "${prefix}: failed to chown domain-XML file to $owner" >&2
        rm -f "$f"; exit 5
    fi
    printf '%s' "$f"
}

# ---------------------------------------------------------------------
# qd_emit_event TAG KEY=VALUE [KEY=VALUE ...]
#
# Emit ONE structured, machine-readable launch/close/error event to the
# journal. Every spawn script that is still *script-launched* (no qdshell
# App1 receiver — see tier5b-vm/qdistro_integration.py) needs ops to be
# able to correlate launch / close / error from journald without scraping
# the free-form `[tierN] ...` stderr lines.
#
# Wire format (stable, greppable from `journalctl -o json`):
#   the MESSAGE field is a single line of space-separated KEY=VALUE
#   tokens, the FIRST of which is always `QDISTRO_EVENT=<launch|close|
#   error>`. The remaining tokens are the KEY=VALUE pairs passed in.
#   Tokens are emitted verbatim, so callers must pass already-sanitised
#   values (the spawn scripts only feed it ids / uids / numeric codes /
#   single-token reasons, never user text).
#
# Sink (in priority order):
#   1. $QDISTRO_EVENT_SINK, if set, is treated as a command the MESSAGE
#      is appended to as a single argv element. Tests point this at a
#      file-append stub so they can assert the EXACT field set without a
#      journal. (e.g. QDISTRO_EVENT_SINK="printf %s\\n >>FILE" is NOT how
#      it works — it's an argv: see the test harness for the exact stub.)
#   2. `logger -t <TAG>` — lands the MESSAGE in journald's MESSAGE field,
#      tagged with SYSLOG_IDENTIFIER=<TAG> so ops can
#      `journalctl -t <TAG> -o json` and grep QDISTRO_EVENT=.
#   3. stderr, if logger is somehow absent (never silently dropped).
#
# Always returns 0 — an event-emit failure must never fail a launch.
qd_emit_event() {
    local tag="$1"; shift
    local msg="QDISTRO_EVENT=$1"; shift || true
    local kv
    for kv in "$@"; do
        msg="$msg $kv"
    done
    if [ -n "${QDISTRO_EVENT_SINK:-}" ]; then
        # Word-split QDISTRO_EVENT_SINK into a command + leading args, then
        # append the message as ONE final argv element.
        # shellcheck disable=SC2086
        ${QDISTRO_EVENT_SINK} "$msg" 2>/dev/null || true
        return 0
    fi
    if command -v logger >/dev/null 2>&1; then
        logger -t "$tag" -- "$msg" 2>/dev/null || true
    else
        printf 'journal-event: %s\n' "$msg" >&2
    fi
    return 0
}

# ---------------------------------------------------------------------
# qd_register_secctx_launch_record SILO ENGINE APPID INSTANCE NAMESPACE
#                                  LAUNCHREC_PATH LAUNCHREC_TOKEN LABEL
#
# Best-effort permission-lineage registration for root launchers that
# wrapped an inner command with qdistro-secctx-exec. The wrapper publishes
# the inner child pid to QDISTRO_LAUNCH_RECORD_PATH; this helper validates
# the matching token and asks the broker to bind live pid -> silo.
#
# A failed registration must never block the launch: under
# lineage_enforce=true the affected permission path simply stays
# default-deny because the broker has no verified launch record.
qd_register_secctx_launch_record() {
    local silo="$1" engine="$2" appid="$3" instance="$4" namespace="$5"
    local launchrec_path="$6" launchrec_token="$7" label="${8:-lineage}"
    local inner_pid="" inner_token="" _

    [ -n "$launchrec_path" ] || return 0
    [ -n "$launchrec_token" ] || return 0
    if ! command -v dbus-send >/dev/null 2>&1; then
        echo "[$label] WARN lineage: dbus-send not found; skipping launch-record registration" >&2
        return 0
    fi

    for _ in $(seq 1 20); do
        if [ -s "$launchrec_path" ]; then
            read -r inner_pid inner_token < "$launchrec_path" || true
            case "$inner_pid" in ''|*[!0-9]*) inner_pid="" ;; esac
            if [ -n "$inner_pid" ] && [ "$inner_token" = "$launchrec_token" ]; then
                break
            fi
            inner_pid=""
        fi
        sleep 0.1
    done

    if [ -z "$inner_pid" ] || ! kill -0 "$inner_pid" 2>/dev/null; then
        echo "[$label] WARN lineage: no inner pid published; skipping launch-record registration" >&2
        rm -f "$launchrec_path" 2>/dev/null || true
        return 0
    fi

    if dbus-send --system --print-reply \
        --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.RegisterLaunch \
        string:"$silo" string:"$engine" string:"$appid" \
        string:"$instance" string:"" uint64:"$inner_pid" \
        string:"$namespace" uint64:0 >/dev/null 2>&1; then
        echo "[$label] lineage: registered pid=$inner_pid as silo=$silo" >&2
    else
        echo "[$label] WARN lineage: RegisterLaunch failed for pid=$inner_pid (lineage-gated permissions stay default-deny)" >&2
    fi
    rm -f "$launchrec_path" 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------------
# qd_free_space_check BASE_DISK TARGET_DIR HEADROOM_BYTES
#
# Fail-closed free-space budget precheck for a qcow2 linked-clone overlay.
# Before `qemu-img create -b BASE`, confirm TARGET_DIR's filesystem has
# enough free space for:
#       budget = base-image VIRTUAL size + HEADROOM_BYTES
# where the base virtual size is the worst-case the overlay can grow to
# (every backing-file cluster eventually copied-on-write), and HEADROOM is
# a documented slack for guest writes (logs, app state, swap-in-overlay).
#
# Querying both numbers is delegated to overridable hooks so tests can
# drive both sides of the budget without a real disk or qemu-img:
#   * $QDISTRO_FREE_BYTES_CMD  — prints available bytes on TARGET_DIR's fs
#                                (default: df -PB1 of the dir, parsed).
#   * $QDISTRO_BASE_VSIZE_CMD  — prints the base image virtual size in
#                                bytes (default: qemu-img info --output=json).
# Each hook is invoked as: `$CMD <arg>` (TARGET_DIR resp. BASE_DISK).
#
# Prints nothing on success and returns 0. On insufficient space it prints
# a single stable diagnostic to stderr and returns 1 (the caller maps that
# to its own ENOSPC exit code + error event). On an unparseable/zero query
# it ALSO returns 1 (fail closed — never launch into a guessed-OK).
qd_free_space_check() {
    local base="$1" dir="$2" headroom="$3"
    local vsize avail budget

    if [ -n "${QDISTRO_BASE_VSIZE_CMD:-}" ]; then
        # shellcheck disable=SC2086
        vsize="$(${QDISTRO_BASE_VSIZE_CMD} "$base" 2>/dev/null)"
    else
        vsize="$(qemu-img info --output=json "$base" 2>/dev/null \
                 | grep -oE '"virtual-size":[[:space:]]*[0-9]+' \
                 | grep -oE '[0-9]+' | head -1)"
    fi

    if [ -n "${QDISTRO_FREE_BYTES_CMD:-}" ]; then
        # shellcheck disable=SC2086
        avail="$(${QDISTRO_FREE_BYTES_CMD} "$dir" 2>/dev/null)"
    else
        # POSIX df, 1-byte blocks; the "Available" column is field 4 of the
        # data row. tail -1 collapses the wrapped-long-device-name case.
        avail="$(df -PB1 "$dir" 2>/dev/null | tail -1 | awk '{print $4}')"
    fi

    # Fail closed on any non-numeric / empty / zero query result.
    case "$vsize" in ''|*[!0-9]*) vsize=0 ;; esac
    case "$avail" in ''|*[!0-9]*) avail=0 ;; esac
    case "$headroom" in ''|*[!0-9]*) headroom=0 ;; esac
    if [ "$vsize" -le 0 ] || [ "$avail" -le 0 ]; then
        echo "free-space precheck: could not determine base vsize ($vsize) or free bytes ($avail) for $dir" >&2
        return 1
    fi

    budget=$((vsize + headroom))
    if [ "$avail" -lt "$budget" ]; then
        echo "free-space precheck: $dir has ${avail}B free, need ${budget}B (base vsize ${vsize}B + headroom ${headroom}B)" >&2
        return 1
    fi
    return 0
}
