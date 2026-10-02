"""Shared presentation attach for qdbrowser chrome and overlay palette."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QPalette
from qdbrowser.theme import attach_presentation, palette_dict, reset_controller_for_tests
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot


def _config(theme_mode: str = "system", appearance: dict | None = None):
    appearance = appearance or {}

    def get(*keys, default=None):
        if keys[:2] == ("general", "theme_mode"):
            return theme_mode
        if keys == ("appearance",):
            return appearance
        return default

    return SimpleNamespace(get=get)


@pytest.fixture(autouse=True)
def _reset_presentation(qapp):
    reset_controller_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")
    yield
    reset_controller_for_tests()


def test_attach_presentation_follows_snapshot(qapp, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    resolved = attach_presentation(qapp, _config("system"))
    assert resolved == example_snapshot().mode
    colors = example_snapshot().colors
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == colors.mSurface
    auto = palette_dict("auto")
    assert auto["bg"] == colors.mSurface
    assert auto["fg"] == colors.mOnSurface
    assert auto["accent"] == colors.mPrimary
    assert auto["border"] == colors.mOutline
    assert auto["selection"] == colors.mPrimary
    assert auto["selection_fg"] == colors.mOnPrimary


def test_palette_dict_explicit_dark_stays_content_policy(qapp, tmp_path, monkeypatch):
    from qdbrowser.theme import ACCENT_LIGHT, BG_DARK, FG, SELECTION

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _config("system"))
    forced = palette_dict("dark")
    assert forced["bg"] == BG_DARK
    assert forced["fg"] == FG
    assert forced["accent"] == ACCENT_LIGHT
    assert forced["selection"] == SELECTION
    assert forced["bg"] != example_snapshot().colors.mSurface


def test_attach_presentation_native_ignores_snapshot(qapp, tmp_path, monkeypatch):
    native = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    resolved = attach_presentation(qapp, _config("native"))
    assert resolved == "native"
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native


def test_live_snapshot_restyles_overlay_without_reload(
        qapp, tmp_path, monkeypatch, window):
    from dataclasses import replace
    from unittest.mock import MagicMock

    from qdbrowser.theme import current_controller, overlay_palette
    from qdistro_presentation.model import with_generation

    snap_a = example_snapshot()
    write_snapshot(str(tmp_path), snap_a, require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _config("system"))
    assert overlay_palette("auto")["bg"] == snap_a.colors.mSurface

    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    dark = window.plugins._instances["dark_mode"]
    dark.apply = MagicMock()

    snap_b = with_generation(replace(
        snap_a,
        colors=replace(snap_a.colors, mSurface="#000000"),
        generation="",
    ))
    write_snapshot(str(tmp_path), snap_b, require_unwritable_dirs=False)
    current_controller()._reload()

    wv.view.page().runJavaScript.assert_called()
    scripts = [call.args[0] for call in wv.view.page().runJavaScript.call_args_list]
    joined = "\n".join(scripts)
    assert snap_b.colors.mSurface in joined
    assert "removed:true" not in joined
    assert "location.reload" not in joined
    assert any("__qdb_translate_overlay" in js for js in scripts)
    assert any("__qdb_reader_overlay" in js for js in scripts)
    assert any("pickRoot" in js for js in scripts) is False
    dark.apply.assert_not_called()
    assert overlay_palette("auto")["bg"] == snap_b.colors.mSurface


def test_theme_mode_light_updates_desktop_dark(
        qapp, tmp_path, monkeypatch, window):
    from qdbrowser.theme import current_controller, current_resolved_theme

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _config("system"))
    assert current_resolved_theme() == "dark"

    dark = window.plugins._instances["dark_mode"]
    dark._desktop_dark = True
    current_controller().set_theme_mode("light")
    assert current_resolved_theme() == "light"
    assert dark._desktop_dark is False


def test_apply_presentation_update_restyles_tab_and_sidebar(window):
    from unittest.mock import MagicMock

    from qdbrowser.webview import WebView

    plug = window.plugins._instances["web_panels"]
    host = plug._host
    sidebar = WebView(url="about:blank")
    host._slot_layout.addWidget(sidebar)
    host._current_webview = sidebar
    views = list(window.iter_webviews())
    assert window._active_webview in views
    assert sidebar in views

    window._active_webview.view.page().runJavaScript = MagicMock()
    sidebar.view.page().runJavaScript = MagicMock()
    dark = window.plugins._instances["dark_mode"]
    dark._desktop_dark = False
    dark._global_default = "auto"
    dark.apply = MagicMock()

    window.apply_presentation_update()
    window._active_webview.view.page().runJavaScript.assert_called()
    sidebar.view.page().runJavaScript.assert_called()
    for mock_js in (
        window._active_webview.view.page().runJavaScript,
        sidebar.view.page().runJavaScript,
    ):
        scripts = [call.args[0] for call in mock_js.call_args_list]
        joined = "\n".join(scripts)
        assert "removed:true" not in joined
        assert any("__qdb_translate_overlay" in js for js in scripts)
        assert any("__qdb_reader_overlay" in js for js in scripts)
        assert any("pickRoot" in js for js in scripts) is False
    dark.apply.assert_any_call(window._active_webview)
    dark.apply.assert_any_call(sidebar)


def test_new_window_reads_live_controller_theme(monkeypatch, window):
    from unittest.mock import MagicMock

    from qdbrowser import theme as theme_mod

    created = []

    def fake_ctor(*args, **kwargs):
        created.append(kwargs.get("resolved_theme"))
        dummy = MagicMock()
        dummy._resolved_theme = kwargs.get("resolved_theme")
        return dummy

    monkeypatch.setattr(
        theme_mod, "current_resolved_theme", lambda fallback=None: "native")
    monkeypatch.setattr("qdbrowser.window.MainWindow", fake_ctor)
    window._new_window()
    assert created == ["native"]
