"""Headless tests for the TUI approver via Textual's Pilot.

No D-Bus, no terminal. The app is wired with a FakeBrokerClient that
tests prod directly to simulate broker signals and assert side effects.

Two FakeBrokerClient modes:
  - auto_emit_on_decide=True (default): simple happy-path tests
  - auto_emit_on_decide=False: contract tests that exercise the real
    fire-and-forget shape (decide() doesn't remove the row; the test
    must call emit_decided() to simulate the broker's signal)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Skip the whole module cleanly if textual isn't installed — the TUI
# approver pulls it transitively, and a missing optional dev dep
# shouldn't break the pre-commit hook for unrelated changes.
pytest.importorskip("textual")

# Promote tui/ onto sys.path so test imports work.
_TUI = Path(__file__).resolve().parents[1] / "tui"
sys.path.insert(0, str(_TUI))

from broker_client import FakeBrokerClient, Request  # noqa: E402
from qdistro_admin_tui import SCOPES, AdminTuiApp, HelpScreen  # noqa: E402


def _req(rid: int, uid: int = 2000, action: str = "test.action") -> Request:
    return Request(id=rid, uid=uid, pid=1234 + rid, exe="/usr/bin/python3.13",
                   action=action, details={"purpose": "smoke test"})


# --- boot / empty state ---------------------------------------------------

@pytest.mark.asyncio
async def test_boots_with_empty_queue_shows_placeholder():
    app = AdminTuiApp(broker=FakeBrokerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "no pending requests" in app.sub_title.lower()


@pytest.mark.asyncio
async def test_boots_with_existing_pending_shows_them():
    app = AdminTuiApp(broker=FakeBrokerClient(pending=[_req(1), _req(2)]))
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#queue", DataTable)
        assert table.row_count == 2
        assert "2 pending" in app.sub_title


# --- approve / deny -------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_default_scope_is_once():
    broker = FakeBrokerClient(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
    assert broker.decided == [(1, "allow", "once")]


@pytest.mark.asyncio
async def test_deny_default_scope_is_once():
    broker = FakeBrokerClient(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
    assert broker.decided == [(1, "deny", "once")]


@pytest.mark.asyncio
async def test_approve_with_no_selection_no_op():
    broker = FakeBrokerClient()
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
    assert broker.decided == []


@pytest.mark.asyncio
async def test_ctrl_y_alias_for_approve():
    """Ctrl+Y should be wired as a GUI-parity alias for `a`."""
    broker = FakeBrokerClient(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+y")
        await pilot.pause()
    assert broker.decided == [(1, "allow", "once")]


@pytest.mark.asyncio
async def test_ctrl_n_alias_for_deny():
    broker = FakeBrokerClient(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
    assert broker.decided == [(1, "deny", "once")]


# --- scope keys -----------------------------------------------------------

@pytest.mark.parametrize("key,expected_scope", [
    ("1", "once"),
    ("2", "1h"),
    ("3", "24h"),
    ("4", "forever"),
    ("5", "forever_exe"),
    # task(072): argv-aware Forever scopes — TUI keys 6/7/8.
    ("6", "forever_argv"),
    ("7", "forever_basename"),
    ("8", "forever_prefix"),
])
@pytest.mark.asyncio
async def test_scope_key_changes_active_scope(key, expected_scope):
    broker = FakeBrokerClient(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(key, "a")
        await pilot.pause()
    assert broker.decided == [(1, "allow", expected_scope)]


@pytest.mark.asyncio
async def test_scope_label_visible_in_subtitle():
    app = AdminTuiApp(broker=FakeBrokerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        assert "24 hours" in app.sub_title


# --- live signal -> refresh ----------------------------------------------

@pytest.mark.asyncio
async def test_emit_pending_signal_adds_row():
    broker = FakeBrokerClient()
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#queue", DataTable)
        assert table.row_count == 0
        broker.emit_pending(_req(1))
        await pilot.pause()
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_pending_signal_rings_bell():
    """_on_pending must ring the terminal bell so a backgrounded admin
    hears new requests without looking at the TUI."""
    broker = FakeBrokerClient()
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        calls = []
        app.bell = lambda: calls.append(True)  # type: ignore[method-assign]
        broker.emit_pending(_req(1))
        await pilot.pause()
        assert calls == [True]


@pytest.mark.asyncio
async def test_decide_removes_row_via_signal_only():
    """Contract test: decide_request is fire-and-forget — the row
    disappears only when the broker emits RequestDecided."""
    broker = FakeBrokerClient(pending=[_req(1), _req(2)],
                              auto_emit_on_decide=False)
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#queue", DataTable)
        assert table.row_count == 2
        await pilot.press("a")
        await pilot.pause()
        # decide_request was called, but no signal yet → row still there
        assert broker.decided == [(1, "allow", "once")]
        assert table.row_count == 2
        # Broker eventually fires the signal
        broker.emit_decided(1, "allow")
        await pilot.pause()
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_decide_removes_row_immediately_when_fake_auto_emits():
    """Backwards-compat: in the simpler (default) fake mode, decide
    removes the row immediately because emit fires synchronously."""
    broker = FakeBrokerClient(pending=[_req(1), _req(2)])  # auto=True
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#queue", DataTable)
        assert table.row_count == 2
        await pilot.press("a")
        await pilot.pause()
        assert table.row_count == 1


# --- navigation + cursor preservation ------------------------------------

@pytest.mark.asyncio
async def test_cursor_glyph_follows_selection():
    """`▶` cursor glyph tracks the selected row; other rows blank."""
    broker = FakeBrokerClient(pending=[_req(1), _req(2), _req(3)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#queue", DataTable)
        assert table.row_count == 3

        def _glyphs():
            # Column 0 = cursor glyph column.
            return [str(table.get_cell_at((r, 0))) for r in range(table.row_count)]

        assert _glyphs() == ["▶", " ", " "]
        await pilot.press("down")
        await pilot.pause()
        assert _glyphs() == [" ", "▶", " "]
        await pilot.press("down")
        await pilot.pause()
        assert _glyphs() == [" ", " ", "▶"]
        await pilot.press("up")
        await pilot.pause()
        assert _glyphs() == [" ", "▶", " "]


@pytest.mark.asyncio
async def test_status_column_is_filled_dot_for_pending():
    """Phase-1 status: every pending row shows `●`."""
    broker = FakeBrokerClient(pending=[_req(1), _req(2)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#queue", DataTable)
        for r in range(table.row_count):
            assert str(table.get_cell_at((r, 1))) == "●"


@pytest.mark.asyncio
async def test_arrow_down_changes_selection_and_detail():
    broker = FakeBrokerClient(pending=[_req(1, action="first"),
                                       _req(2, action="second")])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Initial selection — first request
        sel = app._selected_request()
        assert sel is not None
        assert sel.id == 1 and sel.action == "first"
        await pilot.press("down")
        await pilot.pause()
        # Selection moved → detail pane is rebuilt for the new request.
        # Verifying via _selected_request rather than poking Static
        # internals (no public Renderable accessor in Textual 8).
        sel = app._selected_request()
        assert sel is not None
        assert sel.id == 2 and sel.action == "second"
        await pilot.press("a")
        await pilot.pause()
    assert broker.decided[0][0] == 2


@pytest.mark.asyncio
async def test_cursor_preserved_by_rid_when_queue_grows():
    """If a new request lands while admin is on row 1, the cursor
    should stay on the original request, not slide to the inserted row."""
    broker = FakeBrokerClient(pending=[_req(10), _req(20)],
                              auto_emit_on_decide=False)
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # cursor on rid=20
        await pilot.pause()
        # New request with id=99 — gets appended (dict order)
        broker.emit_pending(_req(99, action="new"))
        await pilot.pause()
        # The cursor should still highlight rid=20 (preserved by rid),
        # so pressing `a` decides 20, not 99 or 10.
        await pilot.press("a")
        await pilot.pause()
    assert broker.decided[0][0] == 20


# --- error UX ------------------------------------------------------------

class _ExplodingBroker(FakeBrokerClient):
    """Raises on get_pending — used to verify error UX."""

    def get_pending(self):
        raise RuntimeError("broker offline")


@pytest.mark.asyncio
async def test_broker_error_clears_table_to_avoid_stale_decides():
    broker = _ExplodingBroker(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#queue", DataTable)
        # refresh_queue ran during on_mount and hit the exception
        assert table.row_count == 0
        # Pressing `a` must not target a stale id
        await pilot.press("a")
        await pilot.pause()
    # No decision recorded because there's nothing selected
    assert broker.decided == []


@pytest.mark.asyncio
async def test_broker_error_sets_persistent_subtitle_banner():
    """Sticky BROKER OFFLINE subtitle so a faded toast doesn't leave the
    user staring at a deceptively-healthy chrome."""
    broker = _ExplodingBroker()
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "BROKER OFFLINE" in app.sub_title


@pytest.mark.asyncio
async def test_broker_error_clears_on_recovery():
    """When the broker comes back, the sticky banner clears."""
    broker = FakeBrokerClient(pending=[_req(1)])
    # Patch get_pending to fail on first call, succeed thereafter
    real_get = broker.get_pending
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call fails")
        return real_get()
    broker.get_pending = flaky  # type: ignore[method-assign]
    app = AdminTuiApp(broker=broker)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "BROKER OFFLINE" in app.sub_title
        await pilot.press("r")  # manual refresh now succeeds
        await pilot.pause()
        assert "BROKER OFFLINE" not in app.sub_title
        assert "1 pending" in app.sub_title


# --- scope-loudness signal -----------------------------------------------

@pytest.mark.asyncio
async def test_default_scope_subtitle_unmarked():
    """`once` is the default — no warning glyph in the subtitle."""
    app = AdminTuiApp(broker=FakeBrokerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "⚠" not in app.sub_title


@pytest.mark.parametrize("scope_key", ["1h", "24h", "forever", "forever_exe"])
@pytest.mark.asyncio
async def test_non_default_scope_subtitle_warns(scope_key):
    """Any scope other than 'once' deserves a visible alert glyph;
    typing 4-then-a accidentally would otherwise be a silent forever."""
    key_to_press = {"1h": "2", "24h": "3", "forever": "4", "forever_exe": "5"}[scope_key]
    app = AdminTuiApp(broker=FakeBrokerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(key_to_press)
        await pilot.pause()
        assert "⚠" in app.sub_title, f"scope {scope_key} subtitle missing warning glyph"


# --- decision confirmation toast -----------------------------------------

@pytest.mark.asyncio
async def test_approve_emits_confirmation_toast():
    """User pressing `a` should see explicit confirmation, not just a
    vanishing row (which is invisible when the queue had only 1 entry)."""
    broker = FakeBrokerClient(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)

    notifications: list[tuple[str, str]] = []
    real_notify = app.notify
    def _capture_notify(message, *, severity="information", timeout=4, **kw):
        notifications.append((message, severity))
        return real_notify(message, severity=severity, timeout=timeout, **kw)
    app.notify = _capture_notify  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
    confirms = [m for m, sev in notifications
                if "approved" in m and "uid=2000" in m]
    assert confirms, f"expected approval toast, got: {notifications}"


@pytest.mark.asyncio
async def test_deny_emits_confirmation_toast():
    broker = FakeBrokerClient(pending=[_req(1)])
    app = AdminTuiApp(broker=broker)

    notifications: list[tuple[str, str]] = []
    real_notify = app.notify
    def _capture_notify(message, *, severity="information", timeout=4, **kw):
        notifications.append((message, severity))
        return real_notify(message, severity=severity, timeout=timeout, **kw)
    app.notify = _capture_notify  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
    confirms = [m for m, sev in notifications
                if "denied" in m and "uid=2000" in m]
    assert confirms, f"expected deny toast, got: {notifications}"


# --- footer label readability --------------------------------------------

def test_footer_scope_labels_are_readable():
    """No `fwd` jargon; spell out the scope semantics in the footer."""
    from textual.binding import Binding as _B
    descriptions = []
    for binding in AdminTuiApp.BINDINGS:
        if isinstance(binding, _B):
            descriptions.append(binding.description)
    # Any binding mentioning a forever scope should not abbreviate it
    forever_descs = [d for d in descriptions if "forever" in d.lower() or "fwd" in d.lower()]
    assert forever_descs, "no forever-scope binding descriptions found"
    for d in forever_descs:
        assert "fwd" not in d.lower(), f"binding description still uses 'fwd': {d!r}"


# --- help overlay --------------------------------------------------------

@pytest.mark.asyncio
async def test_help_overlay_opens_on_question_mark():
    app = AdminTuiApp(broker=FakeBrokerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_help_overlay_dismisses_on_escape():
    app = AdminTuiApp(broker=FakeBrokerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


# --- shortcut coverage (per spec/23 mandate) -----------------------------

def test_every_binding_has_a_handler():
    """spec/23 mandate: enumerate BINDINGS, verify each has an action_*
    method. Catches dead bindings + typos."""
    from textual.binding import Binding as _B
    for binding in AdminTuiApp.BINDINGS:
        if isinstance(binding, _B):
            action = binding.action
        else:
            # tuple form
            _, action, *_rest = binding
        # Action may be "name" or "name('arg')"; strip the args
        action_name = action.split("(", 1)[0].strip()
        assert hasattr(AdminTuiApp, f"action_{action_name}"), (
            f"binding for {binding!r} references action_{action_name} "
            "which is not defined on AdminTuiApp"
        )


# --- meta -----------------------------------------------------------------

def test_scopes_match_broker_vocabulary():
    """Scope keys here must exactly match what the broker accepts;
    drift would silently break cache writes."""
    keys = list(SCOPES.keys())
    assert keys == [
        "once", "1h", "24h",
        "forever", "forever_exe",
        # task(072): argv-aware Forever scopes (cache backend support
        # in task(069); broker _VALID_SCOPES gate lifted in task(072)).
        "forever_argv", "forever_basename", "forever_prefix",
    ]


def test_dbus_broker_methods_called_before_start_raise():
    """Lazy connect: get_pending/decide_request must refuse to operate
    before start() has wired the bus."""
    from broker_client import DBusBrokerClient
    c = DBusBrokerClient(app=None)
    with pytest.raises(RuntimeError, match="before start"):
        c.get_pending()
    with pytest.raises(RuntimeError, match="before start"):
        c.decide_request(1, "allow", "once")


def test_dbus_broker_init_does_not_touch_dbus():
    """Constructing DBusBrokerClient must not import dbus or open a bus
    connection (so a missing/down broker doesn't crash app startup)."""
    import sys as _sys
    # Snapshot dbus-related modules; constructing the client mustn't
    # cause them to be imported on demand if they weren't already there.
    # (We can't fully prove negative side effects without unloading,
    # but we can at least verify the client instance has no proxy.)
    from broker_client import DBusBrokerClient
    c = DBusBrokerClient(app=None)
    assert c._proxy is None
    assert c._bus is None
    assert c._loop is None
    assert c._thread is None
    assert c._started is False


# task(066) — TUI argv split helper for qsu prompts.

class TestSplitArgvFromDetails:
    def _split(self, d):
        from qdistro_admin_tui import _split_argv_from_details
        return _split_argv_from_details(d)

    def test_empty_input(self):
        assert self._split({}) == (None, {})
        assert self._split(None) == (None, {})

    def test_no_argv_keys_passes_through(self):
        d = {"src_app": "qdistro.tier3.user1"}
        argv, rest = self._split(d)
        assert argv is None
        assert rest == d

    def test_argv_extracted_and_shlex_joined(self):
        d = {"argv[00]": "/usr/bin/echo", "argv[01]": "hello world"}
        argv, rest = self._split(d)
        # shlex.join quotes "hello world" because of the embedded space.
        assert argv == "/usr/bin/echo 'hello world'"
        assert rest == {}

    def test_argv_kept_separate_from_other_details(self):
        d = {
            "argv[00]": "id",
            "argv[01]": "-u",
            "target_user": "root",
        }
        argv, rest = self._split(d)
        assert argv == "id -u"
        assert rest == {"target_user": "root"}

    def test_human_argv_key_does_not_conflict(self):
        # qsu also ships a human-readable "argv" key (shlex.join of the
        # full argv). The helper must NOT consume that as an argv[N]
        # element — that key stays in `rest`.
        d = {"argv": "id -u", "argv[00]": "id", "argv[01]": "-u"}
        argv, rest = self._split(d)
        assert argv == "id -u"
        assert rest == {"argv": "id -u"}

    def test_indices_above_cap_dropped(self):
        d = {"argv[00]": "ok", "argv[1025]": "ignored"}
        argv, _ = self._split(d)
        assert argv == "ok"
