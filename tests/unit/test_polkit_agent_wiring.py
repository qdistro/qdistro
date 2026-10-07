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
from typing import ClassVar
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
        self.signals: list[str] = []
        self.signal_specs: list[tuple] = []
        self.closed = False

    def request_name(self, name, flags):
        self.requested_names.append(name)
        return 1

    def add_signal_receiver(self, handler, signal_name=None, **kw):
        self.signals.append(signal_name)
        self.signal_specs.append((signal_name, kw))

    def get_name_owner(self, name):
        return ":1.fake"

    def activate_name_owner(self, name):
        return ":1.fake"

    def get_is_connected(self):
        return not self.closed

    def set_exit_on_disconnect(self, flag):
        # libdbus bus connections exit(1) the process on disconnect; the
        # registrar relies on closing the private connection to retract a
        # stale registration, so it must be disarmed.
        self.exit_on_disconnect = flag

    def close(self):
        self.closed = True

    def __repr__(self):
        return f"<_FakeBus {self.label}>"


class TestMainBusWiring:

    @pytest.fixture
    def wired(self, monkeypatch):
        """Run main() far enough to see which bus gets what, then stop."""
        session = _FakeBus("session")
        system = _FakeBus("system")
        priv = _FakeBus("system-private")
        seen: dict = {}

        monkeypatch.setattr(agent_mod, "_require_admin_account", lambda: None)
        monkeypatch.setattr(agent_mod.dbus, "SessionBus", lambda: session)
        monkeypatch.setattr(
            agent_mod.dbus, "SystemBus",
            lambda private=False: priv if private else system)
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
            return "1", ":1.fake"

        monkeypatch.setattr(agent_mod, "QdistroPolkitAgent", fake_agent)
        monkeypatch.setattr(agent_mod, "_register", fake_register)
        # Registration now waits for a logind session; pretend one exists.
        monkeypatch.setattr(agent_mod, "_session_id", lambda bus: "1")
        # Stop before blocking forever in the GLib main loop.
        loop = mock.MagicMock()
        monkeypatch.setattr(agent_mod.GLib, "MainLoop", lambda: loop)

        rc = agent_mod.main()
        seen["rc"] = rc
        seen["session"] = session
        seen["system"] = system
        seen["private"] = priv
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

    def test_that_connection_is_a_private_system_bus(self, wired):
        """polkitd is a system-bus service, and the registration connection
        must be the dedicated private one so a session change can retract it
        by closing — the shared connection cannot be closed."""
        assert wired["register_bus"] is wired["private"]
        assert wired["register_bus"] is not wired["system"]

    def test_object_and_registration_use_one_path(self, wired):
        assert wired["object_path"] == wired["register_path"] == \
            agent_mod.AGENT_OBJ

    def test_session_bus_is_still_the_singleton_guard(self, wired):
        """The session-bus name is what stops two agents racing inside one
        login. Moving the object to the system bus must not drop it."""
        assert wired["session"].requested_names == [agent_mod.AGENT_BUS]
        assert wired["system"].requested_names == []
        assert wired["private"].requested_names == [], (
            "the agent must not claim its well-known name on the SYSTEM bus "
            "— that is a machine-wide name and the system-bus policy does "
            "not grant it")

    def test_main_returns_only_after_the_loop_is_entered(self, wired):
        assert wired["rc"] == 0

    def test_polkitd_owner_watch_is_scoped_to_the_daemon(self, wired):
        """A forged NameOwnerChanged-shaped payload from another sender
        must not reach the registration cache: the watch is pinned to the
        bus daemon's own name and object path."""
        matched = [kw for sig, kw in wired["system"].signal_specs
                   if sig == "NameOwnerChanged"]
        assert matched, "no NameOwnerChanged watch installed"
        assert matched[0].get("bus_name") == "org.freedesktop.DBus"
        assert matched[0].get("path") == "/org/freedesktop/DBus"


# ---------------------------------------------------------------------------
# Broker delegation
# ---------------------------------------------------------------------------

def _dbus_error(name: str) -> agent_mod.dbus.DBusException:
    return agent_mod.dbus.DBusException("boom", name=name)


