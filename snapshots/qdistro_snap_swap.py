"""qdistro-snap-swap — crash-consistent restore of a state subvolume from a
snapshot (fableplan2 task 05; doc/filesystem.md §"Rollback semantics").

This is the shared "roll back the whole silo" primitive: the template
rollback flow (qdistro-template-promote --rollback --restore-state) and the
admin-app "Roll back this user (full)" action both call it. It does NOT pick
WHICH snapshot to restore or touch any binding — it is handed a snapshot path
and a live state path and makes the snapshot's contents become the live state
without ever leaving state_path missing.

    qdistro-snap-swap restore <snapshot_path> <state_path> [--mechanism M]
    qdistro-snap-swap recover <state_path>

Algorithm (the order is the whole point — a crash at any step leaves
state_path either the old contents or the restored contents, never absent;
task 01 makes an absent state_path a hard launch error):

  1. Materialize a WRITABLE clone of the (read-only) snapshot into a SIBLING
     temp path next to state_path:
       - mechanism=subvolume: ``btrfs subvolume snapshot <snap> <temp>`` — a
         writable clone of the RO snapshot, reflinked, no data copy.
       - mechanism=copy: ``cp -a --reflink=auto <snap> <temp>`` — reflink when
         the fs supports it, full copy otherwise.
  2. Verify the clone (exists, is a directory, is writable). A bad clone is
     removed and the swap aborts — state_path is untouched.
  3. fsync the parent directory so the clone is durable before the swap.
  4. Swap temp into place:
       - When renameat2(RENAME_EXCHANGE) is available, atomically exchange
         temp and state (state := clone, temp := old state), then rename the
         old state aside to ``state-rejected-<ts>``. The exchange is atomic:
         state_path is never missing.
       - Otherwise two ordinary renames (state -> state-rejected-<ts>,
         temp -> state) bracketed by a journal marker so a crash in the gap
         is recoverable.
  5. The displaced old state is kept as ``state-rejected-<ts>`` — this flow
     never deletes it (retention/GC owns that).

A journal marker ``<state_path>.swap-pending.toml`` records the in-flight
swap so :func:`recover` (run at the start of every swap, and exposed as the
``recover`` subcommand) can finish or abort a swap interrupted by a crash.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import os
import subprocess
import sys
import time

# renameat2 errnos that mean "this kernel/filesystem does not support
# RENAME_EXCHANGE" — the ONLY case where it is safe to downgrade to the
# two-rename swap. Any other errno is a real failure and must abort.
_EXCHANGE_UNSUPPORTED = {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP,
                         getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}

# Marker phases, written to the journal before the step they guard.
_PHASE_MATERIALIZED = "materialized"   # clone built in temp; state not moved
_PHASE_EXCHANGED = "exchanged"         # RENAME_EXCHANGE done; temp holds old state
_PHASE_MOVED = "moved"                 # two-rename: state->rejected done or pending


class SnapSwapError(Exception):
    """A swap could not be completed safely. state_path is left intact."""


def log(msg: str) -> None:
    print(f"[snap-swap] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# tiny TOML-ish marker I/O (no pip deps; the marker is our own private format)
# --------------------------------------------------------------------------

def _marker_path(state_path: str) -> str:
    return state_path.rstrip("/") + ".swap-pending.toml"


def _write_marker(state_path: str, fields: dict) -> None:
    path = _marker_path(state_path)
    lines = []
    for k, v in fields.items():
        lines.append(f"{k} = {str(v)!r}")
    data = "\n".join(lines) + "\n"
    # Atomic: temp + rename in the same dir, fsync both file and dir so the
    # marker is durable before the rename it guards happens.
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    _fsync_dir(os.path.dirname(path) or ".")


def _read_marker(state_path: str) -> dict | None:
    path = _marker_path(state_path)
    if not os.path.isfile(path):
        return None
    out: dict = {}
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[k] = v
    return out


def _clear_marker(state_path: str) -> None:
    try:
        os.unlink(_marker_path(state_path))
    except FileNotFoundError:
        pass
    _fsync_dir(os.path.dirname(state_path) or ".")


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# renameat2(RENAME_EXCHANGE) — atomic two-path swap when the kernel/fs offer it
# --------------------------------------------------------------------------

_RENAME_EXCHANGE = 1 << 1  # linux/fs.h
_AT_FDCWD = -100


def _renameat2_exchange(old: str, new: str) -> None:
    """Atomically exchange two existing paths. Raises OSError (ENOSYS/EINVAL
    when the running kernel or filesystem lacks RENAME_EXCHANGE) so the caller
    can fall back to the two-rename journal path."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.renameat2.restype = ctypes.c_int
    libc.renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p,
                               ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    res = libc.renameat2(_AT_FDCWD, os.fsencode(old),
                         _AT_FDCWD, os.fsencode(new), _RENAME_EXCHANGE)
    if res != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), old, None, new)


