#!/usr/bin/env python3
"""qdistro admin TUI — terminal companion to the PyQt admin app.

Same broker, same scope picker, same approve/deny semantics. Designed
to be runnable on any TTY (incl. greetd's tuigreet drop-shell, ssh,
or a serial console) — no compositor required.

See  for the playbook + acceptance.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly from the source tree as well as installed.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import DataTable, Footer, Header, Static  # noqa: E402

from broker_client import BrokerClient, DBusBrokerClient, Request  # noqa: E402
from silo_colors import chip_for_uid  # noqa: E402

# Same vocabulary as the GUI / broker. Dict for O(1) label lookup that
# can't raise StopIteration if a stale key sneaks in.
# task(072): argv-aware scopes match broker._VALID_SCOPES + cache
# _SCOPE_MAP. The TUI key bindings below map 1..8 onto these in order.
SCOPES: dict[str, str] = {
    "once":             "Just this once",
    "1h":               "1 hour",
    "24h":              "24 hours",
    "forever":          "Forever, any command",
    "forever_exe":      "Forever, only this exact program",
    "forever_argv":     "Forever, only this exact argv tuple",
    "forever_basename": "Forever, this argv basename anywhere",
    "forever_prefix":   "Forever, this argv prefix + any trailing args",
}
SCOPE_KEYS: list[str] = list(SCOPES.keys())  # for Pilot tests + ordering


def _split_argv_from_details(
    details: dict[str, str] | None,
) -> tuple[str | None, dict[str, str]]:
    """Pull `argv[NN]` keys out of a request details dict and return
    (shlex-joined argv, remaining-details). Used to render qsu prompts
    cleanly: instead of 30+ `argv[00]=...` rows in Details, the argv
    gets its own labelled line and Details is left for source/dest/etc.

    Returns (None, details-as-passed) when no `argv[NN]` keys are
    present (clipboard / handoff / generic permission). Mirrors the
    broker's `_argv_from_details` and the admin-app's
    `reconstruct_argv_from_details`.
    """
    import re as _re
    import shlex as _shlex
    if not details:
        return (None, dict(details or {}))
    pat = _re.compile(r"^argv\[(\d{2,4})\]$")
    indexed: list[tuple[int, str]] = []
    other: dict[str, str] = {}
    for k, v in dict(details).items():
        m = pat.match(str(k))
        if m is None:
            other[str(k)] = str(v)
            continue
        idx = int(m.group(1))
        if idx > 1024:
            continue
        indexed.append((idx, str(v)))
    if not indexed:
        return (None, other)
    indexed.sort(key=lambda kv: kv[0])
    argv_list = [v for _, v in indexed]
    return (_shlex.join(argv_list), other)


class HelpScreen(ModalScreen):
    """Press ? to bring this up; any key dismisses."""

    BINDINGS = [Binding("escape,q,question_mark,space,enter", "dismiss",
                       "Close", show=False)]

    HELP = """\
[b]qdistro admin TUI[/b]

[b]Decide:[/b]
  a / Ctrl+Y      Approve current request (with active scope)
  d / Ctrl+N      Deny current request

[b]Scope:[/b]
  1               Just this once   (default; no cache write)
  2               1 hour
  3               24 hours
  4               Forever, any command from this user
  5               Forever, only this exact program

[b]Navigation:[/b]
  ↑ / ↓           Move between pending requests
  r               Refresh queue from broker
  ?               This help
  q               Quit

