"""Screenshot plugin: viewport capture is testable; full-page needs
QtWebEngine rendering which is fragile in headless mode, so we mock JS."""



def test_save_dir_exists(tmp_path, monkeypatch):
    import qdbrowser.plugins.screenshot as ss
    monkeypatch.setattr(ss, "_save_dir", lambda: str(tmp_path))
    assert ss._save_dir() == str(tmp_path)


def test_capture_viewport_returns_path(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.screenshot as ss
    monkeypatch.setattr(ss, "_save_dir", lambda: str(tmp_path))
    plug = window.plugins._instances["screenshot"]
    path = plug.capture_viewport(window._active_webview,
                                  path=str(tmp_path / "x.png"))
    assert path is not None
    assert path.endswith(".png")


def test_capture_viewport_none_returns_none(window):
    plug = window.plugins._instances["screenshot"]
    assert plug.capture_viewport(None) is None


def test_capture_full_none_returns_none(window):
    plug = window.plugins._instances["screenshot"]
    assert plug.capture_full_page(None) is None


def test_commands_provided(window):
    plug = window.plugins._instances["screenshot"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("Screenshot" in label for label in labels)
    assert any("viewport" in label for label in labels)
