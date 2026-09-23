"""Clipboard plugin: tag system clipboard writes with qdistro origin
metadata so the compositor-side ClipboardGate can apply finer-grained
policy.

Phase-1 deliverable for track-04. Scope:

- Observe copy events from the QtWebEngine page (via
  ``QWebEnginePage.selectionChanged`` and the page's clipboard hook).
- On copy, set custom MIME types on the system clipboard:
    * ``x-qdistro-origin-url``       — the page URL at copy-time
    * ``x-qdistro-origin-tab-id``    — qdbrowser's stable webview id
    * ``x-qdistro-fetched-at``       — ISO-8601 timestamp

Phase-3 additions (semantic DOM metadata):

- Inject a ``selectionchange`` JS handler on every page load that
  stashes ``window.__qdistro_clipboard_meta`` with semantic DOM
  context: ``isPasswordField``, ``isCodeBlock``, ``isContentEditable``.
- On copy, read the cached metadata and stamp extra MIME types:
    * ``x-qdistro-is-password-field``  — "true"/"false"
    * ``x-qdistro-is-code-block``      — "true"/"false"
    * ``x-qdistro-is-content-editable``— "true"/"false"
    * ``x-qdistro-context-password-field``  — present only when true
    * ``x-qdistro-context-code-block``      — present only when true
    * ``x-qdistro-context-content-editable``— present only when true

The compositor's ``selection_set`` event sees the new MIME-list and
qdshell's ClipboardGate forwards it as ``mime_types=`` in the journal
line. The content_tags broker rule selector can match these types to
enforce finer-grained policy (e.g., deny password-field content from
crossing silo boundaries).

Note: The plugin does NOT block paste — the compositor handles that
side via ``clear_selection``. The plugin only attaches origin metadata
at copy-time.
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QByteArray, QMimeData
from PyQt6.QtGui import QClipboard, QGuiApplication
from PyQt6.QtWebEngineCore import QWebEngineScript

from qdbrowser.plugin import PageObserver

log = logging.getLogger(__name__)


# MIME types we tag. Keep these prefixed `x-qdistro-` so other clipboard
# consumers (e.g. wl-paste) see them as advisory metadata, not the
# payload type.
MIME_ORIGIN_URL = "x-qdistro-origin-url"
MIME_ORIGIN_TAB_ID = "x-qdistro-origin-tab-id"
MIME_FETCHED_AT = "x-qdistro-fetched-at"

# Phase-3 semantic DOM metadata MIME types.
MIME_IS_PASSWORD_FIELD = "x-qdistro-is-password-field"
MIME_IS_CODE_BLOCK = "x-qdistro-is-code-block"
MIME_IS_CONTENT_EDITABLE = "x-qdistro-is-content-editable"

# Presence-only tags for compositor paths that can initially inspect
# only the selection MIME list. The value-bearing MIME types above are
# kept for consumers that can read clipboard data.
MIME_CONTEXT_PASSWORD_FIELD = "x-qdistro-context-password-field"
MIME_CONTEXT_CODE_BLOCK = "x-qdistro-context-code-block"
MIME_CONTEXT_CONTENT_EDITABLE = "x-qdistro-context-content-editable"
_MIME_METADATA_PREFIX = "x-qdistro-"

# Isolated world so page JS cannot overwrite window.__qdistro_clipboard_meta.
# ApplicationWorld shares the DOM with the page but has a separate JS heap.
_CLIPBOARD_JS_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld

# JS snippet injected into every page to capture selection context.
# The handler fires on `selectionchange` and caches the result in
# `window.__qdistro_clipboard_meta` so it is available synchronously
# when the copy event reaches the Qt clipboard hook.
_SELECTIONCHANGE_JS = (
    "(function() {"
    "  if (window.__qdistro_selectionchange_wired) return;"
    "  window.__qdistro_selectionchange_wired = true;"
    "  document.addEventListener('selectionchange', function() {"
    "    var sel = window.getSelection();"
    "    var ae = document.activeElement;"
    # Selections inside <input>/<textarea> don't appear in
    # window.getSelection() — they use the element's own
    # selectionStart/selectionEnd. Detect this by checking the
    # active element directly.
    "    var inFormControl = !!(ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')"
    "                          && typeof ae.selectionStart === 'number'"
    "                          && ae.selectionStart !== ae.selectionEnd);"
    "    if ((!sel || sel.isCollapsed) && !inFormControl) {"
    "      window.__qdistro_clipboard_meta = null;"
    "      return;"
    "    }"
    "    var anchor = sel && !sel.isCollapsed ? sel.anchorNode : null;"
    "    var parent = anchor && anchor.parentElement ? anchor.parentElement : null;"
    # For password detection: check both the selection parent (for
    # contentEditable password fields) and the active element (for
    # native <input type=password> which doesn't expose selection
    # via window.getSelection).
    "    var isPasswd = !!(parent && parent.tagName === 'INPUT'"
    "                      && parent.type === 'password')"
    "                 || !!(ae && ae.tagName === 'INPUT'"
    "                        && ae.type === 'password');"
    "    window.__qdistro_clipboard_meta = {"
    "      url: location.href,"
    "      isPasswordField: isPasswd,"
    "      isCodeBlock: !!(parent && parent.closest && parent.closest('pre, code')),"
    "      isContentEditable: !!(parent && parent.isContentEditable)"
    "                        || !!(ae && ae.isContentEditable)"
    "    };"
    "  });"
    "})()"
)


class ClipboardOriginPlugin(PageObserver):
    """Attach origin metadata to clipboard writes from web pages.

    Subscribes per-webview to selection changes; when the selection is
    non-empty and the user issues a copy command (which writes to the
    Qt application clipboard), we re-stamp the clipboard with extra
    MIME types pointing back at the source.
    """

    name = "clipboard"
    description = "Tag clipboard writes with qdistro origin metadata."
    capabilities = ["page_observer"]
    version = "0.1"

    def __init__(self):
        super().__init__()
        self._window = None
        self._wired_views: dict = {}  # id(webview) -> webview
        self._last_url_by_view: dict = {}  # id(webview) -> url string
        self._clipboard_conn = None
        # Phase-3: cached DOM metadata per view, populated by the JS
        # callback reading window.__qdistro_clipboard_meta.
        self._dom_meta_by_view: dict = {}  # id(webview) -> dict|None
        # Generation counter per view: incremented on navigation/load
        # so stale async JS callbacks don't overwrite cleared metadata.
        self._meta_gen_by_view: dict = {}  # id(webview) -> int
        self._stamping_clipboard = False

    # -- lifecycle ------------------------------------------------------

    def activate(self, app_controller):
        self._window = app_controller
        clip = QGuiApplication.clipboard()
        if clip is not None:
            # When QtWebEngine writes the clipboard via the Copy action,
            # the clipboard's `dataChanged` fires AFTER the payload
            # lands. We hook there to re-stamp the MimeData with our
            # extra types — keeping the original `text/plain` /
            # `text/html` etc. payload intact.
            self._clipboard_conn = clip.dataChanged.connect(
                self._on_clipboard_changed)

        # Walk any existing tabs so re-activated plugins see them.
        if hasattr(app_controller, "_tabs"):
            try:
                for i in range(app_controller._tabs.count()):
                    w = app_controller._tabs.widget(i)
                    if w is not None:
                        self._wire_view(w)
            except Exception as exc:
                log.debug("clipboard: initial tab walk failed: %s", exc)

    def deactivate(self):
        clip = QGuiApplication.clipboard()
        if clip is not None and self._clipboard_conn is not None:
            try:
                clip.dataChanged.disconnect(self._clipboard_conn)
            except (RuntimeError, TypeError):
                pass
        self._clipboard_conn = None
        self._wired_views.clear()
        self._last_url_by_view.clear()
        self._dom_meta_by_view.clear()
        self._meta_gen_by_view.clear()

    # -- page observer hooks --------------------------------------------

    def on_navigation(self, webview, url):
        vid = id(webview)
        self._last_url_by_view[vid] = url
        # Invalidate stale DOM metadata from the previous page so a
        # copy on the new page doesn't inherit the old page's tags.
        self._dom_meta_by_view.pop(vid, None)
        # Bump generation so any in-flight async JS callback from the
        # previous page is discarded when it arrives.
        self._meta_gen_by_view[vid] = self._meta_gen_by_view.get(vid, 0) + 1
        self._wire_view(webview)

    def on_load_finished(self, webview, ok):
        vid = id(webview)
        # Clear stale metadata on every load (success or failure).
        self._dom_meta_by_view.pop(vid, None)
        self._meta_gen_by_view[vid] = self._meta_gen_by_view.get(vid, 0) + 1
        self._wire_view(webview)
        if ok:
            self._inject_selectionchange_handler(webview)

    # -- internals ------------------------------------------------------

    def _wire_view(self, webview):
        vid = id(webview)
        if vid in self._wired_views:
            return
        self._wired_views[vid] = webview
        # Keep a current-URL fallback for the case where on_navigation
        # never fired (initial tab with no nav yet).
        try:
            current = webview.view.url().toString() if webview.view else ""
            if current:
                self._last_url_by_view[vid] = current
        except Exception:
            pass
        # Connect to the page's selectionChanged signal so we can
        # proactively read the JS-side DOM metadata before the user
        # triggers a copy. The cached value is used synchronously in
        # _on_clipboard_changed.
        try:
            page = webview.view.page() if webview.view else None
            if page is not None:
                page.selectionChanged.connect(
                    lambda _vid=vid, _wv=webview: self._on_selection_changed(_vid, _wv)
                )
        except Exception as exc:
            log.debug("clipboard: selectionChanged connect failed: %s", exc)

    def _on_selection_changed(self, vid, webview):
        """Called when the page's text selection changes (Qt signal).

        Fires an async JS read of ``window.__qdistro_clipboard_meta``
        and caches the result. By the time the user presses Ctrl+C,
        the cache is warm and ``_on_clipboard_changed`` reads it
        synchronously.

        Captures the current generation counter so stale callbacks
        from a previous page are silently dropped.
        """
        gen = self._meta_gen_by_view.get(vid, 0)
        self._read_dom_meta(
            webview,
            lambda meta, _vid=vid, _gen=gen: self._cache_dom_meta(_vid, meta, _gen),
        )

    def _cache_dom_meta(self, vid, meta, gen):
        """Store the JS-reported DOM metadata for ``vid``.

        Only writes if ``gen`` matches the current generation for
        ``vid``, preventing stale async callbacks from overwriting
        metadata that was cleared by a navigation or load event.
        """
        if self._meta_gen_by_view.get(vid, 0) != gen:
            return  # stale callback; discard
        self._dom_meta_by_view[vid] = meta

    def _inject_selectionchange_handler(self, webview):
        """Inject the selectionchange JS into ``webview``'s page.

        The JS sets ``window.__qdistro_clipboard_meta`` whenever the
        user changes the text selection. A guard variable
        (``__qdistro_selectionchange_wired``) prevents duplicate
        listeners if the method is called more than once per page.
        """
        try:
            page = webview.view.page() if webview.view else None
            if page is None:
                return
            page.runJavaScript(_SELECTIONCHANGE_JS, _CLIPBOARD_JS_WORLD)
        except Exception as exc:
            log.debug("clipboard: JS injection failed: %s", exc)

    def _read_dom_meta(self, webview, callback):
        """Asynchronously read ``window.__qdistro_clipboard_meta`` from
        ``webview``'s page and invoke ``callback(meta_dict_or_None)``.
        """
        try:
            page = webview.view.page() if webview.view else None
            if page is None:
                callback(None)
                return
            page.runJavaScript(
                "window.__qdistro_clipboard_meta",
                _CLIPBOARD_JS_WORLD,
                callback,
            )
        except Exception:
            callback(None)

    def _focused_view(self):
        """Best-effort lookup of the webview that just wrote the
        clipboard. We don't get a direct signal from QtWebEngine that
        identifies the source view, so we approximate by the currently-
        active tab — which is virtually always the source for a
        user-driven Copy."""
        win = self._window
        if win is None:
            return None
        getter = getattr(win, "_active_webview", None)
        return getter

    def _on_clipboard_changed(self):
        clip = QGuiApplication.clipboard()
        if clip is None:
            return
        # Ownership check: only stamp clipboard payloads that *we* set.
        # Qt's QClipboard.ownsClipboard() is true only when the current
        # owner is this process. Avoid stamping system-clipboard writes
        # from other apps (e.g. a terminal `xclip`) which would attach
        # bogus "browser tab" metadata.
        try:
            if not clip.ownsClipboard():
                return
        except Exception:
            return

        existing = clip.mimeData()
        if existing is None:
            return
        # Guard only the dataChanged event caused by our own setMimeData.
        # Do not trust pre-existing x-qdistro-* formats on page-provided
        # clipboard data; those are stripped below and replaced with our
        # authoritative metadata.
        if self._stamping_clipboard:
            return

        view = self._focused_view()
        if view is None:
            return
        vid = id(view)
        url = self._last_url_by_view.get(vid, "")
        try:
            stable_id = getattr(view, "_stable_id", None)
        except Exception:
            stable_id = None
        tab_id = str(stable_id) if stable_id is not None else ""
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        # Build a NEW QMimeData carrying both the existing payload AND
        # our metadata. We can't mutate `existing` in place — Qt owns
        # the lifecycle. Copy each format across.
        clone = QMimeData()
        for fmt in existing.formats():
            if fmt.startswith(_MIME_METADATA_PREFIX):
                continue
            try:
                data = existing.data(fmt)
                clone.setData(fmt, data)
            except Exception:
                continue
        clone.setData(MIME_ORIGIN_URL, QByteArray(url.encode("utf-8")))
        clone.setData(MIME_ORIGIN_TAB_ID, QByteArray(tab_id.encode("utf-8")))
        clone.setData(MIME_FETCHED_AT, QByteArray(fetched_at.encode("utf-8")))

        # Phase-3: stamp semantic DOM metadata from the cached JS read.
        meta = self._dom_meta_by_view.get(vid)
        is_password = "false"
        is_code = "false"
        is_editable = "false"
        if isinstance(meta, dict):
            is_password = "true" if meta.get("isPasswordField") else "false"
            is_code = "true" if meta.get("isCodeBlock") else "false"
            is_editable = "true" if meta.get("isContentEditable") else "false"
        clone.setData(MIME_IS_PASSWORD_FIELD,
                      QByteArray(is_password.encode("utf-8")))
        clone.setData(MIME_IS_CODE_BLOCK,
                      QByteArray(is_code.encode("utf-8")))
        clone.setData(MIME_IS_CONTENT_EDITABLE,
                      QByteArray(is_editable.encode("utf-8")))
        if is_password == "true":
            clone.setData(MIME_CONTEXT_PASSWORD_FIELD, QByteArray(b"1"))
        if is_code == "true":
            clone.setData(MIME_CONTEXT_CODE_BLOCK, QByteArray(b"1"))
        if is_editable == "true":
            clone.setData(MIME_CONTEXT_CONTENT_EDITABLE, QByteArray(b"1"))

        # Setting mime data triggers `dataChanged` again; the internal
        # flag above prevents an infinite loop without trusting copied
        # page-provided qdistro MIME names.
        self._stamping_clipboard = True
        try:
            clip.setMimeData(clone, QClipboard.Mode.Clipboard)
        finally:
            self._stamping_clipboard = False

    # TODO(track-04-phase-4): forward the same metadata via D-Bus
    # directly to the compositor so the gate doesn't have to parse
    # custom MIME types — useful for clients that strip unknown MIMEs.
