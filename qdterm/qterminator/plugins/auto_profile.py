"""auto_profile — automatic profile switching based on context.

iTerm2's Automatic Profile Switching equivalent: change terminal
profile based on hostname, working directory, or running command.
Most common use: red background on production hosts.

Configuration (config.toml):
    [plugins.auto_profile]
    enabled = false                   # default false (opt-in)
    poll_interval_ms = 2000          # check for SSH every 2s

    # Rules are checked in order; first match wins
    [[plugins.auto_profile.rules]]
    hostname_regex = "^prod-"
    profile = "prod"

    [[plugins.auto_profile.rules]]
    cwd_regex = "^/etc/(nixos|qdistro)"
    profile = "system"

    [[plugins.auto_profile.rules]]
    command_regex = "^sudo\\s+"
    profile = "sudo"

The plugin automatically restores the original profile when leaving
the context that matched (e.g., exiting an SSH session).
"""

import os
import re
import socket
from dataclasses import dataclass

from PyQt6.QtCore import QTimer

from qterminator.config import Config
from qterminator.plugin import Plugin


def _read_proc_cmdline(pid: int) -> list[str] | None:
    """Read ``/proc/<pid>/cmdline`` and return NUL-split argv, or None.

    Returns None for any failure (process gone, permission denied,
    non-Linux platform). Used by SSH detection to learn the remote
    host from the foreground process argv without spawning a child.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not data:
        return None
    # cmdline is NUL-separated and usually NUL-terminated.
    parts = data.split(b"\x00")
    return [p.decode("utf-8", errors="replace") for p in parts if p]


def parse_ssh_host(argv: list[str]) -> str | None:
    """Extract the destination host from an ssh argv.

    Accepts the conventional invocation forms:
      ssh host
      ssh user@host
      ssh -p 2222 host
      ssh -o StrictHostKeyChecking=no host command...
      ssh -l user host

    Returns the destination (host or user@host with the user
    stripped) or None if argv isn't an ssh invocation or no host
    token is present.
    """
    if not argv:
        return None
    if os.path.basename(argv[0]) != "ssh":
        return None
    # Flags that take a value (skip the next token).
    flags_with_arg = {
        "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L",
        "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
    }
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in flags_with_arg:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        # First non-flag token is the destination.
        host = tok
        if "@" in host:
            host = host.split("@", 1)[1]
        return host
    return None


# ---------------------------------------------------------------------------
# Rule definition
# ---------------------------------------------------------------------------

@dataclass
class ProfileRule:
    """A single automatic profile switching rule."""
    hostname_regex: str | None = None
    cwd_regex: str | None = None
    command_regex: str | None = None
    profile: str = ""

    # Compiled regexes (set after loading)
    hostname_re: re.Pattern | None = None
    cwd_re: re.Pattern | None = None
    command_re: re.Pattern | None = None

    def compile(self):
        """Compile regex patterns."""
        if self.hostname_regex:
            self.hostname_re = re.compile(self.hostname_regex)
        if self.cwd_regex:
            self.cwd_re = re.compile(self.cwd_regex)
        if self.command_regex:
            self.command_re = re.compile(self.command_regex)

    def matches(self, hostname: str, cwd: str, command: str) -> bool:
        """Check if this rule matches the given context."""
        if self.hostname_re and not self.hostname_re.search(hostname):
            return False
        if self.cwd_re and not self.cwd_re.search(cwd):
            return False
        if self.command_re and not self.command_re.search(command):
            return False
        return True


# ---------------------------------------------------------------------------
# Per-terminal state
# ---------------------------------------------------------------------------

@dataclass
class TerminalProfileState:
    """Tracks the profile state for one terminal."""
    original_profile: str
    current_profile: str
    applied_by_rule: str | None = None


class AutoProfileService:
    """Manages automatic profile switching for all terminals."""

    def __init__(self, window, rules: list[ProfileRule]):
        self._window = window
        self._rules = rules
        self._states: dict[int, TerminalProfileState] = {}
        self._poll_timer = QTimer()
        self._original_connect = None

    def _get_effective_hostname(self, terminal=None) -> str:
        """Return the contextually correct hostname for rule matching.

        If the terminal's foreground process is ``ssh``, parse its
        argv from ``/proc/<pid>/cmdline`` and return the destination
        host — that's what "automatic profile switching" needs to
        flag prod hosts. Falls back to ``socket.gethostname()`` for
        local activity.
        """
        if terminal is not None:
            try:
                fg = terminal.foreground_pid()
            except Exception:
                fg = 0
            if fg and fg > 0:
                argv = _read_proc_cmdline(fg)
                if argv:
                    host = parse_ssh_host(argv)
                    if host:
                        return host
        try:
            return socket.gethostname()
        except Exception:
            return ""

    def attach_terminal(self, terminal) -> None:
        """Attach to a terminal to watch for profile changes."""
        tid = id(terminal)
        if tid in self._states:
            return

        # Get current profile
        profile_name = getattr(terminal, "_profile_name", "default") or "default"

        self._states[tid] = TerminalProfileState(
            original_profile=profile_name,
            current_profile=profile_name,
        )

    def _on_command_finished(self, terminal, record) -> None:
        """Called when a command finishes - check rules."""
        tid = id(terminal)
        if tid not in self._states:
            return

        state = self._states[tid]

        # Get context
        hostname = self._get_effective_hostname(terminal)
        cwd = record.cwd or ""
        command = record.text or ""

        # Check each rule in order (first match wins)
        matched_rule = None
        for rule in self._rules:
            if rule.matches(hostname, cwd, command):
                matched_rule = rule
                break

        if matched_rule:
            target_profile = matched_rule.profile
            if state.current_profile != target_profile:
                try:
                    terminal.apply_profile(target_profile)
                    state.current_profile = target_profile
                    state.applied_by_rule = matched_rule.profile
                except Exception:
                    pass
        else:
            if state.applied_by_rule is not None:
                try:
                    terminal.apply_profile(state.original_profile)
                    state.current_profile = state.original_profile
                    state.applied_by_rule = None
                except Exception:
                    pass

    def check_rules(self, terminal) -> None:
        """Manually check rules for a terminal."""
        tid = id(terminal)
        if tid not in self._states:
            return

        state = self._states[tid]

        shell_int = getattr(self._window, "shell_integration", None)
        if shell_int is None:
            return

        history = shell_int.get_history(terminal)
        if history is None:
            return

        hostname = self._get_effective_hostname(terminal)
        cwd = history.cwd or ""

        command = ""
        if history.last and history.last.text:
            command = history.last.text

        matched_rule = None
        for rule in self._rules:
            if rule.matches(hostname, cwd, command):
                matched_rule = rule
                break

        if matched_rule:
            target_profile = matched_rule.profile
            if state.current_profile != target_profile:
                try:
                    terminal.apply_profile(target_profile)
                    state.current_profile = target_profile
                    state.applied_by_rule = matched_rule.profile
                except Exception:
                    pass
        else:
            if state.applied_by_rule is not None:
                try:
                    terminal.apply_profile(state.original_profile)
                    state.current_profile = state.original_profile
                    state.applied_by_rule = None
                except Exception:
                    pass

    def detach_terminal(self, terminal) -> None:
        """Detach from a terminal."""
        tid = id(terminal)
        state = self._states.pop(tid, None)

        if state and state.current_profile != state.original_profile:
            try:
                terminal.apply_profile(state.original_profile)
            except Exception:
                pass

    def start_polling(self):
        """Start polling for SSH changes."""
        cfg = Config()
        interval = cfg.get("plugins", "auto_profile", "poll_interval_ms", default=2000)

        self._poll_timer.timeout.connect(self._poll_all)
        self._poll_timer.start(interval)

    def _poll_all(self):
        """Check all terminals for rule matches.

        Safe against a destroyed C++ QTabWidget: if the window has
        been torn down without ``stop_polling`` running first, stop
        the timer and clear our window reference instead of crashing
        the event loop on every tick.
        """
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return
        try:
            count = tabs.count()
        except RuntimeError:
            self.stop_polling()
            self._window = None
            return

        for i in range(count):
            try:
                split = tabs.widget(i)
            except RuntimeError:
                self.stop_polling()
                return
            if split is None:
                continue
            for term in split.find_terminals():
                self.check_rules(term)

    def stop_polling(self):
        """Stop polling."""
        self._poll_timer.stop()


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class AutoProfilePlugin(Plugin):
    name = "auto_profile"
    description = (
        "Automatic profile switching based on hostname, cwd, or command. "
        "e.g., red background when SSH'd to production."
    )
    version = "0.1"
    capabilities = []

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: AutoProfileService | None = None
        self._rules: list[ProfileRule] = []
        self._shell_int_sub = None
        self._original_connect = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = cfg.get("plugins", "auto_profile", "enabled", default=False)
        if not enabled:
            return

        self._window = app_controller

        # Load rules from config
        self._load_rules(cfg)

        if not self._rules:
            return

        # Create service
        self._service = AutoProfileService(app_controller, self._rules)

        # Expose service
        if not hasattr(app_controller, "auto_profile"):
            app_controller.auto_profile = self._service

        # Attach existing terminals to our state map AND to
        # shell_integration. ``subscribe_command_finished`` only
        # fans out for terminals where ``ensure_attached`` has been
        # called — otherwise OSC 133;D is parsed for no one and our
        # global callback never fires.
        shell_int = getattr(app_controller, "shell_integration", None)
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is not None:
            for i in range(tabs.count()):
                split = tabs.widget(i)
                for term in split.find_terminals():
                    self._service.attach_terminal(term)
                    if shell_int is not None:
                        try:
                            shell_int.ensure_attached(term)
                        except Exception:
                            pass

        # Subscribe to shell_integration with the documented 2-arg
        # callback shape.
        if shell_int is not None:
            def global_callback(terminal, record):
                self._service._on_command_finished(terminal, record)
            shell_int.subscribe_command_finished(global_callback)
            self._shell_int_sub = global_callback

        # Wrap _connect_terminal so new terminals also attach + are
        # parsed by shell_integration.
        orig = getattr(app_controller, "_connect_terminal", None)
        if orig is not None:
            self._original_connect = orig
            _svc = self._service
            _shell_int = shell_int

            def wrapped(terminal, _orig=orig):
                _orig(terminal)
                try:
                    _svc.attach_terminal(terminal)
                except Exception:
                    pass
                if _shell_int is not None:
                    try:
                        _shell_int.ensure_attached(terminal)
                    except Exception:
                        pass
            app_controller._connect_terminal = wrapped

        # Start polling
        self._service.start_polling()

    def _load_rules(self, cfg: Config):
        """Load rules from config.

        A bad regex in one rule used to crash plugin activation
        wholesale; now we skip the offending rule and continue.
        """
        rules_list = cfg.get("plugins", "auto_profile", "rules", default=[])

        for rule_dict in rules_list:
            rule = ProfileRule(
                hostname_regex=rule_dict.get("hostname_regex"),
                cwd_regex=rule_dict.get("cwd_regex"),
                command_regex=rule_dict.get("command_regex"),
                profile=rule_dict.get("profile", ""),
            )
            try:
                rule.compile()
            except re.error:
                continue
            self._rules.append(rule)

    def deactivate(self):
        # Restore each terminal's original profile before tearing
        # state down — ``detach_terminal`` does the apply_profile()
        # back to ``state.original_profile``. Iterate live tabs;
        # ``_states`` keys are id(terminal) and we need the live
        # terminal object to call apply_profile on.
        if self._service is not None and self._window is not None:
            tabs = getattr(self._window, "_tabs", None)
            if tabs is not None:
                try:
                    count = tabs.count()
                except RuntimeError:
                    count = 0
                for i in range(count):
                    try:
                        split = tabs.widget(i)
                    except RuntimeError:
                        break
                    if split is None or not hasattr(split, "find_terminals"):
                        continue
                    for term in split.find_terminals():
                        try:
                            self._service.detach_terminal(term)
                        except Exception:
                            pass

        # Stop polling
        if self._service is not None:
            self._service.stop_polling()

        # Unsubscribe from shell_integration
        if self._shell_int_sub is not None:
            shell_int = getattr(self._window, "shell_integration", None)
            if shell_int is not None:
                try:
                    shell_int.unsubscribe_command_finished(self._shell_int_sub)
                except Exception:
                    pass
            self._shell_int_sub = None

        # Restore original _connect_terminal
        if self._original_connect is not None and self._window is not None:
            try:
                self._window._connect_terminal = self._original_connect
            except AttributeError:
                pass
        self._original_connect = None

        # Remove service from window
        if (self._window is not None
                and getattr(self._window, "auto_profile", None) is self._service):
            try:
                del self._window.auto_profile
            except AttributeError:
                pass

        self._service = None
        self._window = None
