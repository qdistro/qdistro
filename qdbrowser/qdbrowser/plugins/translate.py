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
(function(original, translated){
  var id = '__qdb_translate_overlay';
  var old = document.getElementById(id);
  if (old) { old.remove(); return {ok:true, removed:true}; }
  var wrap = document.createElement('div');
  wrap.id = id;
  wrap.style.cssText = ''
    + 'position:fixed;inset:0;z-index:2147483647;'
    + 'background:__BG__;color:__FG__;'
    + 'display:flex;font:14px/1.55 system-ui,sans-serif;'
    + 'overflow:hidden;';
  var left = document.createElement('div');
  var right = document.createElement('div');
  for (var col of [left, right]) {
    col.style.cssText = ''
      + 'flex:1 1 50%;padding:24px;overflow:auto;'
      + 'white-space:pre-wrap;word-wrap:break-word;';
  }
  left.style.borderRight = '1px solid __BORDER__';
  left.textContent = original;
  right.textContent = translated;
  var close = document.createElement('button');
  close.textContent = '×';
  close.style.cssText = ''
    + 'position:absolute;top:12px;right:12px;'
    + 'background:__BG_MID__;color:__FG__;border:1px solid __BORDER__;'
    + 'border-radius:4px;width:32px;height:32px;cursor:pointer;'
    + 'font-size:18px;';
  close.onclick = function(){ wrap.remove(); };
  wrap.appendChild(left);
  wrap.appendChild(right);
  wrap.appendChild(close);
  document.documentElement.appendChild(wrap);
  return {ok:true};
})(__ARGS__)
"""


def _build_overlay_js(original: str, translated: str,
                       mode: str = "auto") -> str:
    """Build the overlay JS. Args go in via one substitution so an
    ``__ARGS__`` literal inside the page text can't corrupt anything,
    and the theme palette is interpolated so the overlay matches the
    user's selected theme."""
    from qdbrowser.theme import palette_dict
    p = palette_dict(mode)
    args = f"{_js_str(original)}, {_js_str(translated)}"
    return (OVERLAY_JS_TEMPLATE
            .replace("__ARGS__", args)
            .replace("__BG__", p["bg"])
            .replace("__BG_MID__", p["bg_mid"])
            .replace("__FG__", p["fg"])
            .replace("__BORDER__", p["border"]))


def _js_str(s: str) -> str:
    return json.dumps(s)


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
        js = (
            "(function(text){"
            "var id='__qdb_translate_toast';"
            "var t=document.getElementById(id);"
            "if(!t){t=document.createElement('div');t.id=id;"
            "t.style.cssText='position:fixed;top:12px;right:12px;"
            "z-index:2147483647;background:#1e1e1e;color:#fff;"
            "border:1px solid #555;padding:8px 14px;border-radius:6px;"
            "font:13px system-ui,sans-serif;';"
            "document.documentElement.appendChild(t);}"
            "t.textContent=text;"
            "setTimeout(function(){t.remove();},2200);"
            f"}})({_js_str(msg)})"
        )
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
