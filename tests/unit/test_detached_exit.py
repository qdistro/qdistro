"""Unit tests for the fail-closed Wayland-bind behavior in app.main().

A locker that fails to bind qdwin_locker_v1 cannot drive the lock:
wayland.LockerClient.set_locked() is a no-op without a bound proxy, so a
"detached" process looks healthy to systemd while being unable to ever
lock the screen — masking the breakage indefinitely.

These tests pin the fail-closed contract:

  - a failed client.connect() makes main() return non-zero (so the unit's
    Restart=always re-attempts the bind with backoff) and NEVER signals
    READY=1;
  - QDLOCKER_ALLOW_DETACHED=1 opts back into staying up detached, but STILL
    withholds READY=1 (the locker is up but cannot lock);
  - a successful bind enters the event loop and signals READY normally.

main() is driven with QDLOCKER_CTRL_SOCKET=0 (no real unix socket) and a
patched LockerClient / QGuiApplication so app.exec() returns immediately.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from qdlocker import app as app_mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Deterministic env: no real systemd notify socket, no ctrl unix
    # socket, default (compositor-expected) wayland mode.
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("QDLOCKER_NO_WAYLAND", raising=False)
    monkeypatch.delenv("QDLOCKER_ALLOW_DETACHED", raising=False)
    monkeypatch.setenv("QDLOCKER_CTRL_SOCKET", "0")
    yield


def _run_main(connect_result):
    """Drive app.main() with a stub LockerClient (connect() -> given result)
    and a stub QGuiApplication whose exec() returns 0 immediately.

    Returns (return_code, notify_ready_called, client_mock).
    """
    client = MagicMock()
    client.connect.return_value = connect_result
    client._display = None  # idle watcher start is skipped

    qapp = MagicMock()
    qapp.exec.return_value = 0

    engine = MagicMock()
    engine.rootObjects.return_value = [object()]  # QML "loaded"

    notify_calls: list[int] = []

    with patch.object(app_mod, "LockerClient", return_value=client), \
         patch.object(app_mod, "QGuiApplication", return_value=qapp), \
         patch.object(app_mod, "QQmlApplicationEngine", return_value=engine), \
         patch.object(app_mod, "LogindWatcher"), \
         patch.object(app_mod, "IdleWatcher"), \
         patch.object(app_mod, "CtrlSocket"), \
         patch.object(app_mod, "load_config",
                      return_value=dict(app_mod._DEFAULT_CONFIG)), \
         patch.object(app_mod, "qmlRegisterUncreatableType"), \
         patch.object(app_mod, "_notify_ready",
                      side_effect=lambda: notify_calls.append(1)):
        rc = app_mod.main(["qdlocker"])
    return rc, bool(notify_calls), client


def test_bind_failure_exits_nonzero_and_withholds_ready():
    rc, notified, client = _run_main(connect_result=False)
    # Fail closed: non-zero so Restart=always re-attempts.
    assert rc != 0
    # Never signal readiness for a locker that cannot drive the lock.
    assert notified is False
    # And we tore down the half-open client before bailing.
    client.disconnect.assert_called_once()


def test_allow_detached_stays_up_but_still_withholds_ready(monkeypatch):
    monkeypatch.setenv("QDLOCKER_ALLOW_DETACHED", "1")
    rc, notified, client = _run_main(connect_result=False)
    # Opt-out: we stay up (enter exec, which our stub returns 0 from)...
    assert rc == 0
    # ...but a detached locker that cannot lock must NOT signal READY=1.
    assert notified is False
    # Staying up: we did not disconnect/bail.
    client.disconnect.assert_not_called()


def test_successful_bind_enters_loop_and_signals_ready():
    rc, notified, client = _run_main(connect_result=True)
    assert rc == 0
    assert notified is True
    client.disconnect.assert_not_called()


def test_no_wayland_dev_mode_signals_ready_without_binding(monkeypatch):
    """QDLOCKER_NO_WAYLAND=1 (dev/standalone, no compositor expected) must
    stay up and signal READY=1 exactly as before — the fail-closed gate is
    keyed on the compositor-expected mode and must not regress this path."""
    monkeypatch.setenv("QDLOCKER_NO_WAYLAND", "1")
    # connect() must never be called in dev mode; make it explode if it is.
    rc, notified, client = _run_main(connect_result=True)
    assert rc == 0
    assert notified is True
    client.connect.assert_not_called()
    client.disconnect.assert_not_called()
