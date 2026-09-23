"""Workspaces plugin."""

import json


def test_load_missing(tmp_path, monkeypatch):
    import qdbrowser.plugins.workspaces as w
    monkeypatch.setattr(w, "WORKSPACES_PATH", str(tmp_path / "missing.json"))
    assert w._load() == {}


def test_load_broken(tmp_path, monkeypatch):
    import qdbrowser.plugins.workspaces as w
    p = tmp_path / "ws.json"
    p.write_text("nope")
    monkeypatch.setattr(w, "WORKSPACES_PATH", str(p))
    assert w._load() == {}


def test_save_writes_json(tmp_path, monkeypatch):
    import qdbrowser.plugins.workspaces as w
    p = tmp_path / "ws.json"
    monkeypatch.setattr(w, "WORKSPACES_PATH", str(p))
    w._save({"work": {"tabs": []}})
    data = json.loads(p.read_text())
    assert "work" in data


def test_plugin_commands_include_save(window):
    plug = window.plugins._instances["workspaces"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("Workspace" in label for label in labels)
    assert any("save" in label.lower() for label in labels)


def test_switch_unknown_workspace(window):
    plug = window.plugins._instances["workspaces"]
    plug._data = {}
    plug._switch_to("nonexistent")
    # Tabs should still be intact.
    assert window._tabs.count() >= 1
