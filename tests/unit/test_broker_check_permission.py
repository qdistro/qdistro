"""Tests for qdistro_admin_broker.Broker.CheckPermission.

CheckPermission is the synchronous "fast-path" gate added for §6.5 S4:
rules-first, cache-second, returns "allow" / "deny" / "unknown" without
touching the admin prompt queue. bats scenarios under
tests/integration/vm/compositor-shell.bats drive the end-to-end qdshell→broker
wiring; this file pins the broker-side semantics in isolation so
regressions surface before a VM round-trip.

Model mirrors test_broker_sendto.py: subclass Broker to bypass the
dbus.service.Object setup and the GLib timers, then call methods
directly.
"""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")  # harness needs real dbus-python

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402


ADMIN_UID = B.ADMIN_UID  # 1000
NON_ADMIN_UID = 2000
PEER_EXE = "/usr/bin/qdshell"
STREAM_ACTION = "qdistro.view-stream.subscribe:qnotebook"


class _StubBroker(Broker):
    """Minimal Broker that skips real dbus registration.

    Tight ratelimit option lets the rate-limit test drive a reject
    without hammering the API. Defaults match the production broker's
    own resolution order.
    """

    def __init__(self, cache_db: str, audit_db: str, rules_dir: str,
                 *, ratelimit_limit: int = 10_000,
                 ratelimit_window_s: float = 1.0):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=ratelimit_limit,
                                     window_s=ratelimit_window_s)
        self._audit_retention_days = 0
        self._io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stub-broker-io")
        self._peer_uid = NON_ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = PEER_EXE
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        self.rules_reloaded_signals: list[int] = []

    def set_peer(self, uid: int, pid: int = 100, exe: str = PEER_EXE,
                 start: int = 0) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_start = start

    # Route peer lookup through the local shim so tests don't hit
    # org.freedesktop.DBus.
    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    # Capture signals in-process.
    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))

    def RulesReloaded(self, rule_count):  # type: ignore[override]
        self.rules_reloaded_signals.append(int(rule_count))


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def broker(tmp_path: Path, rules_dir: Path) -> _StubBroker:
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


def _write_rule(rules_dir: Path, *, decision: str, action: str,
                uid: int | None = None, exe: str | None = None,
                name: str = "r") -> None:
    match: list[str] = []
    match.append(f"    action: {action!r}")
    if uid is not None:
        match.append(f"    uid: {uid}")
    if exe is not None:
        match.append(f"    exe: {exe!r}")
    (rules_dir / f"{name}.yaml").write_text(
        f"- name: {name}\n"
        f"  decision: {decision}\n"
        f"  match:\n" + "\n".join(match) + "\n"
    )


# --- resolution order ------------------------------------------------------

class TestCheckPermissionResolution:
    def test_rule_allow_returns_allow(self, broker, rules_dir):
        _write_rule(rules_dir, decision="allow",
                    action=STREAM_ACTION, uid=NON_ADMIN_UID)
        broker.rules.reload()
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(STREAM_ACTION, {}) == "allow"

    def test_rule_deny_returns_deny(self, broker, rules_dir):
        _write_rule(rules_dir, decision="deny",
                    action=STREAM_ACTION, uid=NON_ADMIN_UID)
        broker.rules.reload()
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(STREAM_ACTION, {}) == "deny"

    def test_cache_allow_returns_allow_when_no_rule(self, broker):
        broker.cache.store(NON_ADMIN_UID, STREAM_ACTION, PEER_EXE,
                           "forever", True, ADMIN_UID)
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(STREAM_ACTION, {}) == "allow"

    def test_cache_deny_returns_deny_when_no_rule(self, broker):
        broker.cache.store(NON_ADMIN_UID, STREAM_ACTION, PEER_EXE,
                           "forever", False, ADMIN_UID)
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(STREAM_ACTION, {}) == "deny"

    def test_no_match_returns_unknown(self, broker):
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(STREAM_ACTION, {}) == "unknown"

    def test_rule_beats_cache(self, broker, rules_dir):
        """Rules are authoritative; cache never overrides."""
        _write_rule(rules_dir, decision="deny",
                    action=STREAM_ACTION, uid=NON_ADMIN_UID)
        broker.rules.reload()
        broker.cache.store(NON_ADMIN_UID, STREAM_ACTION, PEER_EXE,
                           "forever", True, ADMIN_UID)
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(STREAM_ACTION, {}) == "deny"

    def test_tier1_spawn_ignores_cache_without_rule(self, broker):
        action = "qdistro.tier1.spawn:/usr/bin/true"
        broker.cache.store(NON_ADMIN_UID, action, PEER_EXE,
                           "forever", True, ADMIN_UID)
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(action, {}) == "unknown"

    def test_tier1_spawn_rule_allow_still_allows(self, broker, rules_dir):
        action = "qdistro.tier1.spawn:/usr/bin/true"
        _write_rule(rules_dir, decision="allow",
                    action=action, uid=NON_ADMIN_UID)
        broker.rules.reload()
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(action, {}) == "allow"


