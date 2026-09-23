"""Tmux mode — born-in-tmux tabs that look and feel native.

When the plugin is enabled, every new tab spawns ``tmux`` *as* the tab's
root program (not a shell that then runs tmux), with a curated tmux
config that hides the giveaway visual elements. The result:

- No flicker: no "exec tmux ..." command appears in the scrollback —
  the very first thing the PTY sees is the tmux client.
- No status bar: ``status off`` in our config — no green strip at the
  bottom.
- Window/tab title still works: ``set-titles on`` makes tmux propagate
  the inner program's title through OSC sequences, which QTermWidget's
  titlebar already consumes.
- Resize is invisible: tmux honors SIGWINCH on the client PTY, which
  QTermWidget already sends.
- Sessions survive: closing the tab leaves the tmux server holding the
  session. Quitting QTerminator does the same. On next startup the
  plugin re-creates one tab per matching ``qterm-*`` session
  (when ``restore_on_start`` is true) and attaches each.

The plugin exposes a small service via ``app_controller.tmux_mode``:

    .get_session_for_terminal(t)  -> str | None     (detection)
    .list_sessions()              -> list[str]
    .shell_for_new_tab()          -> [program, *args] | None

``agent_control`` reads ``get_session_for_terminal`` to enrich
``list_tabs`` so agents know which tab is backed by which tmux session.

Configuration (config.toml):

    [plugins.tmux_mode]
    enabled = true                   # default false — opt-in
    session_prefix = "qterm"         # session name = <prefix>-<n>
    restore_on_start = true          # reattach existing sessions
    kill_on_close = false            # default: leave server running
"""

import os
import shutil
import subprocess

from PyQt6.QtCore import QTimer

from qterminator.config import CONFIG_DIR, Config
from qterminator.plugin import MenuProvider

TMUX_CONF_DEFAULT = """\
# qterminator tmux_mode config — keeps tmux invisible to the user.
set -g status off
set -g set-titles on
set -g set-titles-string "#{?#{==:#{pane_current_command},tmux},#{host_short},#{pane_current_command}}: #{pane_current_path}"
set -g escape-time 0
set -g focus-events on
set -g mouse on
set -g history-limit 50000
# Don't print "[detached]" / "[exited]" lines that give it away.
set -g detach-on-destroy on
set -g default-terminal "xterm-256color"
"""


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _run_tmux(*args, timeout: float = 2.0, **kwargs) -> subprocess.CompletedProcess:
    """Run ``tmux <args>`` capturing output. Returns the CompletedProcess;
    callers check ``.returncode``. ``FileNotFoundError`` raises through;
    ``TimeoutExpired`` is converted to ``CompletedProcess(returncode=124)``."""
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True, text=True, timeout=timeout, **kwargs,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args=args, returncode=124,
                                           stdout="", stderr="timeout")


class TmuxModeService:
    """Detection + helpers, exposed via ``app_controller.tmux_mode``.

    Constructed even when the plugin is disabled — callers can ask
    ``shell_for_new_tab()`` and get ``None`` if mode is off.
    """

    def __init__(self, prefix: str = "qterm", enabled: bool = False,
                 conf_path: str | None = None):
        self.prefix = prefix
        self.enabled = enabled
        self.conf_path = conf_path

    # -- detection --

    def list_sessions(self) -> list[str]:
        if not _tmux_available():
            return []
        r = _run_tmux("list-sessions", "-F", "#{session_name}")
        if r.returncode != 0:
            return []
        return [s for s in r.stdout.splitlines() if s]

    def own_sessions(self) -> list[str]:
        """Sessions whose names start with our prefix."""
        return [s for s in self.list_sessions() if s.startswith(self.prefix)]

    def get_session_for_terminal(self, terminal) -> str | None:
        """If the tab's root process is a tmux client, return its session."""
        if not _tmux_available():
            return None
        try:
            pid = int(terminal.shell_pid())
        except Exception:
            return None
        if pid <= 0:
            return None
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except OSError:
            return None
        if not comm.startswith("tmux"):
            return None
        r = _run_tmux("list-clients", "-F", "#{client_pid} #{session_name}")
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            cpid, _, sess = line.partition(" ")
            if cpid.isdigit() and int(cpid) == pid:
                return sess or None
        return None

    # -- spawn command builder --

    def _build_argv(self, mode: str, session: str) -> list[str]:
        argv = ["tmux"]
        if self.conf_path:
            argv += ["-f", self.conf_path]
        if mode == "attach":
            argv += ["new-session", "-A", "-s", session]
        elif mode == "fresh":
            argv += ["new-session", "-s", session]
        else:
            raise ValueError(f"unknown mode: {mode}")
        return argv

    def _next_session_name(self) -> str:
        """Pick the lowest unused ``<prefix>-<n>``."""
        existing = set(self.own_sessions())
        n = 1
        while f"{self.prefix}-{n}" in existing:
            n += 1
        return f"{self.prefix}-{n}"

    def shell_for_new_tab(self, session: str | None = None) -> list | None:
        """Return the argv to spawn for a new tab, or None if disabled."""
        if not self.enabled or not _tmux_available():
            return None
        name = session or self._next_session_name()
        return self._build_argv("attach", name)

    def shell_for_attach(self, session: str) -> list:
        return self._build_argv("attach", session)

    def kill_session(self, session: str):
        if _tmux_available():
            _run_tmux("kill-session", "-t", session)