def exchange_supported() -> bool:
    """Best-effort probe: does this build expose renameat2 at all? The real
    ENOSYS/EINVAL fallback still happens per-call (a filesystem may lack
    RENAME_EXCHANGE even when the syscall exists)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return hasattr(libc, "renameat2")
    except OSError:
        return False


# --------------------------------------------------------------------------
# clone materialization
# --------------------------------------------------------------------------

def _materialize_clone(snapshot_path: str, temp_path: str,
                       mechanism: str) -> None:
    """Create a writable clone of ``snapshot_path`` at ``temp_path``."""
    if mechanism == "subvolume":
        btrfs = _which("btrfs")
        if not btrfs:
            raise SnapSwapError(
                "mechanism=subvolume but the btrfs CLI is not installed")
        proc = subprocess.run(
            [btrfs, "subvolume", "snapshot", snapshot_path, temp_path],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise SnapSwapError(
                f"btrfs subvolume snapshot failed: {proc.stderr.strip()}")
    elif mechanism == "copy":
        # cp -a preserves ownership/mode/timestamps/ACLs/xattrs; --reflink=auto
        # gives a CoW copy on a reflink-capable fs, a full copy otherwise.
        proc = subprocess.run(
            ["cp", "-a", "--reflink=auto", snapshot_path, temp_path],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise SnapSwapError(f"cp -a clone failed: {proc.stderr.strip()}")
    else:
        raise SnapSwapError(f"unknown mechanism {mechanism!r}")


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def _verify_clone(temp_path: str) -> None:
    if not os.path.isdir(temp_path):
        raise SnapSwapError(f"clone {temp_path} is not a directory")
    if not os.access(temp_path, os.W_OK):
        raise SnapSwapError(f"clone {temp_path} is not writable")


# --------------------------------------------------------------------------
# recovery — finish or abort an interrupted swap, idempotently
# --------------------------------------------------------------------------

def recover(state_path: str) -> str:
    """Make state_path consistent after a crash mid-swap, idempotently.

    Returns a short word describing what happened: ``none`` (no journal),
    ``aborted`` (restore not yet committed; old state retained),
    ``completed`` (restore finished; clone is now state), or ``cleaned``
    (post-exchange cleanup finished). state_path is guaranteed present on
    return (or the underlying error is raised)."""
    marker = _read_marker(state_path)
    if marker is None:
        return "none"
    phase = marker.get("phase", "")
    temp = marker.get("temp", "")
    rejected = marker.get("rejected", "")

    if phase == _PHASE_MATERIALIZED:
        # The clone was built but never swapped in. Old state is intact at
        # state_path. Abort: drop the orphan clone.
        if temp and os.path.lexists(temp):
            _rmtree(temp)
        _clear_marker(state_path)
        return "aborted"

    if phase == _PHASE_EXCHANGED:
        # The exchanged phase is written BEFORE the atomic RENAME_EXCHANGE, so
        # recovery must work whether or not the exchange actually happened —
        # and it must NEVER touch state_path. In BOTH cases state_path holds a
        # complete tree (the restored clone if the exchange ran, the old state
        # if it did not) and `temp` holds "the thing to set aside as rejected"
        # (the old state, or the unused clone). Moving only temp -> rejected is
        # therefore always safe and never leaves state_path missing.
        if temp and os.path.lexists(temp):
            dest = rejected
            if not dest or os.path.lexists(dest):
                dest = _free_rejected(state_path, rejected)
            os.rename(temp, dest)
            _fsync_dir(os.path.dirname(state_path) or ".")
        _clear_marker(state_path)
        return "cleaned"

    if phase == _PHASE_MOVED:
        # Two-rename path. Either we crashed before state->rejected (state
        # still present) or between the two renames (state absent, clone in
        # temp).
        if os.path.lexists(state_path):
            # state->rejected has NOT happened. Abort: drop the clone.
            if temp and os.path.lexists(temp):
                _rmtree(temp)
            _clear_marker(state_path)
            return "aborted"
        # state is absent: state->rejected happened. Complete temp->state.
        if temp and os.path.lexists(temp):
            os.rename(temp, state_path)
            _fsync_dir(os.path.dirname(state_path) or ".")
            _clear_marker(state_path)
            return "completed"
        # No temp and no state: fall back to the displaced copy so state_path
        # is never left missing (last-resort consistency).
        if rejected and os.path.lexists(rejected) and not os.path.lexists(state_path):
            os.rename(rejected, state_path)
            _fsync_dir(os.path.dirname(state_path) or ".")
            _clear_marker(state_path)
            return "completed"
        raise SnapSwapError(
            f"cannot recover {state_path}: journal phase={phase} but neither "
            f"temp {temp!r} nor rejected {rejected!r} is present")

    raise SnapSwapError(f"unknown journal phase {phase!r} for {state_path}")


def _rmtree(path: str) -> None:
    """Remove a clone left behind by an aborted swap. Tries btrfs subvolume
    delete first (a subvolume cannot be rmtree'd if it has nested subvolumes,
    but our clones are flat), then a plain recursive remove."""
    btrfs = _which("btrfs")
    if btrfs:
        rc = subprocess.run([btrfs, "subvolume", "delete", path],
                            capture_output=True, text=True)
        if rc.returncode == 0:
            return
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# the swap
# --------------------------------------------------------------------------

def _sibling_temp(state_path: str, now: float) -> str:
    parent = os.path.dirname(state_path.rstrip("/")) or "/"
    base = os.path.basename(state_path.rstrip("/"))
    # pid + ms keep concurrent/back-to-back swaps from colliding on one second.
    return os.path.join(
        parent, f".{base}.snap-swap-{int(now * 1000)}-{os.getpid()}")


def _rejected_path(state_path: str, now: float) -> str:
    base = state_path.rstrip("/")
    cand = base + f"-rejected-{int(now)}"
    n = 0
    while os.path.lexists(cand):
        n += 1
        cand = base + f"-rejected-{int(now)}-{n}"
    return cand


def _free_rejected(state_path: str, preferred: str) -> str:
    """A non-colliding rejected path during recovery (the journal's preferred
    name may already be taken by a prior swap)."""
    if preferred and not os.path.lexists(preferred):
        return preferred
    base = state_path.rstrip("/") + "-rejected-recovered"
    cand = base
    n = 0
    while os.path.lexists(cand):
        n += 1
        cand = f"{base}-{n}"
    return cand


def restore(snapshot_path: str, state_path: str, *, mechanism: str = "copy",
            now: float | None = None, allow_exchange: bool = True) -> dict:
    """Restore ``snapshot_path`` into ``state_path`` crash-consistently.

    Returns a dict with the ``rejected`` path (where the displaced state now
    lives), the ``mechanism`` used, and the swap ``method``
    (``exchange``/``two-rename``)."""
    now = time.time() if now is None else now
    snapshot_path = os.path.abspath(snapshot_path)
    state_path = os.path.abspath(state_path)
    if not os.path.isdir(snapshot_path):
        raise SnapSwapError(f"snapshot {snapshot_path} is not a directory")

    # First, finish any swap a prior crash left half-done. The displaced state
    # from THAT swap stays put; we only make state_path consistent.
    recover(state_path)
    if not os.path.lexists(state_path):
        raise SnapSwapError(
            f"state_path {state_path} is missing and no journal exists to "
            f"recover it — refusing to invent state")

    temp = _sibling_temp(state_path, now)
    rejected = _rejected_path(state_path, now)
    if os.path.lexists(temp):
        _rmtree(temp)

    # Step 1+2: build the writable clone in a sibling temp, then verify it.
    _materialize_clone(snapshot_path, temp, mechanism)
    try:
        _verify_clone(temp)
    except SnapSwapError:
        _rmtree(temp)
        raise
    # Step 3: durability before the swap.
    _fsync_dir(os.path.dirname(state_path) or ".")

    journal = {"temp": temp, "rejected": rejected, "snapshot": snapshot_path}
    # The clone exists but is not yet live; record that so a crash here aborts
    # cleanly (old state is still at state_path).
    _write_marker(state_path, {"phase": _PHASE_MATERIALIZED, **journal})

    if allow_exchange and exchange_supported():
        # Write the exchanged phase BEFORE the (atomic) syscall: recovery of
        # this phase only ever moves temp -> rejected and never touches
        # state_path, so it is correct whether or not the exchange ran.
        _write_marker(state_path, {"phase": _PHASE_EXCHANGED, **journal})
        try:
            _renameat2_exchange(temp, state_path)
        except OSError as exc:
            if exc.errno not in _EXCHANGE_UNSUPPORTED:
                # A real failure (EIO/EACCES/ENOSPC/…). The exchange did not
                # take effect (state_path still holds the old state); leave the
                # exchanged journal so recover() safely sets temp aside, and
                # abort rather than silently downgrading to the less-atomic
                # two-rename swap.
                raise SnapSwapError(
                    f"RENAME_EXCHANGE failed ({exc}); state_path left intact, "
                    f"run `qdistro-snap-swap recover {state_path}`") from exc
            # Genuinely unsupported on this fs: nothing was swapped. Re-stamp
            # the journal to the two-rename phase and fall through.
            _write_marker(state_path, {"phase": _PHASE_MOVED, **journal})
            log(f"RENAME_EXCHANGE unsupported ({exc}); using two-rename swap")
        else:
            # state_path is now the clone; temp holds the old state. Set it
            # aside as state-rejected-<ts> and clear the journal.
            os.rename(temp, rejected)
            _fsync_dir(os.path.dirname(state_path) or ".")
            _clear_marker(state_path)
            return {"rejected": rejected, "mechanism": mechanism,
                    "method": "exchange"}
    else:
        _write_marker(state_path, {"phase": _PHASE_MOVED, **journal})

    # Two-rename path with a journal so a crash between the renames recovers.
    os.rename(state_path, rejected)
    os.rename(temp, state_path)
    _fsync_dir(os.path.dirname(state_path) or ".")
    _clear_marker(state_path)
    return {"rejected": rejected, "mechanism": mechanism, "method": "two-rename"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-snap-swap")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_restore = sub.add_parser("restore", help="restore a snapshot into a "
                               "state path, crash-consistently")
    p_restore.add_argument("snapshot_path")
    p_restore.add_argument("state_path")
    p_restore.add_argument("--mechanism", choices=("subvolume", "copy"),
                           default="copy")
    p_restore.add_argument("--no-exchange", action="store_true",
                           help="force the two-rename swap (test/diagnostic)")
    p_recover = sub.add_parser("recover", help="finish/abort an interrupted "
                               "swap so state_path is consistent")
    p_recover.add_argument("state_path")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "restore":
            result = restore(args.snapshot_path, args.state_path,
                             mechanism=args.mechanism,
                             allow_exchange=not args.no_exchange)
            log(f"restored {args.snapshot_path} -> {args.state_path} "
                f"(method={result['method']}, displaced state kept at "
                f"{result['rejected']})")
            print(result["rejected"])
            return 0
        outcome = recover(args.state_path)
        log(f"recover {args.state_path}: {outcome}")
        return 0
    except SnapSwapError as exc:
        log(f"FATAL: {exc}")
        return 1
    except OSError as exc:
        log(f"FATAL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
