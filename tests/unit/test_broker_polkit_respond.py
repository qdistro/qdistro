"""Broker-side polkit responder.

The session polkit agent runs as the admin uid, and polkitd accepts
AuthenticationAgentResponse2 from uid 0 only — so positive polkit
decisions are delivered by the privileged broker:

- RequestPolkitAuth files a request carrying the polkit cookie and the
  identity list polkit offered; an allow — admin click, rule, cache, or
  hook — makes the broker answer polkitd itself.
- RespondPolkitAuth relays an agent-local pam/fprint verdict.
- CancelPolkitAuth retires the queued request when polkitd cancels the
  auth session, so a dead prompt does not linger in the admin queue.

Stub-broker pattern matches test_broker_concurrency.py: subclass Broker
to bypass dbus.service.Object registration + GLib timers, fake
_peer_info, and intercept the actual polkitd call
(_respond_polkit_call) to record what would have been sent.
"""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402

ADMIN_UID = B.ADMIN_UID
NON_ADMIN_UID = 2000
PEER_EXE = "/usr/bin/test-app"

IDENT_ROOT = ("unix-user", {"uid": dbus.UInt32(0)})
IDENT_ADMIN = ("unix-user", {"uid": dbus.UInt32(ADMIN_UID)})
IDENT_GROUP = ("unix-group", {"gid": dbus.UInt32(27)})


class _StubBroker(Broker):
    """Minimal Broker bypassing real D-Bus registration."""

    def __init__(self, cache_db: str, audit_db: str, rules_dir: str):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stub-broker-io")
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = PEER_EXE
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        # Captured (uid, cookie, identity) tuples from _respond_polkit.
        self.polkit_responded: list[tuple] = []
        self.respond_raises: Exception | None = None
        from qdistro_hook_client import HookClient
        self.hooks = HookClient(enabled=False)

    def set_peer(self, uid: int, pid: int = 100, exe: str = PEER_EXE,
                 start: int = 0) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_start = start

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def _peer_matches_admin_control(self, *, uid: int, pid: int,
                                    exe: str, method: str = ""
                                    ) -> tuple[bool, str]:
        if int(uid) in (0, ADMIN_UID):
            return True, "test-injected admin-control peer"
        return False, f"uid {uid} is not admin uid {ADMIN_UID}"

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))

    def ApprovalRevoked(self, caller_uid, action, exe):  # type: ignore[override]
        pass

    def _respond_polkit_call(self, uid, cookie, identity):  # type: ignore[override]
        if self.respond_raises is not None:
            raise self.respond_raises
        self.polkit_responded.append((uid, cookie, identity))


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


def _file_polkit(broker: _StubBroker, cookie: str = "c1",
                 identities=(), action: str = "qdistro.test.action",
                 details: dict | None = None) -> int:
    return broker.RequestPolkitAuth(
        action, details or {}, cookie, identities)


