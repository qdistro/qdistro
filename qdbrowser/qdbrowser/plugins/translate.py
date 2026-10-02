"""LLM-driven page translation.

Posts page text (or current selection) to an OpenAI-compatible
``/chat/completions`` endpoint. Works with:

  - openai.com (set ``[translate] api_base = 'https://api.openai.com/v1'``)
  - Local Ollama (``http://127.0.0.1:11434/v1``, model ``llama3``, key 'ollama')
  - Anthropic via litellm proxy
  - Anything else that speaks the chat-completions shape

Trigger:
  - ``Ctrl+Alt+T``
  - Command palette: "Translate page", "Translate selection"
  - Agent RPC: ``translate(tab_id, target_lang?, selection_only=False)``

Output is **overlaid on the page** as a side-by-side dual-pane —
original column on the left, translation on the right. The overlay is
removable by hitting ``Ctrl+Alt+T`` again.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence

from qdbrowser.config import Config
from qdbrowser.plugin import CommandProvider
from qdbrowser.plugins.agent_control import _RpcError

log = logging.getLogger("qdbrowser.translate")


PROMPT = (
    "You are a precise translator. Translate the user message into "
    "{target_lang}. Preserve paragraph breaks. Output ONLY the "
    "translation — no preamble, no quotes, no explanations. If the "
    "text is already in {target_lang}, return it unchanged."
)


# JS that extracts the readable text (similar to reader_mode's pick).
EXTRACT_TEXT_JS = r"""
(function(){
  function pickRoot(){
    var best = null, score = 0;
    ['article','main','[role=main]','.article','.post','.entry-content']
      .forEach(function(sel){
        document.querySelectorAll(sel).forEach(function(el){
          var s = (el.innerText||'').length;
          if (s > score) { score = s; best = el; }
        });
      });
    if (best && score > 300) return best;
    return document.body;
  }
  return (pickRoot().innerText || '').slice(0, 80000);
})()
"""


GET_SELECTION_JS = r"""(window.getSelection() || '').toString()"""


OVERLAY_JS_TEMPLATE = r"""
(function(payload){
  var original = payload.original;
  var translated = payload.translated;
  var colors = payload.colors;
  var id = '__qdb_translate_overlay';
  var old = document.getElementById(id);
  if (old) { old.remove(); return {ok:true, removed:true}; }
  function applyColors(el, c){
    el.style.setProperty('--qdb-bg', c.bg);
    el.style.setProperty('--qdb-fg', c.fg);
    el.style.setProperty('--qdb-bg-mid', c.bg_mid);
    el.style.setProperty('--qdb-border', c.border);
  }
  var wrap = document.createElement('div');
  wrap.id = id;
  applyColors(wrap, colors);
  wrap.style.position = 'fixed';
  wrap.style.inset = '0';
  wrap.style.zIndex = '2147483647';
  wrap.style.background = 'var(--qdb-bg)';
  wrap.style.color = 'var(--qdb-fg)';
  wrap.style.display = 'flex';
  wrap.style.font = '14px/1.55 system-ui,sans-serif';
  wrap.style.overflow = 'hidden';
  var left = document.createElement('div');
  var right = document.createElement('div');
  for (var col of [left, right]) {
    col.style.cssText = ''
      + 'flex:1 1 50%;padding:24px;overflow:auto;'
      + 'white-space:pre-wrap;word-wrap:break-word;';
  }
  left.style.borderRight = '1px solid var(--qdb-border)';
  left.textContent = original;
  right.textContent = translated;
  var close = document.createElement('button');
  close.textContent = '×';
  close.style.cssText = ''
    + 'position:absolute;top:12px;right:12px;'
    + 'background:var(--qdb-bg-mid);color:var(--qdb-fg);'
    + 'border:1px solid var(--qdb-border);'
    + 'border-radius:4px;width:32px;height:32px;cursor:pointer;'
    + 'font-size:18px;';
  close.onclick = function(){ wrap.remove(); };
  wrap.appendChild(left);
  wrap.appendChild(right);
  wrap.appendChild(close);
  document.documentElement.appendChild(wrap);
  return {ok:true};
})(__PAYLOAD__)
"""


RESTYLE_JS_TEMPLATE = r"""
(function(colors){
  function applyColors(el, c){
    if (!el) return;
    el.style.setProperty('--qdb-bg', c.bg);
    el.style.setProperty('--qdb-fg', c.fg);
    el.style.setProperty('--qdb-bg-mid', c.bg_mid);
    el.style.setProperty('--qdb-border', c.border);
  }
  var wrap = document.getElementById('__qdb_translate_overlay');
  applyColors(wrap, colors);
  applyColors(document.getElementById('__qdb_translate_toast'), colors);
  if (!wrap) return {ok:true, missing:true};
  return {ok:true, restyled:true};
})(__COLORS__)
"""


TOAST_JS_TEMPLATE = r"""
(function(payload){
  var text = payload.text;
  var colors = payload.colors;
  var id = '__qdb_translate_toast';
  var t = document.getElementById(id);
  if (!t) {
    t = document.createElement('div');
    t.id = id;
    t.style.cssText = ''
      + 'position:fixed;top:12px;right:12px;z-index:2147483647;'
      + 'background:var(--qdb-bg);color:var(--qdb-fg);'
      + 'border:1px solid var(--qdb-border);padding:8px 14px;'
      + 'border-radius:6px;font-family:system-ui,sans-serif;';
    document.documentElement.appendChild(t);
  }
  t.style.setProperty('--qdb-bg', colors.bg);
  t.style.setProperty('--qdb-fg', colors.fg);
  t.style.setProperty('--qdb-bg-mid', colors.bg_mid);
  t.style.setProperty('--qdb-border', colors.border);
  t.textContent = text;
  setTimeout(function(){ if (t.parentNode) t.remove(); }, 2200);
  return {ok:true};
})(__PAYLOAD__)
"""


def _build_overlay_js(original: str, translated: str,
                       mode: str = "auto") -> str:
    """Build the overlay JS. Payload goes in via one JSON substitution
    so page text cannot corrupt the script, and colors are validated
    hex literals assigned to CSS variables."""
    from qdbrowser.theme import overlay_palette
    payload = {
        "original": original,
        "translated": translated,
        "colors": overlay_palette(mode),
    }
    return OVERLAY_JS_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload))


def _build_restyle_js(mode: str = "auto") -> str:
    """Update CSS variables on an existing overlay and toast.

    Missing nodes are a no-op; this must not recreate either widget or
    touch overlay/toast text.
    """
    from qdbrowser.theme import overlay_palette
    return RESTYLE_JS_TEMPLATE.replace(
        "__COLORS__", json.dumps(overlay_palette(mode)))


def _build_toast_js(msg: str, mode: str = "auto") -> str:
    """Build the inline toast JS with validated overlay palette colors."""
    from qdbrowser.theme import overlay_palette
    payload = {
        "text": msg,
        "colors": overlay_palette(mode),
    }
    return TOAST_JS_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload))


def call_openai_chat(api_base: str, api_key: str, model: str,
                     system: str, user: str,
                     timeout: float = 30.0) -> str:
    """POST to /chat/completions and return the message content.

    Pure-stdlib (urllib) so no extra deps. Raises RuntimeError on
    network/HTTP failure; returns empty string when there's no choice.
    """
    url = api_base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network: {e.reason}") from e
    data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content", "") or ""


class _WorkerSignal(QObject):
    # (target_webview, original, translation, error)
    done = pyqtSignal(object, str, str, str)


class TranslatePlugin(CommandProvider):
    name = "translate"
    description = "Translate page or selection via an OpenAI-compatible API."
    capabilities = ["command_provider", "translate"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._signal = _WorkerSignal()
        self._signal.done.connect(self._on_done)

    def activate(self, window):
        self._window = window

        # Ctrl+Alt+T
        act = QAction(window)
        act.setShortcut(QKeySequence("Ctrl+Alt+T"))
        act.triggered.connect(
            lambda: self.translate_page(window._active_webview))
        window.addAction(act)

        # Defer agent RPC registration until agent_control is up.
        window.register_agent_methods_later(self)

    def contribute_agent_methods(self, agent_control):
        agent_control.register_method("translate", self._rpc_translate)

    def deactivate(self):
        win = self._window
        if win is not None:
            ac = getattr(win, "agent_control", None)
            if ac is not None:
                ac.unregister_method("translate")

    # -- public API ----------------------------------------------------

    def translate_page(self, webview, target_lang: str | None = None):
        if webview is None:
            return
        self._extract(webview, EXTRACT_TEXT_JS, target_lang)

    def translate_selection(self, webview, target_lang: str | None = None):
        if webview is None:
            return
        self._extract(webview, GET_SELECTION_JS, target_lang)

    # -- internals -----------------------------------------------------

    def _extract(self, webview, js: str, target_lang: str | None):
        def on_text(text):
            if not text:
                self._notify(webview, "(no text to translate)")
                return
            self._kick_off(webview, text, target_lang)
        webview.view.page().runJavaScript(js, on_text)

    def _kick_off(self, webview, text: str, target_lang: str | None):
        cfg = Config()
        target_lang = target_lang or cfg.get(
            "translate", "target_lang", default="English") or "English"
        api_base = cfg.get("translate", "api_base",
                           default="https://api.openai.com/v1")
        api_key = (os.environ.get("QDBROWSER_OPENAI_API_KEY")
                   or cfg.get("translate", "api_key", default="") or "")
        model = cfg.get("translate", "model", default="gpt-4o-mini")
        max_chars = int(cfg.get("translate", "max_chars", default=8000) or 8000)
        timeout = float(cfg.get("translate", "timeout", default=30.0) or 30.0)

        # Enforce TLS when an API key is present — otherwise the bearer
        # token leaks over plaintext HTTP.
        parsed = urlparse(api_base)
        if not parsed.scheme:
            self._notify(webview,
                         "Translate misconfigured: api_base has no scheme")
            return
        if api_key and parsed.scheme != "https":
            self._notify(
                webview,
                "Translate refused: api_key set but api_base is not HTTPS")
            return

        snippet = text[:max_chars]
        target = webview  # capture target in worker closure

        def _worker():
            try:
                translation = call_openai_chat(
                    api_base, api_key, model,
                    PROMPT.format(target_lang=target_lang),
                    snippet,
                    timeout=timeout,
                )
                self._signal.done.emit(target, snippet, translation, "")
            except Exception as exc:  # noqa: BLE001
                # Generic message to the page; full detail goes to logs
                # only — avoids leaking provider error bodies into the
                # DOM where the host page could read them.
                log.warning("translate worker failed: %s", exc)
                self._signal.done.emit(
                    target, snippet, "",
                    "translate failed (see qdbrowser logs)")

        self._notify(webview, "Translating…")
        threading.Thread(target=_worker, daemon=True).start()

    def restyle_overlays(self, webviews=None):
        """Restyle existing translate overlays without reload or re-translate."""
        if webviews is None:
            win = self._window
            if win is None:
                return
            iter_views = getattr(win, "iter_webviews", None)
            webviews = list(iter_views()) if callable(iter_views) else []
            active = getattr(win, "_active_webview", None)
            if active is not None and active not in webviews:
                webviews.append(active)
        js = _build_restyle_js()
        for wv in webviews:
            if wv is None:
                continue
            try:
                wv.view.page().runJavaScript(js)
            except Exception as exc:  # noqa: BLE001
                log.warning("overlay restyle failed: %s", exc)

    def _on_done(self, wv, original: str, translation: str, error: str):
        if wv is None:
            return
        if error:
            self._notify(wv, error)
            return
        js = _build_overlay_js(original, translation)
        try:
            wv.view.page().runJavaScript(js)
        except Exception as exc:
            log.warning("overlay inject failed: %s", exc)

    def _notify(self, webview, msg: str):
        """Inline toast — top-right corner, fades after 2s."""
        js = _build_toast_js(msg)
        try:
            webview.view.page().runJavaScript(js)
        except Exception:
            pass

    # -- agent RPC -----------------------------------------------------

    def _rpc_translate(self, client, tab_id: int,
                       target_lang: str | None = None,
                       selection_only: bool = False):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        ac = self._window.agent_control
        wv = ac._get_webview(tab_id)
        if selection_only:
            self.translate_selection(wv, target_lang=target_lang)
        else:
            self.translate_page(wv, target_lang=target_lang)
        return {"ok": True}

    # -- commands ------------------------------------------------------

    def get_commands(self, window):
        return [
            ("Translate page",
             lambda: self.translate_page(window._active_webview)),
            ("Translate selection",
             lambda: self.translate_selection(window._active_webview)),
        ]
