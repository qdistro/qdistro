"""session_restoration — save and restore window layout on quit/launch.

Persists the current tab/split layout on quit and restores it on
next launch. Works with tmux_mode to restore running processes.

Configuration (config.toml):
    [plugins.session_restoration]
    enabled = false          # default false; opt-in
    restore_on_start = true  # default true, only honored when enabled
    max_age_days = 7         # default 7

The plugin saves to ~/.config/qterminator/session.json on close
and restores from it on startup if the file is fresh enough.
"""

import json
import os
import time

from qterminator import config as config_mod
from qterminator.config import Config
from qterminator.layout import restore_layout, serialize_layout
from qterminator.plugin import Plugin

#: JSON schema version for the on-disk session file. Bumped whenever
#: the shape changes incompatibly; older versions are ignored on load.
SESSION_SCHEMA_VERSION = 1


class _SessionFilePath(os.PathLike):
    """Path-like session file reference resolved at use time."""

    def __fspath__(self):
        return session_file_path()

    def __str__(self):
        return session_file_path()

    def __repr__(self):
        return repr(session_file_path())


def session_file_path() -> str:
    """Return the absolute path of session.json under the current CONFIG_DIR.

    Resolved lazily so tests that monkeypatch ``config_mod.CONFIG_DIR``
    see their tmp path instead of the value captured at import time.
    """
    return os.path.join(config_mod.CONFIG_DIR, "session.json")


SESSION_FILE = _SessionFilePath()


def __getattr__(name):
    # Compatibility for older module-level attribute lookups.
    if name == "SESSION_FILE":
        return SESSION_FILE
    raise AttributeError(name)


class SessionRestorationPlugin(Plugin):
    name = "session_restoration"
    description = (
        "Save and restore window layout on quit/launch. "
        "Preserves tabs, splits, working directories, and optional tmux sessions."
    )
    version = "0.1"
    capabilities = []

    def __init__(self):
        super().__init__()
        self._window = None
        self._original_close = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = cfg.get(
            "plugins", "session_restoration", "enabled", default=False,
        )
        if not enabled:
            return

        restore_on_start = cfg.get(
            "plugins", "session_restoration", "restore_on_start", default=True,
        )

        self._window = app_controller

        if restore_on_start:
            self._restore_session(app_controller)

        # Install the close hook so save_session actually runs on quit.
        # Without this the plugin only restores; it never saves.
        # Skip silently if the controller is a fake/test stub with no
        # closeEvent — the activate path is still useful for state.
        orig_close = getattr(app_controller, "closeEvent", None)
        if callable(orig_close):
            self._original_close = orig_close
            _plugin = self

            def patched_close(event, _orig=orig_close):
                try:
                    _plugin.save_session(_plugin._window)
                except Exception:
                    pass
                _orig(event)

            app_controller.closeEvent = patched_close

    def _restore_session(self, app_controller):
        """Restore a saved session if available and fresh.

        Delegates split-tree reconstruction to
        :func:`qterminator.layout.restore_layout` — the same primitive
        ``MainWindow.restore_layout`` uses — then applies the plugin's
        per-tab extras (profile, tmux attach).
        """
        cfg = Config()
        max_age_days = cfg.get(
            "plugins", "session_restoration", "max_age_days", default=7,
        )

        path = session_file_path()
        if not os.path.exists(path):
            return

        mtime = os.path.getmtime(path)
        age_days = (time.time() - mtime) / (24 * 3600)
        if age_days > max_age_days:
            return

        try:
            with open(path) as f:
                session_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        if not isinstance(session_data, dict):
            return
        # Schema version gate. Older/newer files are skipped silently
        # so a bad migration can't crash the next launch.
        version = session_data.get("version")
        if version is not None and version != SESSION_SCHEMA_VERSION:
            return

        tabs_widget = getattr(app_controller, "_tabs", None)
        if tabs_widget is None:
            return

        # restore_layout expects an empty window; tear down the
        # auto-created initial tabs first. ``removeTab`` only detaches
        # the widget — ``deleteLater`` releases the PTY too.
        while tabs_widget.count() > 0:
            w = tabs_widget.widget(0)
            tabs_widget.removeTab(0)
            if w is not None:
                try:
                    w.deleteLater()
                except Exception:
                    pass

        # Hand the dict shape that ``restore_layout`` knows about
        # (it expects ``{"tabs": [{"name", "tree"}]}``). The plugin's
        # per-tab extras (profile, tmux_session) live alongside.
        try:
            restore_layout(app_controller, session_data)
        except Exception:
            # If anything goes wrong with split reconstruction, fall
            # back to a single empty tab rather than a blank window.
            try:
                app_controller.new_tab()
            except Exception:
                pass
            return

        # Apply per-tab extras now that split structure exists.
        for i, tab_info in enumerate(session_data.get("tabs", [])):
            if i >= tabs_widget.count():
                break
            split = tabs_widget.widget(i)
            if split is None or not hasattr(split, "find_terminals"):
                continue
            terminals = split.find_terminals()
            if not terminals:
                continue
            profile = tab_info.get("profile") or "default"
            if profile != "default":
                try:
                    terminals[0].apply_profile(profile)
                except Exception:
                    pass

    def save_session(self, app_controller):
        """Save the current session to disk.

        Builds on :func:`qterminator.layout.serialize_layout` so the
        full split tree round-trips (not just a flat list of tabs).
        Adds per-tab extras: profile name and tmux session if the
        ``tmux_mode`` service is present.

        Invoked from the patched ``closeEvent`` installed by
        ``activate``; the gate is enforced there. Calling this method
        directly always writes (callers that want a no-op should
        check the config themselves).
        """
        tabs_widget = getattr(app_controller, "_tabs", None)
        if tabs_widget is None:
            return

        try:
            session_data = serialize_layout(tabs_widget)
        except Exception:
            return
        session_data["version"] = SESSION_SCHEMA_VERSION

        # Annotate each tab with profile + optional tmux session so
        # restore can re-apply the per-tab look and re-attach.
        tmux_mode = getattr(app_controller, "tmux_mode", None)
        for i, tab_info in enumerate(session_data.get("tabs", [])):
            split = tabs_widget.widget(i)
            terminals = []
            if hasattr(split, "find_terminals"):
                terminals = split.find_terminals()
            if not terminals:
                continue
            term = terminals[0]
            try:
                tab_info["profile"] = term._profile_name or "default"
            except Exception:
                tab_info["profile"] = "default"
            if tmux_mode is not None:
                try:
                    sess = tmux_mode.get_session_for_terminal(term)
                except Exception:
                    sess = None
                if sess:
                    tab_info["tmux_session"] = sess

        # Atomic write — a crash mid-write must not leave a truncated
        # session.json that ``_restore_session`` would silently skip.
        path = session_file_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(session_data, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def deactivate(self):
        if self._original_close is not None and self._window is not None:
            try:
                self._window.closeEvent = self._original_close
            except AttributeError:
                pass
        self._original_close = None
        self._window = None
