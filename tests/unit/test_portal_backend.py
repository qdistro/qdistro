"""Tests for the qdistro xdg-desktop-portal backend.

Exercises the portal backend's Access, FileChooser, Screenshot, and
Notification flows with a mocked broker (CheckPermission /
RequestPermission) and mocked file picker subprocess.

Model mirrors test_pwd_portal.py: build the backend via __new__ to
bypass dbus.service.Object registration, then call methods directly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

dbus = pytest.importorskip("dbus")

# Make the daemons directory importable so we can load the portal backend.
# parents[2] is qdistro/ (the project root above tests/unit/).
_DAEMONS = Path(__file__).resolve().parents[2] / "daemons"
if str(_DAEMONS) not in sys.path:
    sys.path.insert(0, str(_DAEMONS))

from qdistro_portal_backend import (  # type: ignore[import-not-found]
    PORTAL_BUS_NAME,
    PORTAL_OBJ_PATH,
    ACCESS_IFC,
    FILECHOOSER_IFC,
    SCREENSHOT_IFC,
    NOTIFICATION_IFC,
    RESP_SUCCESS,
    RESP_CANCELLED,
    RESP_OTHER,
    QdistroPortalBackend,
    _check_permission,
    _portal_action,
    _request_permission,
    _run_file_picker,
    verify_frontend,
    resolve_app_id,
)
import qdistro_portal_backend as pb  # type: ignore[import-not-found]


# -- Fixtures -----------------------------------------------------------

@pytest.fixture
def backend():
    """Build a QdistroPortalBackend without a real bus connection."""
    obj = QdistroPortalBackend.__new__(QdistroPortalBackend)
    obj._sys_bus = MagicMock()
    return obj


@pytest.fixture(autouse=True)
def _frontend_ok(request):
    """By default, make the D-Bus sender verify as the trusted portal
    frontend so :func:`resolve_app_id` returns the *forwarded* ``app_id``
    argument (scoped to THAT app). Tests that exercise the DENY path opt
    out with the ``no_attest`` marker and drive verification themselves.

    Note: after the corrected finding #8, the broker action is scoped to
    the frontend-forwarded ``app_id`` argument (sanitized), NOT to the
    sender's exe — so positive tests assert the sanitized argument value,
    e.g. ``org.example.App``.
    """
    if request.node.get_closest_marker("no_attest"):
        yield
        return
    with patch("qdistro_portal_backend.verify_frontend",
               return_value=True):
        yield


def _mock_broker(verdict: str):
    """Return a mock broker interface that returns *verdict* for
    CheckPermission and accepts RequestPermission."""
    iface = MagicMock()
    iface.CheckPermission.return_value = verdict
    iface.RequestPermission.return_value = 1
    return iface


def _patch_broker(backend, verdict: str):
    """Patch the system bus so _broker_iface returns a mock with the
    given verdict."""
    iface = _mock_broker(verdict)
    proxy = MagicMock()
    backend._sys_bus.get_object.return_value = proxy

    def _make_iface(p, name):
        return iface

    with patch("qdistro_portal_backend.dbus.Interface", side_effect=_make_iface):
        pass  # just for the mock setup

    # Simpler: patch at module level
    return iface


# -- Module constants ---------------------------------------------------

class TestModuleConstants:
    def test_bus_name(self):
        assert PORTAL_BUS_NAME == "org.freedesktop.impl.portal.qdistro"

    def test_obj_path(self):
        assert PORTAL_OBJ_PATH == "/org/freedesktop/portal/desktop"

    def test_interface_names(self):
        assert ACCESS_IFC == "org.freedesktop.impl.portal.Access"
        assert FILECHOOSER_IFC == "org.freedesktop.impl.portal.FileChooser"
        assert SCREENSHOT_IFC == "org.freedesktop.impl.portal.Screenshot"
        assert NOTIFICATION_IFC == "org.freedesktop.impl.portal.Notification"


# -- Access portal ------------------------------------------------------

class TestAccessDialog:
    def test_access_allow(self, backend):
        """When broker says allow, AccessDialog returns success."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"):
            resp, results = backend.AccessDialog(
                "/handle", "org.example.App", "",
                "Grant access?", "subtitle", "body", {})
        assert int(resp) == RESP_SUCCESS
        assert dict(results) == {}

    def test_access_deny(self, backend):
        """When broker says deny, AccessDialog returns cancelled."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="deny"):
            resp, results = backend.AccessDialog(
                "/handle", "org.example.App", "",
                "Grant access?", "subtitle", "body", {})
        assert int(resp) == RESP_CANCELLED

    def test_access_unknown_fires_request(self, backend):
        """When broker returns unknown, AccessDialog fires a
        RequestPermission and returns cancelled."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="unknown") as mock_check, \
             patch("qdistro_portal_backend._request_permission") as mock_req:
            resp, results = backend.AccessDialog(
                "/handle", "org.example.App", "",
                "Title", "", "", {})
        assert int(resp) == RESP_CANCELLED
        mock_req.assert_called_once()
        args = mock_req.call_args
        assert args[0][1] == "portal.access:org.example.App"
        assert args[0][2]["app_id"] == "org.example.App"
        assert args[0][2]["title"] == "Title"

    def test_access_scopes_to_forwarded_app_id(self, backend):
        """Once the sender is verified as the frontend, the details dict
        carries the FORWARDED app_id argument (the frontend set it after
        authenticating the real app) — scoped to THAT app."""
        captured = {}

        def fake_check(bus, action, details):
            captured.update(details)
            return "allow"

        with patch("qdistro_portal_backend._check_permission",
                   side_effect=fake_check):
            backend.AccessDialog(
                "/handle", "com.example.Foo", "",
                "Camera access", "sub", "body", {})
        # Scoped to the forwarded app_id, not the frontend's identity.
        assert captured["app_id"] == "com.example.Foo"
        assert captured["title"] == "Camera access"


