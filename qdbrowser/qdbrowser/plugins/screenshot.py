"""Screenshot plugin: viewport / full page / selector capture, saved to disk."""

from __future__ import annotations

import os
import time

from PyQt6.QtGui import QImage, QPainter

from qdbrowser.plugin import CommandProvider


def _save_dir() -> str:
    d = os.path.expanduser("~/Pictures/qdbrowser")
    os.makedirs(d, exist_ok=True)
    return d


# Cap on full-page screenshot composite size — a hostile page can set
# ``scrollWidth``/``scrollHeight`` to anything, and a 4 TB allocation
# attempt is just an OOM. 16384 x 32768 fits any reasonable article.
_MAX_FULL_PAGE_WIDTH = 16384
_MAX_FULL_PAGE_HEIGHT = 32768


class ScreenshotPlugin(CommandProvider):
    name = "screenshot"
    description = "Capture viewport, full page, or a CSS-selector element."
    capabilities = ["command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None

    def activate(self, window):
        self._window = window

    def get_commands(self, window):
        return [
            ("Screenshot: visible viewport",
             lambda: self.capture_viewport(window._active_webview)),
            ("Screenshot: full page",
             lambda: self.capture_full_page(window._active_webview)),
        ]

    def capture_viewport(self, webview, path=None):
        if webview is None:
            return None
        pixmap = webview.view.grab()
        path = path or os.path.join(
            _save_dir(),
            f"viewport-{int(time.time())}.png")
        pixmap.save(path, "PNG")
        return path

    def capture_full_page(self, webview, path=None):
        """Scroll-stitch full-page screenshot via JS-coordinated grabs."""
        if webview is None:
            return None
        page = webview.view.page()
        path = path or os.path.join(
            _save_dir(),
            f"fullpage-{int(time.time())}.png")
        # Read total document size + viewport size synchronously via a
        # blocking callback.
        from PyQt6.QtWidgets import QApplication
        result = {"done": False, "data": None}

        def _cb(v):
            result["data"] = v
            result["done"] = True

        page.runJavaScript(
            "({w:document.documentElement.scrollWidth,"
            "h:document.documentElement.scrollHeight,"
            "vw:window.innerWidth,vh:window.innerHeight,"
            "dpr:window.devicePixelRatio||1})", _cb)
        deadline = time.time() + 3
        while not result["done"] and time.time() < deadline:
            QApplication.instance().processEvents()
        info = result["data"] or {"w": 1280, "h": 800, "vw": 1280, "vh": 800,
                                  "dpr": 1}
        total_w = min(int(info["w"]), _MAX_FULL_PAGE_WIDTH)
        total_h = min(int(info["h"]), _MAX_FULL_PAGE_HEIGHT)
        vh = max(1, int(info["vh"]))

        # Composite by scrolling and grabbing the view repeatedly.
        composite = QImage(total_w, total_h, QImage.Format.Format_ARGB32)
        composite.fill(0)
        painter = QPainter(composite)
        y = 0
        while y < total_h:
            self._scroll_to(page, 0, y)
            QApplication.instance().processEvents()
            time.sleep(0.05)
            QApplication.instance().processEvents()
            pixmap = webview.view.grab()
            painter.drawImage(0, y, pixmap.toImage())
            y += vh
        painter.end()
        composite.save(path, "PNG")
        # Restore scroll.
        self._scroll_to(page, 0, 0)
        return path

    def _scroll_to(self, page, x: int, y: int):
        page.runJavaScript(f"window.scrollTo({x},{y});")
