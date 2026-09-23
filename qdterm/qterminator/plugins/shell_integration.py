"""shell_integration — parse OSC 133/7/8 into structured command history.

Reads the standard shell-integration escape sequences out of the PTY
byte stream and turns them into a per-tab `CommandHistory`: prompt
boundaries, command text (opt-in), exit status, working directory, and
hyperlink ranges. Exposes the result as
``app_controller.shell_integration`` for other plugins to consult, and
extends ``agent_control``'s ``list_tabs`` surface with
``cwd_reported`` + ``last_command``.

Sequences handled (industry-standard, emitted by oh-my-zsh, starship,
bash-preexec, fish 3.6+, kitty/iTerm2/vscode shell-integration):

  - OSC 133 ;A ST        prompt start
  - OSC 133 ;B ST        command start (between prompt and command)
  - OSC 133 ;C ST        command body finished, output begins
  - OSC 133 ;D[;exit] ST command finished
  - OSC 7  ;file://h/p ST current working directory
  - OSC 8  ;params;url ST .. OSC 8 ;; ST    hyperlink range

The output of every consumer that needs "where are the prompts / what
exit code did the last command return / what's the reported CWD" goes
through this one parser. Without it each consumer would re-walk the
byte stream; with it we share the single ShadowScreen subscription per
tab via :class:`ShadowScreenRegistry`.

Config (config.toml):

    [plugins.shell_integration]
    enabled = true               # default true — additive, ~zero idle cost
    capture_command_text = false # default false — typed input may include
                                 # pasted secrets; opt-in only
    history_limit = 200          # per-tab ring of completed commands

This plugin is purely a data feed. It does not render badges, popups,
or scrollback marks itself — those are jobs for downstream plugins
that read this service.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

from qterminator.config import Config
from qterminator.plugin import Plugin

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CommandRecord:
    """One completed command in a tab's history."""
    text: str | None              # None when capture_command_text=False
    exit_status: int | None       # None if the ;D event omitted the code
    started_at: float                # time.time() of ;B (command-start)
    finished_at: float               # time.time() of ;D (command-finish)
    cwd: str | None               # reported via OSC 7 at command start
    output_seq_range: tuple[int, int]  # ShadowScreen seq range [start,end]
    # Telemetry attached by the command_telemetry plugin after ;D; opaque
    # to shell_integration itself. ``None`` when the plugin isn't loaded.
    telemetry: dict | None = None


@dataclass
class CommandStartEvent:
    """Emitted when OSC 133 ;C arrives — the moment a command starts
    executing. Subscribers (e.g. command_telemetry) use it to stamp
    monotonic start times so duration is exact for short commands
    that would otherwise be invisible to a 2 s poller.

    ``text`` is the typed command line when ``capture_command_text``
    is on; otherwise None.
    """
    started_at: float                # time.time() at ;C
    started_at_monotonic: float      # time.monotonic() at ;C
    cwd: str | None
    text: str | None


@dataclass
class PendingCommand:
    """A command that has started but not yet finished."""
    text_chars: list[str] = field(default_factory=list)
    started_at: float | None = None
    output_started_at: float | None = None
    cwd: str | None = None
    output_start_seq: int | None = None


@dataclass
class HyperlinkRange:
    """OSC 8 hyperlink span — recorded as a chunk-seq range and URL.

    We don't track cell positions; the qtermwidget already renders the
    hyperlink markup. ``end_seq == start_seq`` while the link is still
    open (the closing OSC 8 ;; ST hasn't arrived yet).
    """
    start_seq: int
    end_seq: int
    url: str
    params: str = ""


class CommandHistory:
    """Per-tab structured state derived from OSC sequences.

    Mutated by :class:`OSCParser` on the Qt main thread; readers are
    expected to be on the same thread (every other plugin is)."""

    def __init__(self, limit: int = 200):
        self.cwd: str | None = None
        self.current_command: PendingCommand | None = None
        self.history: list[CommandRecord] = []
        self.hyperlinks: list[HyperlinkRange] = []
        # Open OSC-8 link, if any — closing ;; ST writes its end_seq.
        self._open_link: HyperlinkRange | None = None
        self._limit = max(1, int(limit))

    @property
    def last(self) -> CommandRecord | None:
        return self.history[-1] if self.history else None

    def _record(self, rec: CommandRecord) -> None:
        self.history.append(rec)
        if len(self.history) > self._limit:
            del self.history[: len(self.history) - self._limit]


# ---------------------------------------------------------------------------
# OSC parser
# ---------------------------------------------------------------------------

# OSC introducer: ESC ] (0x1b 0x5d).
# Terminator: BEL (0x07) or ESC \ (0x1b 0x5c).
# The body is everything between, no embedded BEL/ESC.
_OSC_RE = re.compile(rb"\x1b\](.*?)(?:\x07|\x1b\\)", re.DOTALL)


