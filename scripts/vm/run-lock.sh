#!/usr/bin/env bash
# Shared host-wide exclusion for qci VM runs and the kiwi builder. Source this
# and call qdistro_run_lock_reexec before creating, deleting, or booting VMs.
# The launcher owns the descriptor until the runner has finished signal cleanup;
# the runner and its workers receive it closed.

qdistro_run_lock_reexec() {
    local script=$1 lock_dir lock_path rc holder lock_fd child_pid="" pending_signal=""
    shift
    # The re-executed command is a direct child of this launcher. A child
    # command inheriting this environment has a different PPID and must acquire
    # its own lock.
    [ "${QDISTRO_RUN_LOCK_GUARD_PID:-}" = "$PPID" ] && return 0
    lock_dir=${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}
    if ! mkdir -p "$lock_dir"; then
        printf 'run lock: cannot create image directory %s\n' "$lock_dir" >&2
        exit 90
    fi
    lock_path=$lock_dir/.qdistro-vm-run.lock
    if ! command -v flock >/dev/null 2>&1; then
        printf 'run lock: flock(1) is required\n' >&2
        exit 90
    fi
    # Open with append, never unlink or replace the inode. Stale PID text is
    # diagnostic only and never used to decide whether the lock may be stolen.
    if ! { exec {lock_fd}>>"$lock_path"; }; then
        printf 'run lock: cannot open %s\n' "$lock_path" >&2
        exit 90
    fi
    if ! flock -n -x "$lock_fd"; then
        holder=$(head -n 1 "$lock_path" 2>/dev/null || true)
        [[ "$holder" =~ ^[0-9]+$ ]] || holder=unknown
        printf 'run lock: %s is held by pid %s; refusing concurrent VM work\n' "$lock_path" "$holder" >&2
        exit 98
    fi

    # Install handlers before starting the child. If a signal arrives between
    # fork and $! assignment, remember it and forward it once the PID is known.
    trap 'if [ -n "$child_pid" ]; then kill -INT "$child_pid" 2>/dev/null || true; else pending_signal=INT; fi' INT
    trap 'if [ -n "$child_pid" ]; then kill -TERM "$child_pid" 2>/dev/null || true; else pending_signal=TERM; fi' TERM
    trap 'if [ -n "$child_pid" ]; then kill -HUP "$child_pid" 2>/dev/null || true; else pending_signal=HUP; fi' HUP
    # Bash gives asynchronous children SIGINT=ignored. Reset it before exec so
    # qci's INT cleanup still runs on a process-group interrupt. Bash closes
    # the lock descriptor in this child; workers cannot inherit it.
    # CPython also sets SIGPIPE and SIGXFSZ to ignored at startup, and execv
    # keeps SIG_IGN, so without a restore every qci descendant would get EFBIG
    # instead of the SIGXFSZ kill a `ulimit -f` bound relies on. Hand the
    # runner this launcher's own dispositions for both (SigIgn is a hex mask,
    # bit N-1 = signal N).
    local sig_ign
    sig_ign=$(awk '$1 == "SigIgn:" { print $2 }' "/proc/$$/status" 2>/dev/null)
    [[ "$sig_ign" =~ ^[0-9a-fA-F]+$ ]] || sig_ign=0
    QDISTRO_RUN_LOCK_GUARD_PID=$$ python3 -c '
import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
ign = int(sys.argv[1], 16)
for sig in (signal.SIGPIPE, signal.SIGXFSZ):
    signal.signal(sig, signal.SIG_IGN if ign >> (sig - 1) & 1 else signal.SIG_DFL)
os.execv(sys.argv[2], sys.argv[2:])' \
        "$sig_ign" "$script" "$@" {lock_fd}>&- &
    child_pid=$!
    [ -z "$pending_signal" ] || kill -s "$pending_signal" "$child_pid" 2>/dev/null || true
    if ! printf '%s\n' "$child_pid" > "$lock_path"; then
        printf 'run lock: cannot write holder pid to %s\n' "$lock_path" >&2
    fi
    # A direct signal to the original launcher must reach the runner. A signal
    # to the process group may also reach it directly. In both cases we wait
    # for its cleanup before releasing our descriptor.
    while :; do
        if wait "$child_pid"; then rc=0; else rc=$?; fi
        # A trapped signal interrupts wait even while the child is cleaning
        # up. Wait again until the child actually exits.
        kill -0 "$child_pid" 2>/dev/null || break
    done
    exit "$rc"
}
