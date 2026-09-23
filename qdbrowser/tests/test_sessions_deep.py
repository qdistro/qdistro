"""Sessions plugin: deeper coverage."""

import json
import os


def test_list_sessions_empty(tmp_path, monkeypatch):
    import qdbrowser.plugins.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", str(tmp_path / "absent"))
    assert s.list_sessions() == []


def test_path_sanitises_name(tmp_path, monkeypatch):
    import qdbrowser.plugins.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", str(tmp_path))
    p = s._path("name with spaces / dangerous")
    assert "/" not in os.path.basename(p)
    assert p.endswith(".json")


def test_load_missing_returns_false(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", str(tmp_path / "s"))
    monkeypatch.setattr(s, "_path",
                        lambda n: str(tmp_path / "s" / (n + ".json")))
    assert s.load_session(window, "ghost") is False


def test_list_after_save(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", str(tmp_path / "s"))
    monkeypatch.setattr(s, "_path",
                        lambda n: str(tmp_path / "s" / (n + ".json")))
    s.save_session(window, "one")
    s.save_session(window, "two")
    names = sorted(s.list_sessions())
    assert names == ["one", "two"]


def test_save_creates_valid_json(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.sessions as s
    monkeypatch.setattr(s, "SESSIONS_DIR", str(tmp_path / "s"))
    monkeypatch.setattr(s, "_path",
                        lambda n: str(tmp_path / "s" / (n + ".json")))
    s.save_session(window, "test")
    p = tmp_path / "s" / "test.json"
    data = json.loads(p.read_text())
    assert "tabs" in data
