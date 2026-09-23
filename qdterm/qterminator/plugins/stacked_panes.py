"""Stacked panes plugin for QTerminator.

Similar to "stack.wez" for WezTerm: lets multiple terminals in a tab
act as a stack where only one is visible at a time. You can cycle
through hidden terminals while keeping them alive (their processes
continue to run).
"""

from qterminator.plugin import MenuProvider


class StackedPanesPlugin(MenuProvider):
    name = "stacked_panes"
    description = "Stack mode: cycle through panes, showing one at a time"
    version = "1.0"
    category = "View"

    def __init__(self):
        # tab widget id() -> bool
        self._stack_mode = {}
        # tab widget id() -> index into terminals list of the visible one
        self._current_visible = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _tab_split_for(self, terminal):
        """Return the tab root split widget containing this terminal, or None."""
        window = terminal.window()
        tabs = getattr(window, "_tabs", None)
        if tabs is None:
            return None
        for i in range(tabs.count()):
            split = tabs.widget(i)
            if split is None:
                continue
            try:
                terms = split.find_terminals()
            except AttributeError:
                continue
            if terminal in terms:
                return split
        return None

    def _terminals_in_tab(self, terminal):
        split = self._tab_split_for(terminal)
        if split is None:
            return []
        return split.find_terminals()

    def _tab_key(self, terminal):
        split = self._tab_split_for(terminal)
        return id(split) if split is not None else None

    # ------------------------------------------------------------------
    # stack operations
    # ------------------------------------------------------------------
    def _stack_on(self, terminal):
        key = self._tab_key(terminal)
        if key is None:
            return
        terminals = self._terminals_in_tab(terminal)
        if len(terminals) <= 1:
            return
        self._stack_mode[key] = True
        try:
            idx = terminals.index(terminal)
        except ValueError:
            idx = 0
        self._current_visible[key] = idx
        for i, term in enumerate(terminals):
            if i == idx:
                term.show()
            else:
                term.hide()

    def _stack_off(self, terminal):
        key = self._tab_key(terminal)
        if key is None:
            return
        terminals = self._terminals_in_tab(terminal)
        for term in terminals:
            term.show()
        self._stack_mode.pop(key, None)
        self._current_visible.pop(key, None)

    def _toggle_stack(self, terminal):
        key = self._tab_key(terminal)
        if key is not None and self._stack_mode.get(key):
            self._stack_off(terminal)
        else:
            self._stack_on(terminal)

    def _cycle(self, terminal, step):
        key = self._tab_key(terminal)
        if key is None or not self._stack_mode.get(key):
            return
        terminals = self._terminals_in_tab(terminal)
        if not terminals:
            return
        current = self._current_visible.get(key, 0)
        if current >= len(terminals):
            current = 0
        new = (current + step) % len(terminals)
        terminals[current].hide()
        terminals[new].show()
        self._current_visible[key] = new
        terminals[new].setFocus()

    def _stack_next(self, terminal):
        self._cycle(terminal, 1)

    def _stack_prev(self, terminal):
        self._cycle(terminal, -1)

    # ------------------------------------------------------------------
    # MenuProvider API
    # ------------------------------------------------------------------
    def get_menu_items(self, terminal):
        key = self._tab_key(terminal)
        in_stack = bool(key is not None and self._stack_mode.get(key))

        items = []
        if in_stack:
            items.append(("Stack Mode Off", lambda t=terminal: self._stack_off(t)))
            items.append(("Stack: Next", lambda t=terminal: self._stack_next(t)))
            items.append(("Stack: Previous", lambda t=terminal: self._stack_prev(t)))
            items.append(("Stack: Show All", lambda t=terminal: self._stack_off(t)))
        else:
            items.append(("Stack Mode On", lambda t=terminal: self._stack_on(t)))
        return items