# --- exe sensitivity (cache keys on exe via argv_exact) -------------------

class TestCheckPermissionExeKey:
    def test_cache_row_scoped_to_exe(self, broker):
        """forever scope stores as match_kind='always' (exe-agnostic);
        1h with argv stores as argv_exact. Pinning argv_exact here
        means a 1h grant must NOT cover a different exe."""
        broker.cache.store(NON_ADMIN_UID, STREAM_ACTION, PEER_EXE,
                           "1h", True, ADMIN_UID, argv=[PEER_EXE])
        broker.set_peer(uid=NON_ADMIN_UID, exe=PEER_EXE)
        assert broker.CheckPermission(
            STREAM_ACTION, {"argv[00]": PEER_EXE}) == "allow"
        broker.set_peer(uid=NON_ADMIN_UID, exe="/usr/bin/other")
        assert broker.CheckPermission(
            STREAM_ACTION, {"argv[00]": PEER_EXE}) == "unknown"

    def test_forever_is_exe_agnostic(self, broker):
        """forever scope uses match_kind='always' — exe doesn't matter."""
        broker.cache.store(NON_ADMIN_UID, STREAM_ACTION, PEER_EXE,
                           "forever", True, ADMIN_UID)
        broker.set_peer(uid=NON_ADMIN_UID, exe="/usr/bin/anything-else")
        assert broker.CheckPermission(STREAM_ACTION, {}) == "allow"


# --- purity: CheckPermission never mutates visible state -----------------

class TestCheckPermissionPurity:
    def test_unknown_does_not_enqueue_prompt(self, broker):
        broker.set_peer(uid=NON_ADMIN_UID)
        broker.CheckPermission(STREAM_ACTION, {})
        assert broker._pending == {}
        assert broker.pending_signals == []

    def test_allow_does_not_enqueue_prompt(self, broker, rules_dir):
        _write_rule(rules_dir, decision="allow",
                    action=STREAM_ACTION, uid=NON_ADMIN_UID)
        broker.rules.reload()
        broker.set_peer(uid=NON_ADMIN_UID)
        broker.CheckPermission(STREAM_ACTION, {})
        assert broker._pending == {}

    def test_unknown_does_not_write_cache(self, broker):
        broker.set_peer(uid=NON_ADMIN_UID)
        before = len(broker.cache.list_all())
        broker.CheckPermission(STREAM_ACTION, {})
        assert len(broker.cache.list_all()) == before

    def test_allow_does_not_write_cache(self, broker, rules_dir):
        _write_rule(rules_dir, decision="allow",
                    action=STREAM_ACTION, uid=NON_ADMIN_UID)
        broker.rules.reload()
        broker.set_peer(uid=NON_ADMIN_UID)
        before = len(broker.cache.list_all())
        broker.CheckPermission(STREAM_ACTION, {})
        assert len(broker.cache.list_all()) == before

    def test_unknown_writes_no_audit_row(self, broker):
        """CheckPermission is a read; nothing should land in audit."""
        import sqlite3
        broker.set_peer(uid=NON_ADMIN_UID)
        broker.CheckPermission(STREAM_ACTION, {})
        con = sqlite3.connect(broker.audit.db_path)
        try:
            (n,) = con.execute("SELECT COUNT(*) FROM audit").fetchone()
        finally:
            con.close()
        assert n == 0