class _RecordingBroker:
    """Records RequestPolkitAuth / WaitForDecision calls and their kwargs."""

    def __init__(self, *, file_raises=None, wait_raises=None, decision=True):
        self.filed: list[tuple] = []
        self.waited: list[tuple] = []
        self.responded: list[tuple] = []
        self.cancelled: list[tuple] = []
        self._file_raises = list(file_raises or [])
        self._wait_raises = list(wait_raises or [])
        self._decision = decision

    def RequestPolkitAuth(self, action, details, cookie, identities, **kw):
        self.filed.append((action, details, cookie, identities, kw))
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

    def RespondPolkitAuth(self, cookie, identities, **kw):
        self.responded.append((cookie, identities, kw))

    def CancelPolkitAuth(self, cookie, **kw):
        self.cancelled.append((cookie, kw))


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

    def test_begin_auth_runs_off_the_main_loop(self, make_agent, monkeypatch):
        """A blocked auth driver must not park the GLib main loop: the loop
        carries the session-registration watches, so an auth waiting on a
        human (WaitForDecision, up to 900s) would freeze registration for
        its whole timeout. Observed live: one stray NetworkManager
        auth_admin BeginAuth stalled the A->B->A session migration."""
        a = make_agent(_RecordingBroker())
        started = []
        def no_idle(*args, **kw):
            raise AssertionError(
                "BeginAuth work scheduled on the main loop via idle_add")
        monkeypatch.setattr(agent_mod.GLib, "idle_add", no_idle)
        monkeypatch.setattr(
            agent_mod.threading, "Thread",
            lambda target, daemon: started.append(target) or mock.MagicMock())
        a.BeginAuthentication("org.qdistro.test", "m", "", {}, "cookie", [],
                              ok_cb=lambda: None, err_cb=lambda e: None)
        assert len(started) == 1, "auth work must start on a worker thread"

    def test_wait_carries_a_timeout_long_enough_for_a_human(self, make_agent):
        """The defect: no timeout= meant dbus-python's 25s default applied to
        a call whose whole job is to wait for an admin to read a prompt."""
        broker = _RecordingBroker()
        assert make_agent(broker)._ask_broker("qsu.exec", {}, "cookie1", []) is True
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
        make_agent(broker)._ask_broker("qsu.exec", {}, "cookie1", [])
        assert broker.filed[0][4].get("timeout") is not None

    def test_a_denied_request_is_denied(self, make_agent):
        broker = _RecordingBroker(decision=False)
        assert make_agent(broker)._ask_broker("qsu.exec", {}, "cookie1", []) is False

    def test_a_lost_decision_does_not_re_file(self, make_agent):
        """The double-prompt bug. Re-filing after the request was already
        accepted gives the admin two identical prompts for one polkit cookie;
        answering one strands the other."""
        broker = _RecordingBroker(
            wait_raises=[_dbus_error("org.freedesktop.DBus.Error.NoReply")])
        assert make_agent(broker)._ask_broker("qsu.exec", {}, "cookie1", []) is False
        assert len(broker.filed) == 1, (
            f"the request was filed {len(broker.filed)} times; a lost "
            "decision must never re-file")

    def test_a_missing_broker_is_retried_once(self, make_agent):
        """ServiceUnknown positively means nothing was filed — the name had
        no owner to receive the call — so retrying cannot duplicate."""
        broker = _RecordingBroker(file_raises=[
            _dbus_error("org.freedesktop.DBus.Error.ServiceUnknown"), None])
        assert make_agent(broker)._ask_broker("qsu.exec", {}, "cookie1", []) is True
        assert len(broker.filed) == 2

    def test_an_ambiguous_filing_error_is_not_retried(self, make_agent):
        """NoReply while filing means we do not know whether the broker got
        it. Retrying might duplicate; denying might waste one prompt. Deny —
        the request is fail-closed and a stray prompt is the lesser harm."""
        broker = _RecordingBroker(
            file_raises=[_dbus_error("org.freedesktop.DBus.Error.NoReply")])
        assert make_agent(broker)._ask_broker("qsu.exec", {}, "cookie1", []) is False
        assert len(broker.filed) == 1

    def test_a_permanently_absent_broker_denies(self, make_agent):
        err = _dbus_error("org.freedesktop.DBus.Error.ServiceUnknown")
        broker = _RecordingBroker(file_raises=[err, err])
        assert make_agent(broker)._ask_broker("qsu.exec", {}, "cookie1", []) is False
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
                "qsu.exec", {}, "cookie1", []) is False, kwargs


