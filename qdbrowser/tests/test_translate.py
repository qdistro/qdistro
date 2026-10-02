"""Translate plugin: HTTP call shape + extraction flow (mocked)."""

import json
from unittest.mock import MagicMock

import pytest


def test_call_openai_chat_posts_and_parses(monkeypatch):
    from qdbrowser.plugins import translate as t

    captured = {}

    class FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode())
        body = json.dumps({"choices": [{"message": {"content": "BONJOUR"}}]})
        return FakeResp(body.encode())

    monkeypatch.setattr(t.urllib.request, "urlopen", fake_urlopen)
    out = t.call_openai_chat(
        "https://api.openai.com/v1", "k", "gpt-4o-mini",
        "system", "hello", timeout=5.0,
    )
    assert out == "BONJOUR"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert len(captured["body"]["messages"]) == 2
    assert captured["body"]["messages"][1]["content"] == "hello"


def test_call_openai_chat_no_auth_when_key_blank(monkeypatch):
    from qdbrowser.plugins import translate as t

    class FakeResp:
        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "X"}}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.header_items())
        return FakeResp()

    monkeypatch.setattr(t.urllib.request, "urlopen", fake_urlopen)
    t.call_openai_chat("https://x", "", "m", "s", "u", timeout=1.0)
    assert "Authorization" not in captured["headers"]


def test_call_openai_chat_raises_on_http_error(monkeypatch):
    import urllib.error

    from qdbrowser.plugins import translate as t

    def fake_urlopen(*_a, **_k):
        from io import BytesIO
        raise urllib.error.HTTPError(
            "https://x", 401, "Unauthorized",
            hdrs=None, fp=BytesIO(b'{"error":"bad key"}'))

    monkeypatch.setattr(t.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as e:
        t.call_openai_chat("https://x", "k", "m", "s", "u")
    assert "401" in str(e.value)


def test_call_openai_chat_handles_empty_choices(monkeypatch):
    from qdbrowser.plugins import translate as t

    class FakeResp:
        def read(self):
            return b'{"choices":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(t.urllib.request, "urlopen",
                        lambda *_a, **_k: FakeResp())
    assert t.call_openai_chat("https://x", "", "m", "s", "u") == ""


def test_plugin_commands_present(window):
    plug = window.plugins._instances["translate"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("Translate page" in label for label in labels)
    assert any("Translate selection" in label for label in labels)


def test_shortcut_registered(window):
    sc = {a.shortcut().toString(): a for a in window.actions()
          if a.shortcut().toString()}
    assert "Ctrl+Alt+T" in sc


def test_translate_page_with_no_webview_noop(window):
    plug = window.plugins._instances["translate"]
    plug.translate_page(None)  # must not raise


def test_translate_page_invokes_runjs(window):
    plug = window.plugins._instances["translate"]
    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    plug.translate_page(wv)
    wv.view.page().runJavaScript.assert_called()
    js = wv.view.page().runJavaScript.call_args[0][0]
    assert "innerText" in js  # extract script


def test_translate_uses_env_api_key(monkeypatch, fresh_config, window,
                                    wait_for_qt):
    from qdbrowser.plugins import translate as t
    monkeypatch.setenv("QDBROWSER_OPENAI_API_KEY", "env-key-123")
    captured = {}

    def fake_call(api_base, api_key, model, system, user, timeout):
        captured["api_key"] = api_key
        return "OK"

    monkeypatch.setattr(t, "call_openai_chat", fake_call)
    plug = window.plugins._instances["translate"]
    plug._pending_webview = window._active_webview
    plug._kick_off(window._active_webview, "hello world", None)
    wait_for_qt(
        lambda: "api_key" in captured,
        timeout_ms=10000,
        description="translate worker to read the env API key",
    )
    assert captured.get("api_key") == "env-key-123"


def test_translate_truncates_to_max_chars(monkeypatch, fresh_config, window,
                                         wait_for_qt):
    from qdbrowser.config import Config
    from qdbrowser.plugins import translate as t
    Config().set("translate", "max_chars", 10)
    captured = {}

    def fake_call(*args, **kw):
        captured["user"] = args[4]
        return "X"

    monkeypatch.setattr(t, "call_openai_chat", fake_call)
    plug = window.plugins._instances["translate"]
    plug._pending_webview = window._active_webview
    plug._kick_off(window._active_webview, "A" * 1000, None)
    wait_for_qt(
        lambda: "user" in captured,
        timeout_ms=10000,
        description="translate worker to receive truncated text",
    )
    assert len(captured["user"]) == 10


def test_overlay_js_contains_two_columns():
    from qdbrowser.plugins.translate import OVERLAY_JS_TEMPLATE
    assert "flex" in OVERLAY_JS_TEMPLATE
    assert "__qdb_translate_overlay" in OVERLAY_JS_TEMPLATE
    assert "var(--qdb-bg)" in OVERLAY_JS_TEMPLATE


def test_overlay_substitution_safe_against_marker_collision():
    """A page that contains the literal '__TRANS__' must not break the
    second substitution — both args go in via a single replace."""
    from qdbrowser.plugins.translate import _build_overlay_js
    js = _build_overlay_js("original with __TRANS__ marker",
                            "translated text")
    assert "__PAYLOAD__" not in js
    assert "__TRANS__" in js
    assert "translated text" in js


def test_restyle_js_updates_variables_without_removing_overlay():
    from qdbrowser.plugins.translate import _build_restyle_js
    from qdbrowser.theme import overlay_palette

    js = _build_restyle_js("dark")
    p = overlay_palette("dark")
    assert "__COLORS__" not in js
    assert "__qdb_translate_overlay" in js
    assert "setProperty" in js
    assert p["bg"] in js
    assert "removed:true" not in js
    assert "location.reload" not in js
    assert "location.href" not in js
    assert "original" not in js
    assert "translated" not in js


def test_restyle_does_not_call_translation_api(monkeypatch, window):
    from qdbrowser.plugins import translate as t

    monkeypatch.setattr(
        t, "call_openai_chat",
        MagicMock(side_effect=AssertionError("translation API must not run")),
    )
    plug = window.plugins._instances["translate"]
    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    plug.restyle_overlays([wv])
    wv.view.page().runJavaScript.assert_called_once()
    js = wv.view.page().runJavaScript.call_args[0][0]
    assert "__qdb_translate_overlay" in js
    assert "removed:true" not in js
    t.call_openai_chat.assert_not_called()


def test_overlay_palette_rejects_non_hex(monkeypatch):
    from qdbrowser import theme as theme_mod
    from qdbrowser.plugins.translate import _build_overlay_js, _build_restyle_js

    monkeypatch.setattr(theme_mod, "palette_dict", lambda mode="auto": {
        "bg": "red; } * { x:expression(alert(1))",
        "bg_mid": "javascript:alert(1)",
        "fg": "#gggggg",
        "border": "url(https://evil.example/x)",
        "accent": "expression(alert(1))",
    })
    p = theme_mod.overlay_palette("auto")
    assert p["bg"] == "#1e1e1e"
    assert p["fg"] == "#d4d4d4"
    assert p["accent"] == "#3d8fd4"
    inject = _build_overlay_js("hello", "bonjour")
    restyle = _build_restyle_js()
    for js in (inject, restyle):
        assert "javascript:" not in js
        assert "expression(" not in js
        assert "url(" not in js
        assert "#1e1e1e" in js
