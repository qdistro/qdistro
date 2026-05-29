"""Fail-closed evaluation of workflow ``conditions`` against the firing
process.

``permissions.md`` §"Secret delivery to privileged tasks" requires that a
secret is "released only if the expected process (e.g. ``git`` in the
expected cgroup via ``qsu``) is asking, not any process with the right
uid." Conditions are the mechanism: a workflow declares

    conditions:
      - uid: dev
      - argv0: git

and the engine resolves the trigger's pid (from the process_spawn
watcher's ``trigger_context["pid"]``), reads ``/proc/<pid>``, and matches
every declared key. The match is **fail closed**: an unreadable /proc, a
recycled pid, an unknown condition key, or any unmatched value rejects the
run. A field that is *silently ignored* (the bug this module fixes) is the
worst outcome on the crown-jewel secret path, so there is no permissive
fallback.

Supported keys (each may be a scalar or a list of acceptable values; a
list matches if ANY entry matches):

    uid      real uid; int, or a username resolved via the passwd db
    gid      real gid; int, or a group name resolved via the group db
    exe      realpath of /proc/<pid>/exe; fnmatch glob
    argv0    argv[0]; matched against the full token AND its basename;
             ``argv[0]`` is accepted as an alias of this key
    comm     /proc/<pid>/comm (the kernel task name); fnmatch glob
    cgroup   the firing cgroup rel-path from trigger_context; fnmatch glob

Any other key is rejected (fail closed) rather than ignored.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from typing import Any

# Shared, fail-closed /proc readers (permission-lineage consolidation).
# The broker ships qdistro_proc_identity.py next to its other modules and
# loads workflow/ in-process, so it is importable as a top-level module.
import qdistro_proc_identity as _pi

logger = logging.getLogger("qdistro.workflow.conditions")

# Keys that require reading the firing process via /proc.
_PROCESS_KEYS = frozenset({"uid", "gid", "exe", "argv0", "argv[0]", "comm"})
# Keys answerable from the trigger context alone (no /proc needed).
_CONTEXT_KEYS = frozenset({"cgroup"})
_KNOWN_KEYS = _PROCESS_KEYS | _CONTEXT_KEYS


def _proc_starttime(pid: int) -> int | None:
    # Shared reader returns 0 (not None) on failure; preserve this
    # module's None-on-failure contract used by the recycled-pid guard.
    st = _pi.read_starttime(pid)
    return st if st != 0 else None


def _read_identity(pid: int) -> dict[str, Any] | None:
    """Read uid/gid/exe/argv0/comm for ``pid`` from /proc. None if gone."""
    return _pi.read_identity(pid)


def _resolve_uid(value: Any) -> int | None:
    return _pi.resolve_uid_name(value)


def _resolve_gid(value: Any) -> int | None:
    return _pi.resolve_gid_name(value)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _match_uid(actual: int, want: Any) -> bool:
    for v in _as_list(want):
        r = _resolve_uid(v)
        if r is not None and r == actual:
            return True
    return False


def _match_gid(actual: int, want: Any) -> bool:
    for v in _as_list(want):
        r = _resolve_gid(v)
        if r is not None and r == actual:
            return True
    return False


def _match_glob(actual: str, want: Any) -> bool:
    for v in _as_list(want):
        if fnmatch.fnmatch(actual, str(v)):
            return True
    return False


def _match_argv0(actual: str, want: Any) -> bool:
    base = os.path.basename(actual)
    for v in _as_list(want):
        pat = str(v)
        if fnmatch.fnmatch(actual, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def evaluate(conditions: list[dict[str, Any]],
             trigger_context: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(ok, reason)``. Empty conditions always pass.

    Fail closed: any unknown key, any /proc read failure when a process
    key is referenced, a recycled pid, or any unmatched condition yields
    ``(False, reason)``.
    """
    if not conditions:
        return True, ""

    # Flatten the list-of-dicts into (key, want) pairs; every pair must
    # match. (The design-doc shape is a list of single-key dicts, but a
    # multi-key dict is accepted and ANDed too.)
    pairs: list[tuple[str, Any]] = []
    for entry in conditions:
        if not isinstance(entry, dict):
            return False, f"condition entry is not a mapping: {entry!r}"
        for k, v in entry.items():
            key = str(k)
            if key not in _KNOWN_KEYS:
                return False, f"unknown condition key {key!r} (fail-closed)"
            pairs.append((key, v))

    needs_proc = any(k in _PROCESS_KEYS for k, _ in pairs)
    ident: dict[str, Any] | None = None
    pid: int | None = None
    if needs_proc:
        raw_pid = trigger_context.get("pid")
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            return False, ("conditions reference process attributes but the "
                           "trigger carries no pid (fail-closed)")
        if pid <= 0:
            return False, f"invalid trigger pid {pid}"
        # Anti-PID-reuse: the process must still be the one the trigger
        # captured. If starttime drifted, the original exited and the pid
        # was recycled — reject rather than gate on a stranger.
        expected_start = trigger_context.get("pid_starttime")
        if expected_start is not None:
            now_start = _proc_starttime(pid)
            if now_start is None:
                return False, f"trigger pid {pid} is gone (fail-closed)"
            if now_start != int(expected_start):
                return False, (f"trigger pid {pid} was recycled "
                               f"(starttime {now_start}!={expected_start})")
        ident = _read_identity(pid)
        if ident is None:
            return False, f"cannot read /proc/{pid} identity (fail-closed)"

    for key, want in pairs:
        if key == "uid":
            if not _match_uid(int(ident["uid"]), want):  # type: ignore[index]
                return False, f"uid {ident['uid']} != required {want!r}"
        elif key == "gid":
            if not _match_gid(int(ident["gid"]), want):  # type: ignore[index]
                return False, f"gid {ident['gid']} != required {want!r}"
        elif key in ("argv0", "argv[0]"):
            if not _match_argv0(str(ident["argv0"]), want):  # type: ignore[index]
                return False, f"argv0 {ident['argv0']!r} != required {want!r}"
        elif key == "exe":
            if not _match_glob(str(ident["exe"]), want):  # type: ignore[index]
                return False, f"exe {ident['exe']!r} != required {want!r}"
        elif key == "comm":
            if not _match_glob(str(ident["comm"]), want):  # type: ignore[index]
                return False, f"comm {ident['comm']!r} != required {want!r}"
        elif key == "cgroup":
            actual_cg = str(trigger_context.get("cgroup", ""))
            if not _match_glob(actual_cg, want):
                return False, f"cgroup {actual_cg!r} != required {want!r}"
        else:  # pragma: no cover - guarded by _KNOWN_KEYS above
            return False, f"unhandled condition key {key!r}"

    return True, ""


def derive_invoker_conditions(invokers: list[str],
                              conditions: list[dict[str, Any]]
                              ) -> list[dict[str, Any]]:
    """If ``roles`` names invoker identities but ``conditions`` does not
    already pin a uid, fold the invokers into a uid condition so the
    ``invoker`` role is actually enforced (F7). Returns a possibly-extended
    conditions list (does not mutate the input)."""
    if not invokers:
        return conditions
    has_uid = any("uid" in c for c in conditions if isinstance(c, dict))
    if has_uid:
        return conditions
    return list(conditions) + [{"uid": list(invokers)}]
