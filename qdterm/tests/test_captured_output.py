"""Tests for the captured_output plugin.

Layers:
  - Service-level: events route into per-sidebar lists; clear / clear_tab
    behave; export writes a sensible file. No Qt dock needed.
  - Plugin lifecycle: subscribe to a real ``triggers`` service on a
    real MainWindow; tabCloseRequested clears the dropped tab's
    entries; the dock surfaces matches with the expected grouping.
"""

import time

import pytest
import qterminator.config as config_mod
from qterminator.config import Config

pytest.importorskip("pyte")


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


from qterminator.plugins.captured_output import (  # noqa: E402
    CapturedOutputPlugin,
    CapturedOutputService,
)
from qterminator.plugins.triggers import TriggerEvent  # noqa: E402

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class _FakeRule:
    def __init__(self, index, sidebar="Captured"):
        self.index = index
        self.options = {"sidebar": sidebar}


class _FakeTriggers:
    def __init__(self, rules):
        self.rules = rules


class _FakeWindow:
    def __init__(self, rules=()):
        self.triggers = _FakeTriggers(list(rules)) if rules else None


def _evt(rule_index, terminal_id="t1", text="m", sidebar=None,
         groups=None, fired_at=None):
    class _Term:
        def title(self): return "fake"
        def __init__(self, ident): self._ident = ident
    term = _Term(terminal_id)
    # Override id() via the same trick as triggers uses — we just pass
    # the real id of the object; tests don't depend on the literal.
    return TriggerEvent(
        rule_index=rule_index,
        rule_name=f"rule#{rule_index}",
        action="capture",
        terminal=term,
        text=text,
        groups=dict(groups or {"match": text}),
        fired_at=fired_at if fired_at is not None else time.time(),
    )


def test_service_routes_capture_into_named_sidebar():
    win = _FakeWindow(rules=[_FakeRule(0, sidebar="URLs")])
    svc = CapturedOutputService(win)
    svc.handle_capture(_evt(0, text="https://example.com/"))
    assert "URLs" in svc.sidebars()
    entries = svc.entries("URLs")
    assert len(entries) == 1
    assert entries[0]["match"] == "https://example.com/"


def test_service_falls_back_to_default_sidebar_without_triggers_service():
    """If the window has no triggers service, the capture lands in the
    plugin's default sidebar name rather than crashing."""
    win = _FakeWindow(rules=())
    svc = CapturedOutputService(win)
    svc.handle_capture(_evt(7, text="x"))
    assert svc.sidebars() == ["Captured"]


def test_service_ignores_non_capture_events():
    """The service is meant to be a generic ``triggers`` subscriber; it
    must filter to action=capture itself so other actions don't bloat
    the buckets."""
    win = _FakeWindow(rules=[_FakeRule(0, sidebar="URLs")])
    svc = CapturedOutputService(win)
    ev = _evt(0)
    ev.action = "notify"
    svc.handle_capture(ev)
    assert svc.sidebars() == []


def test_service_caps_each_sidebar():
    win = _FakeWindow(rules=[_FakeRule(0, sidebar="S")])
    svc = CapturedOutputService(win, max_per_sidebar=3)
    for i in range(5):
        svc.handle_capture(_evt(0, text=f"m{i}"))
    e = svc.entries("S")
    assert [x["match"] for x in e] == ["m2", "m3", "m4"]


def test_service_clear_specific_sidebar():
    win = _FakeWindow(rules=[_FakeRule(0, "A"), _FakeRule(1, "B")])
    svc = CapturedOutputService(win)
    svc.handle_capture(_evt(0, text="aa"))
    svc.handle_capture(_evt(1, text="bb"))
    svc.clear("A")
    assert svc.sidebars() == ["B"]


def test_service_clear_all():
    win = _FakeWindow(rules=[_FakeRule(0, "A"), _FakeRule(1, "B")])
    svc = CapturedOutputService(win)
    svc.handle_capture(_evt(0))
    svc.handle_capture(_evt(1))
    svc.clear()
    assert svc.sidebars() == []


def test_service_clear_tab_drops_just_that_tabs_entries():
    win = _FakeWindow(rules=[_FakeRule(0, "S")])
    svc = CapturedOutputService(win)
    # Make two separate terminal objects → two distinct id()s.
    ev1 = _evt(0, text="m1"); ev2 = _evt(0, text="m2")
    svc.handle_capture(ev1)
    svc.handle_capture(ev2)
    # Drop one tab's entries.
    target = svc.entries("S")[0]["tab_id"]
    svc.clear_tab(target)
    remaining = svc.entries("S")
    assert len(remaining) == 1
    assert remaining[0]["tab_id"] != target


def test_service_clear_tab_removes_empty_sidebars():
    """When clearing a tab empties a sidebar entirely, the sidebar
    name should disappear so the dock can hide it."""
    win = _FakeWindow(rules=[_FakeRule(0, "S")])
    svc = CapturedOutputService(win)
    ev = _evt(0)
    svc.handle_capture(ev)
    svc.clear_tab(svc.entries("S")[0]["tab_id"])
    assert svc.sidebars() == []


