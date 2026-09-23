"""Tests for qdbrowser.qdistro_integration — the App1 receiver wiring.

The SDK + dbus-python are mocked via ``sys.modules`` injection so the
tests run without a session bus. We assert:

  * ``maybe_install`` returns None when the SDK is absent.
  * ``maybe_install`` returns the receiver when the SDK is present.
  * Receive callback opens a URL in a new tab when given a uri-list.
  * URL-extraction rejects ``file://`` / ``data:`` payloads.
  * Send-to helpers return the SDK rows.

These mirror the test pattern qfileman/tests would have if it were
ported — direct mock of the SDK at module import time.
"""
from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest


def _reload_integration():
    """Re-import qdbrowser.qdistro_integration with current sys.modules.

    ``from qdbrowser import qdistro_integration`` is cache-bound to the
    parent package's attribute, which doesn't refresh just because we
    popped from sys.modules. ``importlib.import_module`` forces the
    module loader to rebuild the module object, picking up the
    currently-installed (fake or absent) ``qdistro_app`` SDK.
    """
    sys.modules.pop("qdbrowser.qdistro_integration", None)
    return importlib.import_module("qdbrowser.qdistro_integration")


@pytest.fixture
def fake_sdk(monkeypatch):
    """Inject a fake ``qdistro_app.app_receiver`` module.

    Returns a tuple ``(module, register_calls, send_to_calls,
    send_menu_calls)`` so tests can drive + observe SDK calls.
    """
    mod = types.ModuleType("qdistro_app.app_receiver")
    register_calls: list[tuple] = []
    send_to_calls: list[tuple] = []
    send_menu_calls: list[dict] = []

    fake_receiver = types.SimpleNamespace(
        service_name="org.qdistro.QdBrowser.uid1000",
        silo="work",
    )

    def register_app(name, *, on_receive=None, friendly_name=None,
                     silo=None, supported_kinds=None, **kwargs):
        register_calls.append((name, friendly_name, silo,
                               tuple(supported_kinds or ()), on_receive))
        return fake_receiver

    def send_to_menu_targets(*, self_service=None, kind=None):
        send_menu_calls.append({"self": self_service, "kind": kind})
        return [{"uid": 2000, "service": "org.qdistro.QNotebook.uid2000",
                 "name": "QNotebook", "silo": "personal"}]

    def send_to(uid, service, kind, payload):
        send_to_calls.append((uid, service, kind, payload))
        return True

    mod.register_app = register_app
    mod.send_to_menu_targets = send_to_menu_targets
    mod.send_to = send_to
    pkg = types.ModuleType("qdistro_app")
    pkg.app_receiver = mod
    monkeypatch.setitem(sys.modules, "qdistro_app", pkg)
    monkeypatch.setitem(sys.modules, "qdistro_app.app_receiver", mod)

    # Re-import the integration with the fake SDK in place.
    integ = _reload_integration()
    yield integ, register_calls, send_to_calls, send_menu_calls
    sys.modules.pop("qdbrowser.qdistro_integration", None)


@pytest.fixture
def no_sdk(monkeypatch):
    """Force the SDK import to fail."""
    monkeypatch.setitem(sys.modules, "qdistro_app", None)
    # Also clear any lingering submodule from a previous test that
    # installed a fake SDK; otherwise ``from qdistro_app import
    # app_receiver`` still succeeds because the submodule is cached.
    monkeypatch.delitem(sys.modules, "qdistro_app.app_receiver",
                        raising=False)
    integ = _reload_integration()
    yield integ
    sys.modules.pop("qdbrowser.qdistro_integration", None)


class TestMaybeInstall:
    def test_no_sdk_returns_none(self, no_sdk):
        win = MagicMock()
        assert no_sdk.maybe_install(win) is None

    def test_with_sdk_returns_receiver(self, fake_sdk):
        integ, register_calls, _, _ = fake_sdk
        win = MagicMock()
        r = integ.maybe_install(win)
        assert r is not None
        assert r.service_name == "org.qdistro.QdBrowser.uid1000"
        assert len(register_calls) == 1
        name, friendly, silo, kinds, on_receive = register_calls[0]
        assert name == "QdBrowser"
        assert friendly == "QdBrowser"
        assert "text/uri-list" in kinds
        assert "text/plain" in kinds
        assert callable(on_receive)

    def test_receiver_callback_opens_url(self, fake_sdk, wait_for_qt):
        integ, register_calls, _, _ = fake_sdk
        win = MagicMock()
        integ.maybe_install(win)
        on_receive = register_calls[0][4]
        # Simulate an inbound payload — uri-list with one URL.
        on_receive("text/uri-list", "https://example.com/\n")
        # The callback bounces through QTimer.singleShot; pump events.
        wait_for_qt(
            lambda: win.new_tab.called,
            timeout_ms=2000,
            description="App1 receive callback to open a new tab",
        )
        win.new_tab.assert_called_once()
        kwargs = win.new_tab.call_args.kwargs
        assert kwargs.get("url") == "https://example.com/"


class TestExtractUrls:
    def test_uri_list_single(self):
        integ = _reload_integration()
        urls = integ._extract_urls("text/uri-list", "https://example.com/")
        assert urls == ["https://example.com/"]

    def test_uri_list_multi(self):
        integ = _reload_integration()
        payload = "https://a.example/\nhttps://b.example/\n# comment\n"
        urls = integ._extract_urls("text/uri-list", payload)
        assert urls == ["https://a.example/", "https://b.example/"]

    def test_text_plain_single(self):
        integ = _reload_integration()
        urls = integ._extract_urls("text/plain", "https://example.com/")
        assert urls == ["https://example.com/"]

    def test_rejects_file_url(self):
        integ = _reload_integration()
        assert integ._extract_urls("text/plain", "file:///etc/shadow") == []

    def test_rejects_data_url(self):
        integ = _reload_integration()
        assert integ._extract_urls("text/plain", "data:text/html,<x>") == []

    def test_rejects_bare_path(self):
        integ = _reload_integration()
        assert integ._extract_urls("text/plain", "/etc/passwd") == []

    def test_accepts_about_blank(self):
        integ = _reload_integration()
        assert integ._extract_urls("text/plain", "about:blank") == ["about:blank"]


class TestSendTo:
    def test_send_to_targets(self, fake_sdk):
        integ, _, _, send_menu_calls = fake_sdk
        rows = integ.send_to_targets(kind="text/uri-list")
        assert len(rows) == 1
        assert rows[0]["name"] == "QNotebook"
        assert send_menu_calls[0]["kind"] == "text/uri-list"

    def test_send_to_targets_no_sdk(self, no_sdk):
        assert no_sdk.send_to_targets() == []

    def test_send_payload(self, fake_sdk):
        integ, _, send_to_calls, _ = fake_sdk
        ok = integ.send_payload(2000, "org.qdistro.QNotebook.uid2000",
                                 "https://example.com/")
        assert ok is True
        assert send_to_calls == [(2000, "org.qdistro.QNotebook.uid2000",
                                  "text/uri-list", "https://example.com/")]

    def test_send_payload_no_sdk(self, no_sdk):
        assert no_sdk.send_payload(0, "x", "y") is False


class TestOpenPayload:
    def test_text_html_dropped(self):
        integ = _reload_integration()
        win = MagicMock()
        integ._open_payload(win, "text/html", "<html>x</html>")
        win.new_tab.assert_not_called()

    def test_window_without_new_tab(self):
        integ = _reload_integration()
        win = object()  # no new_tab attr
        # Must not raise.
        integ._open_payload(win, "text/plain", "https://example.com/")
