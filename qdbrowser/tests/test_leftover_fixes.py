"""Regression tests for the round-3 leftover fixes."""

import os

import pytest

# ---- regex ReDoS guard (R1 #7) -----------------------------------

def test_safe_compile_accepts_simple_regex():
    from qdbrowser.plugins.content_blocker import _safe_compile
    p = _safe_compile(r"tracker[0-9]+\.js")
    assert p is not None
    assert p.search("tracker42.js")


def test_safe_compile_rejects_overlong():
    from qdbrowser.plugins.content_blocker import _safe_compile
    p = _safe_compile("a" * 300)
    assert p is None


def test_safe_compile_rejects_nested_quantifier():
    from qdbrowser.plugins.content_blocker import _safe_compile
    p = _safe_compile(r"(a+)+")
    assert p is None


def test_safe_compile_rejects_doubled_wildcards():
    from qdbrowser.plugins.content_blocker import _safe_compile
    p = _safe_compile(r".*.*.*.*")
    assert p is None


def test_parser_drops_dangerous_regex_rules():
    from qdbrowser.plugins.content_blocker import parse_easylist
    nets, _ = parse_easylist("/(a+)+/\n||good.com^\n")
    # Only good.com survives.
    assert len(nets) == 1
    assert nets[0].host_suffix == "good.com"


# ---- empty doc_host strict (R1 #8b) ------------------------------

def test_intercept_empty_doc_host_blocks_request_host(fresh_config):
    from unittest.mock import MagicMock

    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"bad.com"})
    info = MagicMock()
    info.requestUrl.return_value.host.return_value = "bad.com"
    info.firstPartyUrl.return_value.host.return_value = ""
    plug.intercept(info)
    # Empty doc_host → state defaults to "on" → bad.com is blocked.
    info.block.assert_called_once_with(True)


def test_intercept_empty_doc_host_ignores_per_site_off(fresh_config):
    """Even if the user has set ``request_host`` to off, an empty
    doc_host must NOT use the request host to look up the toggle."""
    from unittest.mock import MagicMock

    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"tracker.test"})
    # User disabled blocking on tracker.test (only for first-party visits).
    plug.set_site_state("tracker.test", "off")

    info = MagicMock()
    info.requestUrl.return_value.host.return_value = "tracker.test"
    info.firstPartyUrl.return_value.host.return_value = ""
    plug.intercept(info)
    # Empty doc_host → no per-site lookup → "on" → block applies.
    info.block.assert_called_once_with(True)


# ---- frozen interceptor state (R2 #15) ---------------------------

