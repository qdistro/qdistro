"""triggers — config-driven (pattern, action) rules over terminal output.

iTerm2's most-loved feature, distilled. The user declares rules in
``config.toml``; we run each rule's regex over the byte stream of
every terminal and fire the rule's action when it matches:

    [[plugins.triggers.rules]]
    pattern = '\\b(ERROR|FATAL)\\b'
    action = "notify"
    message = "{tab_title}: {match}"
    cooldown = 5

    [[plugins.triggers.rules]]
    pattern = 'password:\\s*$'
    action = "run_command"
    command = ["pass", "show", "{cwd}/secret"]
    pipe_to_terminal = true

Built-in actions: ``notify``, ``send_text``, ``set_tab_color``,
``run_command``, ``capture``, ``ring_bell``. A consumer plugin can
register additional actions via
``app_controller.triggers.register_action(name, callable)``.

Why this exists:
``output_monitors.py``'s built-in monitors each hand-code one or two
specific patterns. A generic, config-driven trigger system supersedes
the common cases; users add rules without editing Python. Future work
will ship those monitors as default rules.

Design notes:

- Output capture lives on :class:`ShadowScreenRegistry`. We acquire
  one handle per terminal, attach one listener, run all rules over
  each chunk. Multi-chunk patterns work via a per-terminal carry
  buffer (last ``CARRY_BYTES`` bytes from the previous chunks).
- Rules are compiled once at activate. A bad pattern is logged and
  skipped — one busted rule must not kill the others.
- Cooldown is per (rule_index, terminal_id). Re-firing the same rule
  on the same tab within ``cooldown`` seconds is suppressed (the
  most common "notification storm" failure mode).
- We hook :class:`MainWindow._connect_terminal` so every new terminal
  is observed automatically — the user shouldn't have to call any
  API for a config rule to start working.
- Each match emits a ``TriggerEvent`` to every ``subscribe(cb)``
  consumer. ``agent_event_channel`` (later in the queue) reads this
  to forward matches to attached agents.
"""

import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtGui import QColor

from qterminator.config import Config
from qterminator.plugin import Plugin

log = logging.getLogger("qterminator.triggers")


# ---------------------------------------------------------------------------
# Rule / event types
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    index: int
    pattern: re.Pattern
    action: str
    options: dict
    cooldown: float = 0.0

    @property
    def name(self) -> str:
        return self.options.get("name") or f"rule#{self.index}"


@dataclass
class TriggerEvent:
    """One firing of a rule. Subscribers get this; consumers serialize
    it for downstream tools (agent_event_channel, MCP)."""
    rule_index: int
    rule_name: str
    action: str
    terminal: object  # TerminalWidget — opaque to subscribers
    text: str            # The substring that matched.
    groups: dict         # Named groups + 'match'.
    fired_at: float

    def to_dict(self) -> dict:
        return {
            "rule_index": self.rule_index,
            "rule_name": self.rule_name,
            "action": self.action,
            "match": self.text,
            "groups": self.groups,
            "fired_at": self.fired_at,
            "tab_id": id(self.terminal),
        }


# ---------------------------------------------------------------------------
# Per-terminal listener state
# ---------------------------------------------------------------------------

class _TerminalState:
    """Holds the shadow handle, listener, carry buffer, and per-rule
    cooldown timestamps for one observed terminal."""

    CARRY_BYTES = 1024  # last N bytes kept to bridge chunk boundaries

    def __init__(self, terminal, handle, listener):
        self.terminal = terminal
        self.handle = handle
        self.listener = listener
        self.carry = b""
        # rule_index -> monotonic time of last fire
        self.last_fired: dict[int, float] = {}

    def feed_text(self, raw: bytes) -> str:
        """Append ``raw`` to the carry buffer and return the slice
        that should be scanned by all rules (carry + new). Then trim
        carry to the tail."""
        combined = self.carry + raw
        self.carry = combined[-self.CARRY_BYTES:]
        return combined.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Built-in actions