# -- FileChooser portal -------------------------------------------------

class TestFileChooserOpenFile:
    def test_open_save_signature_matches_impl_portal_xml(self):
        for name in ("OpenFile", "SaveFile"):
            method = getattr(QdistroPortalBackend, name)
            assert method._dbus_in_signature == "osssa{sv}"
            assert method._dbus_args == [
                "handle", "app_id", "parent_window", "title", "options"]

    def test_open_denied(self, backend):
        """When broker denies, OpenFile returns cancelled."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="deny"):
            resp, results = backend.OpenFile(
                "/handle", "org.example.App", "", "Open document", {})
        assert int(resp) == RESP_CANCELLED

    def test_open_unknown_fires_request(self, backend):
        """Unknown verdict fires RequestPermission and cancels."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="unknown"), \
             patch("qdistro_portal_backend._request_permission") as mock_req:
            resp, _ = backend.OpenFile(
                "/handle", "org.example.App", "", "Open document", {})
        assert int(resp) == RESP_CANCELLED
        mock_req.assert_called_once()

    def test_open_allowed_with_selection(self, backend):
        """Allow + picker returning a path yields success with URI."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"), \
             patch("qdistro_portal_backend._run_file_picker",
                   return_value=["/home/user/doc.txt"]):
            resp, results = backend.OpenFile(
                "/handle", "org.example.App", "", "Open document", {})
        assert int(resp) == RESP_SUCCESS
        results_d = dict(results)
        uris = list(results_d["uris"])
        assert len(uris) == 1
        assert "doc.txt" in str(uris[0])
        assert str(uris[0]).startswith("file://")

    def test_open_allowed_picker_cancelled(self, backend):
        """Allow but user cancels picker -> cancelled."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"), \
             patch("qdistro_portal_backend._run_file_picker",
                   return_value=[]):
            resp, _ = backend.OpenFile(
                "/handle", "org.example.App", "", "Open document", {})
        assert int(resp) == RESP_CANCELLED

    def test_open_multiple_files(self, backend):
        """Multiple selection returns multiple URIs."""
        paths = ["/tmp/a.txt", "/tmp/b.txt"]
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"), \
             patch("qdistro_portal_backend._run_file_picker",
                   return_value=paths):
            resp, results = backend.OpenFile(
                "/handle", "org.example.App", "",
                "Open documents", {"multiple": True})
        assert int(resp) == RESP_SUCCESS
        uris = list(dict(results)["uris"])
        assert len(uris) == 2


