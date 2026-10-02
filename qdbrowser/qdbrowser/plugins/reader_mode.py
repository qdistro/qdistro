"""Reader mode — extracts the article and renders it in a sandboxed
iframe overlay so the original document's scripts/handlers don't
re-execute when we strip and rewrite the body.

The "naive readability" picker is intentionally simple: prefer
``<article>`` / ``<main>`` / ``[role=main]`` / common WP classes, fall
back to the longest-text container. Good enough for blog/news posts;
not a Readability.js replacement.
"""

from __future__ import annotations

import json
import logging

from qdbrowser.plugin import CommandProvider

log = logging.getLogger("qdbrowser.reader_mode")

OVERLAY_ID = "__qdb_reader_overlay"

# Structural inject template. Colors are one JSON object of validated
# #rrggbb literals; restyle updates CSS variables only and must not
# re-run this toggle/extract path.
READER_JS_TEMPLATE = r"""
(function(colors){
  const OVERLAY_ID = '__qdb_reader_overlay';
  const existing = document.getElementById(OVERLAY_ID);
  if (existing) { existing.remove(); return {ok:true, mode:'off'}; }

  function pickRoot(){
    let best = null, score = 0;
    const candidates = Array.from(document.querySelectorAll(
      'article, main, [role=main], .article, .post, .entry-content'
    ));
    candidates.forEach(el => {
      const s = (el.innerText || '').length;
      if (s > score) { score = s; best = el; }
    });
    if (best && score > 400) return best;
    const divs = Array.from(document.querySelectorAll('div, section'));
    divs.forEach(el => {
      const s = (el.innerText || '').length;
      if (s > score) { score = s; best = el; }
    });
    return best || document.body;
  }

  const root = pickRoot();
  const titleSrc = (document.querySelector('h1') && document.querySelector('h1').innerText)
                   || document.title || '';
  // Pull TEXT, not HTML — keeps inline scripts and event handlers out
  // of the overlay even if our srcdoc CSP fails. Paragraph splitting
  // preserves enough structure for readability.
  const rawText = (root.innerText || '').trim();
  const paragraphs = rawText.split(/\n{2,}/);

  function esc(s){
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
  }

  const body = paragraphs.map(p =>
    '<p>' + esc(p).replace(/\n/g, '<br>') + '</p>').join('');
  const titleHtml = '<h1>' + esc(titleSrc) + '</h1>';

  // srcdoc with a strict CSP — no scripts, no remote resources, no
  // event handlers can fire. allow-same-origin lets the parent restyle
  // CSS variables without re-extracting the article.
  const css =
    ':root{--qdb-bg:' + colors.bg + ';--qdb-fg:' + colors.fg
    + ';--qdb-accent:' + colors.accent + ';}'
    + 'body{background:var(--qdb-bg);color:var(--qdb-fg);font-family:Georgia,serif;'
    + 'max-width:720px;margin:40px auto;padding:24px;line-height:1.6;'
    + 'font-size:18px}'
    + 'h1{font-family:system-ui,sans-serif;margin-top:0}'
    + 'p{margin:0 0 1em}'
    + 'a{color:var(--qdb-accent);text-decoration:underline}';
  const csp = "<meta http-equiv='Content-Security-Policy' "
            + "content=\"default-src 'none'; style-src 'unsafe-inline'\">";
  const srcdoc = '<!doctype html><html><head>'
                + csp
                + '<style>' + css + '</style>'
                + '</head><body>' + titleHtml + body + '</body></html>';

  const iframe = document.createElement('iframe');
  iframe.id = OVERLAY_ID;
  iframe.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;border:0;'
    + 'width:100%;height:100%;background:var(--qdb-bg);';
  iframe.style.setProperty('--qdb-bg', colors.bg);
  iframe.style.setProperty('--qdb-fg', colors.fg);
  iframe.style.setProperty('--qdb-accent', colors.accent);
  iframe.setAttribute('sandbox', 'allow-same-origin');
  iframe.setAttribute('srcdoc', srcdoc);
  document.documentElement.appendChild(iframe);
  return {ok:true, mode:'on', title: titleSrc};
})(__COLORS__)
"""

RESTYLE_JS_TEMPLATE = r"""
(function(colors){
  var iframe = document.getElementById('__qdb_reader_overlay');
  if (!iframe) return {ok:true, missing:true};
  iframe.style.setProperty('--qdb-bg', colors.bg);
  iframe.style.setProperty('--qdb-fg', colors.fg);
  iframe.style.setProperty('--qdb-accent', colors.accent);
  iframe.style.background = colors.bg;
  try {
    var doc = iframe.contentDocument;
    if (doc && doc.documentElement) {
      doc.documentElement.style.setProperty('--qdb-bg', colors.bg);
      doc.documentElement.style.setProperty('--qdb-fg', colors.fg);
      doc.documentElement.style.setProperty('--qdb-accent', colors.accent);
    }
  } catch (e) {}
  return {ok:true, restyled:true};
})(__COLORS__)
"""

# Tests assert sandbox/CSP markers against this name.
READER_JS = READER_JS_TEMPLATE


def _build_inject_js(mode: str = "auto") -> str:
    from qdbrowser.theme import overlay_palette

    return READER_JS_TEMPLATE.replace(
        "__COLORS__", json.dumps(overlay_palette(mode)))


def _build_restyle_js(mode: str = "auto") -> str:
    """Update CSS variables on an existing reader overlay.

    Missing overlay is a no-op. Must not toggle, re-extract, or rewrite
    srcdoc (that would drop the already-extracted article text).
    """
    from qdbrowser.theme import overlay_palette

    return RESTYLE_JS_TEMPLATE.replace(
        "__COLORS__", json.dumps(overlay_palette(mode)))


class ReaderModePlugin(CommandProvider):
    name = "reader_mode"
    description = "Toggle a sandboxed-iframe reading view."
    capabilities = ["reader_mode", "command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None

    def activate(self, window):
        self._window = window

    def toggle(self, webview):
        if webview is None:
            return
        webview.view.page().runJavaScript(_build_inject_js())

    def restyle_overlays(self, webviews=None):
        """Restyle existing reader overlays without reload or re-extract."""
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
                log.warning("reader restyle failed: %s", exc)

    def get_commands(self, window):
        return [("Toggle reader mode",
                 lambda: self.toggle(window._active_webview))]
