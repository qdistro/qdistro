"""badges — translucent corner overlay showing per-terminal metadata.

Adds a small QLabel on top of every TerminalWidget that renders a
template string filled with live variables:

    [profiles.default]
    badge_template = "{hostname}:{cwd_short}"

    [profiles.prod]
    badge_template = "⚠ PROD ⚠  {hostname}"
    badge_color = "#e74c3c"

The label is click-through (``WA_TransparentForMouseEvents``) so it
doesn't steal selection. Re-renders on each shell_integration
``command_finished`` event (cheap — one template substitution +
QLabel.setText) and on demand via a per-terminal refresh tick.

Template variables resolve at render time:

  - ``{hostname}``       socket.gethostname() — cached once
  - ``{cwd}`` /
    ``{cwd_short}``      from shell_integration.get_history(t).cwd
                         (~ for HOME, basename-only for ``_short``)
  - ``{exit_status}``    last command's exit, or "" before first
  - ``{tmux_session}``   from app_controller.tmux_mode if present
  - ``{branch}``         best-effort ``git rev-parse --abbrev-ref HEAD``
                         in cwd, cached 2s per cwd

The badge is purely additive and zero-cost when idle: with no
template configured for the active profile, no label is created.
"""

import os
import shutil
import socket
import subprocess
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QLabel

from qterminator.config import Config
from qterminator.plugin import Plugin

# ---------------------------------------------------------------------------
# Template + variable resolution
# ---------------------------------------------------------------------------

_HOSTNAME_CACHE: str | None = None


def hostname() -> str:
    global _HOSTNAME_CACHE
    if _HOSTNAME_CACHE is None:
        try:
            _HOSTNAME_CACHE = socket.gethostname()
        except Exception:
            _HOSTNAME_CACHE = ""
    return _HOSTNAME_CACHE


def shorten_cwd(cwd: str | None) -> str:
    """Render ``cwd`` shell-style: ``~`` for HOME, full path otherwise."""
    if not cwd:
        return ""
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        return "~" + cwd[len(home):]
    return cwd