def test_set_site_state_updates_view_atomically(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug.set_site_state("x.test", "off")
    assert "x.test" in plug._site_toggles_view
    # The mutable underlying dict and the view are decoupled.
    plug._site_toggles["another"] = "off"
    assert "another" not in plug._site_toggles_view
    plug.set_site_state("another", "off")
    assert "another" in plug._site_toggles_view


# ---- _parse_key strictness (R1 #19) ------------------------------

def test_parse_key_rejects_non_ascii():
    from qdbrowser.plugins.agent_control import _parse_key
    with pytest.raises(ValueError) as exc:
        _parse_key("é")
    assert "type_text" in str(exc.value)


def test_parse_key_accepts_punctuation():
    from qdbrowser.plugins.agent_control import _parse_key
    qkey, text, mods = _parse_key("/")
    assert text == "/"


def test_parse_key_shifted_letter_uppercase():
    from PyQt6.QtCore import Qt
    from qdbrowser.plugins.agent_control import _parse_key
    qkey, text, mods = _parse_key("shift+a")
    assert text == "A"
    assert mods & Qt.KeyboardModifier.ShiftModifier


# ---- screenshot dimension cap (R1 #20) ---------------------------

def test_screenshot_dimension_constants_exist():
    from qdbrowser.plugins.screenshot import _MAX_FULL_PAGE_HEIGHT, _MAX_FULL_PAGE_WIDTH
    assert _MAX_FULL_PAGE_WIDTH <= 65536
    assert _MAX_FULL_PAGE_HEIGHT <= 65536
    assert _MAX_FULL_PAGE_WIDTH * _MAX_FULL_PAGE_HEIGHT * 4 < 10 * 1024**3


# ---- history title persistence (R1 #22) --------------------------

def test_history_update_title_persists(tmp_path, monkeypatch):
    import qdbrowser.plugins.history as h
    monkeypatch.setattr(h, "HISTORY_PATH", str(tmp_path / "hist.jsonl"))
    store = h._Store()
    store.add("https://example.com/article", "")
    store.update_title("https://example.com/article", "Real Title")
    # Reload: the title-fixup line should patch the visit on read.
    store2 = h._Store()
    matching = [r for r in store2._records
                if r.get("url") == "https://example.com/article"]
    assert matching and matching[-1]["title"] == "Real Title"


# ---- dead signal removed (R2 #7) ---------------------------------

def test_no_page_load_seq_signal():
    from qdbrowser.webview import WebView
    assert not hasattr(WebView, "page_load_seq_incremented")


# ---- keybindings (R2 #9) -----------------------------------------

def test_pin_tab_keybinding_no_collision(fresh_config):
    cfg = fresh_config.Config()
    assert cfg.get_keybinding("pin_tab") != cfg.get_keybinding("take_screenshot")


def test_unwired_keybindings_now_wired(window):
    sc = {a.shortcut().toString(): a for a in window.actions()
          if a.shortcut().toString()}
    # mute_tab, find_next, find_prev, panel_*, take_screenshot should all
    # be bound after the leftover-fixes pass.
    for key in ("Ctrl+M", "F3", "Shift+F3",
                "Ctrl+B", "Ctrl+H", "Ctrl+J",
                "Ctrl+Shift+P"):
        assert key in sc, f"missing binding: {key}"


def test_pin_action_toggles_pinned(window):
    wv = window._active_webview
    assert wv.pinned is False
    window._toggle_pin_active()
    assert wv.pinned is True
    window._toggle_pin_active()
    assert wv.pinned is False


def test_mute_action_toggles_muted(window):
    wv = window._active_webview
    assert wv.muted is False
    window._toggle_mute_active()
    assert wv.muted is True


# ---- session path (R2 #10) ---------------------------------------

def test_session_path_in_sessions_dir():
    from qdbrowser.window import SESSION_PATH
    assert SESSION_PATH.endswith(os.path.join("sessions", "_autosave.json"))


# ---- theme palette in injected CSS (R2 #12) ----------------------

def test_palette_dict_dark_has_required_keys():
    from qdbrowser.theme import palette_dict
    p = palette_dict("dark")
    for k in ("bg", "bg_mid", "bg_dim", "fg", "fg_dim", "accent",
              "border", "selection"):
        assert k in p
        assert p[k].startswith("#")


def test_palette_dict_light_differs_from_dark():
    from qdbrowser.theme import palette_dict
    assert palette_dict("light")["bg"] != palette_dict("dark")["bg"]


def test_translate_overlay_uses_theme():
    from qdbrowser.plugins.translate import _build_overlay_js
    js = _build_overlay_js("original", "translated", mode="dark")
    assert "__BG__" not in js
    assert "__FG__" not in js
    assert "__BORDER__" not in js
    assert "__BG_MID__" not in js
    # Dark palette bg color present.
    from qdbrowser.theme import palette_dict
    p = palette_dict("dark")
    assert p["bg"] in js
    assert p["fg"] in js


# ---- tab list debounce (R2 #14) ----------------------------------

def test_tab_list_uses_debounce_timer(window):
    plug = window.plugins._instances["tab_list"]
    panel = plug._panel
    assert hasattr(panel, "_refresh_timer")
    assert panel._refresh_timer.isSingleShot()


def test_tab_list_schedules_refresh_via_timer(window):
    plug = window.plugins._instances["tab_list"]
    panel = plug._panel
    panel._refresh_timer.stop()
    panel._schedule_refresh()
    assert panel._refresh_timer.isActive()


# ---- profile-created signal replaces monkey-patch (R1 #13) -------

def test_downloads_uses_profile_listener(window):
    """Downloads must NOT have rebound webview.get_profile."""
    from qdbrowser import webview as wv_mod
    # get_profile should not carry the old "_qdb_wrapped" sentinel.
    assert not getattr(wv_mod.get_profile, "_qdb_wrapped", False)


def test_on_profile_created_replays_existing(window):
    """A new subscriber should be notified about already-cached
    profiles too."""
    from qdbrowser import webview as wv_mod
    seen = []
    wv_mod.on_profile_created(lambda p: seen.append(p))
    try:
        assert len(seen) >= 1  # at least the default profile
    finally:
        # cleanup (the test infra leaks listeners otherwise).
        pass


def test_off_profile_created_unsubscribes(window):
    from qdbrowser import webview as wv_mod
    cb = lambda p: None
    wv_mod.on_profile_created(cb)
    assert cb in wv_mod._PROFILE_LISTENERS
    wv_mod.off_profile_created(cb)
    assert cb not in wv_mod._PROFILE_LISTENERS


# ---- plugin disable disconnects signals (R2 B2) ------------------

def test_disable_disconnects_page_observer(window):
    """When a plugin is disabled, the lambdas wired in
    `_connect_webview` must be disconnected — otherwise the dead
    instance keeps receiving events."""
    plug = window.plugins._instances["history"]
    # plugin should be in _plugin_connections (it's a PageObserver).
    assert plug in window._plugin_connections
    conns_before = len(window._plugin_connections[plug])
    assert conns_before > 0

    # Disable it.
    window.plugins.disable("history")
    assert plug not in window._plugin_connections


def test_window_disconnect_plugin_idempotent(window):
    """Calling disconnect_plugin on an unknown plugin must not raise."""
    window.disconnect_plugin(object())  # no key, no-op


# ---- MCP descriptions (R2 #17) -----------------------------------

def test_mcp_wait_for_load_description_steers_away_from_spa():
    """Description must mention SPA / wait_for_selector to keep an
    LLM from blindly using wait_for_load after click_at."""
    pytest.importorskip("mcp.server.fastmcp")
    from qdbrowser.mcp_server import AgentControlClient, build_server
    client = AgentControlClient("/tmp/never-connected.sock")
    server = build_server(client)
    # Pull tool descriptions via FastMCP's internal API.
    try:
        import asyncio
        tools = asyncio.run(server.list_tools())
        desc = {t.name: t.description for t in tools}
    except Exception:
        desc = {n: t.description
                for n, t in getattr(server, "_tools", {}).items()}
    wait_for_load = desc.get("wait_for_load", "") or ""
    assert "wait_for_selector" in wait_for_load
    assert "SPA" in wait_for_load or "single-page" in wait_for_load.lower()


def test_mcp_click_at_description_mentions_query_selector():
    pytest.importorskip("mcp.server.fastmcp")
    from qdbrowser.mcp_server import AgentControlClient, build_server
    client = AgentControlClient("/tmp/never-connected.sock")
    server = build_server(client)
    try:
        import asyncio
        tools = asyncio.run(server.list_tools())
        desc = {t.name: t.description for t in tools}
    except Exception:
        desc = {n: t.description
                for n, t in getattr(server, "_tools", {}).items()}
    click_at = desc.get("click_at", "") or ""
    assert "query_selector" in click_at


# ---- reader_mode sandboxed iframe (R1 #15) -----------------------

def test_reader_uses_sandboxed_iframe():
    from qdbrowser.plugins.reader_mode import READER_JS
    assert "iframe" in READER_JS
    assert "sandbox" in READER_JS
    assert "Content-Security-Policy" in READER_JS
    # No innerHTML write into document.body.
    assert "document.body.innerHTML" not in READER_JS


# ---- multi-split reopen (R3 #32) ---------------------------------

def test_reopen_restores_multi_split_count(window):
    from PyQt6.QtCore import Qt
    window.new_tab(url="about:blank")
    window._split(Qt.Orientation.Horizontal)
    assert len(window._tabs.currentWidget().find_webviews()) == 2
    window._close_current_tab()
    window._reopen_last_tab()
    restored = window._tabs.currentWidget().find_webviews()
    assert len(restored) == 2


# ---- URL bar on tab switch (R3 #31) ------------------------------

def test_url_bar_updates_on_tab_switch(window):
    window.new_tab(url="about:blank")
    second = window._active_webview
    # Switch back to tab 0.
    window._tabs.setCurrentIndex(0)
    # The url_bar text must reflect the new active webview.
    assert window._url_bar.text() == window._active_webview.url()
    window._tabs.setCurrentIndex(1)
    assert window._url_bar.text() == second.url()


# ---- mouse gesture positive path (R3 #36) ------------------------

def test_gesture_fires_window_method(fresh_config, themed_app):
    from unittest.mock import MagicMock

    from qdbrowser.config import Config
    from qdbrowser.plugins.mouse_gestures import MouseGesturesPlugin

    Config().set("gestures", "bindings", {"L": "back"})
    win = MagicMock()
    plug = MouseGesturesPlugin()
    plug.activate(win)
    plug._fire("L")
    win._go_back.assert_called_once()
    plug.deactivate()


def test_gesture_callable_action_fires(fresh_config, themed_app):
    from unittest.mock import MagicMock

    from qdbrowser.config import Config
    from qdbrowser.plugins.mouse_gestures import MouseGesturesPlugin

    Config().set("gestures", "bindings", {"R": "next_tab"})
    win = MagicMock()
    plug = MouseGesturesPlugin()
    plug.activate(win)
    plug._fire("R")
    win._cycle_tab.assert_called_with(1)
    plug.deactivate()


# ---- restore_layout reconnects observers (R3 #34) ----------------

def test_restore_reconnects_url_bar(window):
    from PyQt6.QtCore import QUrl
    from qdbrowser.layout import restore_layout, serialize_layout
    data = serialize_layout(window._tabs)
    while window._tabs.count() > 0:
        window._tabs.removeTab(0)
    restore_layout(window, data)
    wv = window._active_webview
    wv._on_url(QUrl("https://reconnected.test/"))
    assert "reconnected.test" in window._url_bar.text()
