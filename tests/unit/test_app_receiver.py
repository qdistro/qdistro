"""Tests for the P03 org.qdistro.App1 launcher contract.

Covers the new methods added to :class:`qdistro_app.AppReceiver`
(``GetName`` / ``GetSilo`` / ``CanReceive`` / ``ReceivePayload`` /
``PayloadReceived`` signal) and the high-level
:mod:`qdistro_app.app_receiver` helper module.

The dbus method/signal decorators are pass-throughs at import time
(they record signature metadata and return the underlying function),
so we can construct and probe receivers without standing up a real
session bus. The bus-claim step is monkeypatched away with a
``_BusFreeAppReceiver`` subclass so the tests don't need a live
broker / dbus-daemon.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

import pytest

pytest.importorskip("dbus")

# Make the SDK importable.
_SDK = Path(__file__).resolve().parents[2] / "sdk"
sys.path.insert(0, str(_SDK))

from qdistro_app import (  # noqa: E402
    APP1_IFACE,
    APP1_OBJ_PATH,
    AppReceiver,
    DEFAULT_KIND,
    _friendly_from_service,
    _kind_accepted,
    _resolve_silo,
)
from qdistro_app import app_receiver as app_receiver_mod  # noqa: E402


class _BusFreeAppReceiver(AppReceiver):
    """AppReceiver subclass that skips real dbus registration.

    All metadata wiring (service name, friendly name, silo, supported
    kinds, last_received tracking) lives on the Python instance — the
    only thing the bypass changes is *where* the dbus.service.Object
    base class would register the path. Same trick as
    test_broker_sendto._StubBroker uses for the broker.
    """

    def __init__(self, service_name: str,
                 on_receive=None,
                 *,
                 friendly_name: str | None = None,
                 silo: str | None = None,
                 supported_kinds: Iterable[str] | None = None):
        self._service_name = str(service_name)
        self._on_receive = on_receive or (lambda kind, payload: None)
        self._friendly_name = (str(friendly_name)
                               if friendly_name
                               else _friendly_from_service(service_name))
        self._silo = str(silo) if silo is not None else _resolve_silo()
        self._supported_kinds = (tuple(supported_kinds)
                                 if supported_kinds else ("*",))
        self._last_received = None
        self._signals: list[tuple[str, str]] = []

    def PayloadReceived(self, kind: str, payload: str) -> None:  # type: ignore[override]
        self._signals.append((str(kind), str(payload)))


# --- friendly name derivation ---------------------------------------------

class TestFriendlyFromService:
    @pytest.mark.parametrize("service, expected", [
        ("org.qdistro.QTerminator.uid1000", "QTerminator"),
        ("org.qdistro.QNotebook.uid2000",   "QNotebook"),
        ("org.qdistro.QFileMan.uid3000",    "QFileMan"),
        ("org.qdistro.SomeApp",             "SomeApp"),
        ("org.qdistro.Multi.Word.uid4000",  "Multi.Word"),
        # uidNNN suffix that isn't all-digits stays attached — the
        # only legitimate "uid<int>" form is broker-injected and
        # always numeric.
        ("org.qdistro.Foo.uidbad",          "Foo.uidbad"),
        # Non-qdistro service names pass through (defensive — the
        # broker's _SERVICE_NAME_RE rejects these before they reach
        # the SDK, but the helper shouldn't crash on them).
        ("org.freedesktop.Notifications",   "org.freedesktop.Notifications"),
    ])
    def test_friendly_from_service(self, service, expected):
        assert _friendly_from_service(service) == expected


# --- kind acceptance ------------------------------------------------------

class TestKindAccepted:
    def test_wildcard_accepts_everything(self):
        assert _kind_accepted(("*",), "text/plain") is True
        assert _kind_accepted(("*",), "application/octet-stream") is True
        assert _kind_accepted(("*",), "anything/at/all") is True

    def test_prefix_wildcard(self):
        assert _kind_accepted(("text/*",), "text/plain") is True
        assert _kind_accepted(("text/*",), "text/markdown") is True
        assert _kind_accepted(("text/*",), "image/png") is False

    def test_exact_match(self):
        assert _kind_accepted(("text/plain",), "text/plain") is True
        assert _kind_accepted(("text/plain",), "text/markdown") is False
        # Case-sensitive — mime types are by convention lowercase, and
        # we don't normalise inside the SDK (one place to look).
        assert _kind_accepted(("text/plain",), "Text/Plain") is False

    def test_multi_kind_list(self):
        kinds = ("text/plain", "image/*", "application/json")
        assert _kind_accepted(kinds, "text/plain") is True
        assert _kind_accepted(kinds, "image/png") is True
        assert _kind_accepted(kinds, "application/json") is True
        assert _kind_accepted(kinds, "video/mp4") is False


# --- AppReceiver new surface ----------------------------------------------

class TestAppReceiverMetadata:
    def test_friendly_name_default_from_service(self):
        r = _BusFreeAppReceiver("org.qdistro.QTerminator.uid1234")
        assert r.friendly_name == "QTerminator"
        assert r.GetName() == "QTerminator"

    def test_friendly_name_override(self):
        r = _BusFreeAppReceiver("org.qdistro.X.uid1",
                                friendly_name="Pretty Name")
        assert r.friendly_name == "Pretty Name"
        assert r.GetName() == "Pretty Name"

    def test_silo_from_env(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "work")
        r = _BusFreeAppReceiver("org.qdistro.A.uid1")
        assert r.silo == "work"
        assert r.GetSilo() == "work"

    def test_silo_override(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "ignored")
        r = _BusFreeAppReceiver("org.qdistro.A.uid1", silo="personal")
        assert r.silo == "personal"
        assert r.GetSilo() == "personal"

    def test_silo_fallback_to_username(self, monkeypatch):
        monkeypatch.delenv("QDISTRO_SILO", raising=False)
        r = _BusFreeAppReceiver("org.qdistro.A.uid1")
        # On the host the value is whoever ran pytest; just assert
        # it's a non-empty string so we don't pin a specific username.
        assert isinstance(r.silo, str)

    def test_supported_kinds_default_wildcard(self):
        r = _BusFreeAppReceiver("org.qdistro.A.uid1")
        assert r.supported_kinds == ("*",)
        assert r.CanReceive("text/plain") is True
        assert r.CanReceive("application/octet-stream") is True

    def test_can_receive_with_prefix_list(self):
        r = _BusFreeAppReceiver(
            "org.qdistro.A.uid1",
            supported_kinds=("text/*", "application/json"))
        assert r.CanReceive("text/plain") is True
        assert r.CanReceive("text/markdown") is True
        assert r.CanReceive("application/json") is True
        assert r.CanReceive("image/png") is False


class TestAppReceiverDelivery:
    def test_receive_records_last_and_calls_callback(self):
        seen: list[tuple[str, str]] = []
        r = _BusFreeAppReceiver(
            "org.qdistro.A.uid1",
            on_receive=lambda k, p: seen.append((k, p)))
        r.Receive("text/plain", "hello")
        assert seen == [("text/plain", "hello")]
        assert r.last_received == ("text/plain", "hello")
        assert r.GetLastReceived() == "[text/plain] hello"

    def test_receive_payload_normalises_to_default_kind(self):
        seen: list[tuple[str, str]] = []
        r = _BusFreeAppReceiver(
            "org.qdistro.A.uid1",
            on_receive=lambda k, p: seen.append((k, p)))
        r.ReceivePayload("just bytes")
        assert seen == [(DEFAULT_KIND, "just bytes")]
        assert r.last_received == (DEFAULT_KIND, "just bytes")
        assert r.GetLastReceived() == f"[{DEFAULT_KIND}] just bytes"

    def test_callback_exception_does_not_break_last_received(self):
        def boom(k, p):
            raise RuntimeError("kaboom")
        r = _BusFreeAppReceiver("org.qdistro.A.uid1", on_receive=boom)
        # _deliver wraps on_receive in try/finally so last_received and
        # the signal still fire even on a buggy app callback.
        with pytest.raises(RuntimeError):
            r.Receive("k", "p")
        assert r.last_received == ("k", "p")

    def test_signal_emitted_per_delivery(self):
        r = _BusFreeAppReceiver("org.qdistro.A.uid1")
        r.Receive("text/plain", "one")
        r.ReceivePayload("two")
        assert r._signals == [
            ("text/plain", "one"),
            (DEFAULT_KIND, "two"),
        ]

    def test_get_last_received_empty_initially(self):
        r = _BusFreeAppReceiver("org.qdistro.A.uid1")
        assert r.last_received is None
        assert r.GetLastReceived() == ""


# --- app_receiver helper module -------------------------------------------

class TestCanonicalServiceName:
    def test_short_name_gets_prefix_and_uid(self):
        s = app_receiver_mod._canonical_service_name("QTerminator")
        assert s == f"org.qdistro.QTerminator.uid{os.geteuid()}"

    def test_partial_qualified_gets_uid(self):
        s = app_receiver_mod._canonical_service_name("org.qdistro.QFileMan")
        assert s == f"org.qdistro.QFileMan.uid{os.geteuid()}"

    def test_fully_qualified_passthrough(self):
        s = app_receiver_mod._canonical_service_name(
            "org.qdistro.QTerminator.uid42")
        assert s == "org.qdistro.QTerminator.uid42"


class TestSessionBusAvailability:
    def test_returns_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS",
                            "unix:path=/run/user/1000/bus")
        assert app_receiver_mod.is_session_bus_available() is True

    def test_returns_false_without_env_or_socket(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert app_receiver_mod.is_session_bus_available() is False

    def test_returns_true_when_socket_exists(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        (tmp_path / "bus").touch()
        assert app_receiver_mod.is_session_bus_available() is True


class TestRegisterAppSkipsWhenBusMissing:
    def test_returns_none_and_logs(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        logs: list[str] = []
        result = app_receiver_mod.register_app("QTerminator",
                                                log=logs.append)
        assert result is None
        assert any("no session bus" in m for m in logs), logs


# --- send_to_menu_targets -------------------------------------------------

class TestSendToMenuTargets:
    """Builds the Send-To menu list from a stubbed broker.

    The helper hits the broker via :func:`qdistro_app.list_receivers`;
    we monkeypatch that to return canned rows so we don't need a real
    session bus / broker.
    """

    def test_excludes_self_service(self, monkeypatch):
        monkeypatch.setattr(app_receiver_mod, "list_receivers",
                            lambda: [
                                (1000, "org.qdistro.QTerminator.uid1000", "QTerminator"),
                                (1000, "org.qdistro.QNotebook.uid1000",   "QNotebook"),
                            ])
        rows = app_receiver_mod.send_to_menu_targets(
            self_service="org.qdistro.QTerminator.uid1000")
        assert [r["service"] for r in rows] == ["org.qdistro.QNotebook.uid1000"]

    def test_returns_row_shape(self, monkeypatch):
        monkeypatch.setattr(app_receiver_mod, "list_receivers",
                            lambda: [
                                (2000, "org.qdistro.QNotebook.uid2000", "QNotebook"),
                            ])
        # Force the cross-uid silo/CanReceive probes to be skipped
        # (uid 2000 != current uid) so we don't accidentally touch
        # the session bus in the test.
        rows = app_receiver_mod.send_to_menu_targets()
        assert len(rows) == 1
        r = rows[0]
        assert set(r.keys()) == {"uid", "service", "name", "silo"}
        assert r["uid"] == 2000
        assert r["service"] == "org.qdistro.QNotebook.uid2000"
        assert r["name"] == "QNotebook"
        # silo defaults to "" for cross-uid peers (probe not reachable)
        assert r["silo"] == ""

    def test_swallows_list_receivers_failure(self, monkeypatch):
        def boom():
            raise RuntimeError("broker down")
        monkeypatch.setattr(app_receiver_mod, "list_receivers", boom)
        assert app_receiver_mod.send_to_menu_targets() == []


# --- regression: existing Receive contract preserved ---------------------

class TestBackwardsCompatibleSurface:
    """The Phase-3 surface (``Receive``, ``GetLastReceived``,
    ``last_received``) must keep its exact behaviour — qstub-notepad,
    test_sdk_sendto, and the broker's _StubBroker all depend on it.
    """

    def test_receive_signature_unchanged(self):
        r = _BusFreeAppReceiver("org.qdistro.A.uid1")
        # Two positional args, no kwargs — matches in_signature="ss".
        r.Receive("kind", "payload")
        assert r.last_received == ("kind", "payload")

    def test_get_last_received_format_matches_stub_notepad(self):
        r = _BusFreeAppReceiver("org.qdistro.A.uid1")
        r.Receive("text/plain", "abc")
        # qstub-notepad formats as `[kind] payload\n` and trims; the
        # SDK never appends the newline, so the canonical assertion
        # is "[kind] payload" verbatim. test_sdk_sendto pins this.
        assert r.GetLastReceived() == "[text/plain] abc"

    def test_iface_constants_unchanged(self):
        # The interface name and object path are wire contracts —
        # changing them breaks every receiver in the field.
        assert APP1_IFACE == "org.qdistro.App1"
        assert APP1_OBJ_PATH == "/org/qdistro/App1"
        assert AppReceiver.OBJ_PATH == APP1_OBJ_PATH