def _decode_file_uri(payload: str) -> str | None:
    """Turn ``file://host/path`` into a plain path. Returns None on junk."""
    try:
        u = urllib.parse.urlparse(payload)
    except ValueError:
        return None
    if u.scheme != "file":
        return None
    path = urllib.parse.unquote(u.path)
    return path or None


class OSCParser:
    """Stateful OSC-stream parser feeding one :class:`CommandHistory`.

    Plug into a :class:`ShadowScreen` via ``add_listener(parser.feed)``.
    Multi-chunk sequences are handled by a small carry-over buffer:
    bytes after the last terminator are remembered and prepended to the
    next chunk.

    ``on_command_finished`` is invoked once per completed command with
    the freshly-recorded :class:`CommandRecord`. Exceptions raised by a
    subscriber are swallowed — a misbehaving consumer must not poison
    the parser.
    """

    # Cap for the carry-over buffer. A real OSC sequence is short; if
    # we accumulate more than this without a terminator, we drop the
    # head — better than unbounded memory growth on a stream that emits
    # bare 0x1b ] with no follower.
    MAX_CARRY = 4096

    def __init__(self, history: CommandHistory,
                 capture_command_text: bool = False,
                 on_command_finished: Callable[[CommandRecord], None] | None = None,
                 on_command_started: Callable[[CommandStartEvent], None] | None = None):
        self._history = history
        self._capture_text = capture_command_text
        self._carry = b""
        self._subscribers: list[Callable[[CommandRecord], None]] = []
        # Parallel ;C subscriber list; fired the moment a command
        # starts executing, so consumers like command_telemetry can
        # snapshot a monotonic start time.
        self._started_subscribers: list[Callable[[CommandStartEvent], None]] = []
        if on_command_finished is not None:
            self._subscribers.append(on_command_finished)
        if on_command_started is not None:
            self._started_subscribers.append(on_command_started)

    @property
    def history(self) -> CommandHistory:
        return self._history

    def add_subscriber(self, cb: Callable[[CommandRecord], None]) -> None:
        self._subscribers.append(cb)

    def remove_subscriber(self, cb: Callable[[CommandRecord], None]) -> None:
        try:
            self._subscribers.remove(cb)
        except ValueError:
            pass

    def add_started_subscriber(self, cb: Callable[[CommandStartEvent], None]) -> None:
        self._started_subscribers.append(cb)

    def remove_started_subscriber(self, cb: Callable[[CommandStartEvent], None]) -> None:
        try:
            self._started_subscribers.remove(cb)
        except ValueError:
            pass

    def feed(self, seq: int, raw: bytes) -> None:
        """Listener entrypoint matching ``ShadowScreen.add_listener``."""
        buf = self._carry + raw
        last_end = 0
        for m in _OSC_RE.finditer(buf):
            self._handle_osc(seq, buf, m)
            last_end = m.end()
        # Tail bytes after the last complete OSC may contain the start
        # of another sequence — carry them over.
        tail = buf[last_end:]
        if len(tail) > self.MAX_CARRY:
            # Drop anything before the most recent ESC, if any, else
            # truncate from the head. Either way bound the buffer.
            esc = tail.rfind(b"\x1b")
            tail = tail[esc:] if esc >= 0 else tail[-self.MAX_CARRY:]
            tail = tail[-self.MAX_CARRY:]
        # Also capture command-text bytes (between ;B and ;C) if asked.
        if self._capture_text and self._history.current_command is not None:
            pending = self._history.current_command
            # The "command typed" phase is between ;B and ;C; we
            # capture only when output_start_seq is still None (i.e.
            # ;C hasn't been observed yet). Strip OSC subsequences so
            # only the visible characters remain.
            if pending.output_start_seq is None:
                visible = _OSC_RE.sub(b"", raw)
                try:
                    text = visible.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                # Stop capture at the first newline/CR — that's where
                # the shell will fire ;C.
                cut = min(
                    i for i in (text.find("\n"), text.find("\r"), len(text))
                    if i >= 0
                )
                pending.text_chars.append(text[:cut])
        self._carry = tail

    # -- per-OSC handling --

    def _handle_osc(self, seq: int, buf: bytes, m: re.Match) -> None:
        body = m.group(1)
        try:
            payload = body.decode("utf-8", errors="replace")
        except Exception:
            return
        if ";" not in payload:
            return
        code, _, rest = payload.partition(";")
        if code == "133":
            self._handle_133(seq, rest)
        elif code == "7":
            self._handle_7(rest)
        elif code == "8":
            self._handle_8(seq, rest)

    def _handle_133(self, seq: int, rest: str) -> None:
        head, _, tail = rest.partition(";")
        kind = head[:1].upper()
        h = self._history
        if kind == "A":
            # Prompt start — close out any orphan pending command.
            h.current_command = None
        elif kind == "B":
            # Command start. ;B may be missing if the shell only emits
            # ;A and ;D — tolerate that by initialising on ;C too.
            h.current_command = PendingCommand(
                started_at=time.time(),
                cwd=h.cwd,
            )
        elif kind == "C":
            # Command body finished, output begins — i.e. the command
            # is now actually executing. We use this as the canonical
            # "command started" event for consumers like
            # command_telemetry; ;B fires when the prompt is drawn but
            # before the user has pressed Enter, which would inflate
            # duration by typing time.
            if h.current_command is None:
                h.current_command = PendingCommand(
                    started_at=time.time(),
                    cwd=h.cwd,
                )
            h.current_command.output_started_at = time.time()
            h.current_command.output_start_seq = seq
            if self._started_subscribers:
                text: str | None = None
                if self._capture_text and h.current_command.text_chars:
                    text = "".join(h.current_command.text_chars).strip("\r\n")
                    if not text:
                        text = None
                ev = CommandStartEvent(
                    started_at=h.current_command.output_started_at,
                    started_at_monotonic=time.monotonic(),
                    cwd=h.current_command.cwd,
                    text=text,
                )
                for cb in list(self._started_subscribers):
                    try:
                        cb(ev)
                    except Exception:
                        pass
        elif kind == "D":
            exit_code: int | None = None
            if tail:
                try:
                    exit_code = int(tail.split(";")[0])
                except ValueError:
                    exit_code = None
            pending = h.current_command or PendingCommand(started_at=time.time())
            text: str | None = None
            if self._capture_text and pending.text_chars:
                text = "".join(pending.text_chars).strip("\r\n")
                if not text:
                    text = None
            output_range = (
                pending.output_start_seq if pending.output_start_seq is not None else seq,
                seq,
            )
            rec = CommandRecord(
                text=text,
                exit_status=exit_code,
                started_at=pending.started_at or time.time(),
                finished_at=time.time(),
                cwd=pending.cwd,
                output_seq_range=output_range,
            )
            h._record(rec)
            h.current_command = None
            for cb in list(self._subscribers):
                try:
                    cb(rec)
                except Exception:
                    pass

    def _handle_7(self, rest: str) -> None:
        cwd = _decode_file_uri(rest)
        if cwd:
            self._history.cwd = cwd
            # If a command is being typed, retroactively update its cwd
            # so the resulting record reports the directory the user
            # is in *now*, not the one they had at ;A.
            if self._history.current_command is not None:
                self._history.current_command.cwd = cwd

    def _handle_8(self, seq: int, rest: str) -> None:
        # OSC 8 payload: <params>;<url>. Closing form has empty params
        # AND empty url ("OSC 8 ;; ST"); rest will be ";".
        params, _, url = rest.partition(";")
        h = self._history
        if url == "" and params == "":
            # Close any open link span.
            if h._open_link is not None:
                h._open_link.end_seq = seq
                h._open_link = None
            return
        link = HyperlinkRange(
            start_seq=seq, end_seq=seq, url=url, params=params,
        )
        h.hyperlinks.append(link)
        h._open_link = link