class _BranchCache:
    """Caches the git branch for a given cwd for ``ttl`` seconds.

    Git invocation cost is tiny (~ms) but we don't want to re-shell on
    every ``;D`` event when nothing has changed. 2s is enough for an
    interactive workflow without making the badge feel stale."""

    def __init__(self, ttl: float = 2.0):
        self._ttl = ttl
        self._cache: dict[str, tuple[float, str]] = {}

    def lookup(self, cwd: str | None) -> str:
        if not cwd or not os.path.isdir(cwd):
            return ""
        now = time.monotonic()
        cached = self._cache.get(cwd)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        branch = self._git_branch(cwd)
        self._cache[cwd] = (now, branch)
        return branch

    @staticmethod
    def _git_branch(cwd: str) -> str:
        git = shutil.which("git")
        if git is None:
            return ""
        try:
            out = subprocess.run(
                [git, "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=0.5,
            )
        except (subprocess.SubprocessError, OSError):
            return ""
        if out.returncode != 0:
            return ""
        return out.stdout.strip()


def render_template(template: str, ctx: dict) -> str:
    """Minimal ``{var}`` substitution that tolerates missing keys
    (renders them as ``""``, *not* ``{var}`` — empty is a better
    default for an always-visible UI label)."""
    out: list[str] = []
    i = 0
    while i < len(template):
        c = template[i]
        if c == "{":
            end = template.find("}", i + 1)
            if end == -1:
                out.append(template[i:])
                break
            key = template[i + 1:end]
            out.append(str(ctx.get(key, "")))
            i = end + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def collect_context(window, terminal, branch_cache: _BranchCache) -> dict:
    """Pull every documented template variable from the live services
    on ``window`` for ``terminal``. Missing services contribute empty
    strings so a template that mentions ``{tmux_session}`` doesn't
    blow up when tmux_mode isn't loaded."""
    cwd = None
    exit_status = ""
    shell_int = getattr(window, "shell_integration", None)
    if shell_int is not None:
        try:
            history = shell_int.get_history(terminal)
            if history is not None:
                cwd = history.cwd
                if history.last is not None and history.last.exit_status is not None:
                    exit_status = str(history.last.exit_status)
        except Exception:
            pass
    tmux_session = ""
    tmux_mode = getattr(window, "tmux_mode", None)
    if tmux_mode is not None:
        try:
            s = tmux_mode.get_session_for_terminal(terminal)
            if s:
                tmux_session = s
        except Exception:
            pass
    branch = branch_cache.lookup(cwd) if cwd else ""
    return {
        "hostname": hostname(),
        "cwd": cwd or "",
        "cwd_short": shorten_cwd(cwd),
        "exit_status": exit_status,
        "tmux_session": tmux_session,
        "branch": branch,
    }


# ---------------------------------------------------------------------------
# Per-terminal badge overlay
# ---------------------------------------------------------------------------

class _BadgeOverlay(QLabel):
    """Click-through QLabel positioned in a terminal's corner."""

    MARGIN = 6
    HIDE_AFTER_KEYPRESS_MS = 1000

    def __init__(self, terminal, template: str, color: str,
                 corner: str = "top-right",
                 parent_widget=None):
        super().__init__(parent_widget or terminal._term)
        self._terminal = terminal
        self._template = template
        self._corner = corner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._apply_style(color)
        self.setText("")
        self.hide()
        # Hide-on-input timer.
        self._unhide_timer = QTimer(self)
        self._unhide_timer.setSingleShot(True)
        self._unhide_timer.timeout.connect(self.show)

    @property
    def template(self) -> str:
        return self._template

    def update_text(self, text: str) -> None:
        self.setText(text)
        if text:
            self.adjustSize()
            self.reposition()
            if not self.isVisible():
                self.show()
        else:
            self.hide()

    def reposition(self) -> None:
        parent = self.parent()
        if parent is None:
            return
        pw = parent.width()
        ph = parent.height()
        w = self.width()
        h = self.height()
        if self._corner == "top-right":
            x = pw - w - self.MARGIN
            y = self.MARGIN
        elif self._corner == "top-left":
            x = self.MARGIN
            y = self.MARGIN
        elif self._corner == "bottom-right":
            x = pw - w - self.MARGIN
            y = ph - h - self.MARGIN
        else:  # bottom-left
            x = self.MARGIN
            y = ph - h - self.MARGIN
        self.move(x, y)

    def hide_briefly(self) -> None:
        """Hide on keypress; re-show after the user stops typing."""
        if self.isVisible():
            self.hide()
        self._unhide_timer.start(self.HIDE_AFTER_KEYPRESS_MS)

    def _apply_style(self, color: str) -> None:
        col = QColor(color)
        if not col.isValid():
            col = QColor("#cccccc")
        # Stylesheet keeps it self-contained — no need to subclass paint.
        css = (
            "QLabel { "
            f"color: {col.name()}; "
            "background: rgba(0,0,0, 96); "
            "border-radius: 4px; "
            "padding: 2px 6px; "
            "font-weight: 600; "
            "}"
        )
        self.setStyleSheet(css)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class BadgesService:
    """Holds per-terminal overlays + the branch cache. Pure-Python so
    headless tests don't need a real window when exercising the
    template / context pipeline."""

    def __init__(self, window):
        self._window = window
        self._overlays: dict[int, _BadgeOverlay] = {}
        self._branch_cache = _BranchCache()

    @property
    def branch_cache(self) -> _BranchCache:
        return self._branch_cache

    def attach(self, terminal) -> _BadgeOverlay | None:
        tid = id(terminal)
        if tid in self._overlays:
            return self._overlays[tid]
        cfg = Config()
        profile_name = getattr(terminal, "_profile_name", "default") or "default"
        profile = cfg.get_profile(profile_name) or {}
        template = (
            profile.get("badge_template")
            or cfg.get("plugins", "badges", "default_template", default="")
            or ""
        )
        if not template:
            return None  # nothing to show — don't allocate the label
        color = (
            profile.get("badge_color")
            or cfg.get("plugins", "badges", "default_color", default="#cccccc")
        )
        corner = cfg.get(
            "plugins", "badges", "corner", default="top-right",
        )
        overlay = _BadgeOverlay(
            terminal, template=template, color=color, corner=corner,
        )
        self._overlays[tid] = overlay
        # Ensure shell_integration is actively parsing this tab — the
        # ;D-driven refresh only fires once a parser exists.
        shell_int = getattr(self._window, "shell_integration", None)
        if shell_int is not None:
            try:
                shell_int.ensure_attached(terminal)
            except Exception:
                pass
        # Wire the hide-on-keypress behaviour. termKeyPressed already
        # exists on the QTermWidget.
        try:
            terminal.term.termKeyPressed.connect(overlay.hide_briefly)
        except Exception:
            pass
        # Reposition on parent resize. The QTermWidget doesn't expose a
        # resize signal we can hook from outside; install an event
        # filter via Qt's resizeEvent override via partial. Simplest:
        # poll the parent's resizeEvent via a timer? No — just respond
        # to the focus_gained / activity signals (rare relative to
        # resize) and on every render. A QTimer single-shot on the
        # next event loop iteration covers initial layout.
        QTimer.singleShot(0, lambda o=overlay: o.reposition())
        self.refresh(terminal)
        return overlay

    def detach(self, terminal) -> None:
        tid = id(terminal)
        overlay = self._overlays.pop(tid, None)
        if overlay is None:
            return
        try:
            terminal.term.termKeyPressed.disconnect(overlay.hide_briefly)
        except (TypeError, RuntimeError):
            pass
        overlay.setParent(None)
        overlay.deleteLater()

    def detach_all(self) -> None:
        for tid in list(self._overlays.keys()):
            overlay = self._overlays.pop(tid)
            overlay.setParent(None)
            overlay.deleteLater()

    def refresh(self, terminal) -> None:
        overlay = self._overlays.get(id(terminal))
        if overlay is None:
            return
        ctx = collect_context(self._window, terminal, self._branch_cache)
        overlay.update_text(render_template(overlay.template, ctx))

    def refresh_all(self) -> None:
        for tid in list(self._overlays.keys()):
            # Locate the terminal object from its id — easier to refresh
            # via the overlay's stored reference.
            overlay = self._overlays[tid]
            term = overlay._terminal
            ctx = collect_context(self._window, term, self._branch_cache)
            overlay.update_text(render_template(overlay.template, ctx))

    @property
    def overlays(self) -> dict[int, _BadgeOverlay]:
        return dict(self._overlays)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class BadgesPlugin(Plugin):
    name = "badges"
    description = (
        "Translucent corner overlay on each terminal showing "
        "templated session metadata (hostname, cwd, exit status, "
        "branch, tmux session)."
    )
    version = "0.1"
    capabilities = ["badges"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: BadgesService | None = None
        self._original_connect = None
        self._shell_int_sub = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "badges", "enabled", default=True,
        ))
        if not enabled:
            return
        self._window = app_controller
        self._service = BadgesService(app_controller)
        if not hasattr(app_controller, "badges"):
            app_controller.badges = self._service
        # Attach existing terminals (the initial tab has been created
        # by now since plugin activate runs after MainWindow.new_tab in
        # the constructor — actually it runs before, so this is a no-op
        # at startup; the _connect_terminal wrap below catches it).
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is not None:
            for i in range(tabs.count()):
                split = tabs.widget(i)
                for term in split.find_terminals():
                    self._service.attach(term)
        # Wrap _connect_terminal so every new terminal gets a badge.
        orig = getattr(app_controller, "_connect_terminal", None)
        if orig is not None:
            self._original_connect = orig

            def wrapped(terminal, _orig=orig, _svc=self._service):
                _orig(terminal)
                try:
                    _svc.attach(terminal)
                except Exception:
                    pass
            app_controller._connect_terminal = wrapped
        # Hook shell_integration so badges refresh on ;D.
        shell_int = getattr(app_controller, "shell_integration", None)
        if shell_int is not None:
            self._shell_int_sub = (
                lambda terminal, _rec: self._service.refresh(terminal)
            )
            shell_int.subscribe_command_finished(self._shell_int_sub)

    def deactivate(self):
        shell_int = (
            getattr(self._window, "shell_integration", None)
            if self._window else None
        )
        if shell_int is not None and self._shell_int_sub is not None:
            try:
                shell_int.unsubscribe_command_finished(self._shell_int_sub)
            except Exception:
                pass
        self._shell_int_sub = None
        if self._service is not None:
            self._service.detach_all()
        if self._original_connect is not None and self._window is not None:
            try:
                self._window._connect_terminal = self._original_connect
            except AttributeError:
                pass
        self._original_connect = None
        if (self._window is not None
                and getattr(self._window, "badges", None) is self._service):
            try:
                del self._window.badges
            except AttributeError:
                pass
        self._service = None