def test_service_listeners_fire_per_entry():
    win = _FakeWindow(rules=[_FakeRule(0, "S")])
    svc = CapturedOutputService(win)
    seen = []
    svc.add_listener(lambda e: seen.append(e["match"]))
    svc.handle_capture(_evt(0, text="hi"))
    assert seen == ["hi"]


def test_service_listener_unsubscribe():
    win = _FakeWindow(rules=[_FakeRule(0, "S")])
    svc = CapturedOutputService(win)
    seen = []
    cb = lambda e: seen.append(e)
    svc.add_listener(cb)
    svc.remove_listener(cb)
    svc.handle_capture(_evt(0))
    assert seen == []


def test_service_listener_exception_doesnt_break_others():
    win = _FakeWindow(rules=[_FakeRule(0, "S")])
    svc = CapturedOutputService(win)
    good = []
    svc.add_listener(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    svc.add_listener(lambda e: good.append(e))
    svc.handle_capture(_evt(0))
    assert len(good) == 1


def test_export_writes_text(tmp_path):
    win = _FakeWindow(rules=[_FakeRule(0, "S")])
    svc = CapturedOutputService(win)
    svc.handle_capture(_evt(0, text="one"))
    svc.handle_capture(_evt(0, text="two"))
    out = tmp_path / "cap.txt"
    n = svc.export_to_text(str(out))
    assert n == 2
    content = out.read_text()
    assert "one" in content and "two" in content


def test_export_filters_to_named_sidebar(tmp_path):
    win = _FakeWindow(rules=[_FakeRule(0, "A"), _FakeRule(1, "B")])
    svc = CapturedOutputService(win)
    svc.handle_capture(_evt(0, text="aaa"))
    svc.handle_capture(_evt(1, text="bbb"))
    out = tmp_path / "only-a.txt"
    n = svc.export_to_text(str(out), sidebar="A")
    assert n == 1
    assert "aaa" in out.read_text()
    assert "bbb" not in out.read_text()


# ---------------------------------------------------------------------------
# Plugin lifecycle on a real MainWindow
# ---------------------------------------------------------------------------

def test_plugin_subscribes_to_triggers_and_collects_matches(qtbot):
    """Real wiring: a triggers rule with action=capture, a real
    terminal that emits matching bytes, and the captured_output
    service that ends up with the match."""
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": r"https?://\S+", "action": "capture", "sidebar": "URLs"},
    ])
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(80)
    term = win.triggers.attached_terminals[0]
    win.shadow_screens._shadows[id(term)][0].feed(
        "see https://example.com/x for more"
    )
    qtbot.wait(20)
    entries = win.captured_output.entries("URLs")
    assert len(entries) == 1
    assert entries[0]["match"].startswith("https://example.com")
    assert entries[0]["tab_id"] == id(term)


def test_plugin_dock_appears_with_grouped_items(qtbot):
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": r"X", "action": "capture", "sidebar": "Marks"},
    ])
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    term = win.triggers.attached_terminals[0]
    win.shadow_screens._shadows[id(term)][0].feed("X X X")
    qtbot.wait(20)
    plugin = win._plugin_manager._instances["captured_output"]
    dock = plugin.show_dock()
    assert dock is not None
    assert dock._tabs.count() == 1
    assert dock._tabs.tabText(0) == "Marks"
    tree = dock._trees["Marks"]
    assert tree.topLevelItemCount() == 1   # one tab-group
    group = tree.topLevelItem(0)
    assert group.childCount() == 3


def test_plugin_tab_close_clears_entries(qtbot):
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": r"hit", "action": "capture", "sidebar": "S"},
    ])
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    win.new_tab()
    qtbot.wait(40)
    terms = win.triggers.attached_terminals
    assert len(terms) == 2
    win.shadow_screens._shadows[id(terms[0])][0].feed("hit")
    win.shadow_screens._shadows[id(terms[1])][0].feed("hit")
    qtbot.wait(20)
    svc = win.captured_output
    assert len(svc.entries("S")) == 2
    closed_tab_id = id(terms[0])
    win._on_tab_close_requested(0)
    qtbot.wait(20)
    remaining = svc.entries("S")
    assert all(e["tab_id"] != closed_tab_id for e in remaining)


def test_plugin_jump_to_tab_changes_index(qtbot):
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": r"hit", "action": "capture", "sidebar": "S"},
    ])
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    win.new_tab()
    qtbot.wait(40)
    terms = win.triggers.attached_terminals
    target = terms[0]  # tab 0
    # Switch focus to tab 1, then jump to tab 0 via the plugin.
    win._tabs.setCurrentIndex(1)
    plugin = win._plugin_manager._instances["captured_output"]
    plugin._jump_to_tab(id(target))
    assert win._tabs.currentIndex() == 0


def test_plugin_disabled_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "captured_output", "enabled", False)
    class FakeWindow:
        pass
    win = FakeWindow()
    p = CapturedOutputPlugin()
    p.activate(win)
    try:
        assert not hasattr(win, "captured_output")
    finally:
        p.deactivate()
