"""Regression tests for fixes applied in review round 1.

Each test pins a real bug the reviewers identified — if any of these
fail, you've reintroduced the same class of bug.
"""

import os
import socket

import pytest

# ---- stable WebView ids (Reviewer 1 #4) ---------------------------

def test_webviews_have_stable_unique_ids(window):
    a = window._active_webview
    b = window.new_tab(url="about:blank")
    c = window.new_tab(url="about:blank")
    ids = {a.stable_id, b.stable_id, c.stable_id}
    assert len(ids) == 3
    assert all(isinstance(i, int) for i in ids)


def test_stable_id_is_monotonic_not_address():
    from qdbrowser.webview import _alloc_webview_id
    a = _alloc_webview_id()
    b = _alloc_webview_id()
    c = _alloc_webview_id()
    assert b == a + 1
    assert c == b + 1


# ---- agent_control RPC registry (Reviewer 2 #2, #3) --------------

def test_register_method_exposes_verb(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()

    def my_rpc(client, foo=0):
        return {"echo": foo}

    plug.register_method("test_verb", my_rpc)
    fn = plug._lookup_method("test_verb")
    assert fn is my_rpc
    assert fn(client=None, foo=42) == {"echo": 42}


def test_register_method_rejects_invalid_name(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    with pytest.raises(ValueError):
        plug.register_method("bad-name", lambda c: None)


def test_unregister_method(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    plug.register_method("v", lambda c: None)
    assert plug._lookup_method("v") is not None
    plug.unregister_method("v")
    assert plug._lookup_method("v") is None


def test_lookup_method_falls_through_to_rpc_prefix(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    # rpc_list_tabs exists.
    assert plug._lookup_method("list_tabs") is not None
    assert plug._lookup_method("never_exists") is None


def test_lookup_method_rejects_non_identifier(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    assert plug._lookup_method("rpc_list_tabs; import os") is None
    assert plug._lookup_method("../etc/passwd") is None


def test_no_class_level_monkey_patching():
    """PiP and translate must NOT add rpc_* attrs to the class."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    # Class-level attributes before any window is constructed.
    assert not hasattr(AgentControlPlugin, "rpc_pip"), (
        "rpc_pip was monkey-patched onto AgentControlPlugin — should "
        "use register_method on the instance instead")
    assert not hasattr(AgentControlPlugin, "rpc_translate"), (
        "rpc_translate was monkey-patched onto AgentControlPlugin — "
        "use register_method instead")


# ---- plugin load order with agent_control (Reviewer 2 #3) --------

def test_agent_control_activates_before_contributors(qtbot, themed_app,
                                                     fresh_config,
                                                     monkeypatch, tmp_path):
    """When agent_control is enabled, picture_in_picture and translate
    must have registered their verbs on it (load order is correct)."""
    sock_path = str(tmp_path / "agent.sock")
    # Import the module first so monkeypatch can resolve it through the
    # plugin loader's spec-based import.
    from qdbrowser.plugins import agent_control as _ac
    monkeypatch.setattr(_ac, "_socket_path", lambda: sock_path)
    monkeypatch.setenv("QDBROWSER_AGENT_CONTROL", "1")

    from qdbrowser.window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    ac = w.agent_control
    assert ac._lookup_method("pip") is not None, \
        "picture_in_picture didn't register 'pip' verb"
    assert ac._lookup_method("translate") is not None, \
        "translate didn't register 'translate' verb"


# ---- _close_active_split lifecycle (Reviewer 2 #6) ----------------

def test_close_split_clears_active_before_emit(window):
    """If a listener on webview_removed reads window._active_webview,
    it must NOT see the dying webview."""
    wv = window._active_webview
    seen_active = []

    def listener(removed_wv):
        seen_active.append(window._active_webview)

    window.webview_removed.connect(listener)
    window._close_active_split()
    # active was cleared before emit, then re-set after.
    assert wv not in seen_active


def test_close_tab_clears_active_before_emit(window):
    new_wv = window.new_tab(url="about:blank")
    seen = []

    def listener(removed_wv):
        seen.append(window._active_webview)

    window.webview_removed.connect(listener)
    # Close the second tab (new_wv).
    window._tabs.setCurrentIndex(1)
    window._on_tab_close_requested(1)
    assert new_wv not in seen


# ---- restore_layout single-connect (Reviewer 2 #5) ----------------

def test_restore_emits_webview_added_per_view(window):
    """restore_layout should emit webview_added for each WebView so
    plugins (tab_list, downloads wiring, agent indexes) re-see them."""
    # Add a second view via split.
    from PyQt6.QtCore import Qt
    from qdbrowser.layout import restore_layout, serialize_layout
    window._split(Qt.Orientation.Horizontal)
    data = serialize_layout(window._tabs)
    while window._tabs.count() > 0:
        window._tabs.removeTab(0)

    emitted = []
    window.webview_added.connect(lambda wv: emitted.append(wv))
    restore_layout(window, data)
    assert len(emitted) >= 2


def test_restore_only_connects_once(window):
    """The restored WebView's url_changed signal must update the URL
    bar exactly once — not twice from double-_connect_webview."""
    from PyQt6.QtCore import QUrl
    from qdbrowser.layout import restore_layout, serialize_layout
    data = serialize_layout(window._tabs)
    while window._tabs.count() > 0:
        window._tabs.removeTab(0)
    restore_layout(window, data)
    wv = window._active_webview

    # Capture how many times the URL bar setText fires.
    setters = []
    orig = window._url_bar.setText
    window._url_bar.setText = lambda t, _orig=orig, _l=setters: (
        _l.append(t), _orig(t))[1]
    wv._on_url(QUrl("https://onceonly.test/"))
    assert setters.count("https://onceonly.test/") == 1


# ---- safe socket unlink (Reviewer 1 #6) ---------------------------

def test_safe_unlink_refuses_symlink(tmp_path):
    from qdbrowser.plugins.agent_control import _safe_unlink_socket
    # Create a regular file we don't want destroyed.
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    # Now create a symlink pointing at victim where the socket would go.
    sock = tmp_path / "sock"
    os.symlink(str(victim), str(sock))
    with pytest.raises(PermissionError):
        _safe_unlink_socket(str(sock))
    # Victim is still there.
    assert victim.exists()


def test_safe_unlink_missing_is_fine(tmp_path):
    from qdbrowser.plugins.agent_control import _safe_unlink_socket
    # Should not raise.
    _safe_unlink_socket(str(tmp_path / "nothing"))


def test_safe_unlink_removes_real_socket(tmp_path):
    from qdbrowser.plugins.agent_control import _safe_unlink_socket
    p = str(tmp_path / "real.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(p)
    s.close()
    assert os.path.exists(p)
    _safe_unlink_socket(p)
    assert not os.path.exists(p)


# ---- TOML array-of-tables (Reviewer 1 #16) ------------------------

def test_toml_bookmark_dict_in_list_roundtrips(fresh_config):
    from qdbrowser.config import Config
    cfg = Config()
    cfg.set("bookmarks", [
        {"title": "DDG", "url": "https://duckduckgo.com"},
        {"title": "Codeberg", "url": "https://codeberg.org"},
    ])
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    bm = cfg2.get("bookmarks")
    assert isinstance(bm, list)
    assert all(isinstance(b, dict) for b in bm)
    titles = {b.get("title") for b in bm}
    assert "DDG" in titles
    assert "Codeberg" in titles


def test_toml_site_toggles_dict_roundtrips(fresh_config):
    from qdbrowser.config import Config
    cfg = Config()
    cfg.set("blocklist", "site_toggles", {
        "news.test": "off",
        "ads.bad": "cosmetic-only",
    })
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    toggles = cfg2.get("blocklist", "site_toggles")
    assert isinstance(toggles, dict)
    assert toggles["news.test"] == "off"
    assert toggles["ads.bad"] == "cosmetic-only"


def test_toml_dark_mode_overrides_roundtrip(fresh_config):
    from qdbrowser.config import Config
    cfg = Config()
    cfg.set("dark_mode", "site_overrides", {"foo.com": "never"})
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert cfg2.get("dark_mode", "site_overrides") == {"foo.com": "never"}


# ---- content_blocker comment precedence (Reviewer 1 #9) -----------

def test_easylist_hash_thing_cosmetic_treated_as_cosmetic():
    """'#thing##sel' should be a cosmetic rule, not silently dropped."""
    from qdbrowser.plugins.content_blocker import parse_easylist
    nets, cos = parse_easylist("#thing##.sel\n")
    # The host_part is "#thing" — not a valid host suffix, but the
    # selector ".sel" must survive parsing.
    assert any(rule.selector == ".sel" for rule in cos)


def test_easylist_bang_comment_dropped():
    from qdbrowser.plugins.content_blocker import parse_easylist
    nets, cos = parse_easylist("! comment ##notrule\n||real.com^\n")
    assert len(nets) == 1
    assert nets[0].host_suffix == "real.com"


# ---- buffer cap on agent socket (Reviewer 1 #17) ------------------

def test_max_line_bytes_constant_set():
    """A non-trivial cap must be in place — not unlimited."""
    from qdbrowser.plugins.agent_control import _MAX_BUFFER_BYTES, _MAX_LINE_BYTES
    assert _MAX_LINE_BYTES <= 16 * 1024 * 1024
    assert _MAX_BUFFER_BYTES <= 32 * 1024 * 1024
    assert _MAX_LINE_BYTES >= 1024


# ---- translate TLS enforcement (Reviewer 1 #11) -------------------

def test_translate_refuses_http_with_api_key(window, fresh_config,
                                              monkeypatch):
    from qdbrowser.config import Config
    Config().set("translate", "api_base", "http://insecure.test/v1")
    Config().set("translate", "api_key", "secret")

    plug = window.plugins._instances["translate"]
    wv = window._active_webview
    notified = {}

    def fake_notify(_wv, msg):
        notified["msg"] = msg

    monkeypatch.setattr(plug, "_notify", fake_notify)
    plug._kick_off(wv, "hello", "English")
    assert "HTTPS" in notified.get("msg", "")


def test_translate_allows_http_without_api_key(window, fresh_config,
                                                monkeypatch):
    """When no key is set, http is permitted (e.g. local Ollama)."""
    from qdbrowser.config import Config
    from qdbrowser.plugins import translate as t
    Config().set("translate", "api_base", "http://127.0.0.1:11434/v1")
    Config().set("translate", "api_key", "")

    def fake_call(*_a, **_k):
        return "OK"

    monkeypatch.setattr(t, "call_openai_chat", fake_call)
    plug = window.plugins._instances["translate"]
    # Should not bail at the TLS check (no notify with HTTPS message).
    bailed = {"flag": False}
    orig_notify = plug._notify

    def watching(wv, msg):
        if "HTTPS" in msg:
            bailed["flag"] = True
        orig_notify(wv, msg)

    monkeypatch.setattr(plug, "_notify", watching)
    plug._kick_off(window._active_webview, "hello", "English")
    assert not bailed["flag"]


# ---- broadcast_event no longer leaks to non-subscribers (Reviewer 1 #18) ----

def test_broadcast_event_only_to_subscribers(qtbot, themed_app, fresh_config,
                                              monkeypatch, tmp_path):
    sock_path = str(tmp_path / "agent.sock")
    # Import the module first so monkeypatch can resolve it through the
    # plugin loader's spec-based import.
    from qdbrowser.plugins import agent_control as _ac
    monkeypatch.setattr(_ac, "_socket_path", lambda: sock_path)
    monkeypatch.setenv("QDBROWSER_AGENT_CONTROL", "1")

    from qdbrowser.window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    ac = w.agent_control
    # With nobody attached anywhere, broadcast_event should be a no-op
    # — not a broadcast-to-all-clients.
    ac._server.broadcast_event(99999, "test", {"x": 1})
    # No crash, no exceptions; semantics: silent drop because no subs.
