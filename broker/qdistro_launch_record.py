"""Broker-issued launch records — the authority record for permission
lineage (``issues/qdistro/permission-lineage-findings.md`` Phase 1).

The invariant the lineage work enforces is

    displayed/claimed silo == launcher record == live kernel identity
                            == policy subject

Before this module the broker had **no** authority record: at decision
time it could only trust a self-asserted secctx string (forgeable — see
finding P0-1) or qdwin's connect-time snapshot (clipboard/handoff only).
A launch record is the missing first link: a *trusted launcher*
(qsu/root-exec, the secctx-exec choke point, the tier sandbox launchers)
registers the process it is about to exec, recording the intended silo +
the kernel facts that bind the record to a specific live process. A later
gate resolves a live pid to this record and revalidates the kernel facts
before believing any silo / sandbox_engine / app_id (see
``qdistro_resolver``).

Design notes:

- **Anchored on ``(pid, starttime)``.** starttime (``/proc/<pid>/stat``
  field 22) is kernel-attested and changes when a PID is recycled, so a
  ``(pid, starttime)`` key cannot be satisfied by a different process that
  later inherits the same PID. This is the same anchor qsu and Option-B
  already rely on. A pidfd may be attached as belt-and-suspenders but the
  ``(pid, starttime)`` pair is the portable, always-present key.
- **Expiry.** Records expire (default 12h) so a long-dead launcher can't
  leave an authority record that a recycled PID later collides with; the
  starttime check already prevents the collision, expiry is depth.
- **No token in the process environment.** The record id is returned to
  the launcher for its own bookkeeping, but a gate never trusts a
  caller-presented id — it resolves by live ``(pid, starttime)``. So a
  leaked id grants nothing (it can't be replayed from another process).
- **Fail-closed.** Lookups that don't find a live, unexpired,
  starttime-matching record return ``None``; the resolver turns that into
  the ``unknown`` subject (deny cross-silo / privileged).

This store is pure-Python and dependency-free so it unit-tests without
D-Bus; the broker owns one instance and feeds it from ``RegisterLaunch``.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# Default record lifetime. Generous — a silo app can run for hours — but
# bounded so the table can't grow without limit and stale records age out.
DEFAULT_TTL_S = 12 * 3600

# Hard cap on simultaneously-live records. A buggy or hostile launcher
# spamming RegisterLaunch can't exhaust broker memory; oldest-expiring
# records are evicted first once the cap is hit (after a reap attempt).
MAX_RECORDS = 4096


@dataclass(frozen=True)
class LaunchRecord:
    """One trusted-launcher attestation that ``pid`` (pinned by
    ``starttime``) is the process the launcher intended for ``silo``.

    The secctx triple (``sandbox_engine`` / ``app_id`` / ``instance_id``)
    is what the launcher tagged via ``wp_security_context_v1``; the
    resolver returns these as the *authoritative* values so gates stop
    trusting the client-supplied copies. The kernel-fact fields
    (``uid`` / ``exe`` / ``selinux_label`` / ``cgroup``) are the evidence
    the resolver revalidates against the live process.
    """
    record_id: str
    silo: str
    sandbox_engine: str
    app_id: str
    instance_id: str
    uid: int
    pid: int
    starttime: int
    exe: str
    selinux_label: str
    cgroup: str
    # Optional namespace / container / VM identifier for tier-2+ launchers
    # that register the inner process (free-form; advisory).
    namespace: str = ""
    issued_at: float = 0.0
    expires_at: float = 0.0

    def is_expired(self, now: float) -> bool:
        return self.expires_at != 0.0 and now >= self.expires_at


class LaunchRecordStore:
    """Thread-safe in-memory launch-record store keyed by ``record_id``
    with a secondary ``(pid, starttime)`` index for resolver lookups.

    ``time_fn`` is injectable for deterministic tests.
    """

    def __init__(self, *, ttl_s: int = DEFAULT_TTL_S,
                 max_records: int = MAX_RECORDS,
                 time_fn: Callable[[], float] = time.time) -> None:
        self._ttl_s = int(ttl_s)
        self._max = int(max_records)
        self._time = time_fn
        self._lock = threading.Lock()
        self._by_id: dict[str, LaunchRecord] = {}
        # (pid, starttime) -> record_id. starttime is part of the key so a
        # recycled PID never aliases an old record.
        self._by_proc: dict[tuple[int, int], str] = {}

    # -- registration -------------------------------------------------
    def register(self, *, silo: str, uid: int, pid: int, starttime: int,
                 exe: str, selinux_label: str = "", cgroup: str = "",
                 sandbox_engine: str = "", app_id: str = "",
                 instance_id: str = "", namespace: str = "",
                 ttl_s: int | None = None) -> LaunchRecord:
        """Create + store a record. Returns the stored record (with its
        ``record_id``). A prior record for the same ``(pid, starttime)`` is
        replaced — re-registration is idempotent on the live process.

        The caller (broker ``RegisterLaunch``) is responsible for having
        re-verified the live ``(pid, starttime, uid, exe, …)`` against
        ``/proc`` *before* calling this; the store does not read ``/proc``
        itself (kept pure for testing).
        """
        now = self._time()
        ttl = self._ttl_s if ttl_s is None else int(ttl_s)
        rec = LaunchRecord(
            record_id=secrets.token_hex(16),
            silo=str(silo),
            sandbox_engine=str(sandbox_engine or ""),
            app_id=str(app_id or ""),
            instance_id=str(instance_id or ""),
            uid=int(uid),
            pid=int(pid),
            starttime=int(starttime),
            exe=str(exe or ""),
            selinux_label=str(selinux_label or ""),
            cgroup=str(cgroup or ""),
            namespace=str(namespace or ""),
            issued_at=now,
            expires_at=(now + ttl) if ttl > 0 else 0.0,
        )
        with self._lock:
            self._reap_locked(now)
            # Evict the previous record for this exact live process, if any.
            proc_key = (rec.pid, rec.starttime)
            old_id = self._by_proc.get(proc_key)
            if old_id is not None:
                self._by_id.pop(old_id, None)
            # Capacity backstop: if still at the cap after reaping, drop the
            # soonest-to-expire record (oldest issued among never-expiring).
            if len(self._by_id) >= self._max:
                self._evict_one_locked()
            self._by_id[rec.record_id] = rec
            self._by_proc[proc_key] = rec.record_id
        return rec

    # -- lookup -------------------------------------------------------
    def get(self, record_id: str) -> LaunchRecord | None:
        """Return a live, unexpired record by id, else ``None``."""
        now = self._time()
        with self._lock:
            rec = self._by_id.get(str(record_id))
            if rec is None:
                return None
            if rec.is_expired(now):
                self._drop_locked(rec)
                return None
            return rec

    def find_by_proc(self, pid: int, starttime: int) -> LaunchRecord | None:
        """Return the live, unexpired, starttime-matching record for
        ``(pid, starttime)``, else ``None``. This is the resolver entry
        point; the starttime match is the anti-PID-reuse guarantee.
        """
        now = self._time()
        with self._lock:
            rid = self._by_proc.get((int(pid), int(starttime)))
            if rid is None:
                return None
            rec = self._by_id.get(rid)
            if rec is None:
                self._by_proc.pop((int(pid), int(starttime)), None)
                return None
            if rec.is_expired(now):
                self._drop_locked(rec)
                return None
            return rec

    def revoke_proc(self, pid: int, starttime: int) -> bool:
        """Explicitly drop the record for a live process (e.g. on exit).
        Returns True if a record was removed."""
        with self._lock:
            rid = self._by_proc.pop((int(pid), int(starttime)), None)
            if rid is None:
                return False
            self._by_id.pop(rid, None)
            return True

    def reap_expired(self) -> int:
        """Drop all expired records. Returns the number removed. Safe to
        call on a timer."""
        now = self._time()
        with self._lock:
            return self._reap_locked(now)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)

    # -- internals (lock held) ----------------------------------------
    def _drop_locked(self, rec: LaunchRecord) -> None:
        self._by_id.pop(rec.record_id, None)
        key = (rec.pid, rec.starttime)
        if self._by_proc.get(key) == rec.record_id:
            self._by_proc.pop(key, None)

    def _reap_locked(self, now: float) -> int:
        expired = [r for r in self._by_id.values() if r.is_expired(now)]
        for r in expired:
            self._drop_locked(r)
        return len(expired)

    def _evict_one_locked(self) -> None:
        # Pick the record with the smallest non-zero expires_at (soonest to
        # die); fall back to the oldest issued_at when all are immortal.
        if not self._by_id:
            return
        def _key(r: LaunchRecord) -> tuple[float, float]:
            exp = r.expires_at if r.expires_at != 0.0 else float("inf")
            return (exp, r.issued_at)
        victim = min(self._by_id.values(), key=_key)
        self._drop_locked(victim)
