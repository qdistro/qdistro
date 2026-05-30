"""qdistro portal frontend (permission-lineage Option A) — pure-core tests.

The frontend owns ``org.freedesktop.portal.Desktop`` on a silo session bus,
kernel-authenticates the app's *own* connection pid, reads its starttime
from ``/proc``, and relays that (pid, starttime) to the broker's
``CheckPermissionForClient`` / ``RequestPermissionForClient``. These tests
exercise the pure core (no live bus) and prove the security invariants:

- the broker is called with the RESOLVED (pid, starttime), never any
  app-claimed pid/app_id;
- a gone / recycled / unreadable ``/proc`` entry fails closed (the broker
  is NOT consulted, the response is CANCELLED);
- an app-supplied pid or app_id field is ignored for the decision;
- broker verdicts map to the correct portal Response codes, with a
  fail-closed default for ``unknown`` / unexpected values.
"""
from __future__ import annotations

import qdistro_portal_frontend as F
import qdistro_proc_identity as pi


# A pid the kernel reports for the app's own connection.
APP_PID = 4242
APP_START = 990011


class _RecordingBroker(F.BrokerClient):
    """Stub broker that records exactly what the frontend relayed."""

    def __init__(self, verdict="allow", rid=7):
        self._verdict = verdict
        self._rid = rid
        self.check_calls = []
        self.request_calls = []

    def check_for_client(self, action, details, client_pid, client_starttime):
        self.check_calls.append((action, dict(details), client_pid,
                                 client_starttime))
        return self._verdict

    def request_for_client(self, action, details, client_pid,
                           client_starttime):
        self.request_calls.append((action, dict(details), client_pid,
                                   client_starttime))
        return self._rid


def _live_proc(monkeypatch, *, pid=APP_PID, starttime=APP_START):
    monkeypatch.setattr(
        pi, "read_starttime",
        lambda p: starttime if int(p) == pid else 0)


# --- resolve_client_tuple: kernel pid + /proc starttime, fail-closed -----

def test_resolve_reads_starttime_from_proc(monkeypatch):
    _live_proc(monkeypatch)
    assert F.resolve_client_tuple(APP_PID) == (APP_PID, APP_START, True)


def test_resolve_gone_process_fails_closed(monkeypatch):
    # starttime 0 == gone / unreadable: ok must be False.
    monkeypatch.setattr(pi, "read_starttime", lambda p: 0)
    pid, start, ok = F.resolve_client_tuple(APP_PID)
    assert ok is False
    assert start == 0


def test_resolve_rejects_nonpositive_pid(monkeypatch):
    # A negative/zero connection pid (e.g. GetConnectionUnixProcessID
    # failed) must never be looked up or trusted.
    called = {"n": 0}

    def _spy(p):
        called["n"] += 1
        return APP_START
    monkeypatch.setattr(pi, "read_starttime", _spy)
    assert F.resolve_client_tuple(0)[2] is False
    assert F.resolve_client_tuple(-1)[2] is False
    assert called["n"] == 0  # never even hit /proc


# --- handle_access: relays the RESOLVED tuple, never app claims ----------

