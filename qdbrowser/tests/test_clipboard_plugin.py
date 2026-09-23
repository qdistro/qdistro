"""Tests for the clipboard origin plugin (Phase-1 + Phase-3 semantic metadata).

Uses mock objects to avoid requiring a running QtWebEngine page — the
plugin's DOM-metadata path is pure Python + cached dict lookups.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QByteArray, QMimeData
from PyQt6.QtGui import QGuiApplication
from qdbrowser.plugins.clipboard import (
    _CLIPBOARD_JS_WORLD,
    _SELECTIONCHANGE_JS,
    MIME_CONTEXT_CODE_BLOCK,
    MIME_CONTEXT_CONTENT_EDITABLE,
    MIME_CONTEXT_PASSWORD_FIELD,
    MIME_FETCHED_AT,
    MIME_IS_CODE_BLOCK,
    MIME_IS_CONTENT_EDITABLE,
    MIME_IS_PASSWORD_FIELD,
    MIME_ORIGIN_TAB_ID,
    MIME_ORIGIN_URL,
    ClipboardOriginPlugin,
)

# -- helpers ---------------------------------------------------------------

def _make_mock_webview(url="https://example.com", stable_id=42):
    """Build a mock WebView with the minimum attributes the plugin needs."""
    wv = MagicMock()
    wv._stable_id = stable_id
    wv.view.url().toString.return_value = url
    wv.view.page().selectionChanged = MagicMock()
    wv.view.page().selectionChanged.connect = MagicMock()
    wv.view.page().runJavaScript = MagicMock()
    return wv


class _FakeClipboard:
    """Minimal stand-in for QClipboard that tracks setMimeData calls."""

    def __init__(self, *, owns=True, mime_data=None):
        self._owns = owns
        self._mime_data = mime_data or QMimeData()
        self.set_calls: list = []

    def ownsClipboard(self):
        return self._owns

    def mimeData(self):
        return self._mime_data

    def setMimeData(self, data, mode):
        self.set_calls.append((data, mode))

    @property
    def dataChanged(self):
        return MagicMock()


# -- constants / JS --------------------------------------------------------

class TestConstants:
    def test_mime_type_strings(self):
        assert MIME_IS_PASSWORD_FIELD == "x-qdistro-is-password-field"
        assert MIME_IS_CODE_BLOCK == "x-qdistro-is-code-block"
        assert MIME_IS_CONTENT_EDITABLE == "x-qdistro-is-content-editable"
        assert MIME_CONTEXT_PASSWORD_FIELD == "x-qdistro-context-password-field"
        assert MIME_CONTEXT_CODE_BLOCK == "x-qdistro-context-code-block"
        assert MIME_CONTEXT_CONTENT_EDITABLE == "x-qdistro-context-content-editable"

    def test_selectionchange_js_is_iife(self):
        assert _SELECTIONCHANGE_JS.startswith("(function()")
        assert _SELECTIONCHANGE_JS.endswith(")()")

    def test_selectionchange_js_has_guard_variable(self):
        assert "__qdistro_selectionchange_wired" in _SELECTIONCHANGE_JS

    def test_selectionchange_js_sets_meta_property(self):
        assert "__qdistro_clipboard_meta" in _SELECTIONCHANGE_JS

    def test_selectionchange_js_detects_password_field(self):
        assert "password" in _SELECTIONCHANGE_JS

    def test_selectionchange_js_checks_active_element_for_password(self):
        """Password detection must check document.activeElement, not just
        selection parent, because native input selections are invisible
        to window.getSelection()."""
        assert "activeElement" in _SELECTIONCHANGE_JS
        assert "ae.type === 'password'" in _SELECTIONCHANGE_JS

    def test_selectionchange_js_detects_code_block(self):
        assert "pre, code" in _SELECTIONCHANGE_JS

    def test_selectionchange_js_detects_content_editable(self):
        assert "isContentEditable" in _SELECTIONCHANGE_JS

    def test_selectionchange_js_handles_form_control_selection(self):
        """Input/textarea selections use selectionStart/End, not
        window.getSelection(). The JS must account for this."""
        assert "selectionStart" in _SELECTIONCHANGE_JS


# -- plugin construction / lifecycle ---------------------------------------

class TestLifecycle:
    def test_init_sets_empty_dicts(self):
        plug = ClipboardOriginPlugin()
        assert plug._dom_meta_by_view == {}
        assert plug._wired_views == {}
        assert plug._last_url_by_view == {}

    def test_deactivate_clears_dom_meta(self):
        plug = ClipboardOriginPlugin()
        plug._dom_meta_by_view[123] = {"isPasswordField": True}
        plug._wired_views[123] = MagicMock()
        plug._last_url_by_view[123] = "https://example.com"
        plug.deactivate()
        assert plug._dom_meta_by_view == {}
        assert plug._wired_views == {}
        assert plug._last_url_by_view == {}


# -- JS injection ---------------------------------------------------------

class TestJSInjection:
    def test_inject_selectionchange_handler_calls_runJavaScript(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        plug._inject_selectionchange_handler(wv)
        wv.view.page().runJavaScript.assert_called_once_with(
            _SELECTIONCHANGE_JS, _CLIPBOARD_JS_WORLD)

    def test_inject_selectionchange_handler_none_page_no_crash(self):
        plug = ClipboardOriginPlugin()
        wv = MagicMock()
        wv.view.page.return_value = None
        plug._inject_selectionchange_handler(wv)  # should not raise

    def test_inject_selectionchange_handler_no_view_no_crash(self):
        plug = ClipboardOriginPlugin()
        wv = MagicMock()
        wv.view = None
        plug._inject_selectionchange_handler(wv)  # should not raise

    def test_on_load_finished_injects_js_when_ok(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        plug.on_load_finished(wv, True)
        # runJavaScript should have been called with the selectionchange JS
        calls = wv.view.page().runJavaScript.call_args_list
        js_args = [c[0][0] for c in calls]
        assert _SELECTIONCHANGE_JS in js_args

    def test_on_load_finished_skips_js_when_not_ok(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        plug.on_load_finished(wv, False)
        # Should only call connect for selectionChanged, not runJavaScript
        # for the injection.
        for call in wv.view.page().runJavaScript.call_args_list:
            assert call[0][0] != _SELECTIONCHANGE_JS


# -- selection changed / DOM metadata caching ------------------------------

class TestDomMetaCaching:
    def test_cache_dom_meta_stores_dict(self):
        plug = ClipboardOriginPlugin()
        meta = {"isPasswordField": True, "isCodeBlock": False,
                "isContentEditable": False}
        plug._cache_dom_meta(42, meta, 0)
        assert plug._dom_meta_by_view[42] == meta

    def test_cache_dom_meta_stores_none(self):
        plug = ClipboardOriginPlugin()
        plug._cache_dom_meta(42, None, 0)
        assert plug._dom_meta_by_view[42] is None

    def test_cache_dom_meta_rejects_stale_generation(self):
        """A callback with an old generation must not overwrite metadata
        cleared by a navigation event."""
        plug = ClipboardOriginPlugin()
        vid = 42
        # Simulate: gen 0 callback arrives, writes metadata.
        plug._cache_dom_meta(vid, {"isPasswordField": True}, 0)
        assert plug._dom_meta_by_view[vid] == {"isPasswordField": True}
        # Navigation bumps generation to 1 and clears metadata.
        plug._meta_gen_by_view[vid] = 1
        plug._dom_meta_by_view.pop(vid, None)
        # A stale gen-0 callback arrives -- must be discarded.
        plug._cache_dom_meta(vid, {"isPasswordField": True}, 0)
        assert vid not in plug._dom_meta_by_view

    def test_cache_dom_meta_accepts_current_generation(self):
        """A callback with the current generation should write."""
        plug = ClipboardOriginPlugin()
        vid = 42
        plug._meta_gen_by_view[vid] = 3
        plug._cache_dom_meta(vid, {"isCodeBlock": True}, 3)
        assert plug._dom_meta_by_view[vid] == {"isCodeBlock": True}

    def test_on_selection_changed_triggers_js_read(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        plug._on_selection_changed(id(wv), wv)
        # The runJavaScript call should read the meta property.
        wv.view.page().runJavaScript.assert_called_once()
        args = wv.view.page().runJavaScript.call_args[0]
        assert args[0] == "window.__qdistro_clipboard_meta"
        assert args[1] == _CLIPBOARD_JS_WORLD

    def test_inject_and_read_use_isolated_world(self):
        """Page JS must not be able to overwrite the password-field tag:
        inject and read both run in ApplicationWorld, not MainWorld."""
        from PyQt6.QtWebEngineCore import QWebEngineScript
        assert _CLIPBOARD_JS_WORLD == (
            QWebEngineScript.ScriptWorldId.ApplicationWorld)
        assert _CLIPBOARD_JS_WORLD != (
            QWebEngineScript.ScriptWorldId.MainWorld)
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        plug._inject_selectionchange_handler(wv)
        plug._on_selection_changed(id(wv), wv)
        for call in wv.view.page().runJavaScript.call_args_list:
            assert call[0][1] == _CLIPBOARD_JS_WORLD

    def test_read_dom_meta_invokes_callback_on_none_page(self):
        plug = ClipboardOriginPlugin()
        wv = MagicMock()
        wv.view.page.return_value = None
        results = []
        plug._read_dom_meta(wv, lambda m: results.append(m))
        assert results == [None]


# -- clipboard stamping with semantic metadata ----------------------------

class TestClipboardStamping:
    """Test _on_clipboard_changed stamps Phase-3 MIME types."""

    def _make_plugin_with_view(self, *, url="https://x.com", stable_id=7,
                                dom_meta=None):
        """Return (plugin, fake_view) ready for _on_clipboard_changed."""
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview(url=url, stable_id=stable_id)
        vid = id(wv)
        plug._window = MagicMock()
        plug._window._active_webview = wv
        plug._wired_views[vid] = wv
        plug._last_url_by_view[vid] = url
        if dom_meta is not None:
            plug._dom_meta_by_view[vid] = dom_meta
        return plug, wv

    def _run_clipboard_changed(self, plug, existing_mime=None):
        """Simulate a clipboard change and return the stamped QMimeData."""
        if existing_mime is None:
            existing_mime = QMimeData()
            existing_mime.setText("hello")

        fake_clip = _FakeClipboard(owns=True, mime_data=existing_mime)
        with patch.object(QGuiApplication, "clipboard",
                          return_value=fake_clip):
            plug._on_clipboard_changed()

        assert len(fake_clip.set_calls) == 1, (
            "Expected exactly one setMimeData call")
        return fake_clip.set_calls[0][0]

    def test_stamps_origin_url(self):
        plug, _ = self._make_plugin_with_view(url="https://test.dev")
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_ORIGIN_URL)) == b"https://test.dev"

    def test_stamps_tab_id(self):
        plug, _ = self._make_plugin_with_view(stable_id=99)
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_ORIGIN_TAB_ID)) == b"99"

    def test_stamps_fetched_at(self):
        plug, _ = self._make_plugin_with_view()
        stamped = self._run_clipboard_changed(plug)
        ts = bytes(stamped.data(MIME_FETCHED_AT)).decode()
        # Should be ISO-8601-ish (at minimum YYYY-MM-DD).
        assert len(ts) >= 10
        assert ts[4] == "-"

    def test_stamps_password_field_false_by_default(self):
        plug, _ = self._make_plugin_with_view()
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_PASSWORD_FIELD)) == b"false"

    def test_stamps_code_block_false_by_default(self):
        plug, _ = self._make_plugin_with_view()
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_CODE_BLOCK)) == b"false"

    def test_stamps_content_editable_false_by_default(self):
        plug, _ = self._make_plugin_with_view()
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_CONTENT_EDITABLE)) == b"false"

    def test_stamps_password_field_true_from_meta(self):
        meta = {"isPasswordField": True, "isCodeBlock": False,
                "isContentEditable": False}
        plug, _ = self._make_plugin_with_view(dom_meta=meta)
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_PASSWORD_FIELD)) == b"true"
        assert bytes(stamped.data(MIME_IS_CODE_BLOCK)) == b"false"
        assert bytes(stamped.data(MIME_IS_CONTENT_EDITABLE)) == b"false"
        assert stamped.hasFormat(MIME_CONTEXT_PASSWORD_FIELD)
        assert not stamped.hasFormat(MIME_CONTEXT_CODE_BLOCK)
        assert not stamped.hasFormat(MIME_CONTEXT_CONTENT_EDITABLE)

    def test_stamps_code_block_true_from_meta(self):
        meta = {"isPasswordField": False, "isCodeBlock": True,
                "isContentEditable": False}
        plug, _ = self._make_plugin_with_view(dom_meta=meta)
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_PASSWORD_FIELD)) == b"false"
        assert bytes(stamped.data(MIME_IS_CODE_BLOCK)) == b"true"
        assert not stamped.hasFormat(MIME_CONTEXT_PASSWORD_FIELD)
        assert stamped.hasFormat(MIME_CONTEXT_CODE_BLOCK)
        assert not stamped.hasFormat(MIME_CONTEXT_CONTENT_EDITABLE)

    def test_stamps_content_editable_true_from_meta(self):
        meta = {"isPasswordField": False, "isCodeBlock": False,
                "isContentEditable": True}
        plug, _ = self._make_plugin_with_view(dom_meta=meta)
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_CONTENT_EDITABLE)) == b"true"
        assert not stamped.hasFormat(MIME_CONTEXT_PASSWORD_FIELD)
        assert not stamped.hasFormat(MIME_CONTEXT_CODE_BLOCK)
        assert stamped.hasFormat(MIME_CONTEXT_CONTENT_EDITABLE)

    def test_stamps_all_true_from_meta(self):
        meta = {"isPasswordField": True, "isCodeBlock": True,
                "isContentEditable": True}
        plug, _ = self._make_plugin_with_view(dom_meta=meta)
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_PASSWORD_FIELD)) == b"true"
        assert bytes(stamped.data(MIME_IS_CODE_BLOCK)) == b"true"
        assert bytes(stamped.data(MIME_IS_CONTENT_EDITABLE)) == b"true"

    def test_none_meta_yields_all_false(self):
        plug, _ = self._make_plugin_with_view(dom_meta=None)
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_PASSWORD_FIELD)) == b"false"
        assert bytes(stamped.data(MIME_IS_CODE_BLOCK)) == b"false"
        assert bytes(stamped.data(MIME_IS_CONTENT_EDITABLE)) == b"false"
        assert not stamped.hasFormat(MIME_CONTEXT_PASSWORD_FIELD)
        assert not stamped.hasFormat(MIME_CONTEXT_CODE_BLOCK)
        assert not stamped.hasFormat(MIME_CONTEXT_CONTENT_EDITABLE)

    def test_non_dict_meta_yields_all_false(self):
        """If JS returns something unexpected, default to false."""
        plug, wv = self._make_plugin_with_view()
        plug._dom_meta_by_view[id(wv)] = "unexpected string"
        stamped = self._run_clipboard_changed(plug)
        assert bytes(stamped.data(MIME_IS_PASSWORD_FIELD)) == b"false"

    def test_preserves_existing_text_payload(self):
        plug, _ = self._make_plugin_with_view()
        stamped = self._run_clipboard_changed(plug)
        assert stamped.hasFormat("text/plain")

    def test_strips_spoofed_qdistro_metadata_before_stamping(self):
        """Page-provided qdistro MIME names must not survive stamping."""
        plug, _ = self._make_plugin_with_view()
        existing = QMimeData()
        existing.setText("spoofed")
        existing.setData(MIME_ORIGIN_URL, QByteArray(b"https://evil.test"))
        existing.setData(MIME_CONTEXT_PASSWORD_FIELD, QByteArray(b"1"))
        existing.setData(MIME_CONTEXT_CODE_BLOCK, QByteArray(b"1"))
        stamped = self._run_clipboard_changed(plug, existing)
        assert bytes(stamped.data(MIME_ORIGIN_URL)) == b"https://x.com"
        assert not stamped.hasFormat(MIME_CONTEXT_PASSWORD_FIELD)
        assert not stamped.hasFormat(MIME_CONTEXT_CODE_BLOCK)

    def test_internal_reentry_guard_prevents_double_stamp(self):
        plug, _ = self._make_plugin_with_view()
        plug._stamping_clipboard = True
        existing = QMimeData()
        existing.setText("already stamping")

        fake_clip = _FakeClipboard(owns=True, mime_data=existing)
        with patch.object(QGuiApplication, "clipboard",
                          return_value=fake_clip):
            plug._on_clipboard_changed()
        assert len(fake_clip.set_calls) == 0

    def test_not_owns_clipboard_skips(self):
        """Don't stamp if another process owns the clipboard."""
        plug, _ = self._make_plugin_with_view()
        existing = QMimeData()
        existing.setText("from xclip")

        fake_clip = _FakeClipboard(owns=False, mime_data=existing)
        with patch.object(QGuiApplication, "clipboard",
                          return_value=fake_clip):
            plug._on_clipboard_changed()
        assert len(fake_clip.set_calls) == 0

    def test_no_focused_view_skips(self):
        """Don't stamp if no active webview."""
        plug = ClipboardOriginPlugin()
        plug._window = MagicMock()
        plug._window._active_webview = None

        existing = QMimeData()
        existing.setText("some text")
        fake_clip = _FakeClipboard(owns=True, mime_data=existing)
        with patch.object(QGuiApplication, "clipboard",
                          return_value=fake_clip):
            plug._on_clipboard_changed()
        assert len(fake_clip.set_calls) == 0