def _write_default_conf(path: str):
    """Write the qterminator tmux config if it isn't present."""
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(TMUX_CONF_DEFAULT)


class TmuxModePlugin(MenuProvider):
    name = "tmux_mode"
    description = "Open tabs invisibly inside tmux; restore sessions on launch"
    version = "0.1"
    category = "Workspace"
    capabilities = ["menu_provider", "tmux_mode"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service = TmuxModeService()
        self._restore_on_start = False
        self._kill_on_close = False
        # Bound-method instance kept so deactivate can identity-compare.
        self._installed_provider = None

    # -- lifecycle --

    def activate(self, app_controller):
        self._window = app_controller
        cfg = Config()
        enabled = bool(cfg.get("plugins", "tmux_mode", "enabled", default=False))
        prefix = cfg.get("plugins", "tmux_mode", "session_prefix",
                          default="qterm")
        self._restore_on_start = bool(cfg.get(
            "plugins", "tmux_mode", "restore_on_start", default=False,
        ))
        self._kill_on_close = bool(cfg.get(
            "plugins", "tmux_mode", "kill_on_close", default=False,
        ))
        conf_path = os.path.join(CONFIG_DIR, "tmux.conf")
        try:
            _write_default_conf(conf_path)
        except OSError:
            conf_path = None

        self._service = TmuxModeService(
            prefix=prefix, enabled=enabled, conf_path=conf_path,
        )
        # Expose the service unconditionally so detection works even
        # when "enabled" is false (a tab might still be tmux because the
        # user typed `tmux` themselves).
        if not hasattr(app_controller, "tmux_mode"):
            app_controller.tmux_mode = self._service
        registry = getattr(app_controller, "shadow_screens", None)
        if registry is not None:
            registry.set_tmux_resolver(self._service.get_session_for_terminal)

        # Install the shell provider hook only if enabled. Without this,
        # new_tab() spawns the user's $SHELL as before.
        if enabled:
            self._installed_provider = self._service.shell_for_new_tab
            app_controller._shell_provider = self._installed_provider

        if enabled and self._restore_on_start:
            # Defer until the initial tab finishes constructing; otherwise
            # we'd add tabs before the first one is wired up.
            QTimer.singleShot(150, self._restore_existing_sessions)

    def deactivate(self):
        if self._window is None:
            return
        if (self._installed_provider is not None
                and getattr(self._window, "_shell_provider", None)
                    is self._installed_provider):
            self._window._shell_provider = None
        self._installed_provider = None
        if getattr(self._window, "tmux_mode", None) is self._service:
            try:
                del self._window.tmux_mode
            except AttributeError:
                pass
        registry = getattr(self._window, "shadow_screens", None)
        if registry is not None:
            registry.set_tmux_resolver(None)

    # -- menu items --

    def get_menu_items(self, terminal):
        if not _tmux_available():
            return []
        items = []
        if self._service.enabled:
            items.append(
                ("New Tab (no tmux)", lambda: self._open_plain_tab()),
            )
        else:
            items.append(
                ("New Tmux-Backed Tab", lambda: self._open_tmux_tab()),
            )
        sessions = self._service.own_sessions()
        if sessions:
            items.append(("---", None))  # separator
            for s in sessions[:8]:
                items.append((
                    f"Attach Tab to: {s}",
                    lambda sess=s: self._open_tmux_tab(session=sess),
                ))
        current = self._service.get_session_for_terminal(terminal)
        if current:
            items.append(("---", None))
            items.append((
                f"Detach (close tab, keep '{current}')",
                lambda: self._detach_current_tab(terminal),
            ))
            items.append((
                f"Kill Session '{current}'",
                lambda sess=current: self._service.kill_session(sess),
            ))
        return items

    # -- actions --

    def _open_tmux_tab(self, session: str | None = None):
        if not self._window:
            return
        argv = self._service.shell_for_attach(
            session or self._service._next_session_name()
        )
        self._window.new_tab(shell_command=argv)

    def _open_plain_tab(self):
        """Force a non-tmux tab even when tmux_mode is enabled."""
        if not self._window:
            return
        # Bypass the shell provider by passing an explicit empty marker;
        # we pass the user's $SHELL explicitly so new_tab doesn't fall
        # back to the provider.
        import os as _os
        shell = _os.environ.get("SHELL", "/bin/bash")
        self._window.new_tab(shell_command=[shell])

    def _detach_current_tab(self, terminal):
        """Send Ctrl-B d (default tmux detach prefix) to the tab. The
        client exits, the tab closes, the session persists."""
        terminal.send_text("\x02d")

    def _restore_existing_sessions(self):
        """At startup, create one tab per existing prefix-matching session."""
        for sess in self._service.own_sessions():
            argv = self._service.shell_for_attach(sess)
            self._window.new_tab(shell_command=argv)
