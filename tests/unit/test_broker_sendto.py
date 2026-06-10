"""Integration tests for the broker's Phase 3 send-to surface.

These construct a real `Broker` object against temp sqlite databases
and invoke methods directly. The dbus method/signal decorators are
pass-throughs at import time (they record signature metadata and
return the underlying function), so the decorated methods are
callable as regular Python — the key wrinkle is `async_callbacks`:
RelayMessage takes `_reply` and `_error` as ordinary positional
parameters when invoked outside a dbus dispatch.

Scope here: things test_sendto.py can't reach with its pure-data
stub, but which are testable without standing up a real system
bus. End-to-end delivery (through a live UserRelay + receiver) is
validated in the VM via gui-tests/11.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")  # harness needs real dbus-python

# Importing broker at top-level is safe with real dbus-python; on
# hosts without it, importorskip above would already have aborted.
import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker, _Request  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402


ADMIN_UID = B.ADMIN_UID  # 1000


class _StubBroker(Broker):
    """Broker subclass that bypasses dbus.service.Object registration
    and the GLib timers. All per-instance state from the real
    __init__ is reproduced against temp dbs so every public method
    runs like in production — just without a real bus connection.
    """

    def __init__(self, cache_db: str, audit_db: str, rules_dir: str):
        # Skip dbus.service.Object.__init__ (no real bus here).
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        # Generous rate limit — tests shouldn't trip it accidentally.
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        # Peer-info shim (overridden by set_peer); admin uid by default
        # so DecideRequest works without extra setup.
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = "/usr/bin/test-harness"
        # start_time=0 is the broker's sentinel for "not available" and
        # disables the DecideRequest TOCTOU pid-recycle check. Without
        # this, the harness's synthetic pid (which may or may not map
        # to a real process on the host) triggers a spurious CallerGone.
        self._peer_start = 0
        # Capture signals in-process instead of emitting them.
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        # Forwards captured here rather than sent over dbus; set to
        # a side_effect-style list of (target_uid, service, kind,
        # payload) tuples on every _relay_forward call.
        self.forwards: list[tuple[int, str, str, str]] = []
        # If non-None, _relay_forward raises this instead of
        # recording. Useful for the "relay unreachable" path.
        self.forward_exc: BaseException | None = None
        # Cross-uid silo gate: override the real session-manager lookup
        # so the test never tries to bind to the system bus. Returns the
        # target silo's state string ("Active" by default so cross-uid
        # relays reach their real subject — action format, enqueue,
        # scope). The P02 gate itself is covered by
        # test_broker_session_manager_handoff.py; with the broker now
        # failing CLOSED by default, leaving this unset would make the
        # real _silo_state hit an absent session manager and refuse.
        self._silo_state_override = lambda _uid: "Active"

    def set_peer(self, uid: int, pid: int = 100,
                 exe: str = "/usr/bin/peer", start: int = 0) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_start = start

    # --- method overrides ------------------------------------------------

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe, self._peer_start)

    def _silo_state(self, target_uid):  # type: ignore[override]
        return self._silo_state_override(target_uid)

    def _relay_forward(self, target_uid, target_service, kind, payload):
        if self.forward_exc is not None:
            raise self.forward_exc
        self.forwards.append(
            (int(target_uid), str(target_service), str(kind), str(payload)))

    # Signals in production; in tests just record.
    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def broker(tmp_path: Path) -> _StubBroker:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


class _ReplyCollector:
    """Stand-in for dbus async reply/error callbacks."""

    def __init__(self):
        self.replies: list[tuple] = []
        self.errors: list[BaseException] = []

    def reply(self, *args) -> None:
        self.replies.append(tuple(args))

    def error(self, exc) -> None:
        # dbus-python would convert this; we just record.
        self.errors.append(exc)


@pytest.fixture
def cb() -> _ReplyCollector:
    return _ReplyCollector()


# --- action format ---------------------------------------------------------

class TestRelayActionFormat:
    def test_action_encodes_target_uid_and_service(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "org.qdistro.StubNotepad.uid3000",
            "text/plain", "hi", cb.reply, cb.error)
        assert len(broker._pending) == 1
        req = next(iter(broker._pending.values()))
        assert req.action == "app.send-to:3000:org.qdistro.StubNotepad.uid3000"

    def test_details_contains_payload_and_kind(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "org.qdistro.StubNotepad.uid3000",
            "text/plain", "the payload",
            cb.reply, cb.error)
        req = next(iter(broker._pending.values()))
        assert req.details["kind"] == "text/plain"
        assert req.details["payload"] == "the payload"
        assert req.details["target_uid"] == "3000"
        assert req.details["target_service"] == "org.qdistro.StubNotepad.uid3000"

    def test_request_is_one_shot(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "org.qdistro.StubNotepad", "k", "v",
            cb.reply, cb.error)
        req = next(iter(broker._pending.values()))
        assert req.one_shot is True
        assert req.delegated is False


# --- validation ------------------------------------------------------------

class TestRelayMessageValidation:
    def test_negative_target_uid_errors(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(-1, "org.qdistro.A", "k", "v",
                            cb.reply, cb.error)
        assert cb.replies == []
        assert len(cb.errors) == 1
        assert "target_uid" in str(cb.errors[0])

    def test_non_qdistro_service_errors(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(3000, "org.freedesktop.DBus", "k", "v",
                            cb.reply, cb.error)
        assert cb.replies == []
        assert len(cb.errors) == 1

    @pytest.mark.parametrize("bad_service", [
        "",
        "org.qdistro.",
        "org.qdistro..Nope",
        "org.qdistro.Bad-Name",
        "org.qdistro.Name\nInjected",
        "com.other.Thing",
    ])
    def test_rejects_malformed_service_names(self, broker, cb, bad_service):
        broker.set_peer(uid=2000)
        broker.RelayMessage(3000, bad_service, "k", "v",
                            cb.reply, cb.error)
        assert cb.errors, (
            f"expected _error for service={bad_service!r}, got reply")
        assert cb.replies == []

    def test_valid_request_enqueues_and_signals(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(3000, "org.qdistro.StubNotepad.uid3000",
                            "text/plain", "hi",
                            cb.reply, cb.error)
        assert cb.errors == []
        assert len(broker._pending) == 1
        # RequestPending signal fired exactly once for this rid.
        rid = next(iter(broker._pending.keys()))
        assert broker.pending_signals == [rid]


# --- one_shot semantics ----------------------------------------------------

class TestOneShotBypassesCache:
    def test_oneshot_skips_existing_cache_row(self, broker, cb):
        # Seed a permissive cache row for the exact (uid, action, exe).
        broker.cache.store(
            2000,
            "app.send-to:3000:org.qdistro.StubNotepad.uid3000",
            "/usr/bin/peer", "forever", True, ADMIN_UID)
        broker.set_peer(uid=2000, exe="/usr/bin/peer")
        broker.RelayMessage(3000, "org.qdistro.StubNotepad.uid3000",
                            "k", "v", cb.reply, cb.error)
        req = next(iter(broker._pending.values()))
        # Decision not short-circuited — admin must still approve.
        assert req.decision is None

    def test_oneshot_skips_matching_rule(self, tmp_path, cb):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        # Rule that would allow any send-to from uid 2000.
        (rules_dir / "00-relay.yaml").write_text("""