# --- rate limit ------------------------------------------------------------

class TestCheckPermissionRateLimit:
    def test_over_limit_raises_dbus_exception(self, tmp_path, rules_dir):
        """After the limit is exhausted, further CheckPermission calls
        raise. Shares the same limiter as the prompt path — so a
        hostile tight loop can't bypass the enforcement by going
        through the lookup-only entry point."""
        import dbus
        br = _StubBroker(
            str(tmp_path / "c.sqlite"),
            str(tmp_path / "a.sqlite"),
            str(rules_dir),
            ratelimit_limit=3,
            ratelimit_window_s=60.0,  # long enough not to auto-expire
        )
        br.set_peer(uid=NON_ADMIN_UID)
        for _ in range(3):
            # First 3 calls are within the limit; resolution is
            # "unknown" since no rule / cache row exists.
            assert br.CheckPermission(STREAM_ACTION, {}) == "unknown"
        with pytest.raises(dbus.DBusException) as ei:
            br.CheckPermission(STREAM_ACTION, {})
        msg = str(ei.value)
        assert "rate limit exceeded" in msg.lower()

    def test_different_actions_have_independent_buckets(self, tmp_path,
                                                          rules_dir):
        """The limiter keys on (uid, action). Two distinct action
        strings must not share a bucket, or the admin-queue
        policy-change path could starve CheckPermission on a
        different action."""
        br = _StubBroker(
            str(tmp_path / "c.sqlite"),
            str(tmp_path / "a.sqlite"),
            str(rules_dir),
            ratelimit_limit=2,
            ratelimit_window_s=60.0,
        )
        br.set_peer(uid=NON_ADMIN_UID)
        for _ in range(2):
            br.CheckPermission("action.a", {})
        # Bucket for action.a is full, but action.b is fresh.
        assert br.CheckPermission("action.b", {}) == "unknown"


# --- details tolerance (shape-symmetric with RequestPermission) ----------

class TestCheckPermissionDetails:
    def test_non_matching_details_remain_unknown(self, broker):
        broker.set_peer(uid=NON_ADMIN_UID)
        details = {
            "app_id": "qnotebook",
            "title": "Draft notes",
            "peer_label": "phone",
            "desired_width": "640",
            "desired_height": "480",
        }
        assert broker.CheckPermission(STREAM_ACTION, details) == "unknown"

    def test_app_id_selector_matches_details(self, broker, rules_dir):
        (rules_dir / "portal-app.yaml").write_text(
            "- name: portal-app\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'com.qdistro.fs.open:*'\n"
            "    uid: 2000\n"
            "    app_id: 'org.example.Allowed'\n"
        )
        broker.rules.reload()
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(
            "com.qdistro.fs.open:org.example.Allowed",
            {"app_id": "org.example.Allowed"},
        ) == "allow"
        assert broker.CheckPermission(
            "com.qdistro.fs.open:org.example.Other",
            {"app_id": "org.example.Other"},
        ) == "unknown"

    def test_stream_action_slug_roundtrip(self, broker, rules_dir):
        """qdshell slugs app_id into
        `qdistro.view-stream.subscribe:<slug>`. A YAML rule keyed on
        the full action string must match — no path normalisation
        beyond what qdshell already does."""
        slugged = "qdistro.view-stream.subscribe:qterminator"
        _write_rule(rules_dir, decision="allow",
                    action=slugged, uid=NON_ADMIN_UID)
        broker.rules.reload()
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(slugged, {}) == "allow"
        # A different slug does NOT match the rule.
        assert broker.CheckPermission(
            "qdistro.view-stream.subscribe:other", {}) == "unknown"
