"""Unit tests for the TUI's bounded broker-error labelling.

These tests exercise pure functions in `tui/broker_client.py`
(`broker_error_label`, `log_broker_error`) and intentionally do NOT
import `textual` — they must run on a host without the TUI's optional
deps so the security-hardening guarantee (no raw D-Bus strings on the
shoulder-visible surface) stays covered everywhere.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Promote tui/ onto sys.path so we can import the broker client module
# directly without dragging in qdistro_admin_tui (which needs textual).
_TUI = Path(__file__).resolve().parents[1] / "tui"
if str(_TUI) not in sys.path:
    sys.path.insert(0, str(_TUI))

from broker_client import (  # noqa: E402
    _BROKER_ERROR_LABELS,
    _GENERIC_BROKER_ERROR_LABEL,
    broker_error_label,
    log_broker_error,
)


class _FakeDBusException(Exception):
    """Duck-types dbus.DBusException for the label lookup (get_dbus_name)."""

    def __init__(self, name: str, message: str):
        super().__init__(message)
        self._name = name

    def get_dbus_name(self) -> str:
        return self._name


def test_known_fully_qualified_name_maps_to_bounded_label():
    exc = _FakeDBusException(
        "org.qdistro.AdminBroker1.ScopeNotPermitted",
        "uid 2003 may not use forever_exe for /usr/bin/secret-tool foo bar",
    )
    label = broker_error_label(exc)
    assert label == _BROKER_ERROR_LABELS["ScopeNotPermitted"]
    # The raw detail (paths/argv/uid) must never appear in the label.
    assert "secret-tool" not in label
    assert "2003" not in label
    assert "/usr/bin" not in label


def test_bare_suffix_name_still_maps():
    # Survives a BUS_NAME prefix change — bare suffix is accepted too.
    exc = _FakeDBusException("AccessDenied", "raw detail here")
    assert broker_error_label(exc) == _BROKER_ERROR_LABELS["AccessDenied"]


def test_bus_level_service_unknown_is_broker_not_running():
    exc = _FakeDBusException(
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "The name org.qdistro.AdminBroker1 was not provided by any .service",
    )
    label = broker_error_label(exc)
    assert label == "Broker not running"
    assert "org.qdistro" not in label


def test_unknown_name_falls_back_to_generic_no_leak():
    exc = _FakeDBusException(
        "org.qdistro.AdminBroker1.SomeFutureError",
        "internal stack trace /home/admin/secret path",
    )
    label = broker_error_label(exc)
    assert label == _GENERIC_BROKER_ERROR_LABEL
    assert "secret" not in label
    assert "/home" not in label


def test_plain_exception_without_dbus_name_is_generic():
    exc = RuntimeError("DBusBrokerClient.GetPending called before start()")
    label = broker_error_label(exc)
    assert label == _GENERIC_BROKER_ERROR_LABEL
    assert "GetPending" not in label


def test_all_labels_are_bounded_and_detail_free():
    # Every mapped label is a short static phrase: no format placeholders,
    # no obviously-injected detail markers.
    for name, label in _BROKER_ERROR_LABELS.items():
        assert label, name
        assert "{" not in label and "}" not in label, name
        assert "\n" not in label, name


def test_log_broker_error_records_raw_detail(caplog):
    exc = _FakeDBusException(
        "org.qdistro.AdminBroker1.ScopeNotPermitted",
        "raw detail with /usr/bin/secret-tool and uid 2003",
    )
    with caplog.at_level(logging.WARNING, logger="qdistro.tui.broker"):
        log_broker_error("decide", exc)
    # The raw detail belongs in the log (not the UI) — assert it's there.
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "secret-tool" in joined
    assert "ScopeNotPermitted" in joined
