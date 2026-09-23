"""Reader mode — extracts the article and renders it in a sandboxed
iframe overlay so the original document's scripts/handlers don't
re-execute when we strip and rewrite the body.

The "naive readability" picker is intentionally simple: prefer
``<article>`` / ``<main>`` / ``[role=main]`` / common WP classes, fall
back to the longest-text container. Good enough for blog/news posts;
not a Readability.js replacement.
"""

from qdbrowser.plugin import CommandProvider

READER_JS = r"""
(function(){
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
  // event handlers can fire. Same-origin to the parent so the page
  // can close it via the postMessage handler at the end of this script.
  const css =
    'body{background:#f4ecd8;color:#222;font-family:Georgia,serif;'
    + 'max-width:720px;margin:40px auto;padding:24px;line-height:1.6;'
    + 'font-size:18px}'
    + 'h1{font-family:system-ui,sans-serif;margin-top:0}'
    + 'p{margin:0 0 1em}'
    + 'a{color:#444;text-decoration:underline}';
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
    + 'width:100%;height:100%;background:#f4ecd8;';
  iframe.setAttribute('sandbox', 'allow-same-origin');
  iframe.setAttribute('srcdoc', srcdoc);
  document.documentElement.appendChild(iframe);
  return {ok:true, mode:'on', title: titleSrc};
})()
"""


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
        webview.view.page().runJavaScript(READER_JS)

    def get_commands(self, window):
        return [("Toggle reader mode",
                 lambda: self.toggle(window._active_webview))]
