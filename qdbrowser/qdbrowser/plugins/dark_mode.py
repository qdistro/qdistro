"""Force dark mode for every page (Dark-Reader-lite).

Strategy: inject a CSS filter at the html root that inverts the page
and rotates the hue back, then un-invert images and videos. Cheap,
imperfect, fast — good enough for most reading.

States (per host):
  - ``auto``       — apply when desktop theme is dark (default)
  - ``always``     — always apply
  - ``never``      — never apply
  - ``contrast``   — high-contrast variant (no hue rotate)

Activated on ``load_finished`` via ``PageObserver``. The CSS lives in
``window.__qdb_force_dark`` so SPA-route changes that wipe `<head>` can
be patched by re-running on each navigation event.
"""

from __future__ import annotations

from urllib.parse import urlparse

from qdbrowser.config import Config
from qdbrowser.plugin import CommandProvider, PageObserver

# Base invert+hue-rotate filter. Skips images, videos, iframes.
CSS_DEFAULT = """
:root { background-color: #181818 !important; }
html { filter: invert(0.92) hue-rotate(180deg) !important;
       background: #181818 !important; }
img, picture, video, iframe, [style*="background-image"],
canvas, svg image, [data-no-darkmode] {
    filter: invert(1) hue-rotate(180deg) !important;
}
"""

CSS_CONTRAST = """
:root { background-color: #000 !important; color: #fff !important; }
html { background: #000 !important; color: #fff !important; }
body, body * { background-color: transparent !important; color: #fff !important; }
a, a * { color: #5ec8ff !important; }
img, picture, video, iframe { filter: brightness(.85) !important; }
"""

INJECT_JS = """
(function(mode){
  var id = '__qdb_force_dark_style';
  var el = document.getElementById(id);
  if (mode === 'off') {
    if (el) el.remove();
    document.documentElement.dataset.qdbDarkmode = 'off';
    return {ok:true, mode:'off'};
  }
  var css = (mode === 'contrast') ? __CSS_CONTRAST__ : __CSS_DEFAULT__;
  if (!el) {
    el = document.createElement('style');
    el.id = id;
    document.documentElement.appendChild(el);
  }
  el.textContent = css;
  document.documentElement.dataset.qdbDarkmode = mode;
  return {ok:true, mode:mode};
})(__MODE__)
"""


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


class DarkModePlugin(PageObserver, CommandProvider):
    name = "dark_mode"
    description = "Force-dark every page (Dark-Reader-lite)."
    capabilities = ["page_observer", "command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._desktop_dark = False
        self._global_default = "auto"   # auto / always / never / contrast
        self._site_overrides: dict = {}

    def activate(self, window):
        self._window = window
        cfg = Config()
        self._global_default = cfg.get(
            "dark_mode", "default", default="auto") or "auto"
        self._site_overrides = (
            cfg.get("dark_mode", "site_overrides", default={}) or {})

        # Resolve desktop theme — qdbrowser stashes it on the window.
        resolved = getattr(window, "_resolved_theme", None)
        if resolved is None:
            from qdbrowser.theme import detect_system_theme
            resolved = detect_system_theme()
        self._desktop_dark = (resolved == "dark")

    def _effective_mode(self, host: str) -> str:
        """Resolve the mode that should apply to ``host`` right now."""
        override = self._site_overrides.get(host)
        if override is None:
            for k, v in self._site_overrides.items():
                if host == k or host.endswith("." + k):
                    override = v
                    break
        mode = override or self._global_default
        if mode == "auto":
            return "always" if self._desktop_dark else "off"
        if mode == "never":
            return "off"
        if mode in ("always", "contrast"):
            return mode
        return "off"

    def on_load_finished(self, webview, ok: bool):
        if not ok:
            return
        self.apply(webview)

    def on_navigation(self, webview, url: str):
        # SPA route changes don't always fire loadFinished — reapply
        # cheaply on URL changes too.
        self.apply(webview)

    def apply(self, webview):
        if webview is None:
            return
        host = _url_host(webview.url())
        mode = self._effective_mode(host)
        js = (INJECT_JS
              .replace("__CSS_DEFAULT__", _js_str(CSS_DEFAULT))
              .replace("__CSS_CONTRAST__", _js_str(CSS_CONTRAST))
              .replace("__MODE__", _js_str(mode)))
        try:
            webview.view.page().runJavaScript(js)
        except Exception:
            pass

    # -- commands ------------------------------------------------------

    def get_commands(self, window):
        wv = window._active_webview if window else None
        host = _url_host(wv.url()) if wv else ""
        out = [
            (f"Force dark: default mode = {self._global_default}",
             self._cycle_global),
        ]
        if host:
            current = self._site_overrides.get(host) or "—"
            for state in ("auto", "always", "never", "contrast"):
                marker = "● " if current == state else "○ "
                out.append((
                    f"Dark mode for {host}: {marker}{state}",
                    lambda h=host, s=state: self._set_override(h, s),
                ))
        return out

    def _cycle_global(self):
        order = ["auto", "always", "never", "contrast"]
        idx = order.index(self._global_default) if self._global_default in order else 0
        self._global_default = order[(idx + 1) % len(order)]
        cfg = Config()
        cfg.set("dark_mode", "default", self._global_default)
        self._persist(cfg)
        if self._window and self._window._active_webview:
            self.apply(self._window._active_webview)

    def _set_override(self, host: str, state: str):
        if state == "auto":
            self._site_overrides.pop(host, None)
        else:
            self._site_overrides[host] = state
        cfg = Config()
        cfg.set("dark_mode", "site_overrides",
                dict(self._site_overrides))
        self._persist(cfg)
        if self._window and self._window._active_webview:
            self.apply(self._window._active_webview)

    @staticmethod
    def _persist(cfg):
        try:
            cfg.save()
        except Exception as exc:
            import logging
            logging.getLogger("qdbrowser.dark_mode").warning(
                "could not persist dark_mode state: %s", exc)


def _js_str(s: str) -> str:
    import json
    return json.dumps(s)
