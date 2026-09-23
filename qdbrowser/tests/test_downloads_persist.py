"""Downloads: history persistence + UI rebuild."""

import json


def test_load_missing(tmp_path, monkeypatch):
    import qdbrowser.plugins.downloads as d
    monkeypatch.setattr(d, "HISTORY_PATH", str(tmp_path / "absent.json"))
    assert d._load_history() == []


def test_load_broken(tmp_path, monkeypatch):
    import qdbrowser.plugins.downloads as d
    p = tmp_path / "h.json"
    p.write_text("not-json")
    monkeypatch.setattr(d, "HISTORY_PATH", str(p))
    assert d._load_history() == []


def test_load_non_list(tmp_path, monkeypatch):
    import qdbrowser.plugins.downloads as d
    p = tmp_path / "h.json"
    p.write_text('{"foo":1}')
    monkeypatch.setattr(d, "HISTORY_PATH", str(p))
    assert d._load_history() == []


def test_save_roundtrip(tmp_path, monkeypatch):
    import qdbrowser.plugins.downloads as d
    p = tmp_path / "h.json"
    monkeypatch.setattr(d, "HISTORY_PATH", str(p))
    entries = [{"path": "/x/a.zip", "size": 1, "url": "x", "ts": 0.0}]
    d._save_history(entries)
    assert json.loads(p.read_text()) == entries


def test_human_size():
    from qdbrowser.plugins.downloads import _human
    assert _human(0) == "0B"
    assert _human(1023) == "1023B"
    assert "KB" in _human(2048)
    assert "MB" in _human(2 * 1024 * 1024)


def test_panel_replays_history(window, tmp_path, monkeypatch):
    from qdbrowser.plugins.downloads import DownloadsPanel
    panel = DownloadsPanel(window, history=[
        {"path": "/x/a.zip", "size_str": "1.0KB"},
        {"path": "/x/b.zip", "size_str": "2.0KB"},
    ])
    assert panel._list.count() == 2


def test_panel_clear_finished_removes_historical(window):
    from qdbrowser.plugins.downloads import DownloadsPanel
    panel = DownloadsPanel(window, history=[
        {"path": "/x/done.zip", "size_str": "1KB"}])
    assert panel._list.count() == 1
    panel._clear_finished()
    assert panel._list.count() == 0


def test_panel_open_dir_uses_xdg_open_no_shell(window, monkeypatch,
                                                fresh_config):
    """Open-dir must use ``subprocess.Popen`` (no shell), passing the
    directory as one argv element so a path with shell metacharacters
    can't escape into the shell."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.downloads import DownloadsPanel
    dangerous = "/tmp/qdbtest$(rm -rf $HOME)"
    Config().set("downloads", "release_dir", dangerous)
    called = {}

    class FakeProc:
        pass

    def fake_popen(args, **kwargs):
        called["args"] = args
        called["shell"] = kwargs.get("shell", False)
        return FakeProc()

    monkeypatch.setattr("qdbrowser.plugins.downloads.subprocess.Popen",
                        fake_popen)
    panel = DownloadsPanel(window)
    panel._open_dir()
    assert called["args"][0] == "xdg-open"
    assert called["args"][1] == dangerous
    assert called["shell"] is False


def test_xdg_open_helper_is_safe(monkeypatch):
    """The _xdg_open helper must never invoke a shell."""
    from qdbrowser.plugins import downloads as d
    seen = {}

    class FakeProc:
        pass

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["shell"] = kwargs.get("shell", False)
        return FakeProc()

    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    d._xdg_open("'; rm -rf / #")
    assert seen["args"] == ["xdg-open", "'; rm -rf / #"]
    assert seen["shell"] is False


def test_xdg_open_helper_empty_path_noop(monkeypatch):
    from qdbrowser.plugins import downloads as d
    called = {"n": 0}

    def fake_popen(*_a, **_k):
        called["n"] += 1

    monkeypatch.setattr(d.subprocess, "Popen", fake_popen)
    d._xdg_open("")
    assert called["n"] == 0


def test_plugin_wires_default_profile(window):
    plug = window.plugins._instances["downloads"]
    # Plugin tracks wired profile ids. Default profile should be wired.
    assert len(plug._wired_profiles) >= 1


def test_plugin_commands(window):
    plug = window.plugins._instances["downloads"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("downloads panel" in label.lower() for label in labels)
    assert any("downloads dir" in label.lower() for label in labels)
    assert any("Clear" in label for label in labels)