# -- wire_view connects selectionChanged -----------------------------------

class TestWireView:
    def test_wire_view_connects_selection_changed(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        plug._wire_view(wv)
        wv.view.page().selectionChanged.connect.assert_called_once()

    def test_wire_view_idempotent(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        plug._wire_view(wv)
        plug._wire_view(wv)  # second call is a no-op
        # connect called exactly once
        assert wv.view.page().selectionChanged.connect.call_count == 1

    def test_wire_view_records_url(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview(url="https://docs.python.org")
        plug._wire_view(wv)
        assert plug._last_url_by_view[id(wv)] == "https://docs.python.org"


# -- stale metadata clearing on navigation/load ---------------------------

class TestStaleMetadataClearing:
    def test_on_navigation_clears_cached_dom_meta(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        vid = id(wv)
        plug._dom_meta_by_view[vid] = {"isPasswordField": True}
        plug.on_navigation(wv, "https://newsite.com")
        assert vid not in plug._dom_meta_by_view

    def test_on_navigation_updates_url(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview(url="https://newsite.com")
        plug.on_navigation(wv, "https://newsite.com")
        assert plug._last_url_by_view[id(wv)] == "https://newsite.com"

    def test_on_navigation_updates_url_on_already_wired_view(self):
        """If the view is already wired, on_navigation still updates the URL."""
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview(url="https://old.com")
        plug._wire_view(wv)
        assert plug._last_url_by_view[id(wv)] == "https://old.com"
        plug.on_navigation(wv, "https://new.com")
        assert plug._last_url_by_view[id(wv)] == "https://new.com"

    def test_on_load_finished_clears_cached_dom_meta(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        vid = id(wv)
        plug._dom_meta_by_view[vid] = {"isCodeBlock": True}
        plug.on_load_finished(wv, True)
        assert vid not in plug._dom_meta_by_view

    def test_on_load_finished_clears_meta_even_on_failure(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        vid = id(wv)
        plug._dom_meta_by_view[vid] = {"isContentEditable": True}
        plug.on_load_finished(wv, False)
        assert vid not in plug._dom_meta_by_view

    def test_on_navigation_bumps_generation(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        vid = id(wv)
        assert plug._meta_gen_by_view.get(vid, 0) == 0
        plug.on_navigation(wv, "https://a.com")
        assert plug._meta_gen_by_view[vid] == 1
        plug.on_navigation(wv, "https://b.com")
        assert plug._meta_gen_by_view[vid] == 2

    def test_on_load_finished_bumps_generation(self):
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        vid = id(wv)
        plug.on_load_finished(wv, True)
        gen1 = plug._meta_gen_by_view[vid]
        plug.on_load_finished(wv, True)
        assert plug._meta_gen_by_view[vid] == gen1 + 1

    def test_stale_callback_after_navigation_is_discarded(self):
        """End-to-end: simulate a stale callback arriving after navigation."""
        plug = ClipboardOriginPlugin()
        wv = _make_mock_webview()
        vid = id(wv)
        # Pre-navigation: cache some metadata at gen 0.
        plug._cache_dom_meta(vid, {"isPasswordField": True}, 0)
        assert plug._dom_meta_by_view.get(vid) == {"isPasswordField": True}
        # Navigation clears metadata and bumps gen.
        plug.on_navigation(wv, "https://safe.com")
        assert vid not in plug._dom_meta_by_view
        # Stale callback from gen 0 arrives.
        plug._cache_dom_meta(vid, {"isPasswordField": True}, 0)
        # Must still be empty -- stale callback was rejected.
        assert vid not in plug._dom_meta_by_view