def test_broker_called_with_resolved_pid_and_starttime(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    resp = F.handle_access("portal.access", "org.example.App",
                           client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_SUCCESS
    assert len(broker.check_calls) == 1
    action, details, pid, start = broker.check_calls[0]
    # The broker got the kernel pid and the /proc-read starttime.
    assert pid == APP_PID
    assert start == APP_START


def test_app_supplied_pid_field_is_ignored(monkeypatch):
    # The app smuggles a different pid into the portal options; the
    # frontend must relay only the kernel-attested connection pid.
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    F.handle_access(
        "portal.access", "org.example.App",
        client_pid=APP_PID, broker=broker,
        extra={"pid": "999999", "client_pid": "1"})
    _action, details, pid, start = broker.check_calls[0]
    assert pid == APP_PID  # NOT 999999 / 1
    assert start == APP_START
    assert pid != 999999 and pid != 1
    # The app's pid claim may ride in advisory details but must NOT be the
    # decision pid/starttime the broker trusts.
    assert (pid, start) == (APP_PID, APP_START)


def test_app_id_is_advisory_not_a_trusted_arg(monkeypatch):
    # The app_id the app claims is sanitized into details (advisory, for
    # shadow logging) but is NEVER passed as a pid/starttime the broker
    # would trust for the decision — those come from /proc resolution.
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    F.handle_access("portal.access", "../../etc/passwd\nINJECT",
                    client_pid=APP_PID, broker=broker)
    action, details, pid, start = broker.check_calls[0]
    # sanitized — no control chars / path traversal survive
    assert "\n" not in details["app_id"]
    assert "/" not in details["app_id"]
    # the decision subject is still the resolved pid, not the app_id
    assert (pid, start) == (APP_PID, APP_START)


# --- fail-closed: an unauthenticatable client never reaches the broker ---

def test_gone_client_does_not_call_broker(monkeypatch):
    monkeypatch.setattr(pi, "read_starttime", lambda p: 0)
    broker = _RecordingBroker(verdict="allow")
    resp = F.handle_access("portal.access", "org.example.App",
                           client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert broker.check_calls == []  # broker NOT consulted
    assert broker.request_calls == []


def test_recycled_client_fails_closed(monkeypatch):
    # Resolver returns ok=False (e.g. starttime drift detected upstream);
    # handle_access must refuse without calling the broker.
    def _recycled(_pid):
        return (APP_PID, 0, False)
    broker = _RecordingBroker(verdict="allow")
    resp = F.handle_access("portal.access", "org.example.App",
                           client_pid=APP_PID, broker=broker,
                           resolver=_recycled)
    assert resp == F.RESP_CANCELLED
    assert broker.check_calls == []


def test_bad_connection_pid_fails_closed(monkeypatch):
    # GetConnectionUnixProcessID failed -> wrapper passes -1.
    monkeypatch.setattr(pi, "read_starttime", lambda p: APP_START)
    broker = _RecordingBroker(verdict="allow")
    resp = F.handle_access("portal.access", "org.example.App",
                           client_pid=-1, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert broker.check_calls == []


# --- unknown -> async prompt with the resolved tuple, fail-closed resp ---

def test_unknown_fires_request_for_client_with_resolved_tuple(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="unknown")
    resp = F.handle_access("portal.access", "org.example.App",
                           client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED  # unknown is not a success
    assert len(broker.request_calls) == 1
    _a, _d, pid, start = broker.request_calls[0]
    assert (pid, start) == (APP_PID, APP_START)


def test_allow_does_not_fire_request(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    F.handle_access("portal.access", "org.example.App",
                    client_pid=APP_PID, broker=broker)
    assert broker.request_calls == []


def test_broker_exception_fails_closed(monkeypatch):
    _live_proc(monkeypatch)

    class _Boom(F.BrokerClient):
        def check_for_client(self, *a):
            raise RuntimeError("broker down")

    resp = F.handle_access("portal.access", "org.example.App",
                           client_pid=APP_PID, broker=_Boom())
    assert resp == F.RESP_CANCELLED


# --- decision_to_response: verdict mapping with fail-closed default ------

def test_decision_mapping():
    assert F.decision_to_response("allow") == F.RESP_SUCCESS
    assert F.decision_to_response("deny") == F.RESP_CANCELLED
    # fail-closed: unknown / garbage are NOT success
    assert F.decision_to_response("unknown") == F.RESP_CANCELLED
    assert F.decision_to_response("") == F.RESP_CANCELLED
    assert F.decision_to_response("ALLOW") == F.RESP_CANCELLED


def test_deny_returns_cancelled(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="deny")
    resp = F.handle_access("portal.access", "org.example.App",
                           client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert broker.request_calls == []  # deny is decisive, no prompt


# --- build_broker_call: action scoping + advisory details ----------------

def test_build_broker_call_scopes_action_and_sanitizes():
    action, details = F.build_broker_call("portal.access", "org.Example.App")
    assert action == "portal.access:org.Example.App"
    assert details == {"app_id": "org.Example.App"}


def test_build_broker_call_empty_app_id_defaults_unknown():
    action, details = F.build_broker_call("portal.access", "")
    assert action == "portal.access:unknown"
    assert details["app_id"] == "unknown"


# === Remaining portal methods on the same attested core ==================
#
# Each method must: (1) relay the RESOLVED (pid, starttime), never an
# app-claimed pid/app_id; (2) fail closed (no broker call) on a gone /
# recycled client; (3) ignore an app-supplied pid/app_id for the decision;
# (4) return the SAFE empty/dropped result on the denied path.

# Each method scopes its own broker action (mirrors the backend naming).
_FS_ACTION = "com.qdistro.fs.open"
_SHOT_ACTION = "com.qdistro.screen.capture"
_NOTIFY_ACTION = "portal.notification"


# --- FileChooser.OpenFile -----------------------------------------------

def test_open_file_allow_uses_fs_action_and_resolved_tuple(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    resp, results = F.handle_open_file("org.example.App",
                                       client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_SUCCESS
    assert results == {}  # no files leaked by the pure gate itself
    assert len(broker.check_calls) == 1
    action, _details, pid, start = broker.check_calls[0]
    # FileChooser-scoped action, decided against the RESOLVED tuple.
    assert action == f"{_FS_ACTION}:org.example.App"
    assert (pid, start) == (APP_PID, APP_START)


def test_open_file_deny_returns_no_files(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="deny")
    resp, results = F.handle_open_file("org.example.App",
                                       client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    # The denied path must carry NO file URIs at all.
    assert results == {}
    assert "uris" not in results
    assert broker.request_calls == []  # deny is decisive, no prompt


def test_open_file_gone_client_fails_closed_no_broker(monkeypatch):
    monkeypatch.setattr(pi, "read_starttime", lambda p: 0)
    broker = _RecordingBroker(verdict="allow")
    resp, results = F.handle_open_file("org.example.App",
                                       client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert results == {}
    assert broker.check_calls == []  # unauthenticatable -> no decision
    assert broker.request_calls == []


def test_open_file_recycled_client_fails_closed(monkeypatch):
    def _recycled(_pid):
        return (APP_PID, 0, False)
    broker = _RecordingBroker(verdict="allow")
    resp, results = F.handle_open_file("org.example.App", client_pid=APP_PID,
                                       broker=broker, resolver=_recycled)
    assert resp == F.RESP_CANCELLED
    assert results == {}
    assert broker.check_calls == []


def test_open_file_app_supplied_pid_ignored(monkeypatch):
    # App smuggles a foreign pid via extra; the decision must use ONLY the
    # kernel-attested connection pid.
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    F.handle_open_file("org.example.App", client_pid=APP_PID, broker=broker,
                       extra={"pid": "999999", "client_pid": "1"})
    _a, _d, pid, start = broker.check_calls[0]
    assert (pid, start) == (APP_PID, APP_START)
    assert pid != 999999 and pid != 1


def test_open_file_broker_exception_fails_closed(monkeypatch):
    _live_proc(monkeypatch)

    class _Boom(F.BrokerClient):
        def check_for_client(self, *a):
            raise RuntimeError("broker down")

    resp, results = F.handle_open_file("org.example.App", client_pid=APP_PID,
                                       broker=_Boom())
    assert resp == F.RESP_CANCELLED
    assert results == {}


def test_open_file_unknown_prompts_with_resolved_tuple(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="unknown")
    resp, results = F.handle_open_file("org.example.App",
                                       client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED  # unknown is not a success
    assert results == {}
    assert len(broker.request_calls) == 1
    _a, _d, pid, start = broker.request_calls[0]
    assert (pid, start) == (APP_PID, APP_START)


# --- FileChooser.SaveFile -----------------------------------------------

def test_save_file_allow_uses_fs_action(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    resp, results = F.handle_save_file("org.example.App",
                                       client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_SUCCESS
    assert results == {}
    action, _d, pid, start = broker.check_calls[0]
    assert action == f"{_FS_ACTION}:org.example.App"
    assert (pid, start) == (APP_PID, APP_START)


def test_save_file_deny_returns_no_files(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="deny")
    resp, results = F.handle_save_file("org.example.App",
                                       client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert results == {}


def test_save_file_gone_client_fails_closed_no_broker(monkeypatch):
    monkeypatch.setattr(pi, "read_starttime", lambda p: 0)
    broker = _RecordingBroker(verdict="allow")
    resp, results = F.handle_save_file("org.example.App",
                                       client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert results == {}
    assert broker.check_calls == []


def test_save_file_app_supplied_pid_ignored(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    F.handle_save_file("org.example.App", client_pid=APP_PID, broker=broker,
                       extra={"client_pid": "1"})
    _a, _d, pid, start = broker.check_calls[0]
    assert (pid, start) == (APP_PID, APP_START)


# --- Screenshot ----------------------------------------------------------

def test_screenshot_allow_uses_capture_action_and_resolved_tuple(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    resp, results = F.handle_screenshot("org.example.App",
                                        client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_SUCCESS
    assert results == {}  # no uri leaked by the pure gate
    action, _d, pid, start = broker.check_calls[0]
    assert action == f"{_SHOT_ACTION}:org.example.App"
    assert (pid, start) == (APP_PID, APP_START)


def test_screenshot_deny_returns_no_uri(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="deny")
    resp, results = F.handle_screenshot("org.example.App",
                                        client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert results == {}
    assert "uri" not in results


def test_screenshot_gone_client_fails_closed_no_broker(monkeypatch):
    monkeypatch.setattr(pi, "read_starttime", lambda p: 0)
    broker = _RecordingBroker(verdict="allow")
    resp, results = F.handle_screenshot("org.example.App",
                                        client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert results == {}
    assert broker.check_calls == []


def test_screenshot_app_supplied_pid_ignored(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    F.handle_screenshot("org.example.App", client_pid=APP_PID, broker=broker,
                        extra={"pid": "999999"})
    _a, _d, pid, start = broker.check_calls[0]
    assert (pid, start) == (APP_PID, APP_START)
    assert pid != 999999


def test_screenshot_broker_exception_fails_closed(monkeypatch):
    _live_proc(monkeypatch)

    class _Boom(F.BrokerClient):
        def check_for_client(self, *a):
            raise RuntimeError("broker down")

    resp, results = F.handle_screenshot("org.example.App", client_pid=APP_PID,
                                        broker=_Boom())
    assert resp == F.RESP_CANCELLED
    assert results == {}


def test_screenshot_unknown_prompts_with_resolved_tuple(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="unknown")
    resp, results = F.handle_screenshot("org.example.App",
                                        client_pid=APP_PID, broker=broker)
    assert resp == F.RESP_CANCELLED
    assert results == {}
    _a, _d, pid, start = broker.request_calls[0]
    assert (pid, start) == (APP_PID, APP_START)


# --- Notification (boolean gate: True == show, False == drop) ------------

def test_notification_allow_shows_with_resolved_tuple(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    assert F.handle_notification("org.example.App",
                                 client_pid=APP_PID, broker=broker) is True
    action, _d, pid, start = broker.check_calls[0]
    assert action == f"{_NOTIFY_ACTION}:org.example.App"
    assert (pid, start) == (APP_PID, APP_START)


def test_notification_deny_dropped(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="deny")
    assert F.handle_notification("org.example.App",
                                 client_pid=APP_PID, broker=broker) is False
    assert broker.request_calls == []


def test_notification_gone_client_dropped_no_broker(monkeypatch):
    monkeypatch.setattr(pi, "read_starttime", lambda p: 0)
    broker = _RecordingBroker(verdict="allow")
    assert F.handle_notification("org.example.App",
                                 client_pid=APP_PID, broker=broker) is False
    assert broker.check_calls == []  # unauthenticatable -> no decision


def test_notification_app_supplied_pid_ignored(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="allow")
    F.handle_notification("org.example.App", client_pid=APP_PID,
                          broker=broker, extra={"client_pid": "1"})
    _a, _d, pid, start = broker.check_calls[0]
    assert (pid, start) == (APP_PID, APP_START)
    assert pid != 1


def test_notification_broker_exception_dropped(monkeypatch):
    _live_proc(monkeypatch)

    class _Boom(F.BrokerClient):
        def check_for_client(self, *a):
            raise RuntimeError("broker down")

    assert F.handle_notification("org.example.App", client_pid=APP_PID,
                                 broker=_Boom()) is False


def test_notification_unknown_dropped_but_prompts(monkeypatch):
    _live_proc(monkeypatch)
    broker = _RecordingBroker(verdict="unknown")
    assert F.handle_notification("org.example.App",
                                 client_pid=APP_PID, broker=broker) is False
    _a, _d, pid, start = broker.request_calls[0]
    assert (pid, start) == (APP_PID, APP_START)
