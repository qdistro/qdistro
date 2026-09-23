"""Recursive split container for web views. Direct port of
qterminator/splitter.py — same semantics, ``TerminalWidget`` swapped for
``WebView``.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter

from qdbrowser.webview import WebView


class SplitContainer(QSplitter):
    """A recursive splitter holding ``WebView``s or nested SplitContainers."""

    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setChildrenCollapsible(False)
        self.setHandleWidth(2)

    def add_webview(self, webview=None, url=None, profile_name="default"):
        if webview is None:
            webview = WebView(url=url, profile_name=profile_name, parent=self)
        self.addWidget(webview)
        self._equalize()
        return webview

    def split(self, webview, orientation, url=None):
        """Split the given webview, adding a new webview beside it."""
        idx = self.indexOf(webview)
        if idx == -1:
            return None

        profile = webview.profile_name

        if self.count() == 1:
            self.setOrientation(orientation)
            new = WebView(url=url, profile_name=profile, parent=self)
            self.insertWidget(idx + 1, new)
            self._equalize()
            return new

        if self.orientation() == orientation:
            new = WebView(url=url, profile_name=profile, parent=self)
            self.insertWidget(idx + 1, new)
            self._equalize()
            return new

        nested = SplitContainer(orientation)
        webview.setParent(None)
        self.insertWidget(idx, nested)
        nested.addWidget(webview)
        new = WebView(url=url, profile_name=profile, parent=nested)
        nested.addWidget(new)
        nested._equalize()
        self._equalize()
        return new

    def remove_webview(self, webview):
        """Returns True if this container is now empty and parent should drop it."""
        idx = self.indexOf(webview)
        if idx != -1:
            webview.setParent(None)
            webview.deleteLater()
            return self._cleanup_after_remove()
        for i in range(self.count()):
            child = self.widget(i)
            if isinstance(child, SplitContainer):
                if child.remove_webview(webview):
                    child.setParent(None)
                    child.deleteLater()
                    return self._cleanup_after_remove()
        return False

    def _cleanup_after_remove(self):
        if self.count() == 0:
            return True
        if self.count() == 1:
            child = self.widget(0)
            if isinstance(child, SplitContainer):
                self.setOrientation(child.orientation())
                while child.count() > 0:
                    self.addWidget(child.widget(0))
                child.setParent(None)
                child.deleteLater()
                self._equalize()
        return False

    def _equalize(self):
        if self.count() > 0:
            total = (self.width()
                     if self.orientation() == Qt.Orientation.Horizontal
                     else self.height())
            size = max(total // self.count(), 1)
            self.setSizes([size] * self.count())

    def find_webviews(self):
        out = []
        for i in range(self.count()):
            child = self.widget(i)
            if isinstance(child, WebView):
                out.append(child)
            elif isinstance(child, SplitContainer):
                out.extend(child.find_webviews())
        return out

    def find_next_webview(self, current, direction):
        views = self.find_webviews()
        if not views or current not in views:
            return None
        idx = views.index(current)
        if direction in ("right", "down"):
            return views[(idx + 1) % len(views)]
        if direction in ("left", "up"):
            return views[(idx - 1) % len(views)]
        return None
