"""Terminal widget wrapping QTermWidget."""

import os

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget
from QTermWidget import QTermWidget

from qterminator.config import Config
from qterminator.plugin import select_url_handler
from qterminator.titlebar import TerminalTitlebar


class _ReadOnlyFilter(QObject):
    """Event filter that swallows input events while the terminal is read-only.

    Installed on the QTermWidget's focus proxy (the inner TerminalDisplay, where
    key/IME events are actually delivered). When the owning
    :class:`TerminalWidget` is read-only, any event that could send data to the
    pty -- key presses/releases, IME composition, and text drops -- is consumed
    so the local input path honours the same contract as broadcast targets
    (``window._on_terminal_key``) and web-share, which already gate on the flag.

    Copy, text selection (left-button drag), scrolling, search and context-menu
    actions are NOT pty writes -- they are routed through Qt selection / window
    QActions -- so they keep working in read-only mode. What IS blocked:

    - key presses/releases and IME composition (typing),
    - text drag-and-drop,
    - middle-button paste (QTermWidget's middle click pastes the primary
      selection straight into the pty).

    Known residual: if a TUI has enabled xterm mouse-reporting, mouse clicks
    emit escape sequences to the pty. The QTermWidget binding exposes no way to
    query that mode, and blanket-blocking left-button events would break text
    selection (the primary read-only use case), so reporting-mode mouse escapes
    are not gated here. Keyboard input -- the path this fix targets -- is fully
    closed.
    """

    # Events that can mutate the pty / inject text into the session.
    _BLOCKED = frozenset({
        QEvent.Type.KeyPress,
        QEvent.Type.KeyRelease,
        QEvent.Type.InputMethod,
        QEvent.Type.Drop,
        QEvent.Type.DragEnter,
        QEvent.Type.DragMove,
    })

    # Mouse events whose middle button triggers a primary-selection paste.
    _MOUSE = frozenset({
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
    })

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def eventFilter(self, obj, event):
        if not self._owner.is_read_only():
            return False
        etype = event.type()
        if etype in self._BLOCKED:
            return True  # swallow: no input reaches the terminal
        if etype in self._MOUSE and event.button() == Qt.MouseButton.MiddleButton:
            return True  # block middle-click paste into the read-only pty
        return False


