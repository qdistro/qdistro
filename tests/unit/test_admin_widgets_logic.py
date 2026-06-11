"""Non-GUI logic tests for admin_app widgets and D-Bus service contracts.

Covers:
  * ErrorDetailsWidget — expanded/collapsed state, set_guidance logic,
    error_message storage. These are pure-logic contracts that hold regardless
    of rendering; no assertion touches pixel dimensions, colours, or layout.
  * NewSiloDialog — uid range, suggested-uid clamping, initial field state.
    Again, pure model/init contracts; no screenshot or visual check.
  * _DBusMenuService and StatusNotifierItemService — contract extension beyond
    what test_admin_notifications.py covers: method-name inventory, signal
    names, interface constants, return-shape assertions. The existing
    test_admin_notifications.py tests the behaviour of each method in depth;
    these tests pin the exported INTERFACE surface so a rename or deletion is
    caught without needing a live D-Bus.

All tests use a STUBBED bus (no live D-Bus session required).  The
fixture pattern is copied verbatim from test_admin_notifications.py
so the two test modules are independently runnable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
QtCore = pytest.importorskip("PyQt6.QtCore")

from PyQt6.QtWidgets import QApplication  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "admin_app"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qdistro_admin_app import (  # noqa: E402
    ErrorDetailsWidget,
    NewSiloDialog,
    StatusNotifierItemService,
    _DBusMenuService,
    SNI_IFACE,
    DBUSMENU_IFACE,
    _MENU_ROOT_ID,
    _MENU_SHOW_ID,
    _MENU_MUTE_ID,
    _MENU_QUIT_ID,
    _variant,
)


# ---------------------------------------------------------------------------
# Fixtures (copied from test_admin_notifications.py; minimal duplication)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _make_stub_sni(callbacks=None):
    """Create StatusNotifierItemService bypassing the real D-Bus bus."""
    if callbacks is None:
        callbacks = {"activate": lambda: None}
    with patch("dbus.service.BusName"), \
         patch("dbus.service.Object.__init__", return_value=None):
        import dbus as _dbus
        sni = StatusNotifierItemService.__new__(StatusNotifierItemService)
        sni._bus = MagicMock(spec=_dbus.Bus)
        sni._object_path = "/StatusNotifierItem"
        sni._callbacks = callbacks
        sni._bus_name_str = "org.kde.StatusNotifierItem-1-1"
        sni._well_known = MagicMock()
        sni._status = "Active"
        sni._tooltip_text = "qdistro admin approvals"
        sni._pending_count = 0
        sni._icon_name = "computer"
        sni._attention_icon_name = "dialog-warning"
        sni._menu_path = "/StatusNotifierItem/menu"
        sni._menu = None
        return sni


def _make_stub_menu(callbacks=None):
    if callbacks is None:
        callbacks = {}
    with patch("dbus.service.Object.__init__", return_value=None):
        menu = _DBusMenuService.__new__(_DBusMenuService)
        menu._callbacks = callbacks
        menu._revision = 1
        menu._mute_label = "Mute 30m"
        return menu


# ---------------------------------------------------------------------------
# ErrorDetailsWidget — model/logic contracts
# ---------------------------------------------------------------------------

class TestErrorDetailsWidgetLogic:
    """Non-GUI contracts for ErrorDetailsWidget.

    The widget exists to protect shoulder-safety: guidance_label is always
    visible but never shows the raw broker string; error_text is hidden by
    default and revealed only on explicit toggle. These tests pin those
    state-machine invariants.
    """

    def test_initial_state_collapsed(self, qapp):
        """Widget starts collapsed (expanded=False)."""
        w = ErrorDetailsWidget("some error message")
        assert w.expanded is False

    def test_error_message_stored(self, qapp):
        """Constructor stores the error_message attribute."""
        msg = "org.freedesktop.DBus.Error.ServiceUnknown: the daemon vanished"
        w = ErrorDetailsWidget(msg)
        assert w.error_message == msg

    def test_error_text_content_matches_message(self, qapp):
        """error_text QTextEdit contains the full raw error message."""
        msg = "org.freedesktop.DBus.Error: broker not found"
        w = ErrorDetailsWidget(msg)
        assert w.error_text.toPlainText() == msg

    def test_guidance_label_initially_hidden(self, qapp):
        """Guidance label is hidden on construction (no guidance set yet)."""
        w = ErrorDetailsWidget("any error")
        assert w.guidance_label.isHidden()

    @pytest.mark.cheat_aware(
        protects="error_text is hidden by default (shoulder-safety invariant)",
        severity="high",
        cheats=["initialise error_text as visible", "skip setVisible(False) call"],
        consequence="raw broker error strings (potentially sensitive) visible by default to bystanders",
    )
    def test_error_text_initially_hidden(self, qapp):
        """Raw error text is hidden on construction (shoulder-safety)."""
        w = ErrorDetailsWidget("any error")
        assert w.error_text.isHidden()

    def test_toggle_button_initial_text(self, qapp):
        """Toggle button reads 'Show Error Details' when collapsed."""
        w = ErrorDetailsWidget("error")
        assert w.toggle_button.text() == "Show Error Details"

    def test_toggle_button_is_checkable(self, qapp):
        """Toggle button must be checkable (not a plain push-button)."""
        w = ErrorDetailsWidget("error")
        assert w.toggle_button.isCheckable()

    def test_set_guidance_shows_label(self, qapp):
        """set_guidance with a non-empty string shows the guidance label."""
        w = ErrorDetailsWidget("raw broker error")
        w.set_guidance("The broker is unreachable. Check the systemd unit.")
        # Use isHidden() rather than isVisible() — the parent widget is not
        # shown in a unit test, so isVisible() would always return False even
        # when the child's own visibility is set. isHidden() checks only the
        # child's own hidden flag.
        assert not w.guidance_label.isHidden()
        assert w.guidance_label.text() == "The broker is unreachable. Check the systemd unit."

    def test_set_guidance_empty_hides_label(self, qapp):
        """set_guidance('') hides and clears the guidance label."""
        w = ErrorDetailsWidget("error")
        w.set_guidance("Some guidance")
        assert not w.guidance_label.isHidden()
        w.set_guidance("")
        assert w.guidance_label.isHidden()
        assert w.guidance_label.text() == ""

    def test_toggle_expand_sets_expanded_true(self, qapp):
        """_toggle_visibility(True) sets expanded=True."""
        w = ErrorDetailsWidget("error")
        w._toggle_visibility(True)
        assert w.expanded is True

    def test_toggle_expand_shows_error_text(self, qapp):
        """_toggle_visibility(True) reveals the raw error text."""
        w = ErrorDetailsWidget("error")
        w._toggle_visibility(True)
        assert not w.error_text.isHidden()

    def test_toggle_expand_changes_button_text(self, qapp):
        """_toggle_visibility(True) changes button text to 'Hide Error Details'."""
        w = ErrorDetailsWidget("error")
        w._toggle_visibility(True)
        assert w.toggle_button.text() == "Hide Error Details"

    def test_toggle_collapse_sets_expanded_false(self, qapp):
        """_toggle_visibility(False) sets expanded=False."""
        w = ErrorDetailsWidget("error")
        w._toggle_visibility(True)
        w._toggle_visibility(False)
        assert w.expanded is False

    def test_toggle_collapse_hides_error_text(self, qapp):
        """_toggle_visibility(False) hides the raw error text again."""
        w = ErrorDetailsWidget("error")
        w._toggle_visibility(True)
        w._toggle_visibility(False)
        assert w.error_text.isHidden()

    def test_toggle_collapse_restores_button_text(self, qapp):
        """_toggle_visibility(False) restores button text to 'Show Error Details'."""
        w = ErrorDetailsWidget("error")
        w._toggle_visibility(True)
        w._toggle_visibility(False)
        assert w.toggle_button.text() == "Show Error Details"

    def test_guidance_does_not_overwrite_error_message(self, qapp):
        """set_guidance must not alter error_message or error_text content."""
        raw = "org.freedesktop.DBus.Error: raw internal detail"
        w = ErrorDetailsWidget(raw)
        w.set_guidance("Friendly user-visible label")
        # raw field untouched
        assert w.error_message == raw
        assert w.error_text.toPlainText() == raw

    @pytest.mark.cheat_aware(
        protects="guidance_label uses PlainText format (no HTML injection)",
        severity="medium",
        cheats=["set textFormat to RichText or AutoText", "omit setTextFormat call"],
        consequence="attacker-controlled guidance string could inject HTML/JS into the admin UI",
    )
    def test_guidance_text_is_plain_text(self, qapp):
        """guidance_label must use PlainText format (no HTML injection)."""
        from PyQt6.QtCore import Qt
        w = ErrorDetailsWidget("error")
        assert w.guidance_label.textFormat() == Qt.TextFormat.PlainText

    def test_object_names_for_accessibility(self, qapp):
        """Widget children must have the documented objectName values."""
        w = ErrorDetailsWidget("error")
        assert w.guidance_label.objectName() == "broker_error_guidance"
        assert w.toggle_button.objectName() == "btn_show_error_details"
        assert w.error_text.objectName() == "broker_error_raw"


# ---------------------------------------------------------------------------
# NewSiloDialog — model/init contracts
# ---------------------------------------------------------------------------

class TestNewSiloDialogLogic:
    """Non-GUI contracts for NewSiloDialog.

    The dialog holds a uid_spin (range 2000–60000) and a name_edit.  These
    tests pin the range enforcement and the suggested_uid initialisation
    behaviour without asserting anything about rendering.
    """

    def test_suggested_uid_stored(self, qapp):
        """Suggested UID is set as the initial spin value."""
        dlg = NewSiloDialog(suggested_uid=2001)
        assert dlg.uid_spin.value() == 2001

    def test_uid_spin_minimum(self, qapp):
        """UID spinner minimum is 2000 (no system-uid allocation)."""
        dlg = NewSiloDialog(suggested_uid=2000)
        assert dlg.uid_spin.minimum() == 2000

    def test_uid_spin_maximum(self, qapp):
        """UID spinner maximum is 60000 (below kernel UID_MAX 65535)."""
        dlg = NewSiloDialog(suggested_uid=2000)
        assert dlg.uid_spin.maximum() == 60000

    def test_suggested_uid_at_minimum_accepted(self, qapp):
        dlg = NewSiloDialog(suggested_uid=2000)
        assert dlg.uid_spin.value() == 2000

    def test_suggested_uid_at_maximum_accepted(self, qapp):
        dlg = NewSiloDialog(suggested_uid=60000)
        assert dlg.uid_spin.value() == 60000

    def test_suggested_uid_clamped_below_minimum(self, qapp):
        """A suggested UID below 2000 is clamped to the minimum."""
        dlg = NewSiloDialog(suggested_uid=1000)
        assert dlg.uid_spin.value() == 2000

    def test_suggested_uid_clamped_above_maximum(self, qapp):
        """A suggested UID above 60000 is clamped to the maximum."""
        dlg = NewSiloDialog(suggested_uid=99999)
        assert dlg.uid_spin.value() == 60000

    def test_name_edit_initially_empty(self, qapp):
        """Name field is initially empty — user must provide a name."""
        dlg = NewSiloDialog(suggested_uid=2000)
        assert dlg.name_edit.text() == ""

    def test_window_title(self, qapp):
        dlg = NewSiloDialog(suggested_uid=2000)
        assert dlg.windowTitle() == "New silo"

    def test_object_names_for_accessibility(self, qapp):
        dlg = NewSiloDialog(suggested_uid=2000)
        assert dlg.name_edit.objectName() == "silo_name_edit"
        assert dlg.uid_spin.objectName() == "silo_uid_spin"

    def test_name_edit_accepts_input(self, qapp):
        """Name edit can hold arbitrary string input."""
        dlg = NewSiloDialog(suggested_uid=2000)
        dlg.name_edit.setText("my-silo-01")
        assert dlg.name_edit.text() == "my-silo-01"

    def test_uid_spin_can_be_updated(self, qapp):
        """UID spinner value can be programmatically updated in range."""
        dlg = NewSiloDialog(suggested_uid=2000)
        dlg.uid_spin.setValue(3500)
        assert dlg.uid_spin.value() == 3500


# ---------------------------------------------------------------------------
# _DBusMenuService — interface name + method name inventory contract
# ---------------------------------------------------------------------------

class TestDBusMenuInterfaceContract:
    """Pin the exported interface surface of _DBusMenuService.

    These tests assert that specific method names exist and return the correct
    shapes, so a rename/deletion is detected without a live D-Bus session.
    The behavioural tests live in TestDBusMenuService in test_admin_notifications.py.
    """

    def test_interface_constant(self):
        assert DBUSMENU_IFACE == "com.canonical.dbusmenu"

    def test_menu_id_constants(self):
        """Menu item IDs must be distinct non-negative integers."""
        ids = [_MENU_ROOT_ID, _MENU_SHOW_ID, _MENU_MUTE_ID, _MENU_QUIT_ID]
        assert all(isinstance(i, int) and i >= 0 for i in ids)
        assert len(set(ids)) == 4, "Menu IDs must all be distinct"

    def test_root_id_is_zero(self):
        """DBusMenu protocol: the root item ID is 0."""
        assert _MENU_ROOT_ID == 0

    def test_get_layout_method_exists(self):
        menu = _make_stub_menu()
        assert callable(getattr(menu, "GetLayout", None))

    def test_get_layout_carries_dbus_export_metadata(self):
        """GetLayout must carry the @dbus.service.method decorator metadata.

        A dropped decorator would remove the _dbus_interface attribute and
        silently unregister the method from the D-Bus object, breaking the
        tray menu without any obvious error at startup.
        """
        menu = _make_stub_menu()
        method = getattr(type(menu), "GetLayout", None)
        assert getattr(method, "_dbus_interface", None) == DBUSMENU_IFACE, (
            f"GetLayout._dbus_interface={getattr(method, '_dbus_interface', None)!r}; "
            f"expected {DBUSMENU_IFACE!r}. The @dbus.service.method decorator may have been removed."
        )
        assert getattr(method, "_dbus_is_method", None) is True, (
            "GetLayout._dbus_is_method is not True — dbus.service.method decorator missing?"
        )

    def test_event_method_exists(self):
        menu = _make_stub_menu()
        assert callable(getattr(menu, "Event", None))

    def test_about_to_show_method_exists(self):
        menu = _make_stub_menu()
        assert callable(getattr(menu, "AboutToShow", None))

    def test_set_mute_label_method_exists(self):
        menu = _make_stub_menu()
        assert callable(getattr(menu, "set_mute_label", None))

    def test_layout_updated_attribute_exists(self):
        """LayoutUpdated must be defined (it is a D-Bus signal)."""
        menu = _make_stub_menu()
        assert hasattr(menu, "LayoutUpdated")

    def test_get_layout_returns_tuple(self):
        """GetLayout must return (revision, root_tuple)."""
        menu = _make_stub_menu()
        result = menu.GetLayout(_MENU_ROOT_ID, -1, [])
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_get_layout_revision_is_positive(self):
        menu = _make_stub_menu()
        revision, _ = menu.GetLayout(_MENU_ROOT_ID, -1, [])
        assert int(revision) >= 1

    def test_get_layout_root_has_three_children(self):
        """Root must have exactly Show / Mute / Quit items."""
        menu = _make_stub_menu()
        _, root = menu.GetLayout(_MENU_ROOT_ID, -1, [])
        _root_id, _root_props, children = root
        assert len(children) == 3

    def test_get_layout_children_have_labels(self):
        """Each menu child must have a non-empty label property."""
        menu = _make_stub_menu()
        _, root = menu.GetLayout(_MENU_ROOT_ID, -1, [])
        _, _, children = root
        for child_id, props, _ in children:
            assert "label" in props
            assert props["label"]  # non-empty

    def test_about_to_show_returns_false(self):
        menu = _make_stub_menu()
        result = menu.AboutToShow(0)
        assert result is False

    def test_event_clicked_invokes_callback(self):
        invoked = []
        menu = _make_stub_menu(callbacks={_MENU_SHOW_ID: lambda: invoked.append(1)})
        menu.Event(_MENU_SHOW_ID, "clicked", _variant(""), 0)
        assert invoked == [1]

    def test_event_non_clicked_is_noop(self):
        invoked = []
        menu = _make_stub_menu(callbacks={_MENU_SHOW_ID: lambda: invoked.append(1)})
        menu.Event(_MENU_SHOW_ID, "hovered", _variant(""), 0)
        assert invoked == []

    def test_event_unknown_item_is_noop(self):
        """Unknown item IDs must not crash."""
        menu = _make_stub_menu()
        menu.Event(9999, "clicked", _variant(""), 0)  # should not raise

    def test_set_mute_label_changes_label(self):
        menu = _make_stub_menu()
        menu.LayoutUpdated = MagicMock()
        menu.set_mute_label("Unmute")
        assert menu._mute_label == "Unmute"

    def test_set_mute_label_bumps_revision(self):
        menu = _make_stub_menu()
        menu.LayoutUpdated = MagicMock()
        rev_before = menu._revision
        menu.set_mute_label("Unmute")
        assert menu._revision == rev_before + 1

    def test_set_mute_label_emits_layout_updated(self):
        """set_mute_label must emit LayoutUpdated(revision, parent_id)."""
        menu = _make_stub_menu()
        menu.LayoutUpdated = MagicMock()
        menu.set_mute_label("Unmute")
        menu.LayoutUpdated.assert_called_once()
        args = menu.LayoutUpdated.call_args[0]
        assert int(args[0]) == menu._revision
        assert int(args[1]) == _MENU_ROOT_ID


# ---------------------------------------------------------------------------
# StatusNotifierItemService — interface name + signal names contract
# ---------------------------------------------------------------------------

class TestSNIInterfaceContract:
    """Pin the exported interface surface of StatusNotifierItemService.

    Behavioural tests live in TestStatusNotifierItemService in
    test_admin_notifications.py. These tests ensure the interface strings and
    signal names match the StatusNotifierItem spec so a rename is caught.
    """

    def test_sni_interface_constant(self):
        assert SNI_IFACE == "org.kde.StatusNotifierItem"

    def test_get_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "Get", None))

    def test_get_all_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "GetAll", None))

    def test_activate_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "Activate", None))

    def test_secondary_activate_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "SecondaryActivate", None))

    def test_context_menu_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "ContextMenu", None))

    def test_new_status_signal_exists(self):
        """NewStatus is a D-Bus signal: the attribute must exist."""
        sni = _make_stub_sni()
        assert hasattr(sni, "NewStatus")

    def test_new_icon_signal_exists(self):
        sni = _make_stub_sni()
        assert hasattr(sni, "NewIcon")

    def test_new_tooltip_signal_exists(self):
        sni = _make_stub_sni()
        assert hasattr(sni, "NewToolTip")

    def test_set_status_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "set_status", None))

    def test_set_tooltip_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "set_tooltip", None))

    def test_set_pending_count_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "set_pending_count", None))

    def test_set_mute_label_method_exists(self):
        sni = _make_stub_sni()
        assert callable(getattr(sni, "set_mute_label", None))

    def test_get_property_returns_string_for_category(self):
        """Category property must return a non-empty string."""
        sni = _make_stub_sni()
        val = sni._get_property("Category")
        import dbus as _dbus
        assert isinstance(val, _dbus.String)
        assert str(val)

    def test_get_property_returns_string_for_id(self):
        sni = _make_stub_sni()
        import dbus as _dbus
        val = sni._get_property("Id")
        assert isinstance(val, _dbus.String)
        assert str(val) == "qdistro-admin"

    def test_get_property_returns_string_for_title(self):
        sni = _make_stub_sni()
        import dbus as _dbus
        val = sni._get_property("Title")
        assert isinstance(val, _dbus.String)
        assert str(val) == "qdistro admin approvals"

    def test_get_property_returns_string_for_status(self):
        sni = _make_stub_sni()
        import dbus as _dbus
        val = sni._get_property("Status")
        assert isinstance(val, _dbus.String)
        assert str(val) in ("Active", "NeedsAttention", "Passive")

    def test_get_property_unknown_raises_dbus_exception(self):
        """Unknown property must raise DBusException, not KeyError."""
        import dbus as _dbus
        sni = _make_stub_sni()
        with pytest.raises(_dbus.DBusException):
            sni._get_property("NoSuchPropertyXYZ")

    def test_set_status_changes_internal_status(self):
        sni = _make_stub_sni()
        sni.NewStatus = MagicMock()
        sni.set_status("NeedsAttention")
        assert sni._status == "NeedsAttention"

    def test_set_status_emits_new_status_signal(self):
        sni = _make_stub_sni()
        emitted = []
        sni.NewStatus = lambda s: emitted.append(s)
        sni.set_status("NeedsAttention")
        assert emitted == ["NeedsAttention"]

    def test_set_status_noop_when_unchanged(self):
        sni = _make_stub_sni()
        emitted = []
        sni.NewStatus = lambda s: emitted.append(s)
        sni.set_status("Active")  # already Active
        assert emitted == []

    def test_set_tooltip_changes_tooltip_text(self):
        sni = _make_stub_sni()
        sni.NewToolTip = MagicMock()
        sni.set_tooltip("3 pending approvals")
        assert sni._tooltip_text == "3 pending approvals"

    def test_set_tooltip_emits_new_tooltip_signal(self):
        sni = _make_stub_sni()
        emitted = []
        sni.NewToolTip = lambda: emitted.append(True)
        sni.set_tooltip("new text")
        assert len(emitted) == 1

    def test_set_tooltip_noop_when_unchanged(self):
        sni = _make_stub_sni()
        emitted = []
        sni.NewToolTip = lambda: emitted.append(True)
        sni.set_tooltip("qdistro admin approvals")  # default value
        assert emitted == []

    def test_set_pending_count_to_nonzero_transitions_to_needs_attention(self):
        sni = _make_stub_sni()
        sni.NewStatus = MagicMock()
        sni.NewIcon = MagicMock()
        sni.set_pending_count(1)
        assert sni._status == "NeedsAttention"

    def test_set_pending_count_to_zero_transitions_to_active(self):
        sni = _make_stub_sni()
        sni.NewStatus = MagicMock()
        sni.NewIcon = MagicMock()
        sni.set_pending_count(1)
        sni.set_pending_count(0)
        assert sni._status == "Active"

    def test_set_pending_count_emits_new_icon(self):
        sni = _make_stub_sni()
        sni.NewStatus = MagicMock()
        icons = []
        sni.NewIcon = lambda: icons.append(True)
        sni.set_pending_count(3)
        assert len(icons) == 1

    def test_activate_invokes_callback(self):
        activated = []
        sni = _make_stub_sni(callbacks={"activate": lambda: activated.append(True)})
        sni.Activate(0, 0)
        assert len(activated) == 1

    def test_activate_without_callback_does_not_crash(self):
        sni = _make_stub_sni(callbacks={})
        sni.Activate(0, 0)  # must not raise
