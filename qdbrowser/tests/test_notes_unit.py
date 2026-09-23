"""Notes plugin."""

import json


def test_load_missing(tmp_path, monkeypatch):
    import qdbrowser.plugins.notes as n
    monkeypatch.setattr(n, "NOTES_PATH", str(tmp_path / "missing.json"))
    assert n._load() == []


def test_load_broken(tmp_path, monkeypatch):
    import qdbrowser.plugins.notes as n
    p = tmp_path / "n.json"
    p.write_text("not-json")
    monkeypatch.setattr(n, "NOTES_PATH", str(p))
    assert n._load() == []


def test_save_writes(tmp_path, monkeypatch):
    import qdbrowser.plugins.notes as n
    p = tmp_path / "n.json"
    monkeypatch.setattr(n, "NOTES_PATH", str(p))
    n._save([{"title": "T", "body": "B"}])
    data = json.loads(p.read_text())
    assert data[0]["title"] == "T"


def test_panel_new_note(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.notes as n
    monkeypatch.setattr(n, "NOTES_PATH", str(tmp_path / "n.json"))
    panel = n.NotesPanel(window)
    panel._notes = []
    panel._refresh()
    panel._new_note()
    assert len(panel._notes) == 1
    assert panel._notes[0]["title"] == "Untitled"


def test_panel_new_from_page(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.notes as n
    monkeypatch.setattr(n, "NOTES_PATH", str(tmp_path / "n.json"))
    panel = n.NotesPanel(window)
    panel._notes = []
    panel._new_note_from_page()
    assert len(panel._notes) == 1
    assert panel._notes[0]["url"] is not None


def test_panel_delete(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.notes as n
    monkeypatch.setattr(n, "NOTES_PATH", str(tmp_path / "n.json"))
    panel = n.NotesPanel(window)
    only = {"title": "A", "body": "", "ts": 1, "url": None}
    panel._notes = [only]
    panel._refresh()
    panel._list.setCurrentRow(0)
    panel._select(panel._list.item(0))
    panel._delete_current()
    # `_delete_current` filters by `is not self._current`, so the
    # single note we set up should be gone.
    assert only not in panel._notes


def test_plugin_commands(window):
    plug = window.plugins._instances["notes"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("New note" in label for label in labels)
    assert any("page" in label.lower() for label in labels)
