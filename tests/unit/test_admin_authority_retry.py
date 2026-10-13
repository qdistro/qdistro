"""The admin app and TUI authenticate on .AuthRequired and retry once.

The broker answers an admin-uid authority call without the polkit action
org.qdistro.broker.approve with .AuthRequired (test_broker_authority_
polkit.py). The installed clients then ask polkitd for the action for
THEIR OWN process with AllowUserInteraction — the session agent prompts for
the admin password — and retry the call once. A cancelled prompt leaves the
request pending and raises .AuthNotGranted; a bulk run stops there instead
of prompting per row. The GUI waits in a nested event loop, never a
blocking call, so the window stays live while the password dialog is up.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tui"))

import broker_client as BC  # noqa: E402

AUTH_REQUIRED = "org.qdistro.AdminBroker1.AuthRequired"
AUTH_NOT_GRANTED = "org.qdistro.AdminBroker1.AuthNotGranted"


class _NamedError(Exception):
    def __init__(self, name, msg="x"):
        super().__init__(msg)
        self._name = name

    def get_dbus_name(self):
        return self._name


class _Flaky:
    """A broker call that answers AuthRequired `n_refusals` times."""

    def __init__(self, n_refusals=1, result="applied"):
        self.n = n_refusals
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.n:
            raise _NamedError(AUTH_REQUIRED)
        return self.result


# -- shared contract (TUI helper) ----------------------------------------


def test_authorized_call_needs_no_prompt():
    asked = []
    call = _Flaky(0)
    assert BC.call_with_authority(call, lambda: asked.append(1) or True) \
        == "applied"
    assert call.calls == 1 and asked == []


@pytest.mark.cheat_aware(
    protects="a cancelled password prompt decides nothing",
    severity="high",
    cheats=["retry without authenticating", "treat a cancel as success"],
    consequence="the TUI claims an approval that never happened, or loops",
)
def test_auth_required_then_cancel_raises_not_granted_once():
    call = _Flaky(1)
    with pytest.raises(BC.AuthNotGranted) as ei:
        BC.call_with_authority(call, lambda: False)
    assert call.calls == 1
    assert BC.broker_error_label(ei.value) == \
        BC._BROKER_ERROR_LABELS["AuthNotGranted"]


def test_auth_required_then_grant_retries_once():
    call = _Flaky(1)
    assert BC.call_with_authority(call, lambda: True) == "applied"
    assert call.calls == 2


def test_second_auth_required_propagates_no_loop():
    call = _Flaky(5)
    asked = []
    with pytest.raises(_NamedError) as ei:
        BC.call_with_authority(call, lambda: asked.append(1) or True)
    assert ei.value.get_dbus_name() == AUTH_REQUIRED
    assert call.calls == 2 and asked == [1]


def test_other_errors_never_prompt():
    asked = []

    def denied():
        raise _NamedError("org.qdistro.AdminBroker1.AccessDenied")
    with pytest.raises(_NamedError):
        BC.call_with_authority(denied, lambda: asked.append(1) or True)
    assert asked == []


def test_start_time_parsing_survives_hostile_comm():
    # comm "a) b (c" — the field after the LAST ')' is field 3.
    fields = ["S"] + [str(i) for i in range(4, 30)]
    stat = "4242 (a) b (c) " + " ".join(fields)
    assert BC.proc_start_time(stat) == 22
    kind, f = BC.own_polkit_subject()
    assert kind == "unix-process"
    assert f["pid"] == os.getpid() and f["uid"] == os.getuid()
    with open("/proc/self/stat") as fh:
        assert f["start-time"] == int(fh.read().rsplit(")", 1)[1].split()[19])


# -- TUI D-Bus client ----------------------------------------------------


def _tui_client(proxy_calls):
    dbus = pytest.importorskip("dbus")

    class _Proxy:
        def DecideRequest(self, rid, decision, scope, dbus_interface=None):
            return proxy_calls(rid, decision, scope)

    class _Bus:
        def get_name_owner(self, _name):
            return ":1.7"

        def get_object(self, name, path):
            return _Proxy() if name == ":1.7" else (name, path)

    c = BC.DBusBrokerClient()
    c._bus, c._dbus = _Bus(), dbus
    return c, dbus


def test_tui_decide_authenticates_and_retries():
    seen = []

    def decide(rid, decision, scope):
        seen.append((rid, decision, scope))
        if len(seen) == 1:
            raise _NamedError(AUTH_REQUIRED)
        return "applied"
    c, _ = _tui_client(decide)
    prompts = []
    c._polkit_authorize = lambda: prompts.append(1) or True
    assert c.decide_request(3, "allow", "once", ":1.7") == "applied"
    assert seen == [(3, "allow", "once")] * 2 and prompts == [1]


def test_tui_decide_cancelled_leaves_request():
    seen = []

    def decide(*a):
        seen.append(a)
        raise _NamedError(AUTH_REQUIRED)
    c, _ = _tui_client(decide)
    c._polkit_authorize = lambda: False
    with pytest.raises(BC.AuthNotGranted):
        c.decide_request(3, "allow", "once", ":1.7")
    assert len(seen) == 1


def test_tui_polkit_question_is_interactive_for_own_process():
    c, dbus = _tui_client(lambda *a: "applied")
    seen = {}

    class _Authority:
        def CheckAuthorization(self, subject, action, details, flags,
                               cancel, timeout=None):
            seen.update(subject=subject, action=action, flags=flags,
                        details=details, timeout=timeout)
            return (dbus.Boolean(True), dbus.Boolean(False), {})

    class _DbusShim:
        def __getattr__(self, name):
            return getattr(dbus, name)

        @staticmethod
        def Interface(obj, iface):
            seen["target"] = (obj, iface)
            return _Authority()
    c._dbus = _DbusShim()
    assert c._polkit_authorize() is True
    assert seen["target"] == ((BC.POLKIT_BUS, BC.POLKIT_OBJ), BC.POLKIT_IFACE)
    kind, fields = seen["subject"]
    assert kind == "unix-process"
    assert int(fields["pid"]) == os.getpid()
    assert int(fields["uid"]) == os.getuid()
    assert int(fields["start-time"]) > 0
    assert seen["action"] == "org.qdistro.broker.approve"
    assert int(seen["flags"]) == 1          # AllowUserInteraction
    assert dict(seen["details"]) == {}
    assert seen["timeout"] >= 120           # outlasts the agent's prompt


@pytest.mark.parametrize("action", ["action_approve_all", "action_deny_all"])
def test_tui_bulk_decide_stops_at_a_cancelled_prompt(action):
    """One cancelled password prompt ends the bulk run: no prompt per
    remaining row."""
    pytest.importorskip("textual")
    import types
    from qdistro_admin_tui import AdminTuiApp

    reqs = [BC.Request(id=i, uid=2000, pid=100 + i, exe="/usr/bin/x",
                       action="a", owner=":1.7") for i in (1, 2, 3)]

    class _Broker(BC.FakeBrokerClient):
        def decide_request(self, rid, decision, scope, owner=""):
            self.decided.append((rid, decision, scope))
            raise BC.AuthNotGranted("cancelled")

    broker = _Broker(pending=reqs)
    notes = []
    fake = types.SimpleNamespace(
        _broker=broker,
        notify=lambda msg, **_k: notes.append(msg),
        push_screen=lambda _screen, cb: cb(True))
    getattr(AdminTuiApp, action)(fake)
    assert [d[0] for d in broker.decided] == [1]
    assert any("1 failure" in n for n in notes), notes


# -- GUI (offscreen) -----------------------------------------------------


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    pytest.importorskip("dbus")
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _bridge(polkit_reply):
    """A BrokerBridge with no bus: the broker proxy and polkit authority
    are fakes. `polkit_reply(reply_handler, error_handler)` answers the
    asynchronous CheckAuthorization."""
    import dbus
    from PyQt6.QtCore import QObject
    import qdistro_admin_app as APP

    class _Authority:
        def __init__(self):
            self.calls = []

        def CheckAuthorization(self, subject, action, details, flags,
                               cancel, reply_handler=None,
                               error_handler=None, timeout=None):
            self.calls.append((subject, action, int(flags), timeout))
            polkit_reply(reply_handler, error_handler)

    b = APP.BrokerBridge.__new__(APP.BrokerBridge)
    QObject.__init__(b)
    b._authorizing = False
    auth = _Authority()
    b._polkit_authority = lambda: auth
    return b, auth, APP, dbus


def _later(fn, ms=0):
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(ms, fn)


def test_gui_authenticates_without_blocking_and_retries(qapp):
    ticks = []
    # While the "password dialog" is up (reply after 50 ms) the app's
    # event loop keeps running: a 10 ms timer fires before the reply.
    b, auth, APP, dbus = _bridge(
        lambda ok, _err: _later(lambda: ok((dbus.Boolean(True),
                                            dbus.Boolean(False), {})), 50))
    _later(lambda: ticks.append("ui"), 10)
    seen = []

    def call():
        seen.append(1)
        if len(seen) == 1:
            raise dbus.DBusException("need auth", name=APP.AUTH_REQUIRED)
        return "applied"
    assert b._with_authority(call) == "applied"
    assert len(seen) == 2 and ticks == ["ui"]
    subject, action, flags, timeout = auth.calls[0]
    assert subject[0] == "unix-process"
    assert int(subject[1]["pid"]) == os.getpid()
    assert action == "org.qdistro.broker.approve"
    assert flags == 1 and timeout >= 120


@pytest.mark.parametrize("answer", ["cancel", "error"])
def test_gui_cancel_or_polkit_error_leaves_request(qapp, answer):
    import dbus

    def reply(ok, err):
        if answer == "cancel":
            _later(lambda: ok((dbus.Boolean(False), dbus.Boolean(True), {})))
        else:
            _later(lambda: err(dbus.DBusException("polkitd gone")))
    b, _auth, APP, _ = _bridge(reply)
    seen = []

    def call():
        seen.append(1)
        raise dbus.DBusException("need auth", name=APP.AUTH_REQUIRED)
    with pytest.raises(dbus.DBusException) as ei:
        b._with_authority(call)
    assert ei.value.get_dbus_name() == APP.AUTH_NOT_GRANTED
    assert seen == [1] and b._authorizing is False
    title, label = APP._friendly_broker_error(ei.value)
    assert "pending" in label


def test_gui_second_prompt_is_refused_while_one_is_open(qapp):
    import dbus
    nested = {}
    holder = {}

    def reply(ok, _err):
        def _reenter():
            try:
                holder["b"]._authorize()
            except dbus.DBusException as e:
                nested["name"] = e.get_dbus_name()
            ok((dbus.Boolean(True), dbus.Boolean(False), {}))
        _later(_reenter)
    b, auth, APP, _ = _bridge(reply)
    holder["b"] = b
    b._authorize()
    assert nested["name"] == APP.AUTH_NOT_GRANTED
    assert len(auth.calls) == 1


def test_gui_only_authority_methods_retry(qapp):
    import dbus
    b, auth, APP, _ = _bridge(lambda ok, _e: _later(
        lambda: ok((dbus.Boolean(True), dbus.Boolean(False), {}))))
    calls = []

    class _Proxy:
        def __getattr__(self, name):
            def m(*a, dbus_interface=None):
                calls.append(name)
                raise dbus.DBusException("x", name=APP.AUTH_REQUIRED)
            return m
    b._proxy = _Proxy()
    with pytest.raises(dbus.DBusException):
        b._call("ListRules")
    assert calls == ["ListRules"] and auth.calls == []
    for name in sorted(APP._AUTHORITY_METHODS):
        calls.clear()
        with pytest.raises(dbus.DBusException):
            b._call(name)
        assert calls == [name, name], name


def test_gui_authority_set_matches_broker():
    pytest.importorskip("dbus")
    pytest.importorskip("PyQt6.QtWidgets")
    import qdistro_admin_app as APP
    import qdistro_admin_broker as B
    assert APP._AUTHORITY_METHODS == B._ADMIN_CONTROL_AUTHORITY_METHODS
    assert APP.BROKER_APPROVE_ACTION == B.BROKER_APPROVE_ACTION
    assert BC.BROKER_APPROVE_ACTION == B.BROKER_APPROVE_ACTION
