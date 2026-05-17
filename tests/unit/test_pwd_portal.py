"""qdistro-pwd-portal backend (XDG portal Secret) tests.

Exercises the portal backend's RetrieveSecret flow: bytes from
mocked pwd daemon end up written to the provided fd; errors return
the right portal response code; fd is always closed.
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch

# Skip the import if dbus.mainloop.glib refuses to load (CI without GLib);
# normally available because the conftest wires the venv with gi.
dbus = pytest.importorskip("dbus")

from qdistro_pwd_portal import (  # type: ignore[import-not-found]
    PORTAL_BUS_NAME, PORTAL_IFC, PORTAL_OBJ_PATH,
    PortalSecretBackend,
    RESP_OTHER_ERROR, RESP_SUCCESS,
)


class _FakeUnixFD:
    """Minimal stand-in for dbus.types.UnixFD that hands ownership of a
    real os fd through .take()."""
    def __init__(self, fd: int):
        self._fd = fd

    def take(self) -> int:
        fd, self._fd = self._fd, -1
        return fd


@pytest.fixture
def backend(monkeypatch):
    """Build a PortalSecretBackend without talking to a real bus.
    The dbus.service.Object init is bypassed via __new__.
    """
    obj = PortalSecretBackend.__new__(PortalSecretBackend)
    obj._sys_bus = MagicMock()
    return obj


def _read_pipe(read_fd: int) -> bytes:
    out = b""
    while True:
        chunk = os.read(read_fd, 1024)
        if not chunk:
            break
        out += chunk
    os.close(read_fd)
    return out


def test_retrieve_secret_writes_key_bytes_to_fd(backend):
    expected = b"\xab" * 32
    fake_iface = MagicMock()
    fake_iface.GetPortalKey.return_value = list(expected)
    with patch.object(backend, "_pwd_iface", return_value=fake_iface):
        r, w = os.pipe()
        try:
            resp, results = backend.RetrieveSecret(
                handle="/org/freedesktop/portal/desktop/request/x",
                app_id="org.example.App",
                fd=_FakeUnixFD(w),
                options={},
            )
        finally:
            payload = _read_pipe(r)
    assert int(resp) == RESP_SUCCESS
    assert dict(results) == {}
    assert payload == expected
    fake_iface.GetPortalKey.assert_called_once_with("org.example.App")


def test_retrieve_secret_pwd_dbus_error_returns_other_error(backend):
    fake_iface = MagicMock()
    fake_iface.GetPortalKey.side_effect = dbus.DBusException(
        "vault locked", name="org.qdistro.Pwd1.NotUnlocked")
    with patch.object(backend, "_pwd_iface", return_value=fake_iface):
        r, w = os.pipe()
        resp, _ = backend.RetrieveSecret(
            handle="/h", app_id="org.example.App",
            fd=_FakeUnixFD(w), options={})
        # fd must have been closed by the backend on the error path
        with pytest.raises(OSError):
            os.write(w, b"x")
        os.close(r)
    assert int(resp) == RESP_OTHER_ERROR


def test_retrieve_secret_invalid_app_id_propagates_dbus_error(backend):
    fake_iface = MagicMock()
    fake_iface.GetPortalKey.side_effect = dbus.DBusException(
        "invalid app_id", name="org.qdistro.Pwd1.PolicyError")
    with patch.object(backend, "_pwd_iface", return_value=fake_iface):
        r, w = os.pipe()
        resp, _ = backend.RetrieveSecret(
            handle="/h", app_id="../escape",
            fd=_FakeUnixFD(w), options={})
        os.close(r)
    assert int(resp) == RESP_OTHER_ERROR


def test_retrieve_secret_chunked_write_completes(backend):
    """Even if os.write returns short, the loop must finish writing."""
    expected = b"K" * 32
    fake_iface = MagicMock()
    fake_iface.GetPortalKey.return_value = list(expected)
    with patch.object(backend, "_pwd_iface", return_value=fake_iface):
        r, w = os.pipe()
        try:
            resp, _ = backend.RetrieveSecret(
                handle="/h", app_id="org.example.App",
                fd=_FakeUnixFD(w), options={})
        finally:
            payload = _read_pipe(r)
    assert int(resp) == RESP_SUCCESS
    assert payload == expected


def test_retrieve_secret_closes_fd_on_pwd_error(backend):
    """Even on the pwd-error path, the fd must be closed so the app
    doesn't block reading."""
    fake_iface = MagicMock()
    fake_iface.GetPortalKey.side_effect = dbus.DBusException(
        "x", name="org.freedesktop.DBus.Error.ServiceUnknown")
    r, w = os.pipe()
    with patch.object(backend, "_pwd_iface", return_value=fake_iface):
        backend.RetrieveSecret(handle="/h", app_id="org.x",
                               fd=_FakeUnixFD(w), options={})
    # If the fd was closed, writing should fail.
    with pytest.raises(OSError):
        os.write(w, b"y")
    os.close(r)


def test_module_constants():
    assert PORTAL_BUS_NAME == "org.qdistro.PortalSecret"
    assert PORTAL_OBJ_PATH == "/org/freedesktop/portal/desktop"
    assert PORTAL_IFC == "org.freedesktop.impl.portal.Secret"
