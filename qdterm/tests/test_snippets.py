"""Tests for the snippets plugin.

Three layers:
  - Pure JSON loading + placeholder expansion (no Qt).
  - Picker dialog filtering (Qt, no terminal).
  - Plugin lifecycle on a real MainWindow.
"""

import json
import os

import pytest
import qterminator.config as config_mod
from qterminator.config import Config


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


from qterminator.plugins.snippets import (  # noqa: E402
    SnippetPickerDialog,
    SnippetsPlugin,
    _snippets_path,
    expand_placeholders,
    load_snippets,
    send_snippet,
)

# ---------------------------------------------------------------------------
# load_snippets
# ---------------------------------------------------------------------------

def _write_snippets(payload):
    path = _snippets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def test_load_snippets_returns_empty_when_file_missing():
    assert load_snippets() == []


def test_load_snippets_parses_basic_entries():
    _write_snippets({"snippets": [
        {"name": "a", "text": "hello\n"},
        {"name": "b", "text": "world\n", "tags": ["x"]},
    ]})
    out = load_snippets()
    assert len(out) == 2
    assert out[0]["name"] == "a"
    assert out[1]["tags"] == ["x"]
    assert out[0]["confirm_send"] is True  # default


def test_load_snippets_honors_confirm_send_false():
    _write_snippets({"snippets": [
        {"name": "trusted", "text": "ls\n", "confirm_send": False},
    ]})
    assert load_snippets()[0]["confirm_send"] is False


def test_load_snippets_drops_malformed_entries():
    _write_snippets({"snippets": [
        {"name": "ok", "text": "x"},
        {"name": "no-text"},          # missing text
        {"text": "no-name"},          # missing name
        "not-a-dict",
    ]})
    out = load_snippets()
    assert len(out) == 1
    assert out[0]["name"] == "ok"


def test_load_snippets_handles_corrupt_json():
    path = _snippets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    assert load_snippets() == []


# ---------------------------------------------------------------------------
# expand_placeholders
# ---------------------------------------------------------------------------

def test_expand_placeholders_substitutes():
    out = expand_placeholders(
        "ssh -A ${1:host}\n", lambda label: "prod-1",
    )
    assert out == "ssh -A prod-1\n"


def test_expand_placeholders_same_index_reused():
    out = expand_placeholders(
        "${1:env} -> ${1:env} done", lambda label: "staging",
    )
    assert out == "staging -> staging done"
    # The fill callback should have been called exactly once for
    # idx=1 — we re-use the cached value on the second occurrence.


def test_expand_placeholders_multiple_distinct_indices():
    answers = iter(["alice", "rev1"])
    out = expand_placeholders(
        "git checkout -b ${1:branch} && git reset ${2:ref}",
        lambda label: next(answers),
    )
    assert out == "git checkout -b alice && git reset rev1"


def test_expand_placeholders_user_cancel_returns_none():
    out = expand_placeholders(
        "${1:host}", lambda label: None,
    )
    assert out is None


def test_expand_placeholders_no_markers_passes_through():
    assert expand_placeholders("just text\n", lambda line: "x") == "just text\n"


# ---------------------------------------------------------------------------
# send_snippet
# ---------------------------------------------------------------------------

def test_send_snippet_writes_to_terminal(monkeypatch):
    class FakeTerm:
        sent = []
        def send_text(self, text):
            self.sent.append(text)
    t = FakeTerm()
    snippet = {"name": "x", "text": "ls\n", "confirm_send": False}
    ok = send_snippet(
        None, t, snippet,
        prompt_fn=lambda _l: "",
        confirm_fn=lambda *_: True,
    )
    assert ok and t.sent == ["ls\n"]


def test_send_snippet_aborted_on_placeholder_cancel():
    class FakeTerm:
        sent = []
        def send_text(self, text):
            self.sent.append(text)
    t = FakeTerm()
    snippet = {"name": "x", "text": "ssh ${1:h}\n", "confirm_send": False}
    ok = send_snippet(
        None, t, snippet,
        prompt_fn=lambda _l: None,  # user cancel
        confirm_fn=lambda *_: True,
    )
    assert not ok
    assert t.sent == []


def test_send_snippet_aborted_on_confirm_decline():
    class FakeTerm:
        sent = []
        def send_text(self, text):
            self.sent.append(text)
    t = FakeTerm()
    snippet = {"name": "x", "text": "rm -rf /\n", "confirm_send": True}
    ok = send_snippet(
        None, t, snippet,
        prompt_fn=lambda _l: "",
        confirm_fn=lambda *_: False,
    )
    assert not ok
    assert t.sent == []


# ---------------------------------------------------------------------------
# Picker dialog
# ---------------------------------------------------------------------------

def test_picker_filters_by_name_and_tags(qtbot):
    snippets = [
        {"name": "ssh prod", "text": "...", "tags": ["ssh"]},
        {"name": "kubectx", "text": "...", "tags": ["k8s"]},
        {"name": "git log", "text": "...", "tags": ["git"]},
    ]
    dlg = SnippetPickerDialog(snippets)
    qtbot.addWidget(dlg)
    assert dlg._list.count() == 3
    dlg._refilter("k8s")
    assert dlg._list.count() == 1
    assert dlg._list.item(0).text() == "kubectx"
    dlg._refilter("ssh")
    assert dlg._list.count() == 1
    dlg._refilter("")
    assert dlg._list.count() == 3


def test_picker_accept_returns_selected(qtbot):
    snippets = [{"name": "x", "text": "ls\n", "tags": []}]
    dlg = SnippetPickerDialog(snippets)
    qtbot.addWidget(dlg)
    dlg._accept_current()
    assert dlg.selected() == snippets[0]


# ---------------------------------------------------------------------------
# Plugin lifecycle on a real MainWindow
# ---------------------------------------------------------------------------

def test_plugin_disabled_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "snippets", "enabled", False)
    class FakeWindow:
        pass
    win = FakeWindow()
    p = SnippetsPlugin()
    p.activate(win)
    try:
        assert not hasattr(win, "snippets")
    finally:
        p.deactivate()


def test_plugin_loads_snippets_from_disk(qtbot):
    _write_snippets({"snippets": [
        {"name": "echo hi", "text": "echo hi\n"},
    ]})
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(80)
    svc = win.snippets
    names = [s["name"] for s in svc.snippets]
    assert "echo hi" in names


def test_plugin_menu_items_match_snippets():
    _write_snippets({"snippets": [
        {"name": "alpha", "text": "a\n"},
        {"name": "beta",  "text": "b\n"},
    ]})
    p = SnippetsPlugin()
    class _W: pass
    p.activate(_W())
    try:
        items = p.get_menu_items(terminal=None)
        labels = [label for label, _cb in items]
        assert "alpha" in labels and "beta" in labels
    finally:
        p.deactivate()


def test_plugin_menu_empty_state_when_no_snippets():
    p = SnippetsPlugin()
    class _W: pass
    p.activate(_W())
    try:
        items = p.get_menu_items(terminal=None)
        assert items == [("(no snippets configured)", None)]
    finally:
        p.deactivate()


def test_plugin_reload_picks_up_added_snippet():
    p = SnippetsPlugin()
    class _W: pass
    p.activate(_W())
    try:
        assert p.snippets == []
        _write_snippets({"snippets": [
            {"name": "later", "text": "later\n"},
        ]})
        p.reload()
        assert [s["name"] for s in p.snippets] == ["later"]
    finally:
        p.deactivate()