# ---------------------------------------------------------------------------
# Service exposed on the MainWindow
# ---------------------------------------------------------------------------

class ShellIntegrationService:
    """Per-tab parser registry. ``ensure_attached(terminal)`` is
    idempotent — call it from anywhere that wants to read history."""

    def __init__(self, registry, capture_command_text: bool = False,
                 history_limit: int = 200):
        self._registry = registry
        self._capture_text = capture_command_text
        self._limit = history_limit
        # id(terminal) -> (handle, parser, history)
        self._states: dict[int, tuple] = {}
        # Global ;D subscribers; cb(terminal, CommandRecord).
        self._global_subs: list[Callable] = []
        # Global ;C subscribers; cb(terminal, CommandStartEvent).
        self._global_started_subs: list[Callable] = []

    @property
    def capture_command_text(self) -> bool:
        return self._capture_text

    def ensure_attached(self, terminal) -> CommandHistory:
        tid = id(terminal)
        state = self._states.get(tid)
        if state is not None:
            return state[2]
        handle = self._registry.acquire(terminal)
        history = CommandHistory(limit=self._limit)

        # Fan global subscribers out through closures that include
        # the terminal — both ;C and ;D subscribers usually want to
        # know which tab fired.
        def _on_finished(rec: CommandRecord, term=terminal):
            for cb in list(self._global_subs):
                try:
                    cb(term, rec)
                except Exception:
                    pass

        def _on_started(ev: CommandStartEvent, term=terminal):
            for cb in list(self._global_started_subs):
                try:
                    cb(term, ev)
                except Exception:
                    pass

        parser = OSCParser(
            history=history,
            capture_command_text=self._capture_text,
            on_command_finished=_on_finished,
            on_command_started=_on_started,
        )
        handle.add_listener(parser.feed)
        self._states[tid] = (handle, parser, history)
        return history

    def detach(self, terminal) -> None:
        tid = id(terminal)
        state = self._states.pop(tid, None)
        if state is None:
            return
        handle, parser, _hist = state
        try:
            handle.remove_listener(parser.feed)
        finally:
            handle.release()

    def detach_all(self) -> None:
        for terminal_id in list(self._states.keys()):
            _, parser, _hist = self._states[terminal_id]
            handle = self._states[terminal_id][0]
            try:
                handle.remove_listener(parser.feed)
            finally:
                handle.release()
            self._states.pop(terminal_id, None)

    def get_history(self, terminal) -> CommandHistory | None:
        state = self._states.get(id(terminal))
        return state[2] if state else None

    def get_or_create_history(self, terminal) -> CommandHistory:
        return self.ensure_attached(terminal)

    def subscribe_command_finished(self,
                                   cb: Callable) -> None:
        """Subscribe to every command-finished event across every tab
        the service has attached to. Callback signature:
        ``cb(terminal, CommandRecord)``. Idempotent attachment is the
        caller's responsibility — calling ``ensure_attached(terminal)``
        before issuing the subscription guarantees coverage on a
        specific tab."""
        if cb not in self._global_subs:
            self._global_subs.append(cb)

    def unsubscribe_command_finished(self, cb: Callable) -> None:
        try:
            self._global_subs.remove(cb)
        except ValueError:
            pass

    def subscribe_command_started(self, cb: Callable) -> None:
        """Subscribe to every command-started (OSC 133 ;C) event across
        every tab the service has attached to. Callback signature:
        ``cb(terminal, CommandStartEvent)``.

        Paired with ``subscribe_command_finished`` for consumers
        (e.g. command_telemetry) that need exact duration on commands
        shorter than any polling interval.
        """
        if cb not in self._global_started_subs:
            self._global_started_subs.append(cb)

    def unsubscribe_command_started(self, cb: Callable) -> None:
        try:
            self._global_started_subs.remove(cb)
        except ValueError:
            pass

    # -- agent_control surface helpers --

    def serialize_last_command(self, terminal) -> dict | None:
        """Return ``last_command`` dict for ``agent_control.list_tabs``.
        Returns None if no commands have completed on this tab yet."""
        hist = self.get_history(terminal)
        if hist is None or not hist.history:
            return None
        rec = hist.history[-1]
        out = {
            "text": rec.text,
            "exit_status": rec.exit_status,
            "started_at": rec.started_at,
            "finished_at": rec.finished_at,
            "cwd": rec.cwd,
        }
        if rec.telemetry is not None:
            out["telemetry"] = rec.telemetry
        return out

    def serialize_history(self, terminal, limit: int = 50) -> list[dict]:
        hist = self.get_history(terminal)
        if hist is None:
            return []
        recs = hist.history[-limit:] if limit > 0 else list(hist.history)
        out = []
        for r in recs:
            entry = {
                "text": r.text,
                "exit_status": r.exit_status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "cwd": r.cwd,
                "output_seq_range": list(r.output_seq_range),
            }
            if r.telemetry is not None:
                entry["telemetry"] = r.telemetry
            out.append(entry)
        return out


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class ShellIntegrationPlugin(Plugin):
    name = "shell_integration"
    description = (
        "Parse OSC 133/7/8 escape sequences into a structured command "
        "history; expose it as a service for other plugins."
    )
    version = "0.1"
    capabilities = ["shell_integration"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: ShellIntegrationService | None = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "shell_integration", "enabled", default=True,
        ))
        if not enabled:
            return
        registry = getattr(app_controller, "shadow_screens", None)
        if registry is None:
            raise RuntimeError(
                "shell_integration requires MainWindow.shadow_screens"
            )
        capture_text = bool(cfg.get(
            "plugins", "shell_integration", "capture_command_text",
            default=False,
        ))
        limit = int(cfg.get(
            "plugins", "shell_integration", "history_limit", default=200,
        ))
        self._service = ShellIntegrationService(
            registry, capture_command_text=capture_text,
            history_limit=limit,
        )
        self._window = app_controller
        if not hasattr(app_controller, "shell_integration"):
            app_controller.shell_integration = self._service

    def deactivate(self):
        if self._service is not None:
            self._service.detach_all()
        if (self._window is not None
                and getattr(self._window, "shell_integration", None) is self._service):
            try:
                del self._window.shell_integration
            except AttributeError:
                pass
        self._service = None
