"""Web panels plugin."""

import json


def test_load_missing(tmp_path, monkeypatch):
    import qdbrowser.plugins.web_panels as wp
    monkeypatch.setattr(wp, "WEB_PANELS_PATH", str(tmp_path / "absent"))
    assert wp._load() == []


def test_load_non_list(tmp_path, monkeypatch):
    import qdbrowser.plugins.web_panels as wp
    p = tmp_path / "wp.json"
    p.write_text('{"foo": 1}')
    monkeypatch.setattr(wp, "WEB_PANELS_PATH", str(p))
    assert wp._load() == []


def test_save_writes(tmp_path, monkeypatch):
    import qdbrowser.plugins.web_panels as wp
    p = tmp_path / "wp.json"
    monkeypatch.setattr(wp, "WEB_PANELS_PATH", str(p))
    wp._save([{"url": "https://x", "name": "x"}])
    data = json.loads(p.read_text())
    assert data[0]["url"] == "https://x"


def test_host_add_bare_domain_gets_https(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.web_panels as wp
    monkeypatch.setattr(wp, "WEB_PANELS_PATH", str(tmp_path / "wp.json"))
    host = wp.WebPanelHost(window)
    host._panels = []
    host._url_edit.setText("example.com")
    host._add()
    assert host._panels[-1]["url"].startswith("https://")


def test_host_add_with_scheme_preserved(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.web_panels as wp
    monkeypatch.setattr(wp, "WEB_PANELS_PATH", str(tmp_path / "wp.json"))
    host = wp.WebPanelHost(window)
    host._panels = []
    host._url_edit.setText("http://insecure.test")
    host._add()
    assert host._panels[-1]["url"] == "http://insecure.test"


def test_host_add_empty_noop(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.web_panels as wp
    monkeypatch.setattr(wp, "WEB_PANELS_PATH", str(tmp_path / "wp.json"))
    host = wp.WebPanelHost(window)
    host._panels = []
    host._url_edit.setText("   ")
    host._add()
    assert host._panels == []
