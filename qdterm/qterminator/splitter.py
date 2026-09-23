"""Split container for terminal widgets using QSplitter."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter

from qterminator.terminal import TerminalWidget


class SplitContainer(QSplitter):
    """A recursive splitter that holds TerminalWidgets or nested SplitContainers."""

    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setChildrenCollapsible(False)
        self.setHandleWidth(2)
        self.setStyleSheet("QSplitter::handle { background-color: #555; }")

    def add_terminal(self, terminal=None, working_directory=None,
                     shell_command=None):
        """Add a new terminal to this splitter. Returns the terminal."""
        if terminal is None:
            terminal = TerminalWidget(
                parent=self,
                working_directory=working_directory,
                shell_command=shell_command,
            )
        self.addWidget(terminal)
        self._equalize()
        return terminal

    def split(self, terminal, orientation):
        """Split the given terminal, adding a new terminal beside it.

        If this splitter's orientation matches, just insert next to the terminal.
        If only one child, change orientation directly.
        Otherwise, wrap the terminal in a nested SplitContainer.
        """
        idx = self.indexOf(terminal)
        if idx == -1:
            return None

        cwd = terminal.working_directory()

        if self.count() == 1:
            # Only one child — just set orientation and add beside it
            self.setOrientation(orientation)
            new_term = TerminalWidget(parent=self, working_directory=cwd)
            self.insertWidget(idx + 1, new_term)
            self._equalize()
            return new_term

        if self.orientation() == orientation:
            # Same orientation, insert next to the terminal
            new_term = TerminalWidget(parent=self, working_directory=cwd)
            self.insertWidget(idx + 1, new_term)
            self._equalize()
            return new_term

        # Different orientation with multiple children: wrap in nested splitter.
        # Must detach terminal first to avoid "replace with sibling" warning.
        nested = SplitContainer(orientation)
        terminal.setParent(None)
        self.insertWidget(idx, nested)
        nested.addWidget(terminal)
        new_term = TerminalWidget(parent=nested, working_directory=cwd)
        nested.addWidget(new_term)
        nested._equalize()
        self._equalize()
        return new_term

    def remove_terminal(self, terminal):
        """Remove a terminal from the split tree.

        Returns True if this container is now empty and should be removed.
        """
        idx = self.indexOf(terminal)
        if idx != -1:
            terminal.setParent(None)
            terminal.deleteLater()
            return self._cleanup_after_remove()

        # Search in nested splitters
        for i in range(self.count()):
            child = self.widget(i)
            if isinstance(child, SplitContainer):
                if child.remove_terminal(terminal):
                    # Child splitter is now empty, remove it
                    child.setParent(None)
                    child.deleteLater()
                    return self._cleanup_after_remove()
        return False

    def _cleanup_after_remove(self):
        """After removing a child, unnest if only one child remains."""
        if self.count() == 0:
            return True  # signal parent to remove us

        if self.count() == 1:
            # Unnest: promote the single child up
            child = self.widget(0)
            if isinstance(child, SplitContainer):
                # Move all grandchildren into this splitter
                self.setOrientation(child.orientation())
                while child.count() > 0:
                    w = child.widget(0)
                    self.addWidget(w)
                child.setParent(None)
                child.deleteLater()
                self._equalize()

        return False

    def _equalize(self):
        """Set all children to equal sizes."""
        if self.count() > 0:
            total = self.width() if self.orientation() == Qt.Orientation.Horizontal else self.height()
            size = max(total // self.count(), 1)
            self.setSizes([size] * self.count())

    def find_terminals(self):
        """Recursively find all TerminalWidgets in this tree."""
        terminals = []
        for i in range(self.count()):
            child = self.widget(i)
            if isinstance(child, TerminalWidget):
                terminals.append(child)
            elif isinstance(child, SplitContainer):
                terminals.extend(child.find_terminals())
        return terminals

    def find_next_terminal(self, current, direction):
        """Find the next terminal in the given direction (left/right/up/down).

        Returns None if no terminal found in that direction.
        """
        terminals = self.find_terminals()
        if not terminals or current not in terminals:
            return None

        idx = terminals.index(current)
        if direction in ("right", "down"):
            return terminals[(idx + 1) % len(terminals)]
        elif direction in ("left", "up"):
            return terminals[(idx - 1) % len(terminals)]
        return None
