"""qdistro-polkit-agent — which logind session it registers for, and waiting.

Guest journals showed the user unit crash-looping every RestartSec forever:

    registration failed: org.freedesktop.PolicyKit1.Error.Failed:
        Cannot determine session the caller is in

polkitd accepts RegisterAuthenticationAgent only for the session it computes
for the caller: sd_pid_get_session(pid), else the user's display session
(sd_uid_get_display). The agent runs in user@1000.service, which is started
by linger at boot and pulls in qdwin-session.target before any greetd login,
so there is often no display session -- only logind's class=manager session
for the user manager. The agent then exited 1 and was respawned forever.

These pin: the session lookup mirrors polkitd's (own login session, else the
display session, never a manager session, never the stale shared
XDG_SESSION_ID); no session means wait, not exit; a later login registers;
a display-session change re-registers.
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

sys.modules.setdefault("pam", mock.MagicMock())  # noqa: F401

import dbus  # noqa: E402

import qdistro_polkit_agent as agent_mod  # noqa: E402


class _Logind:
    """Fake system bus: logind sessions/users plus polkitd's Authority.

    polkitd bookkeeping is modelled faithfully so the tests catch the defect
    astra r149 found: a registration belongs to the CONNECTION that made it
    and survives until that connection closes (polkitd removes it in its
    name-owner cleanup); a second live registration for the same session is
    refused; and both Register and Unregister refuse a subject session that
    differs from the one polkitd computes for the caller -- so an
    Unregister for an already-superseded session never lands.

    The world object itself is the shared connection used for logind queries
    and signal watches; `new_connection()` mints the private connections a
    SessionRegistrar asks for.
    """

    def __init__(self):
        # session path -> (id, class)
        self.sessions: dict[str, tuple[str, str]] = {}
        self.pid_session: str | None = None
        self.display: tuple[str, str] = ("", "/")
        self.has_user = True
        self.registered: list[str] = []       # accepted Register calls, in order
        self.unregistered: list[str] = []     # accepted Unregister calls
        self.registrations: dict = {}         # session id -> _Conn holding it
        self.conns: list = []
        self.register_error: Exception | None = None
        self.register_hook = None             # runs inside Register, pre-checks
        self.owner_error: Exception | None = None
        self.signals: list[str] = []
        # Unique-name owner of polkitd's well-known name. A restart mints a
        # new one; the name is never owned by two different daemons.
        self.polkit_owner = ":1.40"

    # -- shared-connection surface (logind queries, signal watches) --
    # dbus.Interface(obj, iface) wraps whatever get_object returns; return
    # objects that already expose every method we call.
    def get_object(self, bus_name, path):
        return _Obj(self, self, bus_name, path)

    def add_signal_receiver(self, handler, signal_name=None, **kw):
        self.signals.append(signal_name)

    def get_name_owner(self, name):
        if self.owner_error is not None:
            raise self.owner_error
        if str(name) == agent_mod.POLKIT_BUS and self.polkit_owner:
            return self.polkit_owner
        raise dbus.DBusException(
            "name has no owner",
            name="org.freedesktop.DBus.Error.NameHasNoOwner")

    def get_is_connected(self):
        return True

    def close(self):
        raise AssertionError("the shared connection must never be closed")

    # -- private connections and polkitd's name-owner cleanup --
    def new_connection(self):
        return _Conn(self)

    def drop_connection(self, conn):
        for sid, held_by in list(self.registrations.items()):
            if held_by is conn:
                del self.registrations[sid]

    def restart_polkitd(self, owner=":1.99"):
        """A daemon restart: the registration table dies with the old
        instance and the name is taken by a new unique owner."""
        self.registrations.clear()
        self.polkit_owner = owner

    def caller_session(self):
        """The session polkitd computes for a call: the same lookup the
        agent performs for itself (own login session else display)."""
        return agent_mod._session_id(self)


class _Conn:
    """One private connection to the fake system bus."""

    def __init__(self, world):
        self.world = world
        self.closed = False
        # libdbus's default: the process exits when this connection drops
        # or is closed. The registrar must clear it.
        self.exit_on_disconnect = True
        world.conns.append(self)

    def get_object(self, bus_name, path):
        return _Obj(self.world, self, bus_name, path)

    def add_signal_receiver(self, handler, signal_name=None, **kw):
        self.world.signals.append(signal_name)

    def get_name_owner(self, name):
        return self.world.get_name_owner(name)

    def get_is_connected(self):
        return not self.closed

    def set_exit_on_disconnect(self, flag):
        # libdbus bus connections default this to True; the registrar must
        # disarm it or close() would exit(1) the whole process.
        self.exit_on_disconnect = flag

    def close(self):
        self.closed = True
        self.world.drop_connection(self)


class _Obj:
    def __init__(self, fake, conn, bus_name, path):
        self.fake, self.conn, self.bus_name, self.path = \
            fake, conn, bus_name, path

    def _dead(self) -> bool:
        """A proxy addressed to a UNIQUE name dies with that owner: once
        the daemon it named is gone the destination no longer exists and
        the bus daemon answers ServiceUnknown. A proxy on the well-known
        name instead re-resolves to whoever owns it now."""
        dest = str(self.bus_name)
        return dest.startswith(":") and dest != self.fake.polkit_owner

    # org.freedesktop.login1.Manager
    def GetSessionByPID(self, pid):
        if self.fake.pid_session is None:
            raise dbus.DBusException(
                "PID has no session",
                name="org.freedesktop.login1.NoSessionForPID")
        return self.fake.pid_session

    def GetUser(self, uid):
        if not self.fake.has_user:
            raise dbus.DBusException(
                "no user", name="org.freedesktop.login1.NoUserForUID")
        return "/org/freedesktop/login1/user/_1000"

    # org.freedesktop.DBus.Properties
    def Get(self, iface, prop):
        if iface == agent_mod.LOGIND_IFACE_USER and prop == "Display":
            return self.fake.display
        sid, cls = self.fake.sessions[self.path]
        return {"Id": sid, "Class": cls}[prop]

    # org.freedesktop.PolicyKit1.Authority — same checks polkitd 127 makes:
    # the subject session must equal the session computed for the caller's
    # connection, and one session cannot hold two live registrations.
    def RegisterAuthenticationAgent(self, subject, locale, path):
        fake = self.fake
        if fake.register_hook is not None:
            fake.register_hook()
        if self._dead():
            raise dbus.DBusException(
                "the destination no longer exists",
                name="org.freedesktop.DBus.Error.ServiceUnknown")
        if fake.register_error is not None:
            raise fake.register_error
        sid = str(subject[1]["session-id"])
        caller = fake.caller_session()
        if caller is None:
            raise dbus.DBusException(
                "Cannot determine session the caller is in",
                name="org.freedesktop.PolicyKit1.Error.Failed")
        if sid != caller:
            raise dbus.DBusException(
                "Passed session and the session the caller is in differs",
                name="org.freedesktop.PolicyKit1.Error.Failed")
        # polkitd 127 keys agents by subject: a second registration for the
        # same session is refused even from the connection holding it.
        if sid in fake.registrations:
            raise dbus.DBusException(
                "An authentication agent already exists for the given "
                "subject",
                name="org.freedesktop.PolicyKit1.Error.Failed")
        fake.registrations[sid] = self.conn
        fake.registered.append(sid)

    def UnregisterAuthenticationAgent(self, subject, path):
        fake = self.fake
        if self._dead():
            raise dbus.DBusException(
                "the destination no longer exists",
                name="org.freedesktop.DBus.Error.ServiceUnknown")
        sid = str(subject[1]["session-id"])
        caller = fake.caller_session()
        if caller is None or sid != caller:
            raise dbus.DBusException(
                "Passed session and the session the caller is in differs",
                name="org.freedesktop.PolicyKit1.Error.Failed")
        if fake.registrations.get(sid) is self.conn:
            del fake.registrations[sid]
            fake.unregistered.append(sid)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.delenv("QDISTRO_POLKIT_SESSION_ID", raising=False)
    monkeypatch.setattr(agent_mod.dbus, "Interface", lambda obj, iface: obj)
    monkeypatch.setattr(agent_mod.syslog, "syslog", mock.MagicMock())
    return _Logind()


def _login(fake, sid="3", cls="user"):
    path = f"/org/freedesktop/login1/session/_3{sid}"
    fake.sessions[path] = (sid, cls)
    fake.display = (sid, path)
    return path


def _registrar(fake):
    """A SessionRegistrar wired to the fake: shared world for signals and
    logind, per-registration private connections for the authority."""
    return agent_mod.SessionRegistrar(
        fake, agent_mod.AGENT_OBJ,
        make_connection=fake.new_connection,
        make_agent=lambda conn: mock.MagicMock())


def _held(fake):
    """Session ids polkitd currently holds registrations for."""
    return sorted(fake.registrations)


class TestSessionLookup:

    def test_linger_only_has_no_session(self, fake):
        """The crash-loop state: only the user manager's own session."""
        fake.sessions["/s/_31"] = ("1", "manager")
        assert agent_mod._session_id(fake) is None

    def test_display_session_when_not_in_a_session_scope(self, fake):
        """user@.service process: polkitd falls back to the display session,
        so that is the subject that must be passed."""
        _login(fake, "4")
        assert agent_mod._session_id(fake) == "4"

    def test_own_login_session_wins(self, fake):
        """Started by hand from a terminal: polkitd uses the pid's session."""
        _login(fake, "4")
        fake.sessions["/s/_37"] = ("7", "user")
        fake.pid_session = "/s/_37"
        assert agent_mod._session_id(fake) == "7"

    def test_manager_session_for_pid_is_ignored(self, fake):
        """sd_pid_get_session() never resolves user@.service; neither may we."""
        fake.sessions["/s/_31"] = ("1", "manager")
        fake.pid_session = "/s/_31"
        _login(fake, "4")
        assert agent_mod._session_id(fake) == "4"

    def test_stale_xdg_session_id_is_not_trusted(self, fake, monkeypatch):
        """The user manager keeps XDG_SESSION_ID from an earlier login."""
        monkeypatch.setenv("XDG_SESSION_ID", "2")
        assert agent_mod._session_id(fake) is None
        _login(fake, "5")
        assert agent_mod._session_id(fake) == "5"

    def test_no_logind_user(self, fake):
        fake.has_user = False
        assert agent_mod._session_id(fake) is None

    def test_test_override(self, fake, monkeypatch):
        monkeypatch.setenv("QDISTRO_POLKIT_SESSION_ID", "c9")
        assert agent_mod._session_id(fake) == "c9"