# ---------------------------------------------------------------------------

def _resolve_template(template: str, context: dict) -> str:
    """Minimal {var} substitution that tolerates missing keys."""
    out = []
    i = 0
    while i < len(template):
        c = template[i]
        if c == "{":
            end = template.find("}", i + 1)
            if end == -1:
                out.append(template[i:])
                break
            key = template[i + 1:end]
            out.append(str(context.get(key, "{" + key + "}")))
            i = end + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _action_notify(service: "TriggersService", rule: Rule,
                   event: TriggerEvent) -> None:
    tab_title = getattr(event.terminal, "title", lambda: "")()
    ctx = {"tab_title": tab_title, **event.groups}
    msg = _resolve_template(rule.options.get("message", "{match}"), ctx)
    title = rule.options.get("title", "QTerminator")
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.Popen(
            ["notify-send", title, msg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        pass


def _action_send_text(service: "TriggersService", rule: Rule,
                      event: TriggerEvent) -> None:
    template = rule.options.get("text", "")
    if not template:
        return
    tab_title = getattr(event.terminal, "title", lambda: "")()
    ctx = {"tab_title": tab_title, **event.groups}
    txt = _resolve_template(template, ctx)
    try:
        event.terminal.send_text(txt)
    except Exception:
        log.exception("triggers: send_text failed for rule %s", rule.name)


def _action_set_tab_color(service: "TriggersService", rule: Rule,
                          event: TriggerEvent) -> None:
    color = rule.options.get("color")
    if not color:
        return
    window = service._window
    if window is None:
        return
    tabs = getattr(window, "_tabs", None)
    if tabs is None:
        return
    # Locate which tab index hosts this terminal.
    for i in range(tabs.count()):
        split = tabs.widget(i)
        if event.terminal in split.find_terminals():
            try:
                tabs.tabBar().setTabTextColor(i, QColor(color))
            except Exception:
                log.exception("triggers: setTabTextColor failed")
            return


def _action_run_command(service: "TriggersService", rule: Rule,
                        event: TriggerEvent) -> None:
    cmd = rule.options.get("command")
    if not cmd or not isinstance(cmd, list):
        return
    tab_title = getattr(event.terminal, "title", lambda: "")()
    cwd_reported = None
    shell_int = getattr(service._window, "shell_integration", None)
    if shell_int is not None:
        try:
            hist = shell_int.get_history(event.terminal)
            cwd_reported = hist.cwd if hist else None
        except Exception:
            cwd_reported = None
    ctx = {
        "tab_title": tab_title,
        "cwd": cwd_reported or "",
        **event.groups,
    }
    resolved = [_resolve_template(arg, ctx) for arg in cmd]
    pipe = bool(rule.options.get("pipe_to_terminal"))
    try:
        if pipe:
            proc = subprocess.run(
                resolved, capture_output=True, text=True, timeout=10,
            )
            out = proc.stdout
            if out:
                try:
                    event.terminal.send_text(out)
                except Exception:
                    pass
        else:
            subprocess.Popen(
                resolved,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        log.exception("triggers: run_command failed for rule %s", rule.name)


def _action_capture(service: "TriggersService", rule: Rule,
                    event: TriggerEvent) -> None:
    sidebar = rule.options.get("sidebar", "Captured")
    service._sidebars.setdefault(sidebar, []).append({
        "match": event.text,
        "groups": event.groups,
        "tab_id": id(event.terminal),
        "fired_at": event.fired_at,
    })
    # Cap each sidebar to avoid unbounded growth.
    cap = int(rule.options.get("sidebar_limit", 1000))
    bucket = service._sidebars[sidebar]
    if len(bucket) > cap:
        del bucket[: len(bucket) - cap]


def _action_ring_bell(service: "TriggersService", rule: Rule,
                      event: TriggerEvent) -> None:
    try:
        # Route through TerminalWidget.send_text so the bell byte is suppressed
        # on read-only panes (writing it goes back into the PTY, same as any
        # shell-injected input).
        event.terminal.send_text("\a")
    except Exception:
        pass


BUILTIN_ACTIONS: dict[str, Callable] = {
    "notify": _action_notify,
    "send_text": _action_send_text,
    "set_tab_color": _action_set_tab_color,
    "run_command": _action_run_command,
    "capture": _action_capture,
    "ring_bell": _action_ring_bell,
}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class TriggersService:
    """Per-window registry of rules, attached terminals, actions, and
    subscribers. Exposed via ``app_controller.triggers``."""

    def __init__(self, window, rules: list[Rule]):
        self._window = window
        self._rules = rules
        self._registry = window.shadow_screens
        self._states: dict[int, _TerminalState] = {}  # id(terminal) -> state
        self._actions: dict[str, Callable] = dict(BUILTIN_ACTIONS)
        self._subscribers: list[Callable[[TriggerEvent], None]] = []
        self._sidebars: dict[str, list[dict]] = {}

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    @property
    def attached_terminals(self) -> list:
        return [state.terminal for state in self._states.values()]

    # -- actions / subscribers --

    def register_action(self, name: str, fn: Callable) -> None:
        if not callable(fn):
            raise TypeError("action must be callable")
        self._actions[name] = fn

    def unregister_action(self, name: str) -> None:
        # Refuse to drop built-ins so user plugins can't silently
        # disable e.g. ``notify``.
        if name in BUILTIN_ACTIONS:
            raise ValueError(f"cannot unregister built-in action: {name!r}")
        self._actions.pop(name, None)

    def subscribe(self, cb: Callable[[TriggerEvent], None]) -> None:
        if cb not in self._subscribers:
            self._subscribers.append(cb)

    def unsubscribe(self, cb) -> None:
        try:
            self._subscribers.remove(cb)
        except ValueError:
            pass

    # -- sidebar buckets --

    def sidebar(self, name: str) -> list[dict]:
        return list(self._sidebars.get(name, []))

    def sidebars(self) -> dict[str, list[dict]]:
        return {k: list(v) for k, v in self._sidebars.items()}

    def clear_sidebar(self, name: str) -> None:
        self._sidebars.pop(name, None)

    # -- terminal lifecycle --

    def attach(self, terminal) -> None:
        tid = id(terminal)
        if tid in self._states:
            return
        if not self._rules:
            return  # no rules → don't even pay the shadow refcount
        handle = self._registry.acquire(terminal)
        state = _TerminalState(terminal, handle, listener=None)

        def listener(seq: int, raw: bytes, t=terminal):
            self._on_data(t, seq, raw)
        state.listener = listener
        handle.add_listener(listener)
        self._states[tid] = state

    def detach(self, terminal) -> None:
        tid = id(terminal)
        state = self._states.pop(tid, None)
        if state is None:
            return
        try:
            state.handle.remove_listener(state.listener)
        finally:
            state.handle.release()

    def detach_all(self) -> None:
        for tid in list(self._states.keys()):
            state = self._states[tid]
            try:
                state.handle.remove_listener(state.listener)
            finally:
                state.handle.release()
            self._states.pop(tid, None)

    # -- input path --

    def _on_data(self, terminal, _seq: int, raw: bytes) -> None:
        state = self._states.get(id(terminal))
        if state is None:
            return
        text = state.feed_text(raw)
        if not text:
            return
        now = time.monotonic()
        for rule in self._rules:
            self._scan_rule(state, rule, text, now)

    def _scan_rule(self, state: _TerminalState, rule: Rule,
                   text: str, now: float) -> None:
        last = state.last_fired.get(rule.index, 0.0)
        for m in rule.pattern.finditer(text):
            # Cooldown gate: count fires only when allowed.
            if rule.cooldown > 0 and (now - last) < rule.cooldown:
                continue
            groups = {"match": m.group(0)}
            for k, v in (m.groupdict() or {}).items():
                if v is not None:
                    groups[k] = v
            # Bare-number groups by index too (1-based) for convenience.
            for i, g in enumerate(m.groups() or (), start=1):
                if g is not None:
                    groups[str(i)] = g
            event = TriggerEvent(
                rule_index=rule.index,
                rule_name=rule.name,
                action=rule.action,
                terminal=state.terminal,
                text=m.group(0),
                groups=groups,
                fired_at=time.time(),
            )
            state.last_fired[rule.index] = now
            last = now
            self._dispatch(rule, event)

    def _dispatch(self, rule: Rule, event: TriggerEvent) -> None:
        fn = self._actions.get(rule.action)
        if fn is not None:
            try:
                fn(self, rule, event)
            except Exception:
                log.exception(
                    "triggers: action %r failed for rule %s",
                    rule.action, rule.name,
                )
        else:
            log.warning(
                "triggers: unknown action %r for rule %s",
                rule.action, rule.name,
            )
        for sub in list(self._subscribers):
            try:
                sub(event)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def load_rules(raw_rules: list) -> list[Rule]:
    """Compile config rules. Bad rules are skipped with a log line so
    a single bad entry doesn't kill the others."""
    rules: list[Rule] = []
    if not isinstance(raw_rules, list):
        return rules
    for i, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            continue
        pattern_src = raw.get("pattern")
        action = raw.get("action")
        if not pattern_src or not action:
            log.warning("triggers: rule %d missing pattern/action — skipped", i)
            continue
        flags = 0
        if raw.get("ignore_case"):
            flags |= re.IGNORECASE
        if raw.get("multiline"):
            flags |= re.MULTILINE
        try:
            compiled = re.compile(pattern_src, flags)
        except re.error as e:
            log.warning("triggers: rule %d bad regex %r: %s — skipped",
                        i, pattern_src, e)
            continue
        rules.append(Rule(
            index=i,
            pattern=compiled,
            action=str(action),
            options={k: v for k, v in raw.items()
                     if k not in {"pattern", "action"}},
            cooldown=float(raw.get("cooldown", 0)),
        ))
    return rules


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class TriggersPlugin(Plugin):
    name = "triggers"
    description = (
        "Config-driven regex→action rules over terminal output. "
        "Built-in actions: notify, send_text, set_tab_color, "
        "run_command, capture, ring_bell."
    )
    version = "0.1"
    capabilities = ["triggers"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: TriggersService | None = None
        self._original_connect = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "triggers", "enabled", default=True,
        ))
        if not enabled:
            return
        registry = getattr(app_controller, "shadow_screens", None)
        if registry is None:
            raise RuntimeError("triggers requires MainWindow.shadow_screens")
        raw_rules = cfg.get("plugins", "triggers", "rules", default=[]) or []
        rules = load_rules(raw_rules)
        self._window = app_controller
        self._service = TriggersService(app_controller, rules)
        if not hasattr(app_controller, "triggers"):
            app_controller.triggers = self._service
        # Attach to any pre-existing terminals (none at first activate,
        # but a layout-restore path could have spun some up already).
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is not None:
            for i in range(tabs.count()):
                split = tabs.widget(i)
                for t in split.find_terminals():
                    self._service.attach(t)
        # Wrap _connect_terminal so every new terminal is attached.
        orig = getattr(app_controller, "_connect_terminal", None)
        if orig is not None:
            self._original_connect = orig

            def wrapped(terminal, _orig=orig, _svc=self._service):
                _orig(terminal)
                try:
                    _svc.attach(terminal)
                except Exception:
                    log.exception("triggers: failed to attach new terminal")
            app_controller._connect_terminal = wrapped

    def deactivate(self):
        if self._service is not None:
            self._service.detach_all()
        if (self._window is not None
                and getattr(self._window, "triggers", None) is self._service):
            try:
                del self._window.triggers
            except AttributeError:
                pass
        if self._original_connect is not None and self._window is not None:
            try:
                self._window._connect_terminal = self._original_connect
            except AttributeError:
                pass
        self._original_connect = None
        self._service = None
