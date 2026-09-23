"""History store + page-observer behavior."""

import json
import os


def test_store_load_missing_file(tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "missing"))
    store = h._Store()
    assert store.all() == []


def test_store_add_persists(tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "hist.jsonl"))
    store = h._Store()
    store.add("https://example.com", "Example")
    assert any("example.com" in r["url"] for r in store.all())
    # File written.
    with open(tmp_path / "hist.jsonl") as f:
        first = json.loads(f.readline())
    assert first["url"] == "https://example.com"


def test_store_skips_aboutblank(tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    store = h._Store()
    store.add("about:blank", "blank")
    store.add("data:text/html,foo", "data")
    assert store.all() == []


def test_store_caps_memory(tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    monkeypatch.setattr(h, "MAX_HISTORY", 5)
    store = h._Store()
    for i in range(10):
        store.add(f"https://example.com/{i}", f"page-{i}")
    assert len(store.all()) <= 5


def test_store_load_skips_broken_lines(tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    p = tmp_path / "h.jsonl"
    p.write_text(
        '{"url": "https://a.com", "title": "A", "ts": 1}\n'
        'NOT_JSON\n'
        '{"url": "https://b.com", "title": "B", "ts": 2}\n')
    monkeypatch.setattr(h, "HISTORY_PATH", str(p))
    store = h._Store()
    urls = [r["url"] for r in store.all()]
    assert "https://a.com" in urls
    assert "https://b.com" in urls


def test_panel_filter(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    store = h._Store()
    store.add("https://apple.com", "Apple")
    store.add("https://banana.com", "Banana")
    panel = h.HistoryPanel(window, store)
    panel._refresh()
    assert panel._list.count() == 2
    panel._filter.setText("apple")
    assert panel._list.count() == 1


def test_plugin_on_title_changed_updates_last(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    plug = h.HistoryPlugin()
    plug.activate(window)

    # Insert a record with no title, then notify a title change.
    plug._store._records.append({"url": "https://x.test", "title": "",
                                  "ts": 0})

    class FakeWv:
        def url(self):
            return "https://x.test"

    plug.on_title_changed(FakeWv(), "New Title")
    assert plug._store._records[-1]["title"] == "New Title"


def test_plugin_on_navigation_appends(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    plug = h.HistoryPlugin()
    plug.activate(window)
    plug.build_panel(window)

    class FakeWv:
        def title(self):
            return "Title"

    plug.on_navigation(FakeWv(), "https://navtest.example")
    urls = [r["url"] for r in plug._store.all()]
    assert "https://navtest.example" in urls


def test_history_plugin_marked_persistent():
    import qdbrowser.plugins.history as h
    assert h.HistoryPlugin.persistent is True


def test_page_observer_default_not_persistent():
    from qdbrowser.plugin import PageObserver
    assert PageObserver.persistent is False


def test_plugin_on_navigation_skips_off_the_record(window, tmp_path,
                                                   monkeypatch):
    # Defence in depth: even if the observer is reached for a private
    # webview, nothing is persisted.
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    plug = h.HistoryPlugin()
    plug.activate(window)

    class PrivateWv:
        is_off_the_record = True

        def title(self):
            return "Secret"

    plug.on_navigation(PrivateWv(), "https://private.example")
    assert plug._store.all() == []
    assert not os.path.exists(tmp_path / "h.jsonl")


def test_plugin_on_title_changed_skips_off_the_record(window, tmp_path,
                                                      monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    plug = h.HistoryPlugin()
    plug.activate(window)
    plug._store._records.append({"url": "https://x.test", "title": "",
                                 "ts": 0})

    class PrivateWv:
        is_off_the_record = True

        def url(self):
            return "https://x.test"

    plug.on_title_changed(PrivateWv(), "Should Not Persist")
    # The pre-existing record's title is untouched.
    assert plug._store._records[-1]["title"] == ""


def test_private_tab_does_not_wire_history_observer(window, tmp_path,
                                                    monkeypatch):
    """A private (OTR) tab must not append to the history log on
    navigation, while a normal tab still does."""
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "h.jsonl"))

    plug = h.HistoryPlugin()
    plug.persistent = True
    plug._store = h._Store()

    # Drive _connect_webview through this single persistent observer.
    monkeypatch.setattr(window.plugins, "get_page_observers",
                        lambda: [plug])
    monkeypatch.setattr(window.plugins, "get_url_interceptors", lambda: [])

    private = window.new_tab(url="about:blank", profile_name="private")
    normal = window.new_tab(url="about:blank", profile_name="default")
    assert private.is_off_the_record is True
    assert normal.is_off_the_record is False

    # The persistent history observer must be wired to the normal tab but
    # NOT to the private one.
    conns = window._plugin_connections.get(plug, [])
    assert conns, "history observer should be wired to the normal tab"

    # Simulate navigations on both tabs; only the normal tab is recorded.
    plug.on_navigation(private, "https://secret.example")
    plug.on_navigation(normal, "https://public.example")
    urls = [r["url"] for r in plug._store.all()]
    assert "https://public.example" in urls
    assert "https://secret.example" not in urls
