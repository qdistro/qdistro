#!/bin/bash
# spawn-tier3s.sh — tier 3s (gVisor runsc) launch: the ROOT supervisor of one
# launch, run by qdistro-tier3s-{silo,app}@.service through its launch helper.
# Contract: tier3s/CONTRACT.md (§5 is this script). Dev profile only (O4),
# explicit launch, no fallback to tier 2/3 (O6), network=none only (O3).
#
#   spawn-tier3s.sh <workload> -- <app> [args...]
#
# Env (set by the root launch helper from the root-owned launch stanza):
#   TIER3S_ROOT_LAUNCHER=1   required; there is no direct-admin lane
#   TIER3S_LAUNCH_UNIT       the unit this runs in (verified against our cgroup)
#   TIER3S_ADMIN_UID         admin uid, default 1000 (must be 1000)
#   TIER3S_SILO              silo name (container qdistro-tier3s-<silo>); required:
#                            podapps (qdistro-tier3s-app@<token>) are refused in
#                            Phase A (CONTRACT.md §1)
#   TIER3S_BINDING           the template binding to resolve (the silo row's
#                            template_silo); default TIER3S_SILO
#   TIER3S_LAUNCH_TOKEN      32 lowercase hex, pre-committed by the manager
#   TIER3S_NETWORK           only "none"
#   TIER3S_DEBUG_LOG_DIR     dev diagnostics: admin-owned dir for runsc --debug-log
#   TIER3S_PRINT_PLAN=1      print the plan, exit 0 before the broker gate
#   QDISTRO_PROFILE          must be dev
# Refused from env: TIER3S_SECCOMP_PROFILE TIER3S_ALLOW_PRIVESC
#   TIER3S_KEEP_CAPS TIER3S_RUNTIME TIER3S_CGROUP_PARENT
#
# Order (fail closed, exit 2 on every refusal; the denial oracle is "no podman
# run and no activation record"): profile -> refused knobs -> root launcher +
# unit -> probe -> read-only resolution -> token -> [plan] -> broker gate ->
# activation record -> reap stale + control record -> image -> scope + podman.
#
# Test hook ONLY: TIER3S_TEST_ROOT=<dir> prefixes /etc/qdistro, /usr/lib/qdistro,
# /usr/libexec/qdistro, /run, /proc (own cgroup: proc/self),
# keeps the caller's PATH for fakes and skips the euid-0 check. Refused for
# root; every run in that mode says TEST.
set -uo pipefail
if [ "$EUID" -eq 0 ] || [ -z "${TIER3S_TEST_ROOT:-}" ]; then PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH; fi
umask 077
say() { printf 'spawn-tier3s: %s\n' "$*" >&2; }
refuse() { say "REFUSE: $*"; exit 2; }

T="${TIER3S_TEST_ROOT:-}"
if [ -n "$T" ]; then
    [ "$EUID" -ne 0 ] || refuse "TIER3S_TEST_ROOT is a unit-test hook and is refused for root"
    case "$T" in /?*) ;; *) refuse "TIER3S_TEST_ROOT must be absolute" ;; esac
    say "TEST MODE: TIER3S_TEST_ROOT=$T (not a real launch)"
    ADMIN_PATH="$PATH"
else
    ADMIN_PATH=/usr/bin:/bin
fi
LIBDIR="$T/usr/lib/qdistro/tier3s"
LIBEXEC="$T/usr/libexec/qdistro"
PROBE="$LIBDIR/probe.sh"
SCOPE_HELPER="$LIBEXEC/qdistro-tier3s-scope"
CLEANUP="$LIBEXEC/qdistro-tier3s-cleanup"
WRAPPER=/usr/libexec/qdistro/tier3s-runsc          # what podman records; never prefixed
CTL="$T/run/qdistro-tier3s-ctl"
LAUNCH_PARENT="$T/run/qdistro-tier3s"
RUNSC_BASE="$T/run/qdistro-tier3s-runsc"
PROC="$T/proc"

