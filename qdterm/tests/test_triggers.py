"""Tests for the triggers plugin.

Three layers:
  - Pure rule-loading and template-substitution (no Qt).
  - Service-level tests against a real ShadowScreenRegistry +
    TerminalWidget — feed bytes synthetically and assert actions
    fired with the right context.
  - Lifecycle test against a real MainWindow asserting that
    _connect_terminal is wrapped so new tabs are auto-attached.
"""

from __future__ import annotations

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


@pytest.fixture
def terminal(qtbot):
    from qterminator.terminal import TerminalWidget
    t = TerminalWidget()
    qtbot.addWidget(t)
    t.resize(800, 400)
    t.show()
    qtbot.waitExposed(t)
    yield t


from qterminator.plugins.triggers import (  # noqa: E402
    TriggerEvent,
    TriggersPlugin,
    TriggersService,
    _resolve_template,
    load_rules,
)
from qterminator.shadow_screen import ShadowScreenRegistry  # noqa: E402

# ---------------------------------------------------------------------------
# load_rules
# ---------------------------------------------------------------------------

def test_load_rules_compiles_patterns():
    out = load_rules([
        {"pattern": r"\bERROR\b", "action": "notify"},
        {"pattern": r"(?P<rc>\d+) errors", "action": "notify"},
    ])
    assert len(out) == 2
    assert out[0].pattern.search("ERROR happened")
    m = out[1].pattern.search("5 errors")
    assert m and m.group("rc") == "5"


def test_load_rules_skips_bad_regex():
    out = load_rules([
        {"pattern": "valid", "action": "notify"},
        {"pattern": "(unclosed", "action": "notify"},
    ])
    assert len(out) == 1
    assert out[0].pattern.pattern == "valid"


def test_load_rules_skips_missing_fields():
    out = load_rules([
        {"pattern": "x"},                  # no action
        {"action": "notify"},              # no pattern
        {"pattern": "y", "action": "ring_bell"},
    ])
    assert len(out) == 1
    assert out[0].action == "ring_bell"


def test_load_rules_honors_ignore_case_flag():
    out = load_rules([
        {"pattern": "ERROR", "action": "notify", "ignore_case": True},
    ])
    assert out[0].pattern.search("error")


def test_load_rules_index_preserved():
    out = load_rules([
        {"pattern": "x"},                          # skipped
        {"pattern": "y", "action": "notify"},      # ok, index=1
        {"pattern": "z", "action": "ring_bell"},   # ok, index=2
    ])
    assert [r.index for r in out] == [1, 2]


# ---------------------------------------------------------------------------
# _resolve_template
# ---------------------------------------------------------------------------

def test_template_substitutes_known_keys():
    assert _resolve_template("hi {name}!", {"name": "world"}) == "hi world!"


def test_template_leaves_unknown_keys_visible():
    assert _resolve_template("hi {who}", {}) == "hi {who}"


def test_template_handles_unterminated_brace():
    assert _resolve_template("hi {who", {}) == "hi {who"


# ---------------------------------------------------------------------------
# Service-level: actions fire, cooldown gates, carry buffer bridges
# ---------------------------------------------------------------------------

class _FakeWindow:
    """Minimal stand-in for MainWindow needed by TriggersService.
    Real tabs/terminals not required for the actions we exercise."""

    def __init__(self, registry):
        self.shadow_screens = registry
        self._tabs = None


def _make_service(registry, rules_raw):
    win = _FakeWindow(registry)
    rules = load_rules(rules_raw)
    return TriggersService(win, rules), win