class TestFileChooserSaveFile:
    def test_save_allowed_with_path(self, backend):
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"), \
             patch("qdistro_portal_backend._run_file_picker",
                   return_value=["/home/user/output.pdf"]):
            resp, results = backend.SaveFile(
                "/handle", "org.example.App", "", "Save document", {})
        assert int(resp) == RESP_SUCCESS
        uris = list(dict(results)["uris"])
        assert len(uris) == 1
        assert "output.pdf" in str(uris[0])

    def test_save_denied(self, backend):
        with patch("qdistro_portal_backend._check_permission",
                   return_value="deny"):
            resp, _ = backend.SaveFile(
                "/handle", "org.example.App", "", "Save document", {})
        assert int(resp) == RESP_CANCELLED


# -- Screenshot portal --------------------------------------------------

class TestScreenshot:
    def test_screenshot_allow(self, backend):
        """Until screencopy is wired, allowed Screenshot returns error."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"):
            resp, results = backend.Screenshot(
                "/handle", "org.example.App", "", {})
        assert int(resp) == RESP_OTHER
        assert "uri" not in dict(results)

    def test_screenshot_deny(self, backend):
        """When broker denies, Screenshot returns cancelled."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="deny"):
            resp, _ = backend.Screenshot(
                "/handle", "org.example.App", "", {})
        assert int(resp) == RESP_CANCELLED

    def test_screenshot_unknown_fires_request(self, backend):
        """Unknown triggers RequestPermission and cancels."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="unknown"), \
             patch("qdistro_portal_backend._request_permission") as mock_req:
            resp, _ = backend.Screenshot(
                "/handle", "org.example.App", "", {})
        assert int(resp) == RESP_CANCELLED
        mock_req.assert_called_once()
        args = mock_req.call_args
        assert args[0][1] == "com.qdistro.screen.capture:org.example.App"
        assert args[0][2]["app_id"] == "org.example.App"

    def test_screenshot_action_string(self, backend):
        """The action string sent to broker is com.qdistro.screen.capture
        scoped to the forwarded app_id."""
        captured_action = None

        def fake_check(bus, action, details):
            nonlocal captured_action
            captured_action = action
            return "allow"

        with patch("qdistro_portal_backend._check_permission",
                   side_effect=fake_check):
            backend.Screenshot("/handle", "org.example.App", "", {})
        assert captured_action == "com.qdistro.screen.capture:org.example.App"

    def test_portal_action_scopes_by_app_id(self):
        assert _portal_action("com.qdistro.fs.open", "org.example.App") == \
            "com.qdistro.fs.open:org.example.App"
        assert _portal_action("com.qdistro.fs.open", "weird/app id") == \
            "com.qdistro.fs.open:weird_app_id"


# -- Notification portal ------------------------------------------------

class TestNotification:
    def test_add_notification_allowed(self, backend, capsys):
        """Allowed notification is logged."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"):
            backend.AddNotification("org.example.App", "notif-1", {})
        captured = capsys.readouterr()
        assert "notification allowed" in captured.out
        assert "org.example.App" in captured.out

    def test_add_notification_denied_silent(self, backend, capsys):
        """Denied notification is silently dropped."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="deny"):
            backend.AddNotification("org.example.App", "notif-1", {})
        captured = capsys.readouterr()
        assert "notification allowed" not in captured.out

    def test_add_notification_unknown_fires_request(self, backend):
        """Unknown verdict fires RequestPermission."""
        with patch("qdistro_portal_backend._check_permission",
                   return_value="unknown"), \
             patch("qdistro_portal_backend._request_permission") as mock_req:
            backend.AddNotification("org.example.App", "notif-1", {})
        mock_req.assert_called_once()

    def test_remove_notification_allowed(self, backend, capsys):
        with patch("qdistro_portal_backend._check_permission",
                   return_value="allow"):
            backend.RemoveNotification("org.example.App", "notif-1")
        captured = capsys.readouterr()
        assert "notification removed" in captured.out

    def test_remove_notification_denied(self, backend, capsys):
        with patch("qdistro_portal_backend._check_permission",
                   return_value="deny"):
            backend.RemoveNotification("org.example.App", "notif-1")
        captured = capsys.readouterr()
        assert "notification removed" not in captured.out


# -- Helper: _check_permission ------------------------------------------

class TestCheckPermissionHelper:
    def test_returns_broker_verdict(self):
        mock_bus = MagicMock()
        mock_iface = MagicMock()
        mock_iface.CheckPermission.return_value = "allow"
        with patch("qdistro_portal_backend._broker_iface",
                   return_value=mock_iface):
            result = _check_permission(mock_bus, "some.action", {"k": "v"})
        assert result == "allow"

    def test_dbus_error_returns_deny(self):
        mock_bus = MagicMock()
        with patch("qdistro_portal_backend._broker_iface",
                   side_effect=dbus.DBusException(
                       "broker not running",
                       name="org.freedesktop.DBus.Error.ServiceUnknown")):
            result = _check_permission(mock_bus, "some.action", {})
        assert result == "deny"


# -- Helper: _run_file_picker -------------------------------------------

class TestRunFilePicker:
    def test_picker_returns_selected_path(self):
        with patch("qdistro_portal_backend.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="/home/user/file.txt\n")
            paths = _run_file_picker(title="Open")
        assert paths == ["/home/user/file.txt"]

    def test_picker_cancelled_returns_empty(self):
        with patch("qdistro_portal_backend.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            paths = _run_file_picker(title="Open")
        assert paths == []

    def test_picker_multiple_returns_split(self):
        with patch("qdistro_portal_backend.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="/tmp/a.txt|/tmp/b.txt\n")
            paths = _run_file_picker(title="Open", multiple=True)
        assert paths == ["/tmp/a.txt", "/tmp/b.txt"]

    def test_picker_oserror_returns_empty(self):
        with patch("qdistro_portal_backend.subprocess.run",
                   side_effect=OSError("not found")):
            paths = _run_file_picker(title="Open")
        assert paths == []

    def test_picker_timeout_returns_empty(self):
        import subprocess as sp
        with patch("qdistro_portal_backend.subprocess.run",
                   side_effect=sp.TimeoutExpired("zenity", 120)):
            paths = _run_file_picker(title="Open")
        assert paths == []


# -- Sender = frontend verification (finding #8, corrected) ------------

class TestVerifyFrontend:
    """verify_frontend must attest the D-Bus sender's real pid/exe and
    return True ONLY when the sender is the trusted XDG portal frontend
    (xdg-desktop-portal). Anything else fails closed (False).
    """

    def test_missing_sender_denies(self):
        assert verify_frontend("") is False
        assert verify_frontend(None) is False

    def test_unresolvable_pid_denies(self):
        with patch("qdistro_portal_backend._caller_pid", return_value=0):
            assert verify_frontend(":1.42") is False

    def test_zero_starttime_denies(self):
        with patch("qdistro_portal_backend._caller_pid", return_value=1234), \
             patch.object(pb._pi, "read_starttime", return_value=0):
            assert verify_frontend(":1.42") is False

    def test_missing_exe_denies(self):
        with patch("qdistro_portal_backend._caller_pid", return_value=1234), \
             patch.object(pb._pi, "read_starttime", return_value=999), \
             patch.object(pb._pi, "read_exe", return_value="?"):
            assert verify_frontend(":1.42") is False

    def test_non_frontend_exe_denies(self):
        """A direct same-session caller whose exe is NOT the portal
        frontend (e.g. a malicious app trying to impersonate the frontend
        and forge app_id) is denied."""
        with patch("qdistro_portal_backend._caller_pid", return_value=1234), \
             patch.object(pb._pi, "read_starttime", return_value=999), \
             patch.object(pb._pi, "read_exe",
                          return_value="/usr/bin/evil-app"):
            assert verify_frontend(":1.42") is False

    @staticmethod
    def _a_real_root_file():
        """A real, root-owned, non-group/other-writable regular file on this
        host (used as a stand-in for the frontend binary so the root-owned
        stat check runs against the real filesystem)."""
        import os as _os
        import stat as _stat
        for cand in ("/usr/bin/python3", "/bin/true", "/usr/bin/env",
                     "/bin/sh", "/usr/bin/cat"):
            try:
                st = _os.stat(_os.path.realpath(cand))
            except OSError:
                continue
            if (_stat.S_ISREG(st.st_mode) and st.st_uid == 0
                    and not st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH)):
                return _os.path.realpath(cand)
        pytest.skip("no root-owned reference binary available")

    def test_frontend_exe_verifies(self):
        """A sender whose exe FULL PATH is a canonical, root-owned frontend
        binary verifies."""
        real = self._a_real_root_file()
        with patch("qdistro_portal_backend._caller_pid", return_value=1234), \
             patch.object(pb._pi, "read_starttime", return_value=999), \
             patch.object(pb, "_FRONTEND_EXE_PATHS", frozenset({real})), \
             patch.object(pb._pi, "read_exe", return_value=real):
            assert verify_frontend(":1.42") is True

    def test_tmp_basename_spoof_denies(self):
        """Finding #8 (second review): a same-uid attacker running a
        self-authored binary NAMED xdg-desktop-portal out of /tmp must be
        denied. The old basename check accepted it; the full-path check
        rejects it because /tmp/xdg-desktop-portal is not on the allowlist."""
        with patch("qdistro_portal_backend._caller_pid", return_value=1234), \
             patch.object(pb._pi, "read_starttime", return_value=999), \
             patch.object(pb._pi, "read_exe",
                          return_value="/tmp/xdg-desktop-portal"):
            assert verify_frontend(":1.42") is False

    def test_non_root_owned_frontend_path_denies(self, tmp_path):
        """Even a path ON the allowlist is rejected when the file is not
        root-owned (so an attacker who got a same-name file onto the
        allowlist via a writable location still can't pass)."""
        fake = tmp_path / "xdg-desktop-portal"
        fake.write_text("#!/bin/sh\n")  # owned by the test user, not root
        with patch("qdistro_portal_backend._caller_pid", return_value=1234), \
             patch.object(pb._pi, "read_starttime", return_value=999), \
             patch.object(pb, "_FRONTEND_EXE_PATHS",
                          frozenset({str(fake)})), \
             patch.object(pb._pi, "read_exe", return_value=str(fake)):
            assert verify_frontend(":1.42") is False

    def test_no_proc_identity_module_denies(self, monkeypatch):
        monkeypatch.setattr(pb, "_pi", None)
        assert verify_frontend(":1.42") is False


class TestResolveAppId:
    """resolve_app_id denies a non-frontend sender, and for a verified
    frontend returns the FORWARDED app_id argument (scoped to THAT app —
    not to xdg-desktop-portal)."""

    def test_non_frontend_sender_denied(self):
        with patch("qdistro_portal_backend.verify_frontend",
                   return_value=False):
            app, ok = resolve_app_id("org.real.App", ":1.99")
        assert ok is False and app == ""

    def test_frontend_scopes_to_forwarded_app_id(self):
        """The key correctness assertion for finding #8's fix: a legit
        frontend call is scoped to the forwarded app_id, NOT collapsed to
        the frontend ('xdg-desktop-portal'). Two different forwarded
        app_ids must yield two different scopes."""
        with patch("qdistro_portal_backend.verify_frontend",
                   return_value=True):
            a, ok_a = resolve_app_id("org.app.Aaa", ":1.10")
            b, ok_b = resolve_app_id("org.app.Bbb", ":1.10")
        assert ok_a and a == "org.app.Aaa"
        assert ok_b and b == "org.app.Bbb"
        assert a != b  # not collapsed to one principal


@pytest.mark.no_attest
class TestPortalMethodsDenyNonFrontend:
    """Every gated portal method must DENY when the D-Bus sender is NOT
    the verified portal frontend — a direct same-session caller forging
    app_id must never reach a broker decision.

    Regression for the corrected finding #8: the broken first remediation
    attested the SENDER's exe (always xdg-desktop-portal) and scoped every
    app to one principal; the corrected version verifies the sender IS the
    frontend and otherwise denies.
    """

    def _deny(self):
        return patch("qdistro_portal_backend.verify_frontend",
                     return_value=False)

    def test_access_dialog_denies(self, backend):
        with self._deny(), \
             patch("qdistro_portal_backend._check_permission") as chk:
            resp, _ = backend.AccessDialog(
                "/h", "evil.spoof", "", "t", "s", "b", {}, sender=":1.99")
        assert int(resp) == RESP_CANCELLED
        # Fail closed BEFORE any broker decision is made.
        chk.assert_not_called()

    def test_open_file_denies(self, backend):
        with self._deny(), \
             patch("qdistro_portal_backend._check_permission") as chk:
            resp, _ = backend.OpenFile(
                "/h", "evil.spoof", "", "Open", {}, sender=":1.99")
        assert int(resp) == RESP_CANCELLED
        chk.assert_not_called()

    def test_save_file_denies(self, backend):
        with self._deny(), \
             patch("qdistro_portal_backend._check_permission") as chk:
            resp, _ = backend.SaveFile(
                "/h", "evil.spoof", "", "Save", {}, sender=":1.99")
        assert int(resp) == RESP_CANCELLED
        chk.assert_not_called()

    def test_screenshot_denies(self, backend):
        with self._deny(), \
             patch("qdistro_portal_backend._check_permission") as chk:
            resp, _ = backend.Screenshot(
                "/h", "evil.spoof", "", {}, sender=":1.99")
        assert int(resp) == RESP_CANCELLED
        chk.assert_not_called()

    def test_add_notification_denies(self, backend):
        with self._deny(), \
             patch("qdistro_portal_backend._check_permission") as chk:
            backend.AddNotification("evil.spoof", "n1", {}, sender=":1.99")
        chk.assert_not_called()

    def test_remove_notification_denies(self, backend):
        with self._deny(), \
             patch("qdistro_portal_backend._check_permission") as chk:
            backend.RemoveNotification("evil.spoof", "n1", sender=":1.99")
        chk.assert_not_called()


@pytest.mark.no_attest
class TestPortalMethodsScopeToForwardedAppId:
    """A legitimate frontend call (verified sender) scopes the broker
    action to the FORWARDED app_id argument — to THAT app, not to
    xdg-desktop-portal. This is the positive half of finding #8's fix.
    """

    def _frontend(self):
        return patch("qdistro_portal_backend.verify_frontend",
                     return_value=True)

    def test_access_dialog_scopes_forwarded(self, backend):
        captured = {}

        def fake_check(bus, action, details):
            captured["action"] = action
            captured.update(details)
            return "allow"

        with self._frontend(), \
             patch("qdistro_portal_backend._check_permission",
                   side_effect=fake_check):
            backend.AccessDialog(
                "/h", "org.real.App", "", "t", "s", "b", {},
                sender=":1.5")
        assert captured["app_id"] == "org.real.App"
        assert captured["action"] == "portal.access:org.real.App"

    def test_notification_scopes_forwarded(self, backend, capsys):
        with self._frontend(), \
             patch("qdistro_portal_backend._check_permission",
                   return_value="allow"):
            backend.AddNotification("org.real.App", "n1", {}, sender=":1.5")
        out = capsys.readouterr().out
        assert "org.real.App" in out
        assert "xdg-desktop-portal" not in out


@pytest.mark.no_attest
class TestPortalMethodsHaveSenderKeyword:
    """Each gated method must declare sender_keyword so dbus-python passes
    the kernel-attested sender; without it the attestation is impossible.
    """

    @pytest.mark.parametrize("name", [
        "AccessDialog", "OpenFile", "SaveFile", "Screenshot",
        "AddNotification", "RemoveNotification",
    ])
    def test_method_declares_sender_keyword(self, name):
        method = getattr(QdistroPortalBackend, name)
        assert getattr(method, "_dbus_sender_keyword", None) == "sender"
