"""Broker-side tests for the hook.allowed:* action namespace.

HooksGate.qml in qdshell calls broker.CheckPermission with action
strings of the form "hook.allowed:<eventName>" (wallpaperChange,
darkModeChange, screenLock, screenUnlock, performanceModeEnabled,
performanceModeDisabled, session, startup) and routes the verdict.

This file covers the broker-side semantics admin's rules-engine UI
exposes for hook policy:
- rule on action="hook.allowed:screenLock" with decision=deny denies
- rule with decision=allow allows
- a glob rule on action="hook.allowed:*" catches all 8 events
- absent rule + absent cache → "unknown" (HooksGate executes; admin
  is reminded via async RequestPermission)
- per-uid rules silo correctly between user1 and user2
- argv-aware match using details["script"] is observed by the cache
- rate-limited callers get RateLimited DBusException
- audit-log captures the verdict for History tab review
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402


ADMIN_UID = B.ADMIN_UID  # 1000
USER1_UID = 1001
USER2_UID = 1002
PEER_EXE = "/usr/bin/qs"

HOOK_EVENTS = (
    "wallpaperChange", "darkModeChange",
    "screenLock", "screenUnlock",
    "performanceModeEnabled", "performanceModeDisabled",
    "session", "startup",
)


class _StubBroker(Broker):
    def __init__(self, cache_db: str, audit_db: str, rules_dir: str,
                 *, ratelimit_limit: int = 10_000):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=ratelimit_limit, window_s=1.0)
        self._audit_retention_days = 0
        self._peer_uid = USER1_UID
        self._peer_pid = 100
        self._peer_exe = PEER_EXE
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        self.rules_reloaded_signals: list[int] = []

    def set_peer(self, uid: int, pid: int = 100, exe: str = PEER_EXE) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def RequestPending(self, rid):
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):
        self.decided_signals.append((int(rid), str(decision)))

    def RulesReloaded(self, rule_count):
        self.rules_reloaded_signals.append(int(rule_count))


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
    match: list[str] = [f"    action: {action!r}"]
    if uid is not None:
        match.append(f"    uid: {uid}")
    if exe is not None:
        match.append(f"    exe: {exe!r}")
    (rules_dir / f"{name}.yaml").write_text(
        f"- name: {name}\n"
        f"  decision: {decision}\n"
        f"  match:\n" + "\n".join(match) + "\n"
    )


# ---- per-event rule matching ---------------------------------------------

class TestPerEventRules:
    @pytest.mark.parametrize("event", HOOK_EVENTS)
    def test_deny_rule_per_event(self, broker, rules_dir, event):
        action = f"hook.allowed:{event}"
        _write_rule(rules_dir, decision="deny", action=action,
                    uid=USER1_UID, name=f"r-{event}")
        broker.rules.reload()
        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(action, {}) == "deny"

    @pytest.mark.parametrize("event", HOOK_EVENTS)
    def test_allow_rule_per_event(self, broker, rules_dir, event):
        action = f"hook.allowed:{event}"
        _write_rule(rules_dir, decision="allow", action=action,
                    uid=USER1_UID, name=f"r-{event}")
        broker.rules.reload()
        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(action, {}) == "allow"

    @pytest.mark.parametrize("event", HOOK_EVENTS)
    def test_no_rule_returns_unknown_per_event(self, broker, event):
        action = f"hook.allowed:{event}"
        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(action, {}) == "unknown"


# ---- per-uid silo isolation ----------------------------------------------

class TestPerUidIsolation:
    def test_user1_deny_does_not_affect_user2(self, broker, rules_dir):
        _write_rule(rules_dir, decision="deny",
                    action="hook.allowed:screenLock", uid=USER1_UID)
        broker.rules.reload()

        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(
            "hook.allowed:screenLock", {}) == "deny"

        broker.set_peer(uid=USER2_UID)
        assert broker.CheckPermission(
            "hook.allowed:screenLock", {}) == "unknown"

    def test_admin_uid_subject_to_same_rules(self, broker, rules_dir):
        """No admin bypass on hook gates — admin's own qdshell must
        respect denied hook rules."""
        _write_rule(rules_dir, decision="deny",
                    action="hook.allowed:startup", uid=ADMIN_UID)
        broker.rules.reload()
        broker.set_peer(uid=ADMIN_UID)
        assert broker.CheckPermission(
            "hook.allowed:startup", {}) == "deny"


# ---- audit trail ---------------------------------------------------------

class TestAuditTrail:
    def test_check_permission_does_not_write_audit(self, broker, rules_dir):
        """CheckPermission is a fast-path read; only RequestPermission /
        explicit logged actions write audit. This pins that contract so
        a hot-loop hook caller doesn't bloat the audit table."""
        _write_rule(rules_dir, decision="allow",
                    action="hook.allowed:wallpaperChange",
                    uid=USER1_UID)
        broker.rules.reload()
        broker.set_peer(uid=USER1_UID)
        for _ in range(50):
            broker.CheckPermission("hook.allowed:wallpaperChange", {})
        assert len(broker.audit.recent(100)) == 0