def test_match_fires_subscriber_with_groups(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"errors=(?P<n>\d+)", "action": "ring_bell"},
    ])
    seen: list[TriggerEvent] = []
    svc.subscribe(lambda ev: seen.append(ev))
    svc.attach(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("errors=42 found")
    assert len(seen) == 1
    ev = seen[0]
    assert ev.groups["n"] == "42"
    assert ev.groups["match"] == "errors=42"
    assert ev.rule_index == 0


def test_cooldown_suppresses_rapid_refires(terminal, monkeypatch):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"\bping\b", "action": "ring_bell", "cooldown": 5},
    ])
    fired = []
    svc.subscribe(lambda ev: fired.append(ev))
    svc.attach(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("ping ping ping")
    shadow.feed("ping ping")
    assert len(fired) == 1  # cooldown ate the rest


def test_cooldown_zero_lets_every_match_fire(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"\bping\b", "action": "ring_bell"},
    ])
    fired = []
    svc.subscribe(lambda ev: fired.append(ev))
    svc.attach(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("ping ping ping")
    assert len(fired) == 3


def test_carry_buffer_bridges_chunk_boundaries(terminal):
    """A match split across two chunks (the pattern starts in chunk 1
    and ends in chunk 2) must still fire."""
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"BUILD_OK", "action": "ring_bell"},
    ])
    seen = []
    svc.subscribe(lambda ev: seen.append(ev))
    svc.attach(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("noise BUI")
    shadow.feed("LD_OK noise")
    assert len(seen) == 1


def test_unknown_action_logs_but_doesnt_crash(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"x", "action": "nope_invented"},
    ])
    fired = []
    svc.subscribe(lambda ev: fired.append(ev))
    svc.attach(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("xxx")
    assert len(fired) >= 1  # subscriber still notified


def test_action_exception_does_not_break_subscribers(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"x", "action": "bad_action"},
    ])
    svc.register_action("bad_action", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    sub_seen = []
    svc.subscribe(lambda ev: sub_seen.append(ev))
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("x")
    assert sub_seen, "subscribers must fire even if the action raises"


def test_register_action_can_be_user_action(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"hello", "action": "shout"},
    ])
    calls = []
    svc.register_action("shout", lambda s, r, ev: calls.append(ev.text))
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("hello world")
    assert calls == ["hello"]


def test_register_action_rejects_overriding_builtin(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [])
    # Overriding via register_action is allowed (covers monkey-patching),
    # but unregister_action must refuse to drop a built-in.
    with pytest.raises(ValueError):
        svc.unregister_action("notify")


def test_capture_action_collects_in_named_sidebar(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"https://\S+", "action": "capture", "sidebar": "URLs"},
    ])
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("see https://example.com/x and more")
    items = svc.sidebar("URLs")
    assert len(items) == 1
    assert items[0]["match"].startswith("https://example.com")


def test_capture_action_respects_sidebar_limit(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"\bX\b", "action": "capture",
         "sidebar": "tiny", "sidebar_limit": 3},
    ])
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("X X X X X X")
    assert len(svc.sidebar("tiny")) == 3


def test_send_text_action_writes_to_terminal(terminal, monkeypatch):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"password:", "action": "send_text", "text": "secret\n"},
    ])
    sent = []
    monkeypatch.setattr(terminal, "send_text", lambda t: sent.append(t))
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("Enter password:")
    assert sent == ["secret\n"]


def test_send_text_template_uses_named_groups(terminal, monkeypatch):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"key=(?P<k>\w+)", "action": "send_text",
         "text": "got {k}\n"},
    ])
    sent = []
    monkeypatch.setattr(terminal, "send_text", lambda t: sent.append(t))
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("key=abc123 ")
    assert sent == ["got abc123\n"]


def test_notify_skipped_when_notify_send_missing(terminal, monkeypatch):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"x", "action": "notify", "message": "hi"},
    ])
    monkeypatch.setattr(
        "qterminator.plugins.triggers.shutil.which", lambda _: None,
    )
    # Popen must not be called when which() returns None.
    called = []
    monkeypatch.setattr(
        "qterminator.plugins.triggers.subprocess.Popen",
        lambda *a, **kw: called.append((a, kw)),
    )
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("xx")
    assert called == []


def test_no_rules_skips_shadow_acquire(terminal):
    """With zero configured rules, attach() must not pay any
    shadow-registry overhead — the user shouldn't be charged for the
    plugin just being loaded."""
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [])
    svc.attach(terminal)
    assert reg.refcount(terminal) == 0


def test_detach_releases_shadow_handle(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"x", "action": "ring_bell"},
    ])
    svc.attach(terminal)
    assert reg.refcount(terminal) == 1
    svc.detach(terminal)
    assert reg.refcount(terminal) == 0


