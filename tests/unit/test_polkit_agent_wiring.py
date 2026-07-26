"""qdistro-polkit-agent — bus wiring and broker delegation.

These cover two defects found by driving a real greetd login on a VM, both
of which every existing unit test and every headless VM probe missed.

**The agent object was exported on the wrong bus.** `main()` created the
object on the SESSION bus and then registered with polkitd over a separate
SYSTEM bus connection. polkitd records the unique name of the connection
that called `RegisterAuthenticationAgent` and calls `BeginAuthentication`
back on *that* name — where nothing was exported. Registration succeeded,
`systemctl --user status` showed the unit active and the journal showed
"registered as session polkit agent", and yet no authorization ever reached
the agent: polkitd logged "FAILED to authenticate" while the agent's own
journal stayed empty for the whole attempt.

That is why these tests assert the object and the registration share ONE
connection rather than asserting either fact separately. Each fact was true
before the fix; only the relationship between them was wrong.

**The broker delegation timed out in 25s and then double-filed.**
`WaitForDecision` was called without a `timeout=`, so dbus-python's 25s
default applied to a call that blocks on a human reading a prompt. The
resulting NoReply was caught by a retry that re-filed the request, giving
the admin two identical prompts sharing one polkit cookie, and 25s later
a denial reported as "broker unreachable" — while the broker was up and
healthy throughout.
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

sys.modules.setdefault("pam", mock.MagicMock())  # noqa: F401

import qdistro_polkit_agent as agent_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Bus wiring
# ---------------------------------------------------------------------------

class _FakeBus:
    """Stands in for a dbus connection. Identity is what matters here."""

    def __init__(self, label: str):
        self.label = label
        self.requested_names: list[str] = []

    def request_name(self, name, flags):
        self.requested_names.append(name)
        return 1

    def __repr__(self):
        return f"<_FakeBus {self.label}>"


class TestMainBusWiring:

    @pytest.fixture
    def wired(self, monkeypatch):
        """Run main() far enough to see which bus gets what, then stop."""
        session = _FakeBus("session")
        system = _FakeBus("system")
        seen: dict = {}

        monkeypatch.setattr(agent_mod, "_require_admin_account", lambda: None)
        monkeypatch.setattr(agent_mod.dbus, "SessionBus", lambda: session)
        monkeypatch.setattr(agent_mod.dbus, "SystemBus", lambda: system)
        monkeypatch.setattr(
            agent_mod.dbus.mainloop.glib, "DBusGMainLoop",
            lambda **kw: None)

        def fake_agent(bus, path, *a, **kw):
            seen["object_bus"] = bus
            seen["object_path"] = path
            return mock.MagicMock()

        def fake_register(bus, path):
            seen["register_bus"] = bus
            seen["register_path"] = path

        monkeypatch.setattr(agent_mod, "QdistroPolkitAgent", fake_agent)
        monkeypatch.setattr(agent_mod, "_register", fake_register)
        # Stop before blocking forever in the GLib main loop.
        loop = mock.MagicMock()
        monkeypatch.setattr(agent_mod.GLib, "MainLoop", lambda: loop)

        rc = agent_mod.main()
        seen["rc"] = rc
        seen["session"] = session
        seen["system"] = system
        return seen

    def test_object_and_registration_share_one_connection(self, wired):
        """The defect. polkitd calls back on the connection that registered,
        so the object has to be on that same connection."""
        assert wired["object_bus"] is wired["register_bus"], (
            "the agent object was exported on "
            f"{wired['object_bus']} but registered from "
            f"{wired['register_bus']} — polkitd will call "
            "BeginAuthentication on the registering connection, where no "
            "object exists, and every authorization will silently fail")

    def test_that_connection_is_the_system_bus(self, wired):
        """polkitd is a system-bus service; a session-bus connection cannot
        register with it at all."""
        assert wired["register_bus"] is wired["system"]

    def test_object_and_registration_use_one_path(self, wired):
        assert wired["object_path"] == wired["register_path"] == \
            agent_mod.AGENT_OBJ

    def test_session_bus_is_still_the_singleton_guard(self, wired):
        """The session-bus name is what stops two agents racing inside one
        login. Moving the object to the system bus must not drop it."""
        assert wired["session"].requested_names == [agent_mod.AGENT_BUS]
        assert wired["system"].requested_names == [], (
            "the agent must not claim its well-known name on the SYSTEM bus "
            "— that is a machine-wide name and the system-bus policy does "
            "not grant it")

    def test_main_returns_only_after_the_loop_is_entered(self, wired):
        assert wired["rc"] == 0


# ---------------------------------------------------------------------------
# Broker delegation
# ---------------------------------------------------------------------------

def _dbus_error(name: str) -> agent_mod.dbus.DBusException:
    return agent_mod.dbus.DBusException("boom", name=name)


class _RecordingBroker:
    """Records RequestPermission / WaitForDecision calls and their kwargs."""

    def __init__(self, *, file_raises=None, wait_raises=None, decision=True):
        self.filed: list[tuple] = []
        self.waited: list[tuple] = []
        self._file_raises = list(file_raises or [])
        self._wait_raises = list(wait_raises or [])
        self._decision = decision

    def RequestPermission(self, action, details, **kw):
        self.filed.append((action, details, kw))
        if self._file_raises:
            exc = self._file_raises.pop(0)
            if exc is not None:
                raise exc
        return len(self.filed)

    def WaitForDecision(self, rid, **kw):
        self.waited.append((rid, kw))
        if self._wait_raises:
            exc = self._wait_raises.pop(0)
            if exc is not None:
                raise exc
        return self._decision


@pytest.fixture
def make_agent(monkeypatch):
    def _make(broker):
        a = agent_mod.QdistroPolkitAgent.__new__(agent_mod.QdistroPolkitAgent)
        a._broker = broker
        a._sysbus = mock.MagicMock()
        a._config = []
        monkeypatch.setattr(a, "_broker_iface", lambda: broker)
        return a
    return _make


class TestBrokerDelegation:

    def test_wait_carries_a_timeout_long_enough_for_a_human(self, make_agent):
        """The defect: no timeout= meant dbus-python's 25s default applied to
        a call whose whole job is to wait for an admin to read a prompt."""
        broker = _RecordingBroker()
        assert make_agent(broker)._ask_broker("qsu.exec", {}) is True
        assert len(broker.waited) == 1
        timeout = broker.waited[0][1].get("timeout")
        assert timeout is not None, (
            "WaitForDecision was called without a timeout, so dbus-python's "
            "25s default applies and the admin has 25s to decide")
        assert timeout >= 300, (
            f"a {timeout}s cutoff is not enough time for an admin to notice "
            "and answer a prompt")

    def test_filing_carries_a_bounded_timeout(self, make_agent):
        broker = _RecordingBroker()
        make_agent(broker)._ask_broker("qsu.exec", {})
        assert broker.filed[0][2].get("timeout") is not None

    def test_a_denied_request_is_denied(self, make_agent):
        broker = _RecordingBroker(decision=False)
        assert make_agent(broker)._ask_broker("qsu.exec", {}) is False

    def test_a_lost_decision_does_not_re_file(self, make_agent):
        """The double-prompt bug. Re-filing after the request was already
        accepted gives the admin two identical prompts for one polkit cookie;
        answering one strands the other."""
        broker = _RecordingBroker(
            wait_raises=[_dbus_error("org.freedesktop.DBus.Error.NoReply")])
        assert make_agent(broker)._ask_broker("qsu.exec", {}) is False
        assert len(broker.filed) == 1, (
            f"the request was filed {len(broker.filed)} times; a lost "
            "decision must never re-file")

    def test_a_missing_broker_is_retried_once(self, make_agent):
        """ServiceUnknown positively means nothing was filed — the name had
        no owner to receive the call — so retrying cannot duplicate."""
        broker = _RecordingBroker(file_raises=[
            _dbus_error("org.freedesktop.DBus.Error.ServiceUnknown"), None])
        assert make_agent(broker)._ask_broker("qsu.exec", {}) is True
        assert len(broker.filed) == 2

    def test_an_ambiguous_filing_error_is_not_retried(self, make_agent):
        """NoReply while filing means we do not know whether the broker got
        it. Retrying might duplicate; denying might waste one prompt. Deny —
        the request is fail-closed and a stray prompt is the lesser harm."""
        broker = _RecordingBroker(
            file_raises=[_dbus_error("org.freedesktop.DBus.Error.NoReply")])
        assert make_agent(broker)._ask_broker("qsu.exec", {}) is False
        assert len(broker.filed) == 1

    def test_a_permanently_absent_broker_denies(self, make_agent):
        err = _dbus_error("org.freedesktop.DBus.Error.ServiceUnknown")
        broker = _RecordingBroker(file_raises=[err, err])
        assert make_agent(broker)._ask_broker("qsu.exec", {}) is False
        assert len(broker.filed) == 2

    def test_every_failure_path_fails_closed(self, make_agent):
        """Nothing about this delegation may produce an allow by accident."""
        for kwargs in (
            {"file_raises": [_dbus_error("org.freedesktop.DBus.Error.NoReply")]},
            {"wait_raises": [_dbus_error("org.freedesktop.DBus.Error.NoReply")]},
            {"wait_raises": [_dbus_error("org.qdistro.Broker.AccessDenied")]},
            {"decision": False},
        ):
            assert make_agent(_RecordingBroker(**kwargs))._ask_broker(
                "qsu.exec", {}) is False, kwargs
