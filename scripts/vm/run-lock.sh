#!/usr/bin/env bash
# Shared host-wide exclusion for qci VM runs and the kiwi builder. Source this
# and call qdistro_run_lock_reexec before creating, deleting, or booting VMs.
# flock's parent owns the descriptor; -o closes it in the command so worker
# children cannot keep the lock after their runner exits.

qdistro_run_lock_reexec() {
    local script=$1 lock_dir lock_path rc holder
    shift
    # The re-executed command is a direct child of the flock guardian. A child
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
    # Open with append, never unlink or replace the inode. The child writes its
    # own PID only after the guardian holds the lock; stale text is diagnostic
    # only and never used to decide whether the lock may be stolen.
    if ! touch "$lock_path"; then
        printf 'run lock: cannot open %s\n' "$lock_path" >&2
        exit 90
    fi
    if QDISTRO_RUN_LOCK_PATH=$lock_path flock -n -E 98 -o "$lock_path" \
        bash -c 'export QDISTRO_RUN_LOCK_GUARD_PID=$PPID; printf "%s\n" "$$" > "$QDISTRO_RUN_LOCK_PATH" || exit 90; exec "$@"' \
        bash "$script" "$@"; then
        rc=0
    else
        rc=$?
    fi
    if [ "$rc" -eq 98 ]; then
        holder=$(head -n 1 "$lock_path" 2>/dev/null || true)
        [[ "$holder" =~ ^[0-9]+$ ]] || holder=unknown
        printf 'run lock: %s is held by pid %s; refusing concurrent VM work\n' "$lock_path" "$holder" >&2
    fi
    exit "$rc"
}