def test_detach_all_clears_every_state(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"x", "action": "ring_bell"},
    ])
    svc.attach(terminal)
    svc.detach_all()
    assert svc.attached_terminals == []
    assert reg.refcount(terminal) == 0


def test_subscriber_unsubscribe(terminal):
    reg = ShadowScreenRegistry()
    svc, _ = _make_service(reg, [
        {"pattern": r"x", "action": "ring_bell"},
    ])
    fired = []
    cb = lambda ev: fired.append(ev)
    svc.subscribe(cb)
    svc.unsubscribe(cb)
    svc.attach(terminal)
    reg._shadows[id(terminal)][0].feed("xxx")
    assert fired == []


# ---------------------------------------------------------------------------
# Plugin lifecycle: real MainWindow + _connect_terminal wrapping
# ---------------------------------------------------------------------------

def test_plugin_disabled_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "triggers", "enabled", False)
    class FakeWindow:
        shadow_screens = ShadowScreenRegistry()
    win = FakeWindow()
    p = TriggersPlugin()
    p.activate(win)
    try:
        assert not hasattr(win, "triggers")
    finally:
        p.deactivate()


def test_plugin_activate_loads_rules_from_config():
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": "x", "action": "ring_bell"},
    ])
    class FakeWindow:
        shadow_screens = ShadowScreenRegistry()
        _tabs = None
        _connect_terminal = staticmethod(lambda *a, **k: None)
    win = FakeWindow()
    p = TriggersPlugin()
    p.activate(win)
    try:
        assert hasattr(win, "triggers")
        assert len(win.triggers.rules) == 1
        assert win.triggers.rules[0].action == "ring_bell"
    finally:
        p.deactivate()


def test_plugin_refuses_window_without_shadow_registry():
    class BareWindow:
        pass
    p = TriggersPlugin()
    with pytest.raises(RuntimeError, match="shadow_screens"):
        p.activate(BareWindow())


def test_plugin_wraps_connect_terminal_on_real_window(qtbot, monkeypatch):
    """A real MainWindow has plugins auto-loaded. With a rule in config,
    the initial tab must already be attached when activate finishes,
    and the first PTY chunk should trigger the rule."""
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": "BOOM", "action": "ring_bell"},
    ])
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(100)
    svc = win.triggers
    fired = []
    svc.subscribe(lambda ev: fired.append(ev))
    # The first (and only) tab's terminal should already be attached.
    assert svc.attached_terminals, "service should attach initial tab"
    term = svc.attached_terminals[0]
    shadow = win.shadow_screens._shadows[id(term)][0]
    shadow.feed("BOOM goes")
    assert len(fired) == 1


def test_plugin_attaches_subsequently_created_tab(qtbot):
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": "BANG", "action": "ring_bell"},
    ])
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(100)
    before = len(win.triggers.attached_terminals)
    win.new_tab()
    qtbot.wait(100)
    after = len(win.triggers.attached_terminals)
    assert after == before + 1


def test_set_tab_color_action_updates_tabbar(qtbot):
    """set_tab_color routes through the real QTabBar.setTabTextColor."""
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": "FLAG", "action": "set_tab_color", "color": "#e74c3c"},
    ])
    from PyQt6.QtGui import QColor
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(80)
    term = win.triggers.attached_terminals[0]
    shadow = win.shadow_screens._shadows[id(term)][0]
    shadow.feed("FLAG raised")
    qtbot.wait(20)
    bar = win._tabs.tabBar()
    color = bar.tabTextColor(0)
    assert color == QColor("#e74c3c")


def test_plugin_deactivate_unwraps_connect_terminal(qtbot):
    cfg = Config()
    cfg.set("plugins", "triggers", "rules", [
        {"pattern": "x", "action": "ring_bell"},
    ])
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(80)
    pm = win._plugin_manager
    original_connect = pm._instances["triggers"]._original_connect
    assert original_connect is not None
    pm.disable("triggers")
    # After deactivation, the bound method should be restored.
    assert win._connect_terminal is original_connect
