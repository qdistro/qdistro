from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qdistro_presentation.model import (
    DESKTOP_SETTINGS_UNAVAILABLE,
    example_snapshot,
    with_generation,
)
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook import nb_settings
from qnotebook.appearance import SettingsAdapter
from qnotebook.settings_dialog import SettingsDialog
from qnotebook.theme import attach_presentation, reset_controller_for_tests
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d)
    )
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield


@pytest.fixture
def win(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    yield w


def test_two_categories(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    assert dlg._category_list.count() == 2
    assert dlg._category_list.item(0).text() == "General"
    assert dlg._category_list.item(1).text() == "Shortcuts"


def test_search_filters_categories(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    dlg._search.setText("short")
    assert dlg._category_list.item(0).isHidden()
    assert not dlg._category_list.item(1).isHidden()
    dlg._search.setText("")
    assert not dlg._category_list.item(0).isHidden()
    assert not dlg._category_list.item(1).isHidden()


def test_apply_persists_nb_settings(win, qtbot, tmp_notebook: Path):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    dlg._chk_versioning.setChecked(False)
    dlg._chk_strict_preserve.setChecked(False)
    dlg._apply()
    assert nb_settings.get(tmp_notebook, "versioning_enabled", True) is False
    assert nb_settings.get(tmp_notebook, "strict_preserve", True) is False


def test_apply_persists_global_settings(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    dlg._spin_autosave_secs.setValue(45)
    dlg._chk_autosave.setChecked(False)
    dlg._combo_appearance.setCurrentText("Dark")
    dlg._chk_session_restore.setChecked(False)
    dlg._apply()
    s = QSettings("qnotebook", "qnotebook")
    assert s.value("autosave_ms", type=int) == 45 * 1000
    assert s.value("autosave_enabled", type=bool) is False
    assert s.value("appearance/theme_mode") == "dark"
    assert s.value("dark_mode", type=bool) is True
    assert s.value("session_restore_enabled", type=bool) is False


def test_shortcuts_table_populated(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    rows = dlg._shortcut_table.rowCount()
    assert rows > 5
    assert dlg._shortcut_table.horizontalHeaderItem(0).text() == "Action"
    assert dlg._shortcut_table.horizontalHeaderItem(1).text() == "Shortcut"


def test_apply_writes_shortcut_back(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    # Change the first row's shortcut and apply
    dlg._shortcut_table.item(0, 1).setText("Ctrl+Shift+F12")
    label = dlg._shortcut_table.item(0, 0).text()
    dlg._apply()
    # The window should now have that shortcut on the named action
    for lab, act in win._all_named_actions():
        if lab == label:
            assert act.shortcut().toString() == "Ctrl+Shift+F12"
            break
    else:
        pytest.fail(f"Action {label!r} not found")


def test_shortcut_column_uses_key_sequence_delegate(win, qtbot):
    from PyQt6.QtWidgets import QKeySequenceEdit
    from qnotebook.settings_dialog import _KeySequenceDelegate
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    delegate = dlg._shortcut_table.itemDelegateForColumn(1)
    assert isinstance(delegate, _KeySequenceDelegate)

    # The editor created by the delegate is a QKeySequenceEdit and
    # round-trips the existing cell value.
    index = dlg._shortcut_table.model().index(0, 1)
    editor = delegate.createEditor(dlg._shortcut_table, None, index)
    qtbot.addWidget(editor)
    assert isinstance(editor, QKeySequenceEdit)
    delegate.setEditorData(editor, index)
    assert editor.keySequence().toString() == dlg._shortcut_table.item(0, 1).text()


def test_context_menu_clear_and_reset(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    label = dlg._shortcut_table.item(0, 0).text()
    original = dlg._shortcut_defaults[label]

    # Simulate "Clear" by setting empty text, then "Reset" via the stored default.
    dlg._shortcut_table.item(0, 1).setText("")
    assert dlg._shortcut_table.item(0, 1).text() == ""
    dlg._shortcut_table.item(0, 1).setText(dlg._shortcut_defaults[label])
    assert dlg._shortcut_table.item(0, 1).text() == original


class _RowFinder:
    @staticmethod
    def row_for(dlg, label):
        for r in range(dlg._shortcut_table.rowCount()):
            item = dlg._shortcut_table.item(r, 0)
            if item is not None and item.text() == label:
                return r
        return -1


def test_distinct_shortcuts_have_no_conflict(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    # The initial table is whatever the app shipped with; just assert no
    # spurious conflicts on a fresh dialog.
    assert all(v is False for v in dlg._shortcut_conflicts.values())


def test_duplicate_shortcut_flags_both_rows(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    rows = dlg._shortcut_table.rowCount()
    assert rows >= 2
    # Force the first two rows to share a binding.
    dlg._shortcut_table.item(0, 1).setText("Ctrl+Alt+F11")
    dlg._shortcut_table.item(1, 1).setText("Ctrl+Alt+F11")
    dlg._refresh_shortcut_conflicts()
    label_a = dlg._shortcut_table.item(0, 0).text()
    label_b = dlg._shortcut_table.item(1, 0).text()
    assert dlg._shortcut_conflicts[label_a] is True
    assert dlg._shortcut_conflicts[label_b] is True


def test_resolving_conflict_clears_flag(win, qtbot):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    dlg._shortcut_table.item(0, 1).setText("Ctrl+Alt+F11")
    dlg._shortcut_table.item(1, 1).setText("Ctrl+Alt+F11")
    dlg._refresh_shortcut_conflicts()
    # Resolve by clearing row 1.
    dlg._shortcut_table.item(1, 1).setText("")
    dlg._refresh_shortcut_conflicts()
    label_a = dlg._shortcut_table.item(0, 0).text()
    assert dlg._shortcut_conflicts[label_a] is False


def test_appearance_only_apply_does_not_write_nb_settings(win, tmp_notebook, qtbot, monkeypatch):
    from qnotebook import nb_settings

    path = nb_settings.path_for(tmp_notebook)
    nb_settings.save(
        tmp_notebook,
        {
            "versioning_enabled": True,
            "versioning_prompted": False,
            "strict_preserve": True,
        },
    )
    before = path.read_bytes()
    writes: list = []
    real_set = nb_settings.set_value

    def _spy(*args, **kwargs):
        writes.append(args)
        return real_set(*args, **kwargs)

    monkeypatch.setattr("qnotebook.settings_dialog.nb_settings.set_value", _spy)
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    dlg._combo_appearance.setCurrentText("Light")
    dlg._apply()
    assert writes == []
    assert path.read_bytes() == before
    s = QSettings("qnotebook", "qnotebook")
    assert s.value("appearance/theme_mode") == "light"


def test_open_settings_follows_other_window_mode(win, qtbot, qapp):
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    assert dlg._combo_appearance.currentText() == "Follow desktop"
    win.act_appearance_dark.trigger()
    qapp.processEvents()
    assert dlg._combo_appearance.currentText() == "Dark"
    dlg._combo_appearance.setCurrentText("Light")
    win.act_appearance_native.trigger()
    qapp.processEvents()
    assert dlg._combo_appearance.currentText() == "Light"


def test_cancel_does_not_materialize_font_overrides(win, qtbot):
    s = QSettings("qnotebook", "qnotebook")
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    dlg.reject()
    assert not s.contains("appearance/ui_font_family")
    assert not s.contains("appearance/ui_font_size_pt")


def test_opening_settings_action_works(win, qtbot, monkeypatch):
    # Make sure invoking the menu action opens our dialog (and doesn't crash).
    captured = {}

    class _StubDialog:
        def __init__(self, parent):
            captured["opened"] = True
            self._category_list = type("L", (), {"count": lambda self: 0})()

        def exec(self):
            return 0

    monkeypatch.setattr(
        "qnotebook.settings_dialog.SettingsDialog", _StubDialog
    )
    win.act_settings.trigger()
    assert captured.get("opened") is True


def _scaled_snapshot():
    snap = example_snapshot()
    return with_generation(
        replace(snap, fonts=replace(snap.fonts, ui_scale=1.25, fixed_scale=1.25))
    )


def test_follow_desktop_without_snapshot_shows_unavailable(win, qtbot):
    reset_controller_for_tests()
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    assert dlg._combo_appearance.currentText() == "Follow desktop"
    assert dlg._chk_desktop_fonts.isChecked() is True
    assert DESKTOP_SETTINGS_UNAVAILABLE in dlg.lbl_desktop_status.text()
    dlg._combo_appearance.setCurrentText("Dark")
    dlg._chk_desktop_fonts.setChecked(False)
    assert dlg.lbl_desktop_status.text() == ""


def test_live_update_replaces_unavailable_with_inherited_size(
    win, qtbot, tmp_path, monkeypatch, qapp
):
    reset_controller_for_tests()
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    assert DESKTOP_SETTINGS_UNAVAILABLE in dlg.lbl_desktop_status.text()
    snap = _scaled_snapshot()
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, SettingsAdapter())
    assert DESKTOP_SETTINGS_UNAVAILABLE in dlg.lbl_desktop_status.text()
    dlg.apply_presentation_update()
    text = dlg.lbl_desktop_status.text()
    assert DESKTOP_SETTINGS_UNAVAILABLE not in text
    assert "13.75" in text
    assert snap.fonts.ui_family in text
    reset_controller_for_tests()
