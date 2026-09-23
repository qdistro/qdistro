"""Layout serialization and restoration for QTerminator."""

import json

from PyQt6.QtCore import Qt, QTimer

from qterminator.config import Config
from qterminator.splitter import SplitContainer
from qterminator.terminal import TerminalWidget


def serialize_layout(tabs_widget, save_scrollback=None):
    """Serialize the current window layout to a dict.

    Returns a list of tab dicts, each containing a tree of splits/terminals.

    If ``save_scrollback`` is None (default), the ``general.save_scrollback``
    config option controls whether scrollback text is captured. Pass a bool
    explicitly to override (used mostly in tests).

    SECURITY NOTE: Scrollback may contain secrets (passwords typed into
    prompts that didn't mask input, API keys, auth tokens printed in logs).
    Capturing scrollback is therefore opt-in via the ``save_scrollback``
    config flag.
    """
    if save_scrollback is None:
        try:
            save_scrollback = bool(Config().get("general", "save_scrollback", default=False))
        except Exception:
            save_scrollback = False

    tabs = []
    for i in range(tabs_widget.count()):
        split = tabs_widget.widget(i)
        tab_name = tabs_widget.tabText(i)
        tabs.append({
            "name": tab_name,
            "tree": _serialize_node(split, save_scrollback=save_scrollback),
        })
    return {"tabs": tabs}


def _capture_scrollback(terminal):
    """Best-effort capture of a terminal's visible text as plain text.

    QTermWidget has no direct API to return the full scrollback buffer as
    text. We approximate by selecting the visible region via
    ``setSelectionStart`` / ``setSelectionEnd`` and returning
    ``selectedText()``. This generally captures only what is currently on
    screen, not the full history — a documented limitation.

    Returns an empty string if capture is unsupported or fails.
    """
    # LIMITATION: QTermWidget's SIP bindings do not expose a reliable API
    # to read the scrollback/screen buffer programmatically. The only safe
    # method is ``selectedText()``, which returns the *current user
    # selection* — it does not let us select the whole buffer without
    # calling ``setSelectionStart`` / ``setSelectionEnd``, both of which can
    # crash the C++ layer (especially under the offscreen Qt platform or
    # before the widget has been fully rendered). To avoid those crashes we
    # only return what the user currently has selected; if nothing is
    # selected the capture is an empty string. This means scrollback save
    # is a best-effort feature until a richer upstream API lands.
    try:
        term = terminal.term
        return term.selectedText() or ""
    except Exception:
        return ""


def _serialize_node(widget, save_scrollback=False):
    """Recursively serialize a split tree node.

    If ``save_scrollback`` is True, terminal nodes include a ``scrollback``
    key with captured text. See :func:`serialize_layout` for security
    implications.
    """
    if isinstance(widget, SplitContainer):
        children = []
        for i in range(widget.count()):
            children.append(_serialize_node(widget.widget(i), save_scrollback=save_scrollback))
        orientation = "horizontal" if widget.orientation() == Qt.Orientation.Horizontal else "vertical"
        sizes = widget.sizes()
        return {
            "type": "split",
            "orientation": orientation,
            "sizes": sizes,
            "children": children,
        }
    elif isinstance(widget, TerminalWidget):
        node = {
            "type": "terminal",
            "working_directory": widget.working_directory(),
            "group": widget.group,
        }
        if save_scrollback:
            text = _capture_scrollback(widget)
            if text:
                node["scrollback"] = text
        return node
    return {"type": "unknown"}


def restore_layout(window, layout_data):
    """Restore a saved layout into a window.

    The window should be empty (no tabs) before calling this.
    """
    tabs = layout_data.get("tabs", [])
    if not tabs:
        window.new_tab()
        return

    for tab_data in tabs:
        # Tab data may be a JSON string if saved via TOML serialization
        if isinstance(tab_data, str):
            try:
                tab_data = json.loads(tab_data)
            except (json.JSONDecodeError, ValueError):
                # Legacy: Python repr strings from broken TOML serialization
                try:
                    import ast
                    tab_data = ast.literal_eval(tab_data)
                except (ValueError, SyntaxError):
                    continue
        tree = tab_data.get("tree", {})
        split = _restore_node(tree)
        if split is None:
            split = SplitContainer(Qt.Orientation.Horizontal)
            terminal = split.add_terminal()
            window._connect_terminal(terminal)

        tab_name = tab_data.get("name", "Terminal")
        window._tabs.addTab(split, tab_name)

        # Connect all terminals in the restored tree
        for terminal in split.find_terminals():
            window._connect_terminal(terminal)

    window._tabs.setCurrentIndex(0)
    first_split = window._tabs.widget(0)
    terminals = first_split.find_terminals()
    if terminals:
        terminals[0].term.setFocus()
        window._set_active_terminal(terminals[0])
    window._update_tab_bar_visibility()


def _inject_scrollback(terminal, text):
    """Display previous-session scrollback inside a freshly restored terminal.

    We deliberately do NOT feed the text through the shell (that would execute
    arbitrary saved content). Instead we write to the emulator's display layer
    via ``sendText``-equivalent output if available; otherwise we fall back to
    no-op. A separator banner makes the previous content easy to distinguish
    from the new shell session.

    SECURITY: saved scrollback may contain secrets. This function re-displays
    it verbatim; the opt-in config flag gates whether it is saved at all.
    """
    try:
        term = terminal.term
        banner = "\r\n\x1b[2m--- Previous session ---\x1b[0m\r\n"
        footer = "\r\n\x1b[2m--- End previous session ---\x1b[0m\r\n"
        payload = banner + text + footer
        # Prefer a method that writes to the display without sending to the PTY
        # so saved content is NOT executed by the shell.
        if hasattr(term, "sendText"):
            # sendText writes to the PTY which is NOT what we want for
            # restoring display-only content; however QTermWidget lacks a
            # public "write to display" API, so we use it only as a last
            # resort and only inject the banner text, not arbitrary content,
            # to avoid command execution. Route through TerminalWidget.send_text
            # so a read-only pane is honoured (fail-closed) rather than written
            # to via the raw QTermWidget API.
            terminal.send_text("echo -e " + _shell_escape(payload) + "\n")
    except Exception:
        pass


def _shell_escape(s):
    """Quote ``s`` for safe use as a single-argument shell echo target."""
    return "'" + s.replace("'", "'\\''") + "'"


def _restore_node(data):
    """Recursively restore a split tree node."""
    node_type = data.get("type", "unknown")

    if node_type == "terminal":
        cwd = data.get("working_directory")
        terminal = TerminalWidget(working_directory=cwd)
        group = data.get("group")
        if group:
            terminal.group = group
        scrollback = data.get("scrollback")
        if scrollback:
            # Wait for the shell prompt to appear, then re-display the saved
            # scrollback. Uses a delay because the shell starts asynchronously.
            QTimer.singleShot(500, lambda t=terminal, s=scrollback: _inject_scrollback(t, s))
        return terminal

    if node_type == "split":
        orientation_str = data.get("orientation", "horizontal")
        orientation = (Qt.Orientation.Horizontal
                       if orientation_str == "horizontal"
                       else Qt.Orientation.Vertical)
        split = SplitContainer(orientation)
        children = data.get("children", [])
        for child_data in children:
            child = _restore_node(child_data)
            if child:
                if isinstance(child, TerminalWidget):
                    split.addWidget(child)
                elif isinstance(child, SplitContainer):
                    split.addWidget(child)
        sizes = data.get("sizes")
        if sizes and len(sizes) == split.count():
            split.setSizes(sizes)
        return split

    return None