class TestRequestPolkitAuth:

    def test_files_a_pending_request_carrying_the_polkit_context(
            self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        req = broker._pending[rid]
        assert req.polkit_cookie == "cookie-abc"
        assert req.polkit_identities == [
            ("unix-user", {"uid": dbus.UInt32(0)})]
        assert rid in broker.pending_signals

    def test_restricted_to_the_admin_uid(self, broker):
        broker.set_peer(NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)

    def test_admin_approval_answers_polkitd(self, broker):
        """DecideRequest allow → AuthenticationAgentResponse2 with the
        request's own cookie and an identity from the offered list."""
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        assert broker.DecideRequest(rid, "allow", "once") == "applied"
        assert len(broker.polkit_responded) == 1
        uid, cookie, identity = broker.polkit_responded[0]
        assert uid == ADMIN_UID
        assert cookie == "cookie-abc"
        assert identity == ("unix-user", {"uid": dbus.UInt32(0)})

    def test_deny_answers_nothing(self, broker):
        rid = _file_polkit(broker)
        broker.DecideRequest(rid, "deny", "once")
        assert broker.polkit_responded == []

    def test_an_ordinary_request_never_answers_polkitd(self, broker):
        """RequestPermission carries no cookie — the respond path must
        be a no-op for it."""
        rid = broker.RequestPermission("qdistro.test.action", {})
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded == []

    def test_the_response_identity_comes_from_the_offered_list(
            self, broker):
        """polkitd rejects a response naming an identity it did not
        offer. On this image the list is [unix-user uid=0]; where the
        requesting uid is offered, prefer it."""
        rid = _file_polkit(
            broker, "c1", [IDENT_ROOT])
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded[0][2][1]["uid"] == 0

        rid = _file_polkit(
            broker, "c2", [IDENT_ROOT, IDENT_ADMIN])
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded[1][2][1]["uid"] == ADMIN_UID

    def test_an_empty_identity_list_means_no_response(self, broker,
                                                      capsys):
        rid = _file_polkit(broker, "c3", [])
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded == []
        assert "no usable identity" in capsys.readouterr().out

    def test_the_response_precedes_the_waiter_release(self, broker):
        """Ordering matters: the agent completes BeginAuthentication the
        instant its WaitForDecision returns, and polkitd tears the
        cookie's session down then. A response sent after the waiter
        release lands on "No session for cookie" — observed live."""
        rid = _file_polkit(broker, "cookie-x", [IDENT_ROOT])
        seen = []
        def reply(v):
            seen.append(bool(v) and bool(broker.polkit_responded))
        broker._pending[rid].waiters.append((reply, lambda e: None))
        broker.DecideRequest(rid, "allow", "once")
        assert seen == [True], (
            "the waiter fired before AuthenticationAgentResponse2 was "
            "sent — polkitd will have already torn the session down")

    def test_a_respond_failure_is_logged_not_fatal(self, broker, capsys):
        """A stale cookie or dead polkitd must not corrupt the decision —
        it was already made and audited."""
        broker.respond_raises = dbus.DBusException(
            "gone", name="org.freedesktop.DBus.Error.ServiceUnknown")
        rid = _file_polkit(broker, "c4", [IDENT_ROOT])
        assert broker.DecideRequest(rid, "allow", "once") == "applied"
        assert "AuthenticationAgentResponse2 failed" in \
            capsys.readouterr().out

    def test_a_cached_allow_answers_polkitd(self, broker):
        """A 'forever' scope grant auto-approves the next identical
        polkit auth — the response must still be delivered."""
        broker.set_peer(0)
        assert broker.cache.store(ADMIN_UID, "qdistro.test.action",
                                  PEER_EXE, "forever", True, ADMIN_UID)
        broker.set_peer(ADMIN_UID)
        rid = _file_polkit(broker, "cookie-cached", [IDENT_ROOT],
                           details={})
        req = broker._pending[rid]
        assert req.decision is True
        assert len(broker.polkit_responded) == 1
        assert broker.polkit_responded[0][1] == "cookie-cached"


class TestRespondPolkitAuth:

    def test_relays_the_verdict_to_polkitd(self, broker):
        broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT])
        assert broker.polkit_responded == [
            (ADMIN_UID, "cookie-9", ("unix-user", {"uid": dbus.UInt32(0)}))]

    def test_restricted_to_the_admin_uid(self, broker):
        broker.set_peer(NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT])

    def test_empty_identities_fail_closed(self, broker):
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [])
        assert broker.polkit_responded == []


class TestCancelPolkitAuth:

    def test_decides_the_matching_request_deny(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        replies = []
        req = broker._pending[rid]
        req.waiters.append((replies.append, lambda e: None))
        broker.CancelPolkitAuth("cookie-abc")
        req = broker._pending[rid]
        assert req.decision is False
        assert replies == [False]
        assert (rid, "deny") in broker.decided_signals
        assert broker.polkit_responded == []

    def test_an_unknown_cookie_is_a_no_op(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.CancelPolkitAuth("someone-elses-cookie")
        assert broker._pending[rid].decision is None
        assert broker.decided_signals == []

    def test_an_already_decided_request_is_untouched(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.DecideRequest(rid, "allow", "once")
        broker.CancelPolkitAuth("cookie-abc")
        assert broker._pending[rid].decision is True

    def test_an_empty_cookie_never_matches_ordinary_requests(
            self, broker):
        """polkit_cookie is "" on non-polkit requests; an empty cancel
        must not sweep the queue."""
        rid = broker.RequestPermission("qdistro.test.action", {})
        broker.CancelPolkitAuth("")
        assert broker._pending[rid].decision is None

    def test_restricted_to_the_admin_uid(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.set_peer(NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.CancelPolkitAuth("cookie-abc")
        broker.set_peer(ADMIN_UID)
        assert broker._pending[rid].decision is None


class TestIdentityNormalization:

    def test_numeric_fields_stay_uint32_rest_become_strings(self):
        idents = B._polkit_identities([
            ("unix-user", {"uid": dbus.UInt32(0), "name": "root"}),
            ("unix-group", {"gid": dbus.UInt32(27)}),
        ])
        assert idents == [
            ("unix-user", {"uid": dbus.UInt32(0), "name": "root"}),
            ("unix-group", {"gid": dbus.UInt32(27)}),
        ]

    def test_malformed_entries_are_dropped(self):
        assert B._polkit_identities([None, "junk", IDENT_ADMIN]) == [
            ("unix-user", {"uid": dbus.UInt32(ADMIN_UID)})]

    def test_pick_prefers_the_requesting_uid(self):
        picked = B._pick_polkit_identity(
            [IDENT_ROOT, IDENT_ADMIN], ADMIN_UID)
        assert picked[1]["uid"] == ADMIN_UID

    def test_pick_falls_back_to_first_unix_user_then_first_entry(self):
        # first unix-user wins over an earlier non-unix-user entry
        assert B._pick_polkit_identity(
            [IDENT_GROUP, IDENT_ROOT], ADMIN_UID)[0] == "unix-user"
        assert B._pick_polkit_identity([IDENT_GROUP], ADMIN_UID) == \
            ("unix-group", {"gid": dbus.UInt32(27)})
        assert B._pick_polkit_identity([], ADMIN_UID) is None
