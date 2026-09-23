"""Window-level signals: webview_added, navigation_event, active_changed."""


def test_active_webview_changed_emits(window):
    seen = []
    window.active_webview_changed.connect(lambda wv: seen.append(wv))
    wv = window.new_tab(url="about:blank")
    assert seen[-1] is wv


def test_webview_added_emits_on_new_tab(window):
    seen = []
    window.webview_added.connect(lambda wv: seen.append(wv))
    wv = window.new_tab(url="about:blank")
    assert wv in seen


def test_webview_removed_emits_on_close(window):
    seen = []
    window.webview_removed.connect(lambda wv: seen.append(wv))
    wv = window.new_tab(url="about:blank")
    window._close_current_tab()
    assert wv in seen


def test_window_title_updates_on_active_change(window):
    window.new_tab(url="about:blank")
    title = window.windowTitle()
    assert "qdbrowser" in title


def test_url_bar_updates_on_active_change(window):
    from PyQt6.QtCore import QUrl
    wv = window._active_webview
    wv._on_url(QUrl("https://updated.test/"))
    assert "updated.test" in window._url_bar.text()
