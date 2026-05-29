"""Resolve a live process to an authoritative permission subject
(``issues/qdistro/permission-lineage-findings.md`` Phase 2).

A gate must never turn a *claimed* string (silo / sandbox_engine /
app_id) straight into a policy subject (finding P0-1). Instead it hands a
live ``pid`` to :func:`resolve_subject`, which:

1. Reads the live kernel facts from ``/proc`` (uid, exe, starttime,
   SELinux label, cgroup) via the shared :mod:`qdistro_proc_identity`
   readers — fail-closed.
2. Looks up the broker's launch record for ``(pid, starttime)``. The
   starttime match is the anti-PID-reuse anchor: a recycled PID cannot
   resolve to an old launcher's record.
3. Revalidates the record's kernel facts against the *live* values. Every
   axis that both sides supply must match; an axis missing on either side
   is *skipped* (not failed) so SELinux-off / unconfined hosts and
   launchers that couldn't read a field still resolve — the always-present
   floor is ``(pid, starttime)`` plus the record's existence.

Outcome — the :class:`Subject`:

- **verified** subject: a matching launch record exists and every shared
  axis agreed. ``silo`` / ``sandbox_engine`` / ``app_id`` are the
  *launcher-attested* values from the record, never the caller's claim.
- **unknown** subject (``verified is False``): no record, an expired
  record, a recycled PID, an unreadable ``/proc``, or any axis mismatch.
  It carries the live ``uid`` / ``exe`` (kernel-checked, still usable for
  the tier-0 admin-prompt path) but **empty** silo / sandbox_engine /
  app_id, so a forged claim can only ever *fail* a non-empty selector,
  never satisfy one. Failure posture (findings §"Failure posture"):
  isolate-as-unknown with zero cross-silo / privileged authority.

Pure-Python and dependency-free apart from the shared ``/proc`` readers,
so it unit-tests by monkeypatching those readers + feeding a store.
"""
from __future__ import annotations

from dataclasses import dataclass

import qdistro_proc_identity as _pi  # type: ignore[import-not-found]

# The silo string of an unresolved / unverified subject. Empty so it can
# never equal a real silo and never satisfy a non-empty rule selector.
UNKNOWN_SILO = ""


@dataclass(frozen=True)
class Subject:
    """An authoritative (or explicitly unknown) permission subject."""
    uid: int                 # live real uid (kernel-checked); -1 if gone
    exe: str                 # live /proc/<pid>/exe; "?" if unreadable
    silo: str                # launcher-attested silo, or UNKNOWN_SILO
    sandbox_engine: str      # launcher-attested, or "" when unverified
    app_id: str              # launcher-attested, or "" when unverified
    selinux_label: str       # live label ("" if SELinux off / gone)
    cgroup: str              # live cgroup ("" if unreadable)
    verified: bool           # True iff a launch record matched all axes
    record_id: str           # the matched record id, or ""
    reason: str              # human-readable why-not when not verified

    @property
    def is_unknown(self) -> bool:
        return not self.verified


def _unknown(uid: int, exe: str, label: str, cgroup: str,
             reason: str) -> Subject:
    return Subject(
        uid=uid, exe=exe, silo=UNKNOWN_SILO,
        sandbox_engine="", app_id="",
        selinux_label=label, cgroup=cgroup,
        verified=False, record_id="", reason=reason,
    )


def resolve_subject(pid: int, store) -> Subject:
    """Resolve live ``pid`` to a :class:`Subject` against ``store`` (a
    :class:`qdistro_launch_record.LaunchRecordStore`). Fail-closed: any
    read failure, missing/expired record, recycled PID, or axis mismatch
    yields an ``unknown`` subject. ``store`` may be ``None`` (no launch
    records wired yet) — then every caller resolves to ``unknown`` but
    still carries its live uid/exe, preserving the legacy tier-0 path.
    """
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return _unknown(-1, "?", "", "", "invalid-pid")
    if pid_i <= 0:
        return _unknown(-1, "?", "", "", "invalid-pid")

    exe, starttime = _pi.read_exe_and_starttime(pid_i)
    if starttime == 0:
        # Process gone / stat unreadable — cannot anchor anything.
        return _unknown(-1, exe, "", "", "proc-gone")

    live_uid = _pi.read_uid(pid_i)
    live_label = _pi.read_selinux_label(pid_i)
    live_cgroup = _pi.read_cgroup(pid_i)
    uid_val = int(live_uid) if live_uid is not None else -1

    if store is None:
        return _unknown(uid_val, exe, live_label, live_cgroup,
                        "no-launch-record-store")

    rec = store.find_by_proc(pid_i, starttime)
    if rec is None:
        return _unknown(uid_val, exe, live_label, live_cgroup,
                        "no-launch-record")

    # Revalidate the record's kernel facts against the live process.
    # starttime already matched (it is part of the lookup key).
    #
    # Fail-closed rule (findings §"Failure posture"; tightened after the
    # codex review of 2026-05-28): an axis is *skipped* only when the
    # RECORD does not carry it. When the record carries a fact, the live
    # process MUST present a matching value — a live value that is missing
    # where the record had one is itself suspicious (an unreadable /proc
    # field, or a SELinux-off live read against a record minted with a
    # label) and fails closed. This is what keeps an unreadable /proc from
    # yielding a verified subject. Records minted on a SELinux-off host
    # simply have an empty `selinux_label`, so that axis is skipped on both
    # sides — the SELinux-as-one-axis-not-sole-anchor posture (Q#3).
    mismatches: list[str] = []
    # uid: the cheapest always-present kernel fact (the broker runs as
    # root, so a live process always exposes /proc/<pid>/status). An
    # unreadable uid (None) therefore means "not a normally-readable live
    # process" → fail closed.
    if live_uid is None or int(rec.uid) != int(live_uid):
        mismatches.append(f"uid live={live_uid} record={rec.uid}")
    # exe: required when the record captured a real exe path.
    if rec.exe and rec.exe != "?":
        if not exe or exe == "?" or exe != rec.exe:
            mismatches.append(f"exe live={exe!r} record={rec.exe!r}")
    # SELinux label: required when the record captured one.
    if rec.selinux_label:
        if not live_label or live_label != rec.selinux_label:
            mismatches.append(
                f"label live={live_label!r} record={rec.selinux_label!r}")
    # cgroup: required when the record captured one.
    if rec.cgroup:
        if not live_cgroup or live_cgroup != rec.cgroup:
            mismatches.append(
                f"cgroup live={live_cgroup!r} record={rec.cgroup!r}")

    if mismatches:
        # A record exists but the live process diverged from it — treat as
        # unknown (fail closed) rather than trusting a half-matching record.
        return _unknown(uid_val, exe, live_label, live_cgroup,
                        "record-mismatch: " + "; ".join(mismatches))

    return Subject(
        uid=uid_val, exe=exe,
        silo=rec.silo,
        sandbox_engine=rec.sandbox_engine,
        app_id=rec.app_id,
        selinux_label=live_label, cgroup=live_cgroup,
        verified=True, record_id=rec.record_id, reason="ok",
    )