class TerminalWidget(QWidget):
    """Wraps QTermWidget with titlebar, signals, and config integration."""

    # Signals
    title_changed = pyqtSignal(str)
    finished = pyqtSignal()
    focus_gained = pyqtSignal(object)  # emits self
    close_request = pyqtSignal(object)  # emits self
    split_horizontal_request = pyqtSignal(object)
    split_vertical_request = pyqtSignal(object)
    new_tab_request = pyqtSignal()

    # Activity signals
    activity_detected = pyqtSignal(object)
    silence_detected = pyqtSignal(object)

    def __init__(self, parent=None, working_directory=None, profile="default",
                 shell_command=None):
        super().__init__(parent)
        self._profile_name = profile
        self._config = Config()
        self._active = False
        self._read_only = False
        self._group = None  # group name for broadcast
        self._monitor_activity = False
        self._monitor_silence = False
        self._shell_command = shell_command
        self._setup_ui(working_directory)
        self._connect_signals()

    def _setup_ui(self, working_directory):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Titlebar
        self._titlebar = TerminalTitlebar(self)
        self._titlebar.close_clicked.connect(lambda: self.close_request.emit(self))
        self._titlebar.clicked.connect(lambda: self._on_focus())
        layout.addWidget(self._titlebar)

        # Terminal
        self._term = QTermWidget(0)  # 0 = don't start shell yet

        # Read-only enforcement: swallow key/IME/drop events on the terminal
        # while _read_only is set, so the local input path matches the
        # broadcast / web-share contract (which already honour the flag).
        # The window system delivers key/IME events to the *focus widget*,
        # which for QTermWidget is its focus proxy (the inner TerminalDisplay),
        # NOT the QTermWidget itself -- a filter on the outer widget would be
        # bypassed by real keystrokes. Install on the proxy when present, with
        # the outer widget as a defensive fallback.
        self._read_only_filter = _ReadOnlyFilter(self)
        # Filter the focus proxy (keystroke target) and, defensively, the outer
        # widget too -- drag/drop and any later-routed events may land there.
        targets = {self._term}
        proxy = self._term.focusProxy()
        if proxy is not None:
            targets.add(proxy)
        for tgt in targets:
            tgt.installEventFilter(self._read_only_filter)

        # Apply config
        profile = self._config.get_profile(self._profile_name)
        font = QFont(profile["font_family"], profile["font_size"])
        # Ligatures: enable OpenType ligature shaping when requested.
        # PreferDefault = full text shaping (ligatures); NoSubpixelAntialias is unset.
        # Note: QTermWidget always disables kerning at the C++ level for performance,
        # but ligature substitution (separate OpenType feature) is honored by Qt's
        # text shaper when drawing multi-character strings.
        if profile.get("font_ligatures", False):
            font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
        self._term.setTerminalFont(font)
        self._term.setColorScheme(profile["color_scheme"])
        self._term.setHistorySize(profile["scrollback_lines"])
        self._term.setScrollBarPosition(QTermWidget.ScrollBarPosition.ScrollBarRight)
        self._term.setKeyBindings("linux")

        # setBlinkingCursor may not be available in all SIP binding versions
        if profile.get("cursor_blink", True) and hasattr(self._term, "setBlinkingCursor"):
            self._term.setBlinkingCursor(True)

        # Scrollback
        if profile.get("scrollback_infinite", False):
            self._term.setHistorySize(-1)

        # Opacity
        opacity = profile.get("background_opacity", 1.0)
        if opacity < 1.0:
            self._term.setTerminalOpacity(opacity)

        # Auto-close behavior
        exit_action = profile.get("exit_action", "close")
        self._exit_action = exit_action
        self._term.setAutoClose(exit_action == "close")

        # Working directory
        if working_directory:
            self._term.setWorkingDirectory(working_directory)
        else:
            self._term.setWorkingDirectory(os.getcwd())

        # Shell — either an explicit program+argv (e.g. plugin-driven tmux
        # mode) or fall back to the user's $SHELL.
        if self._shell_command:
            argv = list(self._shell_command)
            self._term.setShellProgram(argv[0])
            args = argv[1:]
            if args:
                self._term.setArgs(args)
        else:
            shell = os.environ.get("SHELL", "/bin/bash")
            self._term.setShellProgram(shell)

        layout.addWidget(self._term)

        # Start shell
        self._term.startShellProgram()

    def _connect_signals(self):
        self._term.finished.connect(self._on_finished)
        self._term.titleChanged.connect(self._on_title_changed)
        self._term.termGetFocus.connect(self._on_focus)
        self._term.activity.connect(self._on_activity)
        self._term.silence.connect(self._on_silence)
        self._term.bell.connect(self._on_bell)

        # URL activation (Ctrl+click)
        self._term.urlActivated.connect(self._on_url_activated)

        # Copy on selection
        profile = self._config.get_profile(self._profile_name)
        if profile.get("copy_on_selection", False):
            self._term.copyAvailable.connect(self._on_copy_available)

        # Scroll on keystroke
        self._scroll_on_keystroke = profile.get("scroll_on_keystroke", True)
        if self._scroll_on_keystroke:
            self._term.termKeyPressed.connect(self._on_key_scroll)

    def _on_finished(self):
        if self._exit_action == "restart":
            # Restart the shell
            self._term.startShellProgram()
            return
        if self._exit_action == "hold":
            # Keep terminal open, don't emit close
            return
        self.finished.emit()
        self.close_request.emit(self)

    def _on_bell(self, message):
        profile = self._config.get_profile(self._profile_name)
        if profile.get("visible_bell", False):
            self._flash_bell()

    def _flash_bell(self):
        """Brief visual flash for bell."""
        self._term.setStyleSheet("background-color: #ffffff;")
        QTimer.singleShot(80, lambda: self._term.setStyleSheet(""))

    def _on_url_activated(self, url, from_context_menu):
        """Open URL when Ctrl+clicked in terminal."""
        url_str = url.toString()
        # Try plugin URL handlers first
        try:
            window = self.window()
            pm = getattr(window, '_plugin_manager', None)
            if pm:
                handler = select_url_handler(pm.get_url_handlers(), url_str)
                if handler:
                    handler.handle_url(url_str)
                    return
        except Exception:
            pass
        # Fallback: open with system browser
        import webbrowser
        webbrowser.open(url_str)

    def _on_copy_available(self, available):
        if available:
            self._term.copyClipboard()

    def _on_key_scroll(self, event):
        self._term.scrollToEnd()

    def _on_title_changed(self):
        title = self._term.title() or "Terminal"
        self._titlebar.set_title(title)
        self.title_changed.emit(title)

    def _on_focus(self):
        self.focus_gained.emit(self)

    # Focus indication

    def set_active(self, active):
        """Mark this terminal as the active (focused) one."""
        self._active = active
        self._titlebar.set_active(active)

    def is_active(self):
        return self._active

    # Public API

    @property
    def term(self):
        """Access the underlying QTermWidget."""
        return self._term

    def title(self):
        t = self._term.title()
        if not t or t == "QTermWidget":
            return "Terminal"
        return t

    def copy_clipboard(self):
        self._term.copyClipboard()

    def paste_clipboard(self):
        if self._read_only:
            return
        if self._confirm_dangerous_paste():
            self._term.pasteClipboard()

    def paste_selection(self):
        if self._read_only:
            return
        if self._confirm_dangerous_paste():
            self._term.pasteSelection()

    def _confirm_dangerous_paste(self):
        """Warn before pasting multiline text or text ending with newline.

        Returns True if paste should proceed, False to cancel.
        """
        from PyQt6.QtWidgets import QMessageBox
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if not text:
            return True

        has_newline = '\n' in text or '\r' in text
        if not has_newline:
            return True

        line_count = text.count('\n') + 1
        preview = text[:300]
        if len(text) > 300:
            preview += '...'

        msg = f"Paste {line_count} lines? This may execute commands.\n\n{preview}"
        result = QMessageBox.question(
            self, "Confirm Paste", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def clear(self):
        self._term.clear()

    def zoom_in(self):
        self._term.zoomIn()

    def zoom_out(self):
        self._term.zoomOut()

    def send_text(self, text, force=False):
        """Write text to the pty.

        Honours read-only mode (fail-closed): when the pane is read-only the
        write is suppressed unless ``force=True``. This centralizes the
        read-only gate so every text-injection surface -- broadcast, snippets,
        smart-clipboard, scheduler/triggers, and the MCP/agent control RPCs --
        respects the flag, not just direct keyboard input. Returns True if the
        text was sent, False if it was suppressed.
        """
        if self._read_only and not force:
            return False
        self._term.sendText(text)
        return True

    def selected_text(self):
        return self._term.selectedText()

    def shell_pid(self):
        return self._term.getShellPID()

    def foreground_pid(self):
        return self._term.getForegroundProcessId()

    def working_directory(self):
        return self._term.workingDirectory()

    def set_font(self, family, size):
        font = QFont(family, size)
        self._term.setTerminalFont(font)

    def set_color_scheme(self, name):
        self._term.setColorScheme(name)

    def set_scrollback(self, lines):
        self._term.setHistorySize(lines)

    def apply_profile(self, profile_name):
        """Apply a named profile to this terminal."""
        self._profile_name = profile_name
        profile = self._config.get_profile(profile_name)
        self.set_font(profile["font_family"], profile["font_size"])
        self.set_color_scheme(profile["color_scheme"])
        self.set_scrollback(profile["scrollback_lines"])
        if profile.get("show_titlebar", True):
            self._titlebar.show()
        else:
            self._titlebar.hide()

    def has_running_process(self):
        """Check if a foreground process (other than shell) is running."""
        shell_pid = self.shell_pid()
        fg_pid = self.foreground_pid()
        return fg_pid > 0 and fg_pid != shell_pid

    def toggle_search(self):
        self._term.toggleShowSearchBar()

    # Activity/silence monitoring

    def set_monitor_activity(self, enabled):
        self._monitor_activity = enabled
        self._term.setMonitorActivity(enabled)

    def set_monitor_silence(self, enabled, timeout=10):
        self._monitor_silence = enabled
        self._term.setMonitorSilence(enabled)
        if enabled:
            self._term.setSilenceTimeout(timeout)

    def _on_activity(self):
        if self._monitor_activity:
            self._titlebar.set_activity(True)
            self.activity_detected.emit(self)

    def _on_silence(self):
        if self._monitor_silence:
            self._titlebar.set_activity(False)
            self.silence_detected.emit(self)

    # Read-only mode

    def is_read_only(self):
        return self._read_only

    def set_read_only(self, read_only):
        self._read_only = read_only
        self._titlebar.set_read_only(read_only)

    def toggle_read_only(self):
        self.set_read_only(not self._read_only)

    # Reset

    def reset(self):
        """Reset terminal state."""
        self.send_text("reset\n")

    def reset_clear(self):
        """Reset terminal and clear scrollback."""
        self._term.clear()
        self.send_text("reset\n")

    # Grouping for broadcast input

    @property
    def group(self):
        return self._group

    @group.setter
    def group(self, name):
        self._group = name
        self._titlebar.set_group(name)

    # Focus / context menu
    # (Read-only key/IME filtering is handled by _ReadOnlyFilter, installed on
    # the QTermWidget in _setup_ui.)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._term.setFocus()
        self.focus_gained.emit(self)

    def contextMenuEvent(self, event):
        from qterminator.context_menu import build_context_menu
        menu = build_context_menu(self)
        menu.exec(event.globalPos())
