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
    """Fake system bus: logind sessions/users plus polkitd's Authority."""

    def __init__(self):
        # session path -> (id, class)
        self.sessions: dict[str, tuple[str, str]] = {}
        self.pid_session: str | None = None
        self.display: tuple[str, str] = ("", "/")
        self.has_user = True
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.register_error: Exception | None = None
        self.signals: list[str] = []

    # dbus.Interface(obj, iface) wraps whatever get_object returns; return
    # objects that already expose every method we call.
    def get_object(self, bus_name, path):
        return _Obj(self, bus_name, path)

    def add_signal_receiver(self, handler, signal_name=None, **kw):
        self.signals.append(signal_name)


class _Obj:
    def __init__(self, fake, bus_name, path):
        self.fake, self.bus_name, self.path = fake, bus_name, path

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

    # org.freedesktop.PolicyKit1.Authority
    def RegisterAuthenticationAgent(self, subject, locale, path):
        if self.fake.register_error is not None:
            raise self.fake.register_error
        self.fake.registered.append(str(subject[1]["session-id"]))

    def UnregisterAuthenticationAgent(self, subject, path):
        self.fake.unregistered.append(str(subject[1]["session-id"]))


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
        reg = agent_mod.SessionRegistrar(fake, agent_mod.AGENT_OBJ)
        assert reg.reconcile() is True  # GLib source stays installed
        assert fake.registered == []
        assert reg.session_id is None

    def test_main_does_not_exit_without_a_session(self, fake, monkeypatch):
        """The restart loop: main() must reach the main loop, not return 1."""
        session = mock.MagicMock()
        monkeypatch.setattr(agent_mod, "_require_admin_account", lambda: None)
        monkeypatch.setattr(agent_mod.dbus, "SessionBus", lambda: session)
        monkeypatch.setattr(agent_mod.dbus, "SystemBus", lambda: fake)
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
        assert {"SessionNew", "SessionRemoved"} <= set(fake.signals)

    def test_login_later_registers_for_that_session(self, fake, monkeypatch):
        monkeypatch.setattr(agent_mod.GLib, "timeout_add", lambda *a: 1)
        reg = agent_mod.SessionRegistrar(fake, agent_mod.AGENT_OBJ)
        reg.reconcile()
        _login(fake, "6")
        reg._on_logind_signal("6", "/org/freedesktop/login1/session/_36")
        assert fake.registered == ["6"]
        assert reg.session_id == "6"
        reg.reconcile()  # idempotent
        assert fake.registered == ["6"]

    def test_relogin_moves_the_registration(self, fake):
        _login(fake, "6")
        reg = agent_mod.SessionRegistrar(fake, agent_mod.AGENT_OBJ)
        reg.reconcile()
        _login(fake, "8")
        reg.reconcile()
        assert fake.unregistered == ["6"]
        assert fake.registered == ["6", "8"]
        assert reg.session_id == "8"

    def test_logout_unregisters_and_waits(self, fake):
        _login(fake, "6")
        reg = agent_mod.SessionRegistrar(fake, agent_mod.AGENT_OBJ)
        reg.reconcile()
        fake.display = ("", "/")
        reg.reconcile()
        assert fake.unregistered == ["6"]
        assert reg.session_id is None

    def test_polkit_refusal_is_retried_not_fatal(self, fake):
        _login(fake, "6")
        fake.register_error = dbus.DBusException(
            "Cannot determine session the caller is in",
            name="org.freedesktop.PolicyKit1.Error.Failed")
        reg = agent_mod.SessionRegistrar(fake, agent_mod.AGENT_OBJ)
        assert reg.reconcile() is True
        assert reg.session_id is None
        fake.register_error = None
        reg.reconcile()
        assert fake.registered == ["6"]

    def test_waiting_logs_once(self, fake):
        reg = agent_mod.SessionRegistrar(fake, agent_mod.AGENT_OBJ)
        for _ in range(5):
            reg.reconcile()
        assert agent_mod.syslog.syslog.call_count == 1