- name: blanket
  decision: allow
  match:
    uid: 2000
    action: "app.send-to:3000:org.qdistro.StubNotepad.uid3000"
""")
        br = _StubBroker(
            str(tmp_path / "a.sqlite"),
            str(tmp_path / "b.sqlite"),
            str(rules_dir))
        br.set_peer(uid=2000)
        br.RelayMessage(3000, "org.qdistro.StubNotepad.uid3000",
                        "k", "v", cb.reply, cb.error)
        req = next(iter(br._pending.values()))
        assert req.decision is None, (
            "one_shot must not resolve via rules — admin must see every call")


# --- DecideRequest on one-shot --------------------------------------------

class TestDecideOneShotScope:
    def _enqueue(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(3000, "org.qdistro.StubNotepad.uid3000",
                            "text/plain", "hi",
                            cb.reply, cb.error)
        rid = next(iter(broker._pending.keys()))
        broker.set_peer(uid=ADMIN_UID)
        return rid

    @pytest.mark.parametrize("scope", [
        "1h", "24h",
        "forever", "forever_exe",
        # task(072): argv-aware scopes are also rejected for one-shot
        # actions (RelayMessage / etc.) — the (target_uid, target_service)
        # pair is too fine-grained for any persistent grant to be useful.
        "forever_argv", "forever_basename", "forever_prefix",
    ])
    def test_non_once_scopes_rejected(self, broker, cb, scope):
        rid = self._enqueue(broker, cb)
        with pytest.raises(Exception) as ei:
            broker.DecideRequest(rid, "allow", scope)
        assert "not permitted for one-shot" in str(ei.value)

    def test_once_allowed(self, broker, cb):
        rid = self._enqueue(broker, cb)
        broker.DecideRequest(rid, "allow", "once")
        # Forward invoked once with the right args.
        assert broker.forwards == [
            (3000, "org.qdistro.StubNotepad.uid3000", "text/plain", "hi"),
        ]
        # Reply delivered, no error.
        assert cb.replies == [()]
        assert cb.errors == []

    def test_deny_surfaces_denied_error(self, broker, cb):
        rid = self._enqueue(broker, cb)
        broker.DecideRequest(rid, "deny", "once")
        assert broker.forwards == []   # never delivered
        assert cb.replies == []
        assert len(cb.errors) == 1
        assert "denied" in str(cb.errors[0]).lower()

    def test_allow_does_not_write_cache(self, broker, cb):
        rid = self._enqueue(broker, cb)
        broker.DecideRequest(rid, "allow", "once")
        # Cache has no send-to row after a one-shot approve — the
        # action-specific guard in DecideRequest + scope=once already
        # preventing a row are both in effect.
        rows = [r for r in broker.cache.list_all()
                if r["action"].startswith("app.send-to:")]
        assert rows == []

    def test_forward_failure_propagates(self, broker, cb):
        import dbus
        rid = self._enqueue(broker, cb)
        broker.forward_exc = dbus.DBusException(
            "relay not running", name="org.qdistro.AdminBroker1.TargetNotReady")
        broker.DecideRequest(rid, "allow", "once")
        assert cb.replies == []
        assert len(cb.errors) == 1
        # The exception we supplied or a wrapping one must mention the cause.
        assert "relay" in str(cb.errors[0]).lower()


# --- audit log integration -------------------------------------------------

class TestAuditTrail:
    def _rows(self, broker):
        import sqlite3
        con = sqlite3.connect(broker.audit.db_path)
        try:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(
                "SELECT caller_uid, action, decision, scope, source, approver_uid "
                "FROM audit ORDER BY id")]
        finally:
            con.close()

    def test_approve_writes_prompt_row(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(3000, "org.qdistro.StubNotepad.uid3000",
                            "text/plain", "hi",
                            cb.reply, cb.error)
        rid = next(iter(broker._pending.keys()))
        broker.set_peer(uid=ADMIN_UID)
        broker.DecideRequest(rid, "allow", "once")
        rows = self._rows(broker)
        assert len(rows) == 1
        r = rows[0]
        assert r["caller_uid"] == 2000
        assert r["action"] == "app.send-to:3000:org.qdistro.StubNotepad.uid3000"
        assert r["decision"] == 1
        assert r["scope"] == "once"
        assert r["source"] == "prompt"
        assert r["approver_uid"] == ADMIN_UID

    def test_deny_writes_prompt_row_decision_zero(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(3000, "org.qdistro.StubNotepad.uid3000",
                            "text/plain", "hi",
                            cb.reply, cb.error)
        rid = next(iter(broker._pending.keys()))
        broker.set_peer(uid=ADMIN_UID)
        broker.DecideRequest(rid, "deny", "once")
        rows = self._rows(broker)
        assert rows[-1]["decision"] == 0
        assert rows[-1]["source"] == "prompt"

    def test_forbidden_scope_does_not_write_allow(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(3000, "org.qdistro.StubNotepad.uid3000",
                            "k", "v", cb.reply, cb.error)
        rid = next(iter(broker._pending.keys()))
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(Exception):
            broker.DecideRequest(rid, "allow", "forever")
        rows = self._rows(broker)
        # The forbidden scope attempt is rejected before any audit write;
        # nothing in the log until the admin picks a valid scope.
        assert rows == []


# --- ListReceivers authz ---------------------------------------------------

class TestListReceiversAuthz:
    def test_no_admin_check_reads_whatever_run_user_offers(self, broker, monkeypatch):
        """ListReceivers is intentionally callable by any uid — see
        the broker docstring. This test pins that: calling as a
        non-admin peer returns a value, not a DBusException.
        """
        # Point os.listdir at an empty dir so we don't crash on the
        # real /run/user layout and so the return is empty+deterministic.
        monkeypatch.setattr(B.os, "listdir",
                            lambda _p: [])  # no uids discovered
        broker.set_peer(uid=2000)
        result = broker.ListReceivers()
        # dbus.Array is list-like; the important property is that no
        # exception was raised.
        assert list(result) == []