# --- arguments ------------------------------------------------------------
[ "$#" -ge 3 ] && [ "$2" = -- ] || refuse "usage: spawn-tier3s.sh <workload> -- <app> [args...]"
WORKLOAD="$1"; shift 2
APP_ARGV=("$@")
[[ "$WORKLOAD" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || refuse "invalid workload name '$WORKLOAD'"
APP_BASE="${APP_ARGV[0]##*/}"
[[ "$APP_BASE" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || refuse "invalid app '${APP_ARGV[0]}'"

# --- 1. profile: dev only (README O4) -------------------------------------
profile="${QDISTRO_PROFILE:-}"
if [ -z "$profile" ] && [ -f "$T/etc/qdistro/profile" ] && [ ! -L "$T/etc/qdistro/profile" ]; then
    profile="$(sed -n 's/^QDISTRO_PROFILE=//p' "$T/etc/qdistro/profile" | tail -1)"
fi
[ "$profile" = dev ] \
    || refuse "tier 3s is dev-profile only in this PoC (QDISTRO_PROFILE=${profile:-<unset>}); there is no hardened launch path and no fallback tier"

# --- 2. knobs a launch may not take from the environment ------------------
for k in TIER3S_SECCOMP_PROFILE TIER3S_ALLOW_PRIVESC TIER3S_KEEP_CAPS TIER3S_RUNTIME TIER3S_CGROUP_PARENT; do
    [ -z "${!k:-}" ] || refuse "$k is not accepted from the environment"
done
[ "${TIER3S_NETWORK:-none}" = none ] || refuse "TIER3S_NETWORK=${TIER3S_NETWORK} (tier 3s is network=none only)"

# --- 3. root launcher, admin, launch unit ---------------------------------
[ "${TIER3S_ROOT_LAUNCHER:-0}" = 1 ] \
    || refuse "TIER3S_ROOT_LAUNCHER=1 is required (launch through qdistro-tier3s-silo@.service; no direct-admin lane)"
[ -n "$T" ] || [ "$EUID" -eq 0 ] || refuse "the root launcher must run as root"
ADMIN_UID="${TIER3S_ADMIN_UID:-1000}"
[[ "$ADMIN_UID" =~ ^[1-9][0-9]*$ ]] || refuse "TIER3S_ADMIN_UID '$ADMIN_UID' is not a non-root uid"
[ -n "$T" ] || [ "$ADMIN_UID" = 1000 ] || refuse "qdistro's admin uid is 1000, got $ADMIN_UID"
ADMIN_USER="$(getent passwd "$ADMIN_UID" | cut -d: -f1)"
[ -n "$ADMIN_USER" ] || refuse "no user for uid $ADMIN_UID"
SILO="${TIER3S_SILO:-}"
[ -z "$SILO" ] || [[ "$SILO" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || refuse "invalid silo name '$SILO'"
# Phase A ships silos only (CONTRACT.md §1): no tier3s pod-app unit exists and
# no session-manager API starts one, so a launch without a silo is refused here.
[ -n "$SILO" ] || refuse "tier 3s pod apps (qdistro-tier3s-app@<token>.service) are not shipped in Phase A; launch a tier3s silo (TIER3S_SILO)"
BINDING="${TIER3S_BINDING:-$SILO}"
[[ "$BINDING" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || refuse "invalid binding name '$BINDING'"
UNIT="${TIER3S_LAUNCH_UNIT:-}"
[[ "$UNIT" =~ ^qdistro-tier3s-(silo|app)@[a-z0-9_-]+\.service$ ]] \
    || refuse "TIER3S_LAUNCH_UNIT '$UNIT' is not a tier3s launch unit"
own="$(sed -n 's/^0:://p' "$PROC/self/cgroup" 2>/dev/null | head -1)"
[ "${own##*/}" = "$UNIT" ] || refuse "not running in $UNIT (own cgroup: ${own:-?})"

as_admin() {   # every podman / broker / resolver call runs as the admin uid
    runuser -u "$ADMIN_USER" -- env -i PATH="$ADMIN_PATH" HOME="$(getent passwd "$ADMIN_UID" | cut -d: -f6)" \
        USER="$ADMIN_USER" LOGNAME="$ADMIN_USER" XDG_RUNTIME_DIR="/run/user/$ADMIN_UID" "$@"
}
pm() { as_admin podman "$@"; }

# --- 4. prerequisite screen (no fallback) ---------------------------------
probe_out="$("$PROBE" --user "$ADMIN_USER" 2>&1)"; probe_rc=$?
[ "$probe_rc" -eq 0 ] || refuse "probe failed (rc=$probe_rc): $(printf '%s\n' "$probe_out" | grep '^RESULT\|^REFUSE' | tail -1)"

# --- 5. read-only resolution ----------------------------------------------
SECCOMP="$LIBDIR/seccomp/$WORKLOAD.json"
[ -f "$SECCOMP" ] && [ ! -L "$SECCOMP" ] || refuse "no seccomp profile $SECCOMP for workload '$WORKLOAD' (tier 3s has no podman-default fallback)"
IMAGE="localhost/qdistro/tier3s-$WORKLOAD:latest"
STATE_PATH=""; GENERATION=""; RESOLVER=()
if [ -n "$SILO" ]; then
    if command -v qdistro-resolve-binding >/dev/null 2>&1; then RESOLVER=(qdistro-resolve-binding)
    elif [ -x /usr/libexec/qdistro/qdistro-resolve-binding ]; then RESOLVER=(/usr/libexec/qdistro/qdistro-resolve-binding)
    else refuse "TIER3S_SILO=$SILO set but qdistro-resolve-binding not found"; fi
    read_binding() {   # read_binding [--record]: sets RB_GEN RB_STATE, returns the resolver rc
        local out rc k v
        out="$(as_admin "${RESOLVER[@]}" "$BINDING" "$@" --launch-env)"; rc=$?
        RB_GEN=""; RB_STATE=""
        while IFS='=' read -r k v; do
            case "$k" in GENERATION) RB_GEN="$v" ;; STATE_PATH) RB_STATE="$v" ;; esac
        done <<< "$out"
        return "$rc"
    }
    read_binding; rc=$?
    case "$rc" in
        0)  [[ "$RB_GEN" =~ ^sha256:[0-9a-f]{64}$ ]] || refuse "resolver returned a non-digest for $SILO: '$RB_GEN'"
            [ -n "$RB_STATE" ] && [ -d "$RB_STATE" ] && [ ! -L "$RB_STATE" ] \
                || refuse "state_path '$RB_STATE' for silo $SILO is missing or not a directory"
            GENERATION="$RB_GEN"; IMAGE="$RB_GEN"; STATE_PATH="$RB_STATE" ;;
        3)  say "silo $SILO runs UNTEMPLATED (no binding); image $IMAGE" ;;
        *)  refuse "binding resolution failed for silo $SILO (rc=$rc); no tag fallback" ;;
    esac
fi
DEBUG_FLAGS=()
if [ -n "${TIER3S_DEBUG_LOG_DIR:-}" ]; then
    d="$TIER3S_DEBUG_LOG_DIR"
    case "$d" in /?*) ;; *) refuse "TIER3S_DEBUG_LOG_DIR must be absolute" ;; esac
    [ -d "$d" ] && [ ! -L "$d" ] && [ "$(stat -c %u -- "$d")" = "$ADMIN_UID" ] \
        || refuse "TIER3S_DEBUG_LOG_DIR $d is not an admin-owned directory"
    DEBUG_FLAGS=(--runtime-flag=debug "--runtime-flag=debug-log=${d%/}/")
fi

# --- 6. token, names --------------------------------------------------------
if [ -n "${TIER3S_LAUNCH_TOKEN:-}" ]; then
    [[ "$TIER3S_LAUNCH_TOKEN" =~ ^[0-9a-f]{32}$ ]] || refuse "TIER3S_LAUNCH_TOKEN must be 32 lowercase hex digits"
    TOKEN="$TIER3S_LAUNCH_TOKEN"
else
    TOKEN="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
fi
if [ -n "$SILO" ]; then
    CONTAINER="qdistro-tier3s-$SILO"; WANT_UNIT="qdistro-tier3s-silo@$SILO.service"
else
    CONTAINER="qdistro-tier3s-app-$TOKEN"; WANT_UNIT="qdistro-tier3s-app@$TOKEN.service"
fi
[ "$UNIT" = "$WANT_UNIT" ] || refuse "launch unit $UNIT does not match this launch ($WANT_UNIT)"
SCOPE_UNIT="qdistro-tier3s-$TOKEN.scope"
CTL_DIR="$CTL/$TOKEN"
LAUNCH_DIR="$LAUNCH_PARENT/$TOKEN"
RUNSC_ROOT="/run/qdistro-tier3s-runsc/$ADMIN_UID"
SPAWN_ACTION="qdistro.tier3s.spawn:$WORKLOAD/$APP_BASE"

# The podman command (CONTRACT.md §5): every flag is load-bearing.
# shellcheck disable=SC2054  # the commas are tmpfs mount options
PODMAN_ARGV=(
    --runtime "$WRAPPER"                 # the pinned runsc via the wrapper (D-A1 state root inside)
    --runtime-flag=network=none          # runsc's own network stack off, not just podman's
    "${DEBUG_FLAGS[@]}"
    --cgroup-manager=cgroupfs            # with the admin-delegated scope this keeps every process in it (D-A3b)
    run --rm --name "$CONTAINER"
    --label "qdistro_tier3s_token=$TOKEN" --label "qdistro_tier3s_unit=$UNIT"
    --security-opt label=disable         # runsc rejects a non-empty SELinux process label
    --security-opt no-new-privileges
    --cap-drop=ALL
    --security-opt "seccomp=/usr/lib/qdistro/tier3s/seccomp/$WORKLOAD.json"
    --userns=keep-id --user 1000:1000    # admin keep-id (D4 C1)
    --read-only
    --tmpfs /tmp:rw,size=64m,mode=1777
    --tmpfs /run/user/1000:rw,U,mode=0700        # U -> OCI uid=1000,gid=1000; gVisor mounts tmpfs as root otherwise
    --tmpfs /home/admin/.cache:rw,U,mode=0700
    --pids-limit=512                     # parity with tier 2 ONLY: runsc --ignore-cgroups does not enforce it; TasksMax on the scope does
    --network=none
    --env HOME=/home/admin --env XDG_RUNTIME_DIR=/run/user/1000 --env LANG=C.UTF-8
)
[ -z "$STATE_PATH" ] || PODMAN_ARGV+=(-v "$STATE_PATH:/home/admin:rw")   # no recursive chown
PODMAN_ARGV+=("$IMAGE" "${APP_ARGV[@]}")
SCOPE_ARGV=(--scope "--unit=$SCOPE_UNIT" --collect
    -p Delegate=yes -p TasksMax=1024 -p MemoryMax=2G     # set by root; enforcement is Phase C
    "-p" "BindsTo=$UNIT" "-p" "Before=$UNIT"             # never outlives the launch unit; alive through its ExecStop/ExecStopPost
    -- "$SCOPE_HELPER" enter "$TOKEN" "$ADMIN_UID" -- podman)

# --- 7. plan (test/inspection hook; no side effect yet) --------------------
if [ "${TIER3S_PRINT_PLAN:-0}" = 1 ]; then
    printf 'ENGINE=qdistro.tier3s\nWORKLOAD=%s\nCONTAINER=%s\nTOKEN=%s\nUNIT=%s\nSCOPE_UNIT=%s\n' \
        "$WORKLOAD" "$CONTAINER" "$TOKEN" "$UNIT" "$SCOPE_UNIT"
    printf 'SPAWN_ACTION=%s\nIMAGE=%s\nSTATE=%s\nSECCOMP=%s\nNETWORK=none\nBINDING=%s\n' \
        "$SPAWN_ACTION" "$IMAGE" "${STATE_PATH:-none}" "$SECCOMP" "$BINDING"
    printf 'CTL_DIR=%s\nLAUNCH_DIR=%s\nRUNSC_ROOT=%s\n' "$CTL_DIR" "$LAUNCH_DIR" "$RUNSC_ROOT"
    printf 'SCOPE_ARG=%s\n' "${SCOPE_ARGV[@]}"
    printf 'PODMAN_ARG=%s\n' "${PODMAN_ARGV[@]}"
    exit 0
fi

# --- 8. broker gate --------------------------------------------------------
# copied from spawn-tier2.sh:950-984, unify after Phase B. The broker treats
# qdistro.tier3s.spawn: as rules-only (A-ii); only an explicit "allow" passes.
command -v dbus-send >/dev/null 2>&1 || refuse "dbus-send not found; broker authorization required"
broker_gate() {
    local _action="$1" _label="$2" _out _status _reply
    _out=$(as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$_action" \
        "dict:string:string:" 2>&1)
    _status=$?
    _reply=$(printf '%s' "$_out" | tr -d ' \t\n')
    if [ "$_status" -ne 0 ]; then
        refuse "broker authorization failed for $_label (action='$_action')"
    fi
    case "$_reply" in
        allow|string\"allow\") ;;
        deny|string\"deny\")
            refuse "broker denied $_label (action='$_action' decision=deny)" ;;
        unknown|string\"unknown\"|"")
            refuse "broker has no allow rule for $_label (action='$_action' decision=unknown)" ;;
        *)
            refuse "broker returned unsupported verdict for $_label (action='$_action' reply='$_reply')" ;;
    esac
}
broker_gate "$SPAWN_ACTION" "$WORKLOAD/$APP_BASE"

# --- 9. activation record (only after allow) -------------------------------
if [ -n "$GENERATION" ]; then
    read_binding --record; rc=$?
    [ "$rc" -eq 0 ] || refuse "activation recording failed for silo $SILO (rc=$rc)"
    [ "$RB_GEN" = "$GENERATION" ] && [ "$RB_STATE" = "$STATE_PATH" ] \
        || refuse "binding for silo $SILO changed between resolution and activation ($GENERATION -> $RB_GEN)"
fi

# --- 10. reap stale launches, then the control record ----------------------
trusted_dir() {   # trusted_dir <dir> <mode>: real dir, ours, exact mode
    [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u %a' -- "$1")" = "$EUID $2" ]
}
trusted_dir "$CTL" 700 || refuse "$CTL is not a root 0700 directory (systemd-tmpfiles --create qdistro-tier3s.conf)"
trusted_dir "$LAUNCH_PARENT" 755 || refuse "$LAUNCH_PARENT is not a root 0755 directory (tmpfiles)"
[ -d "$RUNSC_BASE/$ADMIN_UID" ] && [ ! -L "$RUNSC_BASE/$ADMIN_UID" ] \
    || refuse "runsc state root $RUNSC_BASE/$ADMIN_UID is missing (tmpfiles; the probe checks it)"
"$CLEANUP" --reap-stale --except-unit "$UNIT" || say "WARN: reaping stale tier3s launches failed (see above); continuing with $TOKEN"

state_write() {   # state_write KEY=VALUE...: merge into $CTL_DIR/state atomically, under the lock
    (
        flock -w 60 9 || { say "cannot take $CTL/.lock"; exit 1; }
        declare -A S=(); local k v kv
        if [ -f "$CTL_DIR/state" ]; then
            while IFS='=' read -r k v; do [ -n "$k" ] && S["$k"]="$v"; done < "$CTL_DIR/state"
        fi
        for kv in "$@"; do S["${kv%%=*}"]="${kv#*=}"; done
        for k in "${!S[@]}"; do printf '%s=%s\n' "$k" "${S[$k]}"; done | LC_ALL=C sort > "$CTL_DIR/state.new.$$" \
            && mv -f "$CTL_DIR/state.new.$$" "$CTL_DIR/state"
    ) 9>"$CTL/.lock"
}
[ ! -e "$CTL_DIR" ] && [ ! -e "$LAUNCH_DIR" ] || refuse "token $TOKEN is already in use"
mkdir -m 0700 "$CTL_DIR" || refuse "cannot create $CTL_DIR"
CLEANED=0
run_cleanup() {
    [ "$CLEANED" = 0 ] || return 0
    CLEANED=1
    "$CLEANUP" "$TOKEN" || { say "cleanup of $TOKEN FAILED; control record $CTL_DIR preserved"; return 1; }
}
on_exit() {
    local rc=$?
    run_cleanup || [ "$rc" -ne 0 ] || rc=70
    exit "$rc"
}
trap on_exit EXIT
trap 'say "signal: tearing down $TOKEN"; exit 143' TERM INT HUP
state_write schema=1 "token=$TOKEN" "container=$CONTAINER" "unit=$UNIT" "scope_unit=$SCOPE_UNIT" \
    "admin_uid=$ADMIN_UID" "runsc_root=$RUNSC_ROOT" "per_launch_dir=/run/qdistro-tier3s/$TOKEN" phase=created \
    || refuse "cannot write the control record"
mkdir -m 0700 "$LAUNCH_DIR" && chown "$ADMIN_UID:$(id -g "$ADMIN_USER")" "$LAUNCH_DIR" \
    || refuse "cannot create the per-launch dir $LAUNCH_DIR"

# --- 11. image ---------------------------------------------------------------
pm image exists "$IMAGE" || refuse "image $IMAGE is not in admin's store (tier3s/make-tier3s-image.sh $WORKLOAD)"

# --- 12. scope + podman ----------------------------------------------------
printf 'LAUNCH_TOKEN=%s\nCONTAINER=%s\nIMAGE=%s\nSCOPE_UNIT=%s\n' "$TOKEN" "$CONTAINER" "$IMAGE" "$SCOPE_UNIT"
systemd-run "${SCOPE_ARGV[@]}" "${PODMAN_ARGV[@]}" &
child=$!
starttime() {   # field 22 of /proc/<pid>/stat
    local s; { read -r s < "$PROC/$1/stat"; } 2>/dev/null || return 1
    s="${s##*) }"; set -- $s; [ -n "${20:-}" ] && echo "${20}"
}
in_scope() {   # in_scope <pid> <scope cgroup rel>
    local c; c="$(sed -n 's/^0:://p' "$PROC/$1/cgroup" 2>/dev/null | head -1)"
    [ -n "$c" ] && { [ "$c" = "$2" ] || [[ "$c" == "$2"/* ]]; }
}
recorded=0
for _ in $(seq 1 240); do
    kill -0 "$child" 2>/dev/null || break
    read -r st spid cpid cid < <(pm inspect --format '{{.State.Status}} {{.State.Pid}} {{.State.ConmonPid}} {{.Id}}' "$CONTAINER" 2>/dev/null)
    if [ "${st:-}" = running ] && [[ "${spid:-}" =~ ^[1-9][0-9]*$ ]] && [[ "${cpid:-}" =~ ^[1-9][0-9]*$ ]]; then
        rel="$(systemctl show -p ControlGroup --value "$SCOPE_UNIT" 2>/dev/null)"
        [[ "$rel" == /*"/$SCOPE_UNIT" ]] || { say "scope $SCOPE_UNIT has no cgroup ('$rel')"; exit 2; }
        for p in "$spid" "$cpid"; do
            in_scope "$p" "$rel" || { say "runtime process $p is OUTSIDE the owning scope $rel; tearing down"; exit 2; }
        done
        sst="$(starttime "$spid")" && cst="$(starttime "$cpid")" || { say "cannot read starttime of $spid/$cpid"; exit 2; }
        state_write phase=running "container_id=$cid" "scope_cgroup=$rel" \
            "sentry_pid=$spid" "sentry_starttime=$sst" "conmon_pid=$cpid" "conmon_starttime=$cst" \
            || { say "cannot record the running launch"; exit 2; }
        say "running: $CONTAINER sentry=$spid conmon=$cpid in $rel"
        recorded=1
        break
    fi
    sleep 0.25
done
[ "$recorded" = 1 ] || kill -0 "$child" 2>/dev/null || say "podman exited before the launch was recorded"
if [ "$recorded" != 1 ] && kill -0 "$child" 2>/dev/null; then
    say "launch did not reach running within 60 s; tearing down"; exit 2
fi
wait "$child"
exit $?
