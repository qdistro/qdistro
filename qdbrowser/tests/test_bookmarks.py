"""Bookmark store + side panel behavior."""

import json


def test_load_empty_when_no_file(tmp_path, monkeypatch):
    import qdbrowser.plugins.bookmarks as bm
    monkeypatch.setattr(bm, "BOOKMARKS_PATH", str(tmp_path / "b.json"))
    assert bm._load() == []


def test_load_broken_file_returns_empty(tmp_path, monkeypatch):
    import qdbrowser.plugins.bookmarks as bm
    p = tmp_path / "b.json"
    p.write_text("not-json-at-all")
    monkeypatch.setattr(bm, "BOOKMARKS_PATH", str(p))
    assert bm._load() == []


def test_load_non_list_returns_empty(tmp_path, monkeypatch):
    import qdbrowser.plugins.bookmarks as bm
    p = tmp_path / "b.json"
    p.write_text('{"not": "a list"}')
    monkeypatch.setattr(bm, "BOOKMARKS_PATH", str(p))
    assert bm._load() == []


def test_save_roundtrip(tmp_path, monkeypatch):
    import qdbrowser.plugins.bookmarks as bm
    p = tmp_path / "b.json"
    monkeypatch.setattr(bm, "BOOKMARKS_PATH", str(p))
    data = [{"title": "DDG", "url": "https://duckduckgo.com"}]
    bm._save(data)
    assert json.loads(p.read_text()) == data


def test_panel_adds_current(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.bookmarks as bm
    monkeypatch.setattr(bm, "BOOKMARKS_PATH",
                        str(tmp_path / "bookmarks.json"))
    panel = bm.BookmarksPanel(window)
    panel._add_current()
    assert len(panel.all()) >= 1


def test_panel_delete_current(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.bookmarks as bm
    monkeypatch.setattr(bm, "BOOKMARKS_PATH",
                        str(tmp_path / "bookmarks.json"))
    panel = bm.BookmarksPanel(window)
    panel._add_current()
    assert len(panel.all()) == 1
    panel._list.setCurrentRow(0)
    panel._delete_current()
    assert len(panel.all()) == 0


def test_panel_filter_excludes_nonmatching(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.bookmarks as bm
    monkeypatch.setattr(bm, "BOOKMARKS_PATH",
                        str(tmp_path / "bookmarks.json"))
    panel = bm.BookmarksPanel(window)
    panel._bookmarks = [
        {"title": "Apple", "url": "https://apple.com"},
        {"title": "Banana", "url": "https://banana.com"},
    ]
    panel._refresh()
    assert panel._list.count() == 2
    panel._filter.setText("app")
    assert panel._list.count() == 1


def test_plugin_get_commands(window):
    plug = window.plugins._instances["bookmarks"]
    cmds = plug.get_commands(window)
    labels = [label for label, _ in cmds]
    assert any("Bookmark this page" in label for label in labels)
    assert any("bookmarks panel" in label.lower() for label in labels)
