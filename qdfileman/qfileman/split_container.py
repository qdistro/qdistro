"""Recursive split container for :class:`qfileman.pane.FilePane`.

Mirrors the design of ``qterminator.splitter.SplitContainer``: a
``QSplitter`` that holds either leaf widgets (``FilePane``) or nested
``SplitContainer`` instances. Splits in the orientation already matched
by the parent slot in next to a sibling; cross-orientation splits wrap
the target in a nested splitter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter, QWidget

from qfileman.pane import FilePane

log = logging.getLogger(__name__)


class SplitContainer(QSplitter):
    """Recursive splitter for FilePane leaves."""

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self.setChildrenCollapsible(False)
        self.setHandleWidth(3)

    # --------------------------------------------------------------- API
    def add_pane(self, pane: FilePane) -> FilePane:
        """Append a pre-existing pane as a new child."""
        self.addWidget(pane)
        self._equalize()
        return pane

    def split(
        self,
        target: FilePane,
        orientation: Qt.Orientation,
        new_pane_factory: Callable[[], FilePane],
    ) -> FilePane | None:
        """Split ``target`` and return the newly inserted pane.

        ``new_pane_factory`` is a no-arg callable that constructs the new
        pane (typically wired up by the host window so plugins / initial
        path can be set on it before display).
        """
        idx = self.indexOf(target)
        if idx == -1:
            # Recurse into nested splitters.
            for i in range(self.count()):
                child = self.widget(i)
                if isinstance(child, SplitContainer):
                    result = child.split(target, orientation, new_pane_factory)
                    if result is not None:
                        return result
            return None

        new_pane = new_pane_factory()

        # Only one child: just set the orientation and append.
        if self.count() == 1:
            self.setOrientation(orientation)
            self.insertWidget(idx + 1, new_pane)
            self._equalize()
            return new_pane

        # Same orientation: simple insertion alongside the sibling.
        if self.orientation() == orientation:
            self.insertWidget(idx + 1, new_pane)
            self._equalize()
            return new_pane

        # Cross orientation: wrap the target inside a nested splitter.
        nested = SplitContainer(orientation)
        target.setParent(None)
        self.insertWidget(idx, nested)
        nested.addWidget(target)
        nested.addWidget(new_pane)
        nested._equalize()
        self._equalize()
        return new_pane

    def remove_pane(self, pane: FilePane) -> bool:
        """Remove ``pane`` from the tree.

        Returns True if this container is now empty and should itself be
        removed by its parent.

        After a nested removal we eagerly collapse any sub-splitter that
        ends up with a single child — that single child is promoted up
        in place of the splitter, so we don't accumulate trivial wrapper
        splitters as the user closes panes.
        """
        idx = self.indexOf(pane)
        if idx != -1:
            pane.setParent(None)
            pane.deleteLater()
            return self._cleanup_after_remove()

        for i in range(self.count()):
            child = self.widget(i)
            if isinstance(child, SplitContainer):
                if child.remove_pane(pane):
                    child.setParent(None)
                    child.deleteLater()
                    return self._cleanup_after_remove()
                if child.count() == 1:
                    # Nested splitter has one leaf left — promote it.
                    inner = child.widget(0)
                    inner.setParent(None)
                    self.insertWidget(i, inner)
                    child.setParent(None)
                    child.deleteLater()
                    return self._cleanup_after_remove()
        return False

    def find_panes(self) -> list[FilePane]:
        """All FilePanes anywhere in this tree, in left-to-right order."""
        out: list[FilePane] = []
        for i in range(self.count()):
            child = self.widget(i)
            if isinstance(child, FilePane):
                out.append(child)
            elif isinstance(child, SplitContainer):
                out.extend(child.find_panes())
        return out

    # ----------------------------------------------------------- helpers
    def _cleanup_after_remove(self) -> bool:
        """Collapse single-child splitters; signal empty to parent."""
        if self.count() == 0:
            return True

        if self.count() == 1:
            child = self.widget(0)
            if isinstance(child, SplitContainer):
                # Promote the grandchild widgets up to this level.
                self.setOrientation(child.orientation())
                while child.count() > 0:
                    self.addWidget(child.widget(0))
                child.setParent(None)
                child.deleteLater()
                self._equalize()
        return False

    def _equalize(self) -> None:
        """Divide the splitter's extent equally among its children.

        ``QSplitter.setSizes`` only takes effect when the widget has a
        real on-screen extent. Before the first paint, ``self.width()`` /
        ``self.height()`` return 0, which would clamp every child to 1
        pixel. Setting equal stretch factors makes QSplitter divide the
        extent evenly once a real layout happens, so panes start at the
        expected proportions on first paint as well as after splits.
        """
        if self.count() == 0:
            return
        for i in range(self.count()):
            self.setStretchFactor(i, 1)
        extent = (
            self.width()
            if self.orientation() == Qt.Orientation.Horizontal
            else self.height()
        )
        if extent > 0:
            size = max(extent // self.count(), 1)
            self.setSizes([size] * self.count())

    def showEvent(self, event):  # noqa: N802 (Qt API)
        """Re-equalise once the widget has its real extent."""
        super().showEvent(event)
        self._equalize()