Decisions are mirrored to the GUI app instantly via the broker's
RequestDecided signal. Both surfaces stay in sync.
"""

    def compose(self) -> ComposeResult:
        yield Static(self.HELP, id="help_body", classes="help_body")

    def action_dismiss(self) -> None:
        self.app.pop_screen()


class DetailPane(Static):
    """Right pane: details of the currently-selected request."""

    DEFAULT_CSS = """
    DetailPane {
        padding: 1 2;
        border: round $primary;
    }
    """

    def render_request(self, req: Request | None, scope_key: str) -> None:
        if req is None:
            self.update("[dim](no request selected)[/dim]")
            return
        scope_label = SCOPES.get(scope_key, scope_key)
        argv_line, other_details = _split_argv_from_details(req.details)
        details = ", ".join(
            f"{k}={v}" for k, v in other_details.items()
        ) or "(none)"
        chip = chip_for_uid(req.uid)
        text = (
            f"[b][on {chip}] {req.uid} [/on {chip}]  pid={req.pid}[/b]\n"
            f"Action: {req.action}\n"
            f"[dim]{req.exe}[/dim]\n"
        )
        if argv_line is not None:
            # qsu / spec/21 — show argv as its own line with shlex.join,
            # not as 30+ noisy `argv[NN]=...` entries inside Details.
            text += f"Argv: [b]{argv_line}[/b]\n"
        text += (
            f"Details: {details}\n\n"
            f"Scope: [b]{scope_label}[/b]   "
            f"[dim](press 1-5 to change)[/dim]\n\n"
            f"[green]a[/green] approve   [red]d[/red] deny   [yellow]?[/yellow] help"
        )
        self.update(text)


class QueueTable(DataTable):
    """Left pane: the pending request queue."""

    DEFAULT_CSS = """
    QueueTable {
        border: round $primary;
        height: 100%;
    }
    """


class AdminTuiApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body  { height: 1fr; }
    QueueTable { width: 40%; }
    DetailPane { width: 1fr; }
    .help_body { padding: 1 2; border: round $accent; }
    """

    TITLE = "qdistro admin approvals (TUI)"

    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("a,ctrl+y", "approve", "Approve"),
        Binding("d,ctrl+n", "deny", "Deny"),
        Binding("ctrl+r", "create_rule_from_current", "Create Rule"),
        Binding("alt+a", "approve_all", "Approve All"),
        Binding("alt+d", "deny_all", "Deny All"),
        Binding("1", "scope('once')", "1:once"),
        Binding("2", "scope('1h')", "2:1h"),
        Binding("3", "scope('24h')", "3:24h"),
        Binding("4", "scope('forever')", "4:forever-any"),
        Binding("5", "scope('forever_exe')", "5:forever-exe"),
        # task(072): argv-aware Forever scopes for qsu prompts.
        Binding("6", "scope('forever_argv')", "6:forever-argv"),
        Binding("7", "scope('forever_basename')", "7:forever-base"),
        Binding("8", "scope('forever_prefix')", "8:forever-prefix"),
        # Scope shortcuts (Ctrl+Shift+1..8)
        Binding("ctrl+shift+1", "scope('once')", "Ctrl+Shift+1:once"),
        Binding("ctrl+shift+2", "scope('1h')", "Ctrl+Shift+2:1h"),
        Binding("ctrl+shift+3", "scope('24h')", "Ctrl+Shift+3:24h"),
        Binding("ctrl+shift+4", "scope('forever')", "Ctrl+Shift+4:forever-any"),
        Binding("ctrl+shift+5", "scope('forever_exe')", "Ctrl+Shift+5:forever-exe"),
        Binding("ctrl+shift+6", "scope('forever_argv')", "Ctrl+Shift+6:forever-argv"),
        Binding("ctrl+shift+7", "scope('forever_basename')", "Ctrl+Shift+7:forever-base"),
        Binding("ctrl+shift+8", "scope('forever_prefix')", "Ctrl+Shift+8:forever-prefix"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    # How often to poll the broker as a backstop against missed signals.
    # Signals are the primary refresh trigger; the timer is purely a
    # safety net for broker restarts (see ).
    POLL_INTERVAL_S = 30.0

    def __init__(self, broker: BrokerClient | None = None):
        super().__init__()
        # Allow injection for tests; default to the real D-Bus client
        self._broker: BrokerClient = broker if broker is not None else DBusBrokerClient(self)
        self._scope: str = "once"
        # Local mirror of the queue, indexed by rid. Avoids per-keypress
        # broker round-trips; refreshed only on signals + the safety
        # poll + after explicit `r`.
        self._pending: dict[int, Request] = {}
        # Ordered list of rids in display order; row index -> rid.
        self._row_to_id: list[int] = []
        self._broker_started = False
        # Sticky error state — set by refresh_queue on get_pending failure,
        # cleared on next successful refresh. Drives the BROKER OFFLINE
        # subtitle override so a transient toast disappearing doesn't
        # leave the user looking at a falsely-healthy chrome.
        self._broker_error: str | None = None

    # -- Composition --
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield QueueTable(id="queue", cursor_type="row")
            yield DetailPane(id="detail")
        yield Footer()

    # -- Lifecycle --
    def on_mount(self) -> None:
        table = self.query_one("#queue", QueueTable)
        # Leading cursor glyph + status column per spec/25 Queue item.
        # Cursor glyph `▶` is a redundant cue for the selected row; the
        # default Textual row-highlight relies on background tint which
        # disappears in 16-color / monochrome terminals and in plain-text
        # captures. Status is Phase-1-minimal: every pending request is
        # `●` (new). Phase 2 will use `○` (in-review) / `⌛` (waiting on
        # phone) as those states land in the broker payload.
        table.add_columns("", "", "uid", "action", "exe")
        try:
            self._broker.start(self._on_pending, self._on_decided)
            self._broker_started = True
        except Exception as e:  # noqa: BLE001
            # Bad/missing broker: render an in-app error instead of
            # bubbling a traceback up to the user.
            self.notify(f"broker unavailable: {e}", severity="error",
                        timeout=10)
            return
        self.refresh_queue()
        self.set_interval(self.POLL_INTERVAL_S, self.refresh_queue)

    def on_unmount(self) -> None:
        if self._broker_started:
            self._broker.stop()
            self._broker_started = False

    # -- Broker signal handlers --
    def _on_pending(self, _rid: int) -> None:
        # Audible cue so the admin doesn't need eyes on the TUI. Textual
        # writes BEL (\a); most terminals map that to an audible beep
        # and/or visual flash (visualbell). Urgency-gating (skip bell
        # for low-urgency requests) waits on broker payload support.
        self.bell()
        self.refresh_queue()

    def _on_decided(self, rid: int, _decision: str) -> None:
        # Cheap path: if we know about this rid, remove it locally and
        # update the table without a broker round-trip. The next poll
        # will reconcile if the broker disagrees.
        if rid in self._pending:
            del self._pending[rid]
            self._rebuild_table()
        else:
            self.refresh_queue()

    # -- Refresh / table --
    def refresh_queue(self) -> None:
        try:
            pending = self._broker.get_pending()
        except Exception as e:  # noqa: BLE001
            # Stale state would silently mis-target subsequent decides;
            # clear so user sees an empty table. The sticky _broker_error
            # drives a persistent subtitle override (toast alone fades
            # after timeout, leaving a deceptively-healthy chrome).
            self._pending.clear()
            self._row_to_id.clear()
            self.query_one("#queue", QueueTable).clear()
            self._broker_error = str(e)
            self._update_detail()
            self._update_subtitle()
            self.notify(f"broker error: {e}", severity="error", timeout=8)
            return
        # Successful fetch — clear sticky error if any
        if self._broker_error is not None:
            self._broker_error = None
            self.notify("broker connection restored", severity="information",
                        timeout=4)
        self._pending = {r.id: r for r in pending}
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        table = self.query_one("#queue", QueueTable)
        # Preserve cursor by rid, not by row index — matches the GUI.
        prev_rid = None
        if 0 <= (table.cursor_row or 0) < len(self._row_to_id):
            prev_rid = self._row_to_id[table.cursor_row]

        table.clear()
        self._row_to_id = []
        for req in self._pending.values():
            # Cursor glyph filled in after move_cursor() below; status
            # is `●` for every row in Phase 1. The uid cell is a Rich
            # Text so the per-silo color chip survives the DataTable's
            # renderable pipeline (plain strings have no style channel).
            chip = chip_for_uid(req.uid)
            uid_cell = Text(f" {req.uid} ", style=f"black on {chip}")
            table.add_row(" ", "●", uid_cell, req.action, req.exe)
            self._row_to_id.append(req.id)

        if not self._row_to_id:
            self._update_detail()
            self._update_subtitle()
            return

        # Restore cursor on the same rid if still present, else on the
        # row that took its place (clamped).
        if prev_rid is not None and prev_rid in self._pending:
            target = self._row_to_id.index(prev_rid)
        else:
            target = 0
        table.move_cursor(row=target)
        self._paint_cursor_glyph()
        self._update_detail()
        self._update_subtitle()

    def _paint_cursor_glyph(self) -> None:
        """Set the cursor-glyph cell: `▶` on the selected row, space elsewhere.

        Called after any cursor move. Cheap: two single-cell updates at
        most (old cursor row + new cursor row); we repaint all for
        simplicity since queues are small in Phase 1.
        """
        table = self.query_one("#queue", QueueTable)
        current = table.cursor_row
        for r in range(table.row_count):
            table.update_cell_at((r, 0), "▶" if r == current else " ")

    def _update_subtitle(self) -> None:
        # Broker offline: replace the subtitle entirely with a high-
        # contrast banner. Stays sticky until the next successful refresh.
        if self._broker_error is not None:
            self.sub_title = f"⚠ BROKER OFFLINE — press r to retry  ({self._broker_error})"
            return
        n = len(self._pending)
        scope_label = SCOPES[self._scope]
        # Make non-default scope visually loud — typing `5` then `a`
        # accidentally is the costliest UX failure mode (forever-exe).
        scope_chunk = (
            f"scope: {scope_label}" if self._scope == "once"
            else f"⚠ scope: {scope_label}"
        )
        self.sub_title = (
            f"{n} pending  •  {scope_chunk}"
            if n else f"(no pending requests)  •  {scope_chunk}"
        )

    def _selected_request(self) -> Request | None:
        table = self.query_one("#queue", QueueTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._row_to_id):
            return None
        rid = self._row_to_id[row]
        return self._pending.get(rid)

    def _update_detail(self) -> None:
        self.query_one("#detail", DetailPane).render_request(
            self._selected_request(), self._scope,
        )

    # When the focused DataTable handles `up`/`down` itself, our app-level
    # action bindings don't fire — so we also listen for RowHighlighted
    # (emitted on any cursor move, including mouse) and repaint the glyph
    # / detail pane from there. The explicit actions below still exist
    # for the case where focus is elsewhere.
    def on_data_table_row_highlighted(self, event) -> None:  # noqa: ANN001
        if event.control.id != "queue":
            return
        self._paint_cursor_glyph()
        self._update_detail()

    # -- Actions --
    def action_cursor_up(self) -> None:
        self.query_one("#queue", QueueTable).action_cursor_up()
        self._paint_cursor_glyph()
        self._update_detail()

    def action_cursor_down(self) -> None:
        self.query_one("#queue", QueueTable).action_cursor_down()
        self._paint_cursor_glyph()
        self._update_detail()

    def action_scope(self, key: str) -> None:
        if key not in SCOPES:
            return
        self._scope = key
        self._update_detail()
        self._update_subtitle()

    def action_approve(self) -> None:
        self._decide("allow")

    def action_deny(self) -> None:
        self._decide("deny")

    def action_refresh(self) -> None:
        self.refresh_queue()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_create_rule_from_current(self) -> None:
        """Create a rule from the currently selected request."""
        req = self._selected_request()
        if req is None:
            self.notify("No request selected", severity="warning", timeout=3)
            return
        # In TUI, we can't easily create rules, so just notify
        self.notify(
            f"Would create rule for uid={req.uid} action={req.action}",
            severity="information",
            timeout=5,
        )

    def action_approve_all(self) -> None:
        """Approve all pending requests."""
        try:
            pending = self._broker.get_pending()
            approved_count = 0
            for req in pending:
                self._broker.decide_request(req.id, "allow", self._scope)
                approved_count += 1
            self.notify(
                f"Approved {approved_count} requests (scope: {SCOPES[self._scope]})",
                severity="information",
                timeout=5,
            )
        except Exception as e:  # noqa: BLE001
            self.notify(f"Error approving all: {e}", severity="error", timeout=8)

    def action_deny_all(self) -> None:
        """Deny all pending requests."""
        try:
            pending = self._broker.get_pending()
            denied_count = 0
            for req in pending:
                self._broker.decide_request(req.id, "deny", self._scope)
                denied_count += 1
            self.notify(
                f"Denied {denied_count} requests",
                severity="information",
                timeout=5,
            )
        except Exception as e:  # noqa: BLE001
            self.notify(f"Error denying all: {e}", severity="error", timeout=8)

    def _decide(self, decision: str) -> None:
        req = self._selected_request()
        if req is None:
            self.notify("no request selected", severity="warning", timeout=3)
            return
        try:
            self._broker.decide_request(req.id, decision, self._scope)
        except Exception as e:  # noqa: BLE001
            self.notify(f"decide failed: {e}", severity="error", timeout=8)
            return
        # Confirm the action explicitly. Row vanishing is implicit
        # feedback but easy to miss when the queue had only one entry
        # (table just empties); a tired admin can't tell whether the
        # press registered. Toast removes that ambiguity.
        scope_label = SCOPES[self._scope]
        verb = "approved" if decision == "allow" else "denied"
        self.notify(
            f"{verb} uid={req.uid} pid={req.pid} action={req.action}  "
            f"(scope: {scope_label})",
            severity="information",
            timeout=4,
        )
        # Don't double-refresh — the broker's RequestDecided signal will
        # arrive shortly and trigger _on_decided which updates the table.
        # This was the source of triple-refresh storms in the prior code.


def main() -> int:
    app = AdminTuiApp()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
