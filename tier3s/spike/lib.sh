# tier3s/spike/lib.sh — shared helpers for the Phase S spike scripts.
# THROWAWAY (Phase S only). Sourced by scripts that run as root INSIDE the
# dev test VM; nothing here runs on the host. Every podman/runsc step runs as
# admin (uid 1000) via runuser, the dev-profile direct-admin lane (no secctx
# on the sandbox side, 03 Phase S).
set -u
ADMIN=admin
ADMIN_UID=1000
ADMIN_RT=/run/user/$ADMIN_UID
WRAPPER=/usr/libexec/qdistro/tier3s-runsc
RUNSC=/usr/libexec/qdistro/runsc/runsc
IMG=localhost/tier3s-spike/tw-terminals:20260929
SPIKE_SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Work dir readable by admin (/root is 0700 in the VM, so the seccomp JSON
# and anything podman-as-admin reads is copied here).
WORK=/var/tmp/tier3s-spike
mkdir -p "$WORK"
chmod 0755 "$WORK"
SMOKE=$WORK/smoke.json
install -m 0644 "$SPIKE_SRC/smoke.json" "$SMOKE"

# Run a command as admin with a scrubbed environment (no caller env leaks
# into podman or the runtime wrapper).
as_admin() {
    runuser -u "$ADMIN" -- env -i PATH=/usr/bin:/bin HOME=/home/$ADMIN \
        USER=$ADMIN LOGNAME=$ADMIN XDG_RUNTIME_DIR=$ADMIN_RT \
        DBUS_SESSION_BUS_ADDRESS=unix:path=$ADMIN_RT/bus "$@"
}

say() { printf '\n## %s\n' "$*"; }

# runsc's state root. runsc defaults it to $XDG_RUNTIME_DIR/runsc, else
# /var/run/runsc (runsc/config/flags.go DefaultRootDir); the Phase 0 wrapper's
# `env -i` strips XDG_RUNTIME_DIR, so the exact 03 command fails with
# "mkdir /var/run/runsc: permission denied" (logged in s1 section A0). The
# spike passes the root as a per-launch runtime flag, and must then pass the
# SAME global flags to every later podman call on that container.
T3S_ROOTFLAG=--runtime-flag=root=$ADMIN_RT/runsc

# podman global options for tier 3s (everything before the subcommand).
t3s_global() {
    T3S_GLOBAL=(podman --runtime "$WRAPPER" --runtime-flag=network=none)
    [ -n "${T3S_ROOTFLAG:-}" ] && T3S_GLOBAL+=("$T3S_ROOTFLAG")
    T3S_GLOBAL+=("${T3S_RTFLAGS[@]}")
}

# The Phase S step-1 podman flag set, verbatim from 03 (minus image/argv),
# plus $T3S_ROOTFLAG. Callers append extra --runtime-flag values BEFORE
# `run` via T3S_RTFLAGS and extra run options via T3S_RUNOPTS.
t3s_podman_argv() {
    t3s_global
    T3S_ARGV=("${T3S_GLOBAL[@]}")
    T3S_ARGV+=(run --rm
        --security-opt label=disable --security-opt no-new-privileges
        --security-opt "seccomp=$SMOKE"
        --cap-drop=ALL --userns=keep-id --user 1000:1000 --read-only
        --tmpfs /tmp:size=64m --tmpfs /run/user/1000:rw,U,mode=0700
        --network=none)
    T3S_ARGV+=("${T3S_RUNOPTS[@]}")
}
T3S_RTFLAGS=()
T3S_RUNOPTS=()

PIN=$SPIKE_SRC/../RUNSC_RELEASE
# Which pinned file (RUNSC_RELEASE key) a pid's executable is, by sha512 of
# /proc/<pid>/exe (the running inode, not the path). "-" if none.
pin_key_of() {
    local h
    h=$(sha512sum < "/proc/$1/exe" 2>/dev/null | cut -d' ' -f1) || { echo "?"; return; }
    awk -F= -v h="$h" '$1 ~ /_sha512$/ && $2 == h { print $1; f=1 } END { if (!f) print "-" }' "$PIN"
}

# Print every descendant of $1 (inclusive) with exe, pin key, cgroup, cmdline.
proc_report() {
    local root=$1 p kids
    local -a q=("$root")
    while [ ${#q[@]} -gt 0 ]; do
        p=${q[0]}; q=("${q[@]:1}")
        [ -d /proc/$p ] || continue
        printf 'pid=%s ppid=%s uid=%s comm=%s exe=%s pin=%s cgroup=%s\n    cmdline=%s\n' "$p" \
            "$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null)" \
            "$(awk '/^Uid:/{print $2}' /proc/$p/status 2>/dev/null)" \
            "$(cat /proc/$p/comm 2>/dev/null)" \
            "$(readlink /proc/$p/exe 2>/dev/null || echo '?')" \
            "$(pin_key_of "$p")" \
            "$(sed 's/^0:://' /proc/$p/cgroup 2>/dev/null)" \
            "$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | cut -c1-240)"
        kids=$(cat /proc/$p/task/*/children 2>/dev/null)
        for k in $kids; do q+=("$k"); done
    done
}

# Recursive cgroup.procs of a cgroup v2 path (relative to /sys/fs/cgroup).
cgroup_tree_procs() {
    local base=/sys/fs/cgroup/${1#/} d p
    [ -d "$base" ] || { echo "(no cgroup $base)"; return 1; }
    find "$base" -type d | sort | while read -r d; do
        for p in $(cat "$d/cgroup.procs" 2>/dev/null); do
            printf '%s\tpid=%s comm=%s exe=%s\n' "${d#/sys/fs/cgroup}" "$p" \
                "$(cat /proc/$p/comm 2>/dev/null)" "$(readlink /proc/$p/exe 2>/dev/null)"
        done
    done
}
