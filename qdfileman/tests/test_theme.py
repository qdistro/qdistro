"""Tests for theme.apply_theme."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QPushButton

from qfileman.theme import _underlying_style_name, apply_theme


def test_apply_theme_dark_sets_dark_window_color(qapp):
    """The dark palette uses an explicit dark window colour."""
    apply_theme(qapp, "dark")
    color = qapp.palette().color(QPalette.ColorRole.Window)
    # Lightness 0..255; Fusion default is bright. Our dark palette is 53,53,53.
    assert color.lightness() < 80, f"dark mode should be dark, got {color.getRgb()}"


def test_apply_theme_light_is_default_palette(qapp):
    """Light mode resets to a default Fusion palette."""
    # Switch to dark first, then to light, to prove the override is removed.
    apply_theme(qapp, "dark")
    apply_theme(qapp, "light")
    assert _underlying_style_name(qapp).lower() == "fusion"
    # Compare against a fresh QPalette() — light mode should match it.
    default = QPalette()
    assert (
        qapp.palette().color(QPalette.ColorRole.Window).getRgb()
        == default.color(QPalette.ColorRole.Window).getRgb()
    )


def test_apply_theme_returns_resolved_mode(qapp):
    assert apply_theme(qapp, "dark") == "dark"
    assert apply_theme(qapp, "light") == "light"
    assert apply_theme(qapp, "system") == "system"


def test_apply_theme_unknown_mode_falls_back_with_warning(qapp, caplog):
    with caplog.at_level("WARNING", logger="qfileman.theme"):
        resolved = apply_theme(qapp, "neon")
    assert resolved == "system"
    assert any("unknown theme mode" in r.message for r in caplog.records)


def test_apply_theme_light_uses_fusion_style(qapp):
    """Light mode opts into Fusion for consistent rendering."""
    apply_theme(qapp, "light")
    assert _underlying_style_name(qapp).lower() == "fusion"


def test_apply_theme_dark_uses_fusion_style(qapp):
    """Dark mode also opts into Fusion (the palette is Fusion-shaped)."""
    apply_theme(qapp, "dark")
    assert _underlying_style_name(qapp).lower() == "fusion"


@pytest.mark.parametrize("with_qss", [False, True])
def test_apply_theme_native_restores_captured_baseline(qapp, with_qss):
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QStyleFactory
    from qfileman.theme import reset_controller_for_tests

    windows = next((n for n in QStyleFactory.keys() if n.lower() == "windows"), None)
    if windows is None:
        pytest.skip("Windows style required to distinguish Fusion")
    original_style = _underlying_style_name(qapp)
    original_pal = QPalette(qapp.palette())
    original_qss = qapp.styleSheet()
    reset_controller_for_tests()
    try:
        qapp.setStyle(windows)
        pal = QPalette(qapp.palette())
        pal.setColor(QPalette.ColorRole.Window, QColor("#c8dcc8"))
        qapp.setPalette(pal)
        qapp.setStyleSheet("QWidget { background-color: #c8dcc8; }" if with_qss else "")
        window_rgb = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
        qss = qapp.styleSheet()
        apply_theme(qapp, "dark")
        assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() != window_rgb
        assert _underlying_style_name(qapp).lower() == "fusion"
        assert apply_theme(qapp, "native") == "native"
        assert _underlying_style_name(qapp).lower() == "windows"
        assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == window_rgb
        assert qapp.styleSheet() == qss
    finally:
        reset_controller_for_tests()
        qapp.setStyle(original_style)
        qapp.setPalette(original_pal)
        qapp.setStyleSheet(original_qss)


def test_apply_theme_system_is_a_noop(qapp):
    """``system`` mode must not touch style or palette — Qt's platform
    integration owns that. Set a known non-default state first, then
    verify ``system`` leaves it intact."""
    apply_theme(qapp, "dark")
    style_before = qapp.style().objectName()
    palette_before = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    resolved = apply_theme(qapp, "system")
    assert resolved == "system"
    assert qapp.style().objectName() == style_before, "system mode must not call setStyle"
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == palette_before, (
        "system mode must not call setPalette"
    )


def test_attach_presentation_follows_snapshot(qapp, tmp_path):
    from qdistro_presentation.model import example_snapshot
    from qdistro_presentation.paths import ResolvedPath
    from qdistro_presentation.publish import write_snapshot
    from qdistro_presentation.qt import PresentationController
    from qfileman.config import Config
    from qfileman.theme import apply_theme, reset_controller_for_tests

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    Config._instance = None
    Config._data = None
    cfg = Config()
    cfg.set("general", "theme_mode", "system")
    reset_controller_for_tests()
    ctrl = PresentationController(
        qapp,
        theme_mode="system",
        snapshot_path=ResolvedPath(
            path=str(tmp_path / "current.json"),
            kind="override",
            expected_uid=None,
            watch=False,
        ),
        apply_legacy=lambda app, mode: apply_theme(app, mode),
        apply_system_fallback=lambda app: apply_theme(app, "system"),
        watch=False,
    )
    assert ctrl.state.using_shared_palette is True
    assert (
        qapp.palette().color(QPalette.ColorRole.Window).name() == example_snapshot().colors.mSurface
    )
    ctrl.set_theme_mode("native")
    ctrl.stop()
    reset_controller_for_tests()


def test_presentation_update_keeps_two_pane_paths(qapp, tmp_dir, tmp_path):
    from dataclasses import replace

    from qdistro_presentation.model import example_snapshot, with_generation
    from qdistro_presentation.paths import ResolvedPath
    from qdistro_presentation.publish import write_snapshot
    from qdistro_presentation.qt import PresentationController
    from qfileman.theme import apply_theme, reset_controller_for_tests
    from qfileman.window import FileManagerWindow

    other = tmp_path / "other"
    other.mkdir()
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    reset_controller_for_tests()
    ctrl = PresentationController(
        qapp,
        theme_mode="system",
        snapshot_path=ResolvedPath(
            path=str(tmp_path / "current.json"),
            kind="override",
            expected_uid=None,
            watch=False,
        ),
        apply_legacy=lambda app, mode: apply_theme(app, mode),
        apply_system_fallback=lambda app: apply_theme(app, "system"),
        watch=False,
    )
    win = FileManagerWindow()
    try:
        win._update_path(str(tmp_dir))
        new_pane = win._split_right()
        new_pane._update_path(str(other))
        before = [pane.current_path for pane in win._split_root.find_panes()]
        second = with_generation(replace(example_snapshot(), mode="light"))
        write_snapshot(
            str(tmp_path),
            second,
            require_unwritable_dirs=False,
            skip_unchanged=False,
        )
        ctrl._reload()
        win.apply_presentation_update()
        after = [pane.current_path for pane in win._split_root.find_panes()]
        assert after == before
        assert str(tmp_dir) in after
        assert str(other) in after
        assert len(after) == 2
        assert ctrl.state.generation == second.generation
        assert ctrl.state.snapshot.mode == "light"
    finally:
        win.close()
        win.deleteLater()
        ctrl.stop()
        reset_controller_for_tests()


def test_preferences_dialog_apply_presentation_update_polishes(qapp):
    from qfileman.preferences import PreferencesDialog

    dlg = PreferencesDialog()
    try:
        dlg.show()
        dlg.apply_presentation_update()
        assert dlg.isVisible()
    finally:
        dlg.close()
        dlg.deleteLater()


def _item_named(file_list, name: str):
    for i in range(file_list.count()):
        item = file_list.item(i)
        if item is not None and item.text() == name:
            return item
    return None


def test_toolbar_retains_named_theme_icons(qapp):
    from qfileman.icons import THEME_ICON_PROPERTY
    from qfileman.window import FileManagerWindow

    win = FileManagerWindow()
    try:
        buttons = {btn.toolTip(): btn for btn in win.findChildren(QPushButton)}
        assert buttons["Go back"].property(THEME_ICON_PROPERTY) == "go-previous"
        assert buttons["Go forward"].property(THEME_ICON_PROPERTY) == "go-next"
        assert buttons["Go up one level"].property(THEME_ICON_PROPERTY) == "go-up"
        assert buttons["Refresh"].property(THEME_ICON_PROPERTY) == "view-refresh"
        home = next(b for b in buttons.values() if b.toolTip() == "Home")
        assert home.property(THEME_ICON_PROPERTY) == "go-home"
    finally:
        win.close()
        win.deleteLater()


@pytest.mark.cheat_aware(
    protects="Live appearance updates re-query named and file icons without rebuilding listings",
    severity="important",
    cheats=[
        "restore apply_presentation_update to only unpolish/polish/update",
        "call FilePane._load_files from the update path",
        "drop item-identity or icon_size assertions",
        "skip QIcon.fromTheme / file_icon / pixmap-cache / provider replacement checks",
    ],
    consequence="Icon-theme changes leave stale chrome/file icons or reset selection and view size",
)
def test_presentation_update_refreshes_icons_without_rebuilding(qapp, tmp_dir, monkeypatch):
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QIcon, QPixmapCache
    from PyQt6.QtWidgets import QFileIconProvider

    from qfileman.icons import file_icon as real_file_icon
    from qfileman.pane import FilePane
    from qfileman.window import FileManagerWindow

    win = FileManagerWindow()
    loads = {"n": 0}
    original_load = FilePane._load_files

    def counting_load(self):
        loads["n"] += 1
        return original_load(self)

    monkeypatch.setattr(FilePane, "_load_files", counting_load)
    try:
        win._update_path(str(tmp_dir))
        assert loads["n"] == 1
        target = _item_named(win.file_list, "file1.txt")
        assert target is not None
        win.file_list.setCurrentItem(target)
        win.file_list.setIconSize(QSize(40, 40))
        scroll = win.file_list.verticalScrollBar().value()
        old_provider = win.fs_model.iconProvider()
        old_pane_provider = win._active_pane._icon_provider

        theme_names: list[str] = []
        real_from_theme = QIcon.fromTheme

        def tracking_from_theme(name, *args, **kwargs):
            theme_names.append(str(name))
            return real_from_theme(name, *args, **kwargs)

        monkeypatch.setattr("qfileman.icons.QIcon.fromTheme", tracking_from_theme)

        queried: list[str] = []

        def tracking_file_icon(provider, path):
            queried.append(str(path))
            return real_file_icon(provider, path)

        monkeypatch.setattr("qfileman.pane.file_icon", tracking_file_icon)

        cache_clears: list[bool] = []
        real_clear = QPixmapCache.clear

        def tracking_clear():
            cache_clears.append(True)
            return real_clear()

        monkeypatch.setattr("qfileman.icons.QPixmapCache.clear", tracking_clear)

        win.apply_presentation_update()

        assert loads["n"] == 1
        assert win.file_list.currentItem() is target
        assert _item_named(win.file_list, "file1.txt") is target
        assert win.file_list.iconSize() == QSize(40, 40)
        assert win.current_path == str(tmp_dir)
        assert win.file_list.verticalScrollBar().value() == scroll
        assert cache_clears == [True]
        assert win.fs_model.iconProvider() is not old_provider
        assert win._active_pane._icon_provider is not old_pane_provider
        assert win._active_pane._icon_provider is win._file_icon_provider
        assert win.fs_model.iconProvider() is win._file_icon_provider
        assert isinstance(win._file_icon_provider, QFileIconProvider)
        assert "go-previous" in theme_names
        assert "go-next" in theme_names
        assert "go-up" in theme_names
        assert "view-refresh" in theme_names
        assert "go-home" in theme_names
        listed_paths = {
            (win.file_list.item(i).data(Qt.ItemDataRole.UserRole) or {}).get("path")
            for i in range(win.file_list.count())
        }
        assert str(tmp_dir / "file1.txt") in queried
        assert str(tmp_dir / "subdir") in queried
        assert listed_paths <= set(queried)
    finally:
        win.close()
        win.deleteLater()


def test_presentation_update_keeps_two_pane_selection(qapp, tmp_dir, tmp_path):
    from qfileman.window import FileManagerWindow

    other = tmp_path / "other"
    other.mkdir()
    (other / "alpha.txt").write_text("a")
    (other / "beta.txt").write_text("b")
    win = FileManagerWindow()
    try:
        win._update_path(str(tmp_dir))
        left = _item_named(win.file_list, "file2.txt")
        assert left is not None
        win.file_list.setCurrentItem(left)
        new_pane = win._split_right()
        new_pane._update_path(str(other))
        right = _item_named(new_pane.file_list, "beta.txt")
        assert right is not None
        new_pane.file_list.setCurrentItem(right)
        win.apply_presentation_update()
        panes = {pane.current_path: pane for pane in win._split_root.find_panes()}
        assert set(panes) == {str(tmp_dir), str(other)}
        assert panes[str(tmp_dir)].file_list.currentItem() is left
        assert panes[str(other)].file_list.currentItem() is right
        assert _item_named(panes[str(tmp_dir)].file_list, "file2.txt") is left
        assert _item_named(panes[str(other)].file_list, "beta.txt") is right
    finally:
        win.close()
        win.deleteLater()