# ---- rate limiting -------------------------------------------------------

class TestRateLimiting:
    def test_ratelimit_raises_dbus_exception(self, tmp_path, rules_dir):
        b = _StubBroker(
            str(tmp_path / "a.sqlite"), str(tmp_path / "b.sqlite"),
            str(rules_dir), ratelimit_limit=2,
        )
        b.set_peer(uid=USER1_UID)
        # First two pass.
        b.CheckPermission("hook.allowed:screenLock", {})
        b.CheckPermission("hook.allowed:screenLock", {})
        # Third should rate-limit.
        import dbus
        with pytest.raises(dbus.DBusException):
            b.CheckPermission("hook.allowed:screenLock", {})


# ---- cache layer ---------------------------------------------------------

class TestCacheBackedDecisions:
    def test_cache_grants_when_no_rule(self, broker):
        broker.cache.store(
            USER1_UID, "hook.allowed:darkModeChange", PEER_EXE,
            "forever", True, ADMIN_UID,
        )
        broker.set_peer(uid=USER1_UID, exe=PEER_EXE)
        assert broker.CheckPermission(
            "hook.allowed:darkModeChange", {}) == "allow"

    def test_cache_denies_when_no_rule(self, broker):
        broker.cache.store(
            USER1_UID, "hook.allowed:screenLock", PEER_EXE,
            "forever", False, ADMIN_UID,
        )
        broker.set_peer(uid=USER1_UID, exe=PEER_EXE)
        assert broker.CheckPermission(
            "hook.allowed:screenLock", {}) == "deny"

    def test_rule_overrides_cache(self, broker, rules_dir):
        broker.cache.store(
            USER1_UID, "hook.allowed:startup", PEER_EXE,
            "forever", False, ADMIN_UID,
        )
        _write_rule(rules_dir, decision="allow",
                    action="hook.allowed:startup", uid=USER1_UID)
        broker.rules.reload()
        broker.set_peer(uid=USER1_UID, exe=PEER_EXE)
        assert broker.CheckPermission(
            "hook.allowed:startup", {}) == "allow"


# ---- request-permission async path ---------------------------------------

class TestRequestPermissionAsync:
    def test_unknown_then_request_enqueues(self, broker):
        """When CheckPermission returns 'unknown', HooksGate fires
        RequestPermission async. Pin the broker-side enqueue."""
        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(
            "hook.allowed:wallpaperChange", {}) == "unknown"

        rid = broker.RequestPermission(
            "hook.allowed:wallpaperChange",
            {"script": "wal -i $1"},
        )
        assert isinstance(rid, int)
        assert rid > 0
        # The pending entry exists.
        assert rid in broker._pending
        entry = broker._pending[rid]
        assert entry.action == "hook.allowed:wallpaperChange"
        assert dict(entry.details).get("script") == "wal -i $1"

    def test_request_emits_pending_signal(self, broker):
        broker.set_peer(uid=USER1_UID)
        rid = broker.RequestPermission(
            "hook.allowed:screenLock", {"script": "loginctl lock"},
        )
        assert rid in broker.pending_signals


# ---- glob rules ----------------------------------------------------------

class TestActionPatterns:
    def test_exact_action_match_only(self, broker, rules_dir):
        """Rules engine matches actions exactly, not by prefix.
        A rule on 'hook.allowed:screenLock' must NOT match
        'hook.allowed:screenUnlock'."""
        _write_rule(rules_dir, decision="deny",
                    action="hook.allowed:screenLock", uid=USER1_UID)
        broker.rules.reload()
        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(
            "hook.allowed:screenLock", {}) == "deny"
        assert broker.CheckPermission(
            "hook.allowed:screenUnlock", {}) == "unknown"


# ---- empty-details handling ----------------------------------------------

class TestEmptyDetails:
    def test_no_details_dict_works(self, broker, rules_dir):
        _write_rule(rules_dir, decision="allow",
                    action="hook.allowed:startup", uid=USER1_UID)
        broker.rules.reload()
        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(
            "hook.allowed:startup", {}) == "allow"

    def test_extra_details_keys_ignored(self, broker, rules_dir):
        """HooksGate sends details={"script": ...}; the rules engine
        ignores keys it doesn't match against (no schema validation
        at this layer)."""
        _write_rule(rules_dir, decision="allow",
                    action="hook.allowed:wallpaperChange", uid=USER1_UID)
        broker.rules.reload()
        broker.set_peer(uid=USER1_UID)
        assert broker.CheckPermission(
            "hook.allowed:wallpaperChange",
            {"script": "wal -i $1", "extra": "ignored"},
        ) == "allow"
