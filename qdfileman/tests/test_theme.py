"""Tests for theme.apply_theme."""

from __future__ import annotations

from PyQt6.QtGui import QPalette
from qfileman.theme import apply_theme


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
    assert qapp.style().objectName().lower() == "fusion"
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
    assert qapp.style().objectName().lower() == "fusion"


def test_apply_theme_dark_uses_fusion_style(qapp):
    """Dark mode also opts into Fusion (the palette is Fusion-shaped)."""
    apply_theme(qapp, "dark")
    assert qapp.style().objectName().lower() == "fusion"


def test_apply_theme_native_restores_captured_baseline(qapp):
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QStyleFactory
    from qfileman.theme import reset_controller_for_tests

    original_style = qapp.style().objectName()
    original_pal = QPalette(qapp.palette())
    original_qss = qapp.styleSheet()
    reset_controller_for_tests()
    try:
        keys = {name.lower(): name for name in QStyleFactory.keys()}
        current = qapp.style().objectName()
        for candidate in ("Windows", "GTK+", "Oxygen"):
            mapped = keys.get(candidate.lower())
            if mapped and mapped.lower() != current.lower():
                qapp.setStyle(mapped)
                break
        pal = QPalette(qapp.palette())
        pal.setColor(QPalette.ColorRole.Window, QColor("#c8dcc8"))
        qapp.setPalette(pal)
        qapp.setStyleSheet("QWidget { background-color: #c8dcc8; }")
        style = qapp.style().objectName()
        window_rgb = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
        qss = qapp.styleSheet()
        apply_theme(qapp, "dark")
        assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() != window_rgb
        assert apply_theme(qapp, "native") == "native"
        assert qapp.style().objectName() == style
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