class TestRegistrar:

    def test_no_session_waits_instead_of_failing(self, fake):
        reg = _registrar(fake)
        assert reg.reconcile() is True  # GLib source stays installed
        assert fake.registered == []
        assert reg.session_id is None

    def test_main_does_not_exit_without_a_session(self, fake, monkeypatch):
        """The restart loop: main() must reach the main loop, not return 1."""
        session = mock.MagicMock()
        monkeypatch.setattr(agent_mod, "_require_admin_account", lambda: None)
        monkeypatch.setattr(agent_mod.dbus, "SessionBus", lambda: session)
        monkeypatch.setattr(
            agent_mod.dbus, "SystemBus",
            lambda private=False: fake.new_connection() if private else fake)
        monkeypatch.setattr(agent_mod.dbus.mainloop.glib, "DBusGMainLoop",
                            lambda **kw: None)
        monkeypatch.setattr(agent_mod, "QdistroPolkitAgent",
                            lambda *a, **kw: mock.MagicMock())
        loop = mock.MagicMock()
        monkeypatch.setattr(agent_mod.GLib, "MainLoop", lambda: loop)
        monkeypatch.setattr(agent_mod.GLib, "timeout_add_seconds",
                            lambda *a: 1)
        assert agent_mod.main() == 0
        loop.run.assert_called_once()
        assert fake.registered == []
        assert {"SessionNew", "SessionRemoved", "NameOwnerChanged"} \
            <= set(fake.signals)

    def test_login_later_registers_for_that_session(self, fake, monkeypatch):
        monkeypatch.setattr(agent_mod.GLib, "timeout_add", lambda *a: 1)
        reg = _registrar(fake)
        reg.reconcile()
        _login(fake, "6")
        reg._on_logind_signal("6", "/org/freedesktop/login1/session/_36")
        assert fake.registered == ["6"]
        assert _held(fake) == ["6"]
        assert reg.session_id == "6"
        reg.reconcile()  # idempotent
        assert fake.registered == ["6"]

    def test_relogin_moves_the_registration(self, fake):
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        conn6 = reg._conn
        _login(fake, "8")
        reg.reconcile()
        # The stale registration for 6 is gone with its connection — polkitd
        # refuses UnregisterAuthenticationAgent once the caller's session has
        # moved on, so dropping the connection is the removal mechanism.
        assert conn6.closed
        assert fake.registered == ["6", "8"]
        assert _held(fake) == ["8"]
        assert reg.session_id == "8"
        assert reg._conn is not conn6

    def test_private_connection_is_disarmed_for_exit_on_disconnect(
            self, fake):
        """libdbus bus connections exit(1) the process when they disconnect
        -- including the deliberate close() we use to retract a stale
        registration. SessionRegistrar must disarm that flag or every
        session change silently kills the agent."""
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        assert reg._conn.exit_on_disconnect is False

    def test_return_to_same_session_reregisters(self, fake):
        """A -> B -> A: the first A registration was dropped with its
        connection, so registering for A again is accepted. With the old
        shared-connection code polkitd kept the stale A entry (the Unregister
        it refused) and refused the second registration as a duplicate."""
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        _login(fake, "8")
        reg.reconcile()
        _login(fake, "6")  # B ended; A is the display session again
        reg.reconcile()
        assert fake.registered == ["6", "8", "6"]
        assert _held(fake) == ["6"]
        assert reg.session_id == "6"

    def test_unregister_of_a_superseded_session_is_refused(self, fake):
        """Pins the polkitd rule that makes connection-drop necessary: once
        the caller's session has moved on, UnregisterAuthenticationAgent for
        the old session is rejected — the stale entry can only leave with
        the connection that owns it."""
        conn = fake.new_connection()
        _login(fake, "6")
        agent_mod._authority(conn).RegisterAuthenticationAgent(
            agent_mod._session_subject("6"), "en_US.UTF-8",
            agent_mod.AGENT_OBJ)
        _login(fake, "8")
        with pytest.raises(dbus.DBusException):
            agent_mod._authority(conn).UnregisterAuthenticationAgent(
                agent_mod._session_subject("6"), agent_mod.AGENT_OBJ)
        assert _held(fake) == ["6"]
        conn.close()  # the name-owner cleanup is what actually removes it
        assert _held(fake) == []

    def test_logout_unregisters_and_waits(self, fake):
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        conn6 = reg._conn
        fake.display = ("", "/")
        reg.reconcile()
        assert conn6.closed
        assert _held(fake) == []
        assert reg.session_id is None

    def test_dead_connection_forgets_the_registration(self, fake):
        """If the private transport dies (bus restart), polkitd already
        dropped the registration; the next reconcile re-registers instead of
        trusting the cached session."""
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        reg._conn.close()
        reg.reconcile()
        assert fake.registered == ["6", "6"]
        assert _held(fake) == ["6"]

    def test_polkitd_restart_reregisters_on_the_same_connection(self, fake):
        """polkitd keeps no state across a restart; the connection is still
        fine, so only the cached registration is dropped."""
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        conn = reg._conn
        fake.restart_polkitd(":1.99")
        reg._on_polkit_owner(agent_mod.POLKIT_BUS, ":1.40", ":1.99")
        assert reg.session_id == "6"
        assert fake.registered == ["6", "6"]
        assert reg._conn is conn

    def test_queued_owner_signal_does_not_invalidate(self, fake):
        """A Register call can bus-activate polkitd during the synchronous
        reconcile() in start(); the NameOwnerChanged for that activation
        then arrives only once the main loop runs. It names the same owner
        the registration was made against, so it must not invalidate the
        cache -- the old code forgot session_id here and every later
        reconcile retried a duplicate registration on the same connection,
        which polkitd refuses."""
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        assert fake.registered == ["6"]
        conn = reg._conn
        reg._on_polkit_owner(agent_mod.POLKIT_BUS, "", ":1.40")
        assert reg.session_id == "6"
        assert reg._conn is conn
        assert fake.registered == ["6"]   # no duplicate re-registration
        # A genuine restart afterwards still re-registers, and a later
        # A -> B -> A migration stays clean.
        fake.restart_polkitd(":1.99")
        reg._on_polkit_owner(agent_mod.POLKIT_BUS, ":1.40", ":1.99")
        assert fake.registered == ["6", "6"]
        assert reg._conn is conn
        _login(fake, "8")
        reg.reconcile()
        _login(fake, "6")
        reg.reconcile()
        assert fake.registered == ["6", "6", "8", "6"]
        assert _held(fake) == ["6"]

    def test_owner_lookup_failure_keeps_registration(self, fake):
        """A transient GetNameOwner failure must not invalidate a live
        registration -- only a proven owner change may."""
        _login(fake, "6")
        reg = _registrar(fake)
        reg.reconcile()
        fake.owner_error = dbus.DBusException("bus hiccup")
        reg.reconcile()
        assert reg.session_id == "6"
        assert _held(fake) == ["6"]
        fake.owner_error = None
        reg.reconcile()
        assert fake.registered == ["6"]

    def test_owner_resolution_failure_retires_uncertain_state(self, fake):
        """A get_name_owner failure the registrar cannot classify leaves
        doubt about what the connection did: the conservative answer is
        to retire it, so a later retry can never hit a duplicate
        registration refusal on uncertain state."""
        _login(fake, "6")
        reg = _registrar(fake)
        fake.owner_error = dbus.DBusException("bus hiccup")
        reg.reconcile()
        assert reg.session_id is None
        assert _held(fake) == []
        assert reg._conn is None
        fake.owner_error = None
        reg.reconcile()
        assert reg.session_id == "6"
        assert _held(fake) == ["6"]

    def test_absent_polkitd_retries_on_the_same_connection(self, fake):
        """NameHasNoOwner proves nothing was registered, so the private
        connection survives and the next reconcile registers on it."""
        _login(fake, "6")
        fake.polkit_owner = None
        reg = _registrar(fake)
        reg.reconcile()
        assert reg.session_id is None
        assert _held(fake) == []
        conn = reg._conn
        assert conn is not None and not conn.closed
        fake.polkit_owner = ":1.40"
        reg.reconcile()
        assert reg.session_id == "6"
        assert reg._conn is conn

    def test_restart_inside_registration_keeps_the_retry_clean(self, fake):
        """astra r153: the owner is resolved (D1) and the daemon restarts
        before the Register call runs. The proxy is bound to D1's unique
        name, so the call fails on the dead destination rather than
        landing on D2 -- and the retry registers with, and caches, D2.
        Caching D2's owner next to a D1-made (now-dead) registration was
        the defect: reconcile would then never notice the loss."""
        _login(fake, "6")
        reg = _registrar(fake)
        fake.register_hook = lambda: fake.restart_polkitd(":1.99")
        reg.reconcile()
        assert reg.session_id is None      # the call died with D1
        assert _held(fake) == []
        fake.register_hook = None
        reg.reconcile()
        assert reg.session_id == "6"
        assert reg._polkit_owner == ":1.99"   # cached owner is the live one
        assert _held(fake) == ["6"]
        fake.restart_polkitd(":1.100")        # and a later restart registers
        reg.reconcile()                       # with the instance after it
        assert reg._polkit_owner == ":1.100"
        assert _held(fake) == ["6"]

    def test_polkit_refusal_is_retried_not_fatal(self, fake):
        _login(fake, "6")
        fake.register_error = dbus.DBusException(
            "Cannot determine session the caller is in",
            name="org.freedesktop.PolicyKit1.Error.Failed")
        reg = _registrar(fake)
        assert reg.reconcile() is True
        assert reg.session_id is None
        fake.register_error = None
        reg.reconcile()
        assert fake.registered == ["6"]

    def test_waiting_logs_once(self, fake):
        reg = _registrar(fake)
        for _ in range(5):
            reg.reconcile()
        assert agent_mod.syslog.syslog.call_count == 1
