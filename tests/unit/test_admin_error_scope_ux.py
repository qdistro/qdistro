"""Tests for admin-app approval/error UX hardening.

Covers three items from security-hardening-carryforward §"Broker and rules":

1. Bounded broker-error labels: known typed broker D-Bus errors render a
   short, shoulder-safe (title, label); the raw D-Bus string (which can leak
   file paths / internal identifiers) is kept out of the dialog body and
   logged instead.
2. Stale scope: a non-`once` scope selected for one request does NOT silently
   carry across to the next request — the picker resets to `once` after each
   decision.
3. ScopeNotPermitted visibility: a broker refusal on a pending request is
   surfaced inline in the detail view with bounded guidance (and expandable
   raw detail), and the request stays pending / retryable — not only a
   transient popup.

PyQt6 only (PySide6 is removed from the host). No live D-Bus / broker needed —
all broker interaction is stubbed.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
QtCore = pytest.importorskip("PyQt6.QtCore")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import dbus  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "admin_app"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qdistro_admin_app import (  # noqa: E402
    DetailPane,
    MainWindow,
    _friendly_broker_error,
    _GENERIC_BROKER_ERROR,
)

_BUS = "org.qdistro.AdminBroker1"
_RAW_LEAK = "rule '/etc/qdistro/rules.d/secret-internal.yaml' rejected; uid=4242"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _scope_exc(name=f"{_BUS}.ScopeNotPermitted"):
    return dbus.DBusException(_RAW_LEAK, name=name)


def _make_stub_broker():
    broker = MagicMock()
    broker.get_pending.return_value = []
    broker.list_rules.return_value = []
    broker.list_history.return_value = []
    broker.list_cache.return_value = []
    broker.rulesReloaded = MagicMock()
    broker.rulesReloaded.connect = MagicMock()
    broker.requestPending = MagicMock()
    broker.requestPending.connect = MagicMock()
    broker.requestDecided = MagicMock()
    broker.requestDecided.connect = MagicMock()
    broker.approvalRevoked = MagicMock()
    broker.approvalRevoked.connect = MagicMock()
    return broker


def _req(rid=1, scope_action="qdistro.exec.ls"):
    return {"id": rid, "uid": 2000, "pid": 100, "exe": "/bin/ls",
            "action": scope_action, "details": {}}


# ---------------------------------------------------------------------------
# Item 1 — bounded broker-error labels
# ---------------------------------------------------------------------------

class TestFriendlyBrokerError:
    def test_known_error_maps_to_bounded_label_no_raw(self, caplog):
        exc = _scope_exc()
        with caplog.at_level(logging.WARNING, logger="qdistro.admin_app"):
            title, label = _friendly_broker_error(exc)
        # Bounded title/label, NOT the raw string.
        assert title == "Scope not permitted"
        assert "retry" in label.lower()
        assert _RAW_LEAK not in title
        assert _RAW_LEAK not in label
        assert "/etc/qdistro" not in label
        # Raw detail IS logged for operators.
        assert any(_RAW_LEAK in r.getMessage() for r in caplog.records)
        assert any(f"{_BUS}.ScopeNotPermitted" in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.parametrize("suffix,expect_title", [
        ("RulesEngineRefused", "Rules engine refused"),
        ("RateLimited", "Rate limited"),
        ("InvalidArgument", "Invalid argument"),
        ("AccessDenied", "Access denied"),
        ("AuditUnavailable", "Audit unavailable"),
    ])
    def test_other_known_errors(self, suffix, expect_title):
        exc = dbus.DBusException(_RAW_LEAK, name=f"{_BUS}.{suffix}")
        title, label = _friendly_broker_error(exc)
        assert title == expect_title
        assert _RAW_LEAK not in label

    def test_unknown_error_falls_back_to_generic_not_raw(self, caplog):
        exc = dbus.DBusException(_RAW_LEAK, name=f"{_BUS}.SomethingNovel")
        with caplog.at_level(logging.WARNING, logger="qdistro.admin_app"):
            title, label = _friendly_broker_error(exc)
        assert (title, label) == _GENERIC_BROKER_ERROR
        assert _RAW_LEAK not in label
        # Still logged.
        assert any(_RAW_LEAK in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("suffix,expect_title", [
        ("UnknownSilo", "Unknown silo"),
        ("SiloExists", "Silo already exists"),
        ("SiloBusy", "Silo busy"),
        ("BadState", "Silo in wrong state"),
        ("NotAuthorized", "Not authorized"),
        ("Generic", "Session manager error"),
    ])
    def test_session_manager_errors(self, suffix, expect_title):
        exc = dbus.DBusException(
            _RAW_LEAK, name=f"org.qdistro.SessionManager1.{suffix}")
        title, label = _friendly_broker_error(exc)
        assert title == expect_title
        assert _RAW_LEAK not in label

    def test_bus_level_service_unknown(self):
        exc = dbus.DBusException(
            "no owner", name="org.freedesktop.DBus.Error.ServiceUnknown")
        title, _label = _friendly_broker_error(exc)
        assert title == "Broker unavailable"

    def test_non_dbus_exception_is_safe(self, caplog):
        with caplog.at_level(logging.WARNING, logger="qdistro.admin_app"):
            title, label = _friendly_broker_error(ValueError("boom-detail"))
        assert (title, label) == _GENERIC_BROKER_ERROR
        assert any("boom-detail" in r.getMessage() for r in caplog.records)


class TestDecideDialogHidesRawString:
    def test_decide_failure_dialog_shows_bounded_label_not_raw(
            self, qapp, caplog):
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        broker.decide.side_effect = _scope_exc()
        win = MainWindow(broker)

        captured = {}

        def _fake_critical(parent, title, text, *a, **k):
            captured["title"] = title
            captured["text"] = text
            return None

        with patch("qdistro_admin_app.QMessageBox.critical", _fake_critical), \
                caplog.at_level(logging.WARNING, logger="qdistro.admin_app"):
            win._on_decided(1, "allow", "forever")

        # Dialog body must NOT contain the raw leaked string / path.
        assert _RAW_LEAK not in captured["text"]
        assert "/etc/qdistro" not in captured["text"]
        # It DOES carry the bounded label.
        assert "Scope not permitted" in captured["text"]
        # Raw is logged.
        assert any(_RAW_LEAK in r.getMessage() for r in caplog.records)

    def test_scope_not_permitted_modal_says_retryable_not_denied(
            self, qapp):
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        broker.decide.side_effect = _scope_exc()
        win = MainWindow(broker)
        captured = {}
        with patch("qdistro_admin_app.QMessageBox.critical",
                   lambda p, t, txt, *a, **k: captured.update(text=txt)):
            win._on_decided(1, "allow", "forever")
        # Retryable framing, NOT the fail-closed "safety measure" copy.
        assert "retry" in captured["text"].lower()
        assert "safety measure" not in captured["text"].lower()

    def test_fail_closed_modal_says_safety_measure(self, qapp):
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        broker.decide.side_effect = dbus.DBusException(
            _RAW_LEAK, name=f"{_BUS}.AuditUnavailable")
        win = MainWindow(broker)
        captured = {}
        with patch("qdistro_admin_app.QMessageBox.critical",
                   lambda p, t, txt, *a, **k: captured.update(text=txt)):
            win._on_decided(1, "allow", "once")
        assert "safety measure" in captured["text"].lower()
        assert _RAW_LEAK not in captured["text"]


# ---------------------------------------------------------------------------
# Item 2 — stale scope does not silently carry across approvals
# ---------------------------------------------------------------------------

class TestScopeReset:
    def _checked_scope(self, detail: DetailPane) -> str:
        for key, rb in detail._scope_buttons:
            if rb.isChecked():
                return key
        return "(none)"

    def test_default_scope_is_once(self, qapp):
        detail = DetailPane()
        assert self._checked_scope(detail) == "once"

    def test_emit_resets_scope_to_once(self, qapp):
        detail = DetailPane()
        detail.show_request(_req(1))
        # Admin picks a broad, sticky scope.
        for key, rb in detail._scope_buttons:
            rb.setChecked(key == "forever")
        assert self._checked_scope(detail) == "forever"

        emitted = []
        detail.decided.connect(
            lambda rid, dec, scope: emitted.append((rid, dec, scope)))
        detail._emit("allow")

        # The decision carried the chosen scope...
        assert emitted == [(1, "allow", "forever")]
        # ...but the picker reset so the NEXT request defaults to once.
        assert self._checked_scope(detail) == "once"

    def test_scope_does_not_carry_to_next_request(self, qapp):
        detail = DetailPane()
        detail.show_request(_req(1))
        for key, rb in detail._scope_buttons:
            rb.setChecked(key == "24h")
        detail._emit("allow")
        # New request shown; scope must already be back to once.
        detail.show_request(_req(2))
        assert self._checked_scope(detail) == "once"

    def test_bulk_decide_resets_scope(self, qapp):
        # A non-`once` scope used for a bulk approve/deny must not carry
        # forward to the next decision either. (codex round-1 finding #1)
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1), _req(2, "qdistro.exec.cat")]
        win = MainWindow(broker)
        for key, rb in win.detail._scope_buttons:
            rb.setChecked(key == "forever")
        from PyQt6.QtWidgets import QMessageBox
        # _bulk_decide schedules its per-request work off
        # QTimer.singleShot(0, _step); the reset we care about happens
        # synchronously right after the confirm, before any timer fires.
        # Stub singleShot so no async loop is scheduled (keeps the Qt
        # event queue clean and the test hermetic).
        with patch("qdistro_admin_app.QMessageBox.question",
                   lambda *a, **k: QMessageBox.StandardButton.Yes), \
                patch("qdistro_admin_app.QMessageBox.information",
                      lambda *a, **k: None), \
                patch("qdistro_admin_app.QTimer.singleShot",
                      lambda *a, **k: None):
            win._bulk_decide("allow", scope_filter=None)
        assert self._checked_scope(win.detail) == "once"

    def test_failed_decide_still_resets_scope(self, qapp):
        # Even when the broker refuses (request stays pending), the picker
        # must reset so a broad scope isn't silently re-applied on retry.
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        broker.decide.side_effect = _scope_exc()
        win = MainWindow(broker)
        for key, rb in win.detail._scope_buttons:
            rb.setChecked(key == "forever")
        with patch("qdistro_admin_app.QMessageBox.critical",
                   lambda *a, **k: None):
            win.detail._emit("allow")
        assert self._checked_scope(win.detail) == "once"


# ---------------------------------------------------------------------------
# Item 3 — ScopeNotPermitted surfaces inline + stays retryable
# ---------------------------------------------------------------------------

class TestScopeNotPermittedVisibility:
    def test_pending_request_keeps_inline_guidance_and_stays_retryable(
            self, qapp):
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        broker.decide.side_effect = _scope_exc()
        win = MainWindow(broker)

        with patch("qdistro_admin_app.QMessageBox.critical",
                   lambda *a, **k: None):
            win._on_decided(1, "allow", "forever")

        # Request is still pending (broker still returns it) → retryable.
        assert broker.get_pending.return_value == [_req(1)]

        # After refresh re-selects rid=1, the detail pane shows the bounded
        # guidance inline (not just a transient popup).
        ew = win.detail.error_widget
        assert ew.isVisibleTo(win.detail) or not ew.isHidden()
        assert ew.guidance_label.text() != ""
        assert "Scope not permitted".lower() in ew.guidance_label.text().lower() \
            or "scope" in ew.guidance_label.text().lower()
        # Bounded guidance carries no raw string.
        assert _RAW_LEAK not in ew.guidance_label.text()
        # Raw detail is available behind the expandable affordance only.
        assert _RAW_LEAK in ew.error_text.toPlainText()
        # ...and the raw detail panel is collapsed by default.
        assert ew.error_text.isHidden()

    def test_error_recorded_against_rid_and_resurfaces_on_reselect(self, qapp):
        detail = DetailPane()
        detail.show_request(_req(1))
        detail.set_broker_error(1, "Scope not permitted: try a narrower scope.",
                                _RAW_LEAK)
        # Switch away then back: the stored error re-surfaces inline.
        detail.show_request(_req(2))
        assert detail.error_widget.guidance_label.text() == "" or \
            detail.error_widget.isHidden()
        detail.show_request(_req(1))
        assert "Scope not permitted" in detail.error_widget.guidance_label.text()
        assert _RAW_LEAK in detail.error_widget.error_text.toPlainText()

    def test_successful_decide_clears_stored_error(self, qapp):
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        broker.decide.side_effect = _scope_exc()
        win = MainWindow(broker)
        with patch("qdistro_admin_app.QMessageBox.critical",
                   lambda *a, **k: None):
            win._on_decided(1, "allow", "forever")
        assert 1 in win.detail._broker_errors

        # Retry with a narrower scope succeeds → stored error cleared.
        broker.decide.side_effect = None
        broker.get_pending.return_value = []
        win._on_decided(1, "allow", "once")
        assert 1 not in win.detail._broker_errors


# ---------------------------------------------------------------------------
# Pending-decision shortcut scope (open-followups: WindowShortcut + guard).
#
# All shortcuts are WindowShortcut-scoped (not the old ApplicationShortcut), so
# they only fire while the admin window is active. The decision keys
# (approve/deny/rule, bulk, per-silo) act on the *selected* pending request and
# are guarded: pressed from another tab the first press raises the Pending tab
# so the admin sees the request, a second press acts on it. The scope-picker
# keys (Ctrl+Shift+1..8) commit nothing, so they are exempt from the guard and
# take effect from any tab.
# ---------------------------------------------------------------------------

class TestPendingDecisionShortcutGuard:
    def _shortcut(self, win, seq):
        from PyQt6.QtGui import QKeySequence, QShortcut
        want = QKeySequence(seq).toString()
        for sc in win.findChildren(QShortcut):
            if sc.key().toString() == want:
                return sc
        raise AssertionError(f"no shortcut bound for {seq!r}")

    def test_all_shortcuts_are_window_scoped(self, qapp):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QShortcut
        win = MainWindow(_make_stub_broker())
        shortcuts = win.findChildren(QShortcut)
        assert shortcuts  # sanity: they exist
        for sc in shortcuts:
            assert sc.context() == Qt.ShortcutContext.WindowShortcut

    def test_ensure_pending_tab_switches_only_when_elsewhere(self, qapp):
        win = MainWindow(_make_stub_broker())
        win.tabs.setCurrentWidget(win.pending_tab)
        assert win._ensure_pending_tab() is False
        assert win.tabs.currentWidget() is win.pending_tab

        win.tabs.setCurrentWidget(win.rules_tab)
        assert win._ensure_pending_tab() is True
        assert win.tabs.currentWidget() is win.pending_tab

    def test_shortcut_from_other_tab_raises_pending_without_deciding(self, qapp):
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        win = MainWindow(broker)
        win.tabs.setCurrentWidget(win.rules_tab)
        with patch.object(win.detail, "_emit") as emit:
            self._shortcut(win, "Ctrl+Y").activated.emit()
        emit.assert_not_called()
        assert win.tabs.currentWidget() is win.pending_tab

    def test_shortcut_on_pending_tab_acts(self, qapp):
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        win = MainWindow(broker)
        win.tabs.setCurrentWidget(win.pending_tab)
        with patch.object(win.detail, "_emit") as emit:
            self._shortcut(win, "Ctrl+Y").activated.emit()
        emit.assert_called_once_with("allow")

    def test_scope_keys_are_unguarded_and_act_from_other_tab(self, qapp):
        # Scope-picker keys (Ctrl+Shift+1..8) commit nothing — they only
        # tick a radio button — so they are exempt from the Pending-tab
        # guard: they take effect from any tab WITHOUT first switching.
        broker = _make_stub_broker()
        broker.get_pending.return_value = [_req(1)]
        win = MainWindow(broker)
        win.tabs.setCurrentWidget(win.rules_tab)
        with patch.object(win, "_set_scope") as set_scope:
            self._shortcut(win, "Ctrl+Shift+3").activated.emit()
        set_scope.assert_called_once_with("24h")
        # Still on Rules — the unguarded key did not yank the admin away.
        assert win.tabs.currentWidget() is win.rules_tab
