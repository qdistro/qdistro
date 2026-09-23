"""Window action handlers (toolbar / shortcut targets)."""

from unittest.mock import patch

from PyQt6.QtCore import Qt


def test_navigate_active_creates_tab_when_empty(window):
    # Clear existing tabs.
    while window._tabs.count() > 0:
        window._tabs.removeTab(0)
    window._active_webview = None
    window._navigate_active("https://example.com")
    assert window._tabs.count() == 1


def test_navigate_active_uses_existing_webview(window):
    wv = window._active_webview
    with patch.object(wv, "navigate") as nav:
        window._navigate_active("about:blank")
        nav.assert_called_once()


def test_go_back_forward_safe_with_no_history(window):
    window._go_back()
    window._go_forward()
    # No exceptions.


def test_reload_calls_active(window):
    with patch.object(window._active_webview, "reload") as r:
        window._reload()
        r.assert_called_once()


def test_stop_calls_active(window):
    with patch.object(window._active_webview, "stop") as s:
        window._stop()
        s.assert_called_once()


def test_home_navigates_to_homepage(window, fresh_config):
    window._config.set("general", "homepage", "https://homepage.test")
    with patch.object(window._active_webview, "navigate") as nav:
        window._home()
        nav.assert_called_with("https://homepage.test")


def test_zoom_step_changes_zoom(window):
    before = window._active_webview.zoom()
    window._zoom_step(0.25)
    after = window._active_webview.zoom()
    assert after == before + 0.25


def test_zoom_set_explicit(window):
    window._zoom_set(2.0)
    assert window._active_webview.zoom() == 2.0


def test_toggle_side_panel(window):
    sp = window._side_panel
    initial = sp.isVisible()
    window._toggle_side_panel()
    assert sp.isVisible() == (not initial)


def test_cycle_tab(window):
    window.new_tab()
    window.new_tab()
    assert window._tabs.count() == 3
    window._tabs.setCurrentIndex(0)
    window._cycle_tab(1)
    assert window._tabs.currentIndex() == 1
    window._cycle_tab(-1)
    assert window._tabs.currentIndex() == 0


def test_cycle_tab_wraps(window):
    window.new_tab()
    window._tabs.setCurrentIndex(0)
    window._cycle_tab(-1)
    # Should wrap to last tab.
    assert window._tabs.currentIndex() == window._tabs.count() - 1


def test_switch_to_tab_valid(window):
    window.new_tab()
    window.new_tab()
    window._switch_to_tab(1)
    assert window._tabs.currentIndex() == 1


def test_switch_to_tab_out_of_range_noop(window):
    window._switch_to_tab(99)
    # Index is unchanged (still 0).
    assert window._tabs.currentIndex() == 0


def test_close_split_collapses_tab_when_last(window):
    # Single webview in the tab — closing it collapses the tab.
    assert window._tabs.count() == 1
    window._close_active_split()
    # After collapsing, a fresh tab is opened.
    assert window._tabs.count() == 1


def test_split_emits_webview_added(window):
    seen = []
    window.webview_added.connect(lambda wv: seen.append(wv))
    window._split(Qt.Orientation.Horizontal)
    assert len(seen) == 1


def test_close_split_emits_webview_removed(window):
    window._split(Qt.Orientation.Horizontal)
    seen = []
    window.webview_removed.connect(lambda wv: seen.append(wv))
    window._close_active_split()
    assert len(seen) == 1


def test_reopen_last_tab_restores(window):
    initial = window._tabs.count()
    window.new_tab(url="about:blank")
    assert window._tabs.count() == initial + 1
    window._close_current_tab()
    assert window._tabs.count() == initial
    window._reopen_last_tab()
    assert window._tabs.count() == initial + 1