class _SyncThread:
    """threading.Thread stand-in that runs the target on start()."""

    def __init__(self, target, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestPolkitRespondRelay:
    """The privileged-broker responder path.

    polkitd accepts AuthenticationAgentResponse2 from uid 0 only, so the
    uid-1000 agent never calls it: broker-method approvals are answered
    by the broker itself (it owns the queued request), and pam/fprint
    verdicts relay through RespondPolkitAuth.
    """

    _IDENTS: ClassVar = [("unix-user", {"uid": 0})]

    def _run_begin(self, make_agent, monkeypatch, broker, method,
                   env=None):
        a = make_agent(broker)
        monkeypatch.setattr(
            agent_mod.threading, "Thread",
            lambda target, daemon=None: _SyncThread(target))
        for k, v in dict(env or {}).items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("QDISTRO_POLKIT_METHOD", method)
        ok_calls, err_calls = [], []
        a.BeginAuthentication(
            "org.qdistro.test", "m", "", {}, "cookie9", self._IDENTS,
            ok_cb=lambda: ok_calls.append(1),
            err_cb=lambda e: err_calls.append(e))
        return ok_calls, err_calls

    def test_filing_carries_the_cookie_and_identities(self, make_agent):
        """The broker needs the cookie to answer polkitd and the offered
        identity list to pick a response identity from."""
        broker = _RecordingBroker()
        make_agent(broker)._ask_broker(
            "qsu.exec", {}, "cookie9", self._IDENTS)
        _action, _det, cookie, idents, _kw = broker.filed[0]
        assert cookie == "cookie9"
        assert idents == self._IDENTS

    def test_a_broker_allow_does_not_relay_through_the_agent(
            self, make_agent, monkeypatch):
        """On a broker allow the broker already answered polkitd — the
        agent must not deliver a second response for the same cookie."""
        broker = _RecordingBroker(decision=True)
        ok, err = self._run_begin(make_agent, monkeypatch, broker, "broker")
        assert ok == [1] and err == []
        assert broker.responded == [], (
            "broker-method allow must not call RespondPolkitAuth — the "
            "broker answers polkitd itself when the request is allowed")

    def test_a_local_allow_relays_through_the_broker(
            self, make_agent, monkeypatch):
        """pam/fprint verdicts are local to the agent; the positive
        response reaches polkitd via the privileged broker."""
        broker = _RecordingBroker()
        ok, err = self._run_begin(
            make_agent, monkeypatch, broker, "pam",
            env={"QDISTRO_POLKIT_NONINTERACTIVE": "allow"})
        assert ok == [1] and err == []
        assert len(broker.responded) == 1
        cookie, idents, _kw = broker.responded[0]
        assert cookie == "cookie9"
        assert idents == self._IDENTS

    def test_a_local_deny_relays_nothing(self, make_agent, monkeypatch):
        broker = _RecordingBroker()
        ok, err = self._run_begin(
            make_agent, monkeypatch, broker, "pam",
            env={"QDISTRO_POLKIT_NONINTERACTIVE": "deny"})
        assert ok == [1] and err == []
        assert broker.responded == []

    def test_a_relay_failure_fails_closed(self, make_agent, monkeypatch):
        """If the broker cannot deliver the response, polkit gets an
        error — never a silent success."""
        broker = _RecordingBroker()
        broker.RespondPolkitAuth = mock.MagicMock(
            side_effect=_dbus_error("org.freedesktop.DBus.Error.NoReply"))
        ok, err = self._run_begin(
            make_agent, monkeypatch, broker, "pam",
            env={"QDISTRO_POLKIT_NONINTERACTIVE": "allow"})
        assert err, "an undeliverable response must surface as an error"
        assert ok == []

    def test_cancel_forwards_to_the_broker(self, make_agent):
        """polkitd's CancelAuthentication retires the queued request:
        the broker drops it so the admin prompt does not linger."""
        broker = _RecordingBroker()
        make_agent(broker).CancelAuthentication("cookie9")
        assert broker.cancelled == [("cookie9", mock.ANY)]

    def test_a_cancel_relay_failure_only_logs(self, make_agent):
        """A broker that is down when polkit cancels must not crash the
        agent — the orphaned request reaps on its own."""
        broker = _RecordingBroker()
        broker.CancelPolkitAuth = mock.MagicMock(
            side_effect=_dbus_error("org.freedesktop.DBus.Error.NoReply"))
        make_agent(broker).CancelAuthentication("cookie9")


class _BrokerWorld:
    """System-bus fake where each broker generation owns a unique name and
    an independent request-id counter. A restart mints a new owner; proxies
    bound to a dead unique name fail every call, while the well-known name
    re-resolves — the same rules the real bus daemon applies."""

    def __init__(self):
        self.generation = 0
        self.instances: dict[str, _BrokerInstance] = {}
        self.restart()

    def restart(self):
        self.owner = f":1.broker-{self.generation}"
        self.instances[self.owner] = _BrokerInstance()
        self.generation += 1

    def get_name_owner(self, name):
        return self.owner

    def activate_name_owner(self, name):
        return self.owner

    def get_object(self, bus_name, path):
        return _BrokerProxy(self, bus_name)


class _BrokerInstance:
    def __init__(self):
        self.filed: list = []
        self.waited: list = []


class _BrokerProxy:
    def __init__(self, world, dest):
        self.world, self.dest = world, str(dest)

    def _instance(self):
        dest = self.dest
        if not dest.startswith(":"):
            dest = self.world.owner       # well-known name re-resolves
        inst = self.world.instances.get(dest)
        if inst is None or dest != self.world.owner:
            raise _dbus_error("org.freedesktop.DBus.Error.ServiceUnknown")
        return inst

    def RequestPolkitAuth(self, action, details, cookie, identities, **kw):
        inst = self._instance()
        inst.filed.append(action)
        return len(inst.filed)            # per-instance ids restart at 1

    def WaitForDecision(self, rid, **kw):
        inst = self._instance()
        inst.waited.append(rid)
        return True


class TestBrokerInstanceBinding:

    @pytest.fixture
    def agent(self, monkeypatch):
        monkeypatch.setattr(agent_mod.dbus, "Interface",
                            lambda obj, iface: obj)
        a = agent_mod.QdistroPolkitAgent.__new__(agent_mod.QdistroPolkitAgent)
        a._broker = None
        a._sysbus = _BrokerWorld()
        a._config = []
        return a

    def test_the_proxy_is_bound_to_the_unique_owner(self, agent):
        iface, rid = agent._file_request("qsu.exec", {}, "cookie1", [])
        assert rid == 1
        assert iface.dest == ":1.broker-0", (
            "the proxy is addressed to the well-known name — it would "
            "re-resolve to a restarted broker and pair this request's id "
            "with another instance's counter")

    def test_a_wait_cannot_cross_to_a_restarted_broker(self, agent):
        """astra r153: worker A files rid 1 on instance D1, the broker
        restarts, D2 issues its own rid 1 for an unrelated request. A
        WaitForDecision(1) through the re-resolving well-known name
        would reach D2 and consume the unrelated decision. The proxy is
        bound to D1's unique name, so the wait fails instead."""
        iface_d1, rid1 = agent._file_request("qsu.exec", {}, "cookie1", [])
        assert rid1 == 1
        agent._sysbus.restart()
        d2 = agent._sysbus.instances[agent._sysbus.owner]
        # an unrelated request on D2 reuses rid 1 — the collision the
        # well-known-name proxy would have silently accepted
        assert agent._sysbus.get_object(
            agent._sysbus.owner,
            agent_mod.QDISTRO_BROKER_OBJ).RequestPolkitAuth(
                "qdfileman.trash", {}, "cookie2", []) == rid1
        with pytest.raises(agent_mod.dbus.DBusException):
            iface_d1.WaitForDecision(rid1, timeout=1)
        assert d2.waited == [], "D2 must never see a wait for its own rid"

    def test_filing_after_a_restart_repins_to_the_new_instance(self, agent):
        """A dead cached proxy raises ServiceUnknown at file time —
        provably nothing was filed — so the retry re-resolves and files
        on, then waits on, the new instance."""
        agent._broker_iface()                     # cache a D1 proxy
        agent._sysbus.restart()
        assert agent._ask_broker("qsu.exec", {}, "cookie1", []) is True
        d2 = agent._sysbus.instances[agent._sysbus.owner]
        assert d2.filed == ["qsu.exec"]
        assert d2.waited == [1]
        d1 = agent._sysbus.instances[":1.broker-0"]
        assert d1.filed == [] and d1.waited == []
