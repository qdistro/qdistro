"""Unit tests for the VM-A-served control producer (codex impl-12).

The ``mm-control`` unit's logic — building the source-derived ``Announce``/
``Closed`` and the source-owned watch decision (Closed on source death, NO Closed
on viewer detach) — runs headless here with injected liveness + viewer-poll, so
the honesty claim ("VM-A owns the source lifecycle") is pinned without a VM.
"""
from __future__ import annotations

from multimachine.bridge import SourceWindowInfo
from multimachine.control_source import (
    VIEWER_ALIVE, VIEWER_DATA, VIEWER_EOF, ControlSource, active_state_alive,
    authenticate_viewer, shell_request_commands, viewer_close_requested,
    viewer_shell_requests, watch,
)
from multimachine.sidechannel import (
    Announce, CloseRequest, Closed, RemoteViewerState, ShellOperation,
    ShellRequest, decode, encode,
)


def _src(window_id=1):
    return SourceWindowInfo(
        window_id=window_id, source_machine="vm-a", title="marker",
        app_id="qdwin-marker-client", req_w=1280, req_h=800)


def test_control_capability_authentication_fails_closed():
    good = '{"v":1,"type":"authenticate","capability":"secret"}'
    assert authenticate_viewer(good, "secret")
    for raw in ('{"v":1,"type":"authenticate","capability":"wrong"}',
                '[]', 'not-json',
                '{"v":1,"type":"other","capability":"secret"}'):
        assert not authenticate_viewer(raw, "secret")


class TestControlSourceMessages:
    def test_from_source_mints_stream_id_in_guest(self):
        cs = ControlSource.from_source(_src(), generation=7,
                                       nonce=lambda: "deadbeef")
        assert cs.meta.stream_id == "vs-1-deadbeef"
        assert cs.generation == 7
        assert cs.meta.title == "marker" and cs.meta.req_w == 1280

    def test_explicit_stream_id_is_kept(self):
        cs = ControlSource.from_source(_src(), generation=7, stream_id="fixed")
        assert cs.meta.stream_id == "fixed"

    def test_announce_then_closed_carry_same_stream_id(self):
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        ann, closed = cs.announce(), cs.closed()
        assert isinstance(ann, Announce) and isinstance(closed, Closed)
        assert ann.meta.stream_id == "sid" == closed.stream_id
        assert closed.window_id == 1 and closed.reason == "source-exit"
        # round-trips through the shipped wire codec.
        assert decode(encode(ann)).meta.stream_id == "sid"
        assert decode(encode(closed)).window_id == 1

    def test_announce_then_closed_apply_to_the_viewer_contract(self):
        """The produced bytes drive the real RemoteViewerState: Announce opens the
        proxy, the source-driven Closed removes it."""
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        view = RemoteViewerState(generation=7)
        assert view.apply(decode(encode(cs.announce()))) is True
        assert view.windows and view.windows[1].stream_id == "sid"
        assert view.apply(decode(encode(cs.closed()))) is True
        assert not view.windows           # proxy removed by source-driven Closed


class TestActiveStateClassifier:
    def test_active_is_alive(self):
        assert active_state_alive("ActiveState=active\nSubState=running\n") is True

    def test_terminal_states_are_dead(self):
        assert active_state_alive("ActiveState=inactive\nSubState=dead\n") is False
        assert active_state_alive("ActiveState=failed\nSubState=failed\n") is False

    def test_transient_states_count_alive(self):
        # mid-stop / mid-start must NOT fabricate a premature source-death Closed.
        assert active_state_alive("ActiveState=deactivating\nSubState=stop\n") is True
        assert active_state_alive("ActiveState=activating\nSubState=start\n") is True

    def test_empty_or_probe_error_counts_alive(self):
        # a systemctl/DBus hiccup (empty dump) is NOT evidence of death.
        assert active_state_alive("") is True
        assert active_state_alive("garbage\n") is True


class TestWatchLoop:
    def test_announce_sent_first(self):
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        sent = []
        # viewer dies immediately so the loop terminates after the announce.
        watch(cs, is_source_alive=lambda: True,
              poll_viewer=lambda: VIEWER_EOF, send=sent.append)
        assert len(sent) == 1 and isinstance(sent[0], Announce)

    def test_source_death_emits_closed(self):
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        sent = []
        # alive (top), idle poll, alive (top), idle poll, then dead at the next top.
        alive = iter([True, True, False])
        ev = iter([VIEWER_ALIVE, VIEWER_ALIVE, VIEWER_ALIVE])
        reason = watch(cs, is_source_alive=lambda: next(alive),
                       poll_viewer=lambda: next(ev), send=sent.append)
        assert reason == "source-exit"
        assert isinstance(sent[0], Announce) and isinstance(sent[-1], Closed)
        assert sent[-1].reason == "source-exit"

    def test_viewer_detach_emits_no_closed(self):
        """Viewer EOF while the source is alive is a DETACH — the unit must NOT
        emit Closed (the source window is still alive; the slot frees source-side
        and a fresh resubscribe reclaims it). The honesty distinction."""
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        sent = []
        reason = watch(cs, is_source_alive=lambda: True,
                       poll_viewer=lambda: VIEWER_EOF, send=sent.append)
        assert reason == "viewer-eof"
        assert len(sent) == 1 and isinstance(sent[0], Announce)
        assert not any(isinstance(m, Closed) for m in sent)

    def test_upstream_data_does_not_trigger_closed_while_alive(self):
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        sent = []
        ev = iter([VIEWER_DATA, VIEWER_DATA, VIEWER_EOF])
        reason = watch(cs, is_source_alive=lambda: True,
                       poll_viewer=lambda: next(ev), send=sent.append)
        assert reason == "viewer-eof"
        assert not any(isinstance(m, Closed) for m in sent)

    def test_eof_caused_by_source_death_emits_closed(self):
        """The honest race resolution: a viewer EOF whose root cause is the source
        dying (killing the marker tears down the RDP stream → the viewer
        disconnects) is a SOURCE-driven Closed, not a detach. On EOF we re-check
        liveness; source dead → Closed."""
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        sent = []
        # alive at the loop top (so we poll), then EOF, then dead on the re-check.
        alive = iter([True, False])
        reason = watch(cs, is_source_alive=lambda: next(alive),
                       poll_viewer=lambda: VIEWER_EOF, send=sent.append)
        assert reason == "source-exit"
        assert isinstance(sent[-1], Closed) and sent[-1].reason == "source-exit"


class TestViewerCloseRequested:
    """The pure CloseRequest detector (codex impl-32 Q4, source-mediated close)."""

    def _wire(self, stream_id, window_id=1, gen=7):
        return encode(CloseRequest("close_request", gen, window_id, stream_id))

    def test_matching_stream_id_is_a_close(self):
        assert viewer_close_requested(self._wire("sid"), "sid") is True

    def test_mismatched_stream_id_is_not_a_close(self):
        assert viewer_close_requested(self._wire("other"), "sid") is False

    def test_non_close_message_ignored(self):
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        assert viewer_close_requested(encode(cs.announce()), "sid") is False

    def test_garbage_and_empty_ignored(self):
        assert viewer_close_requested("", "sid") is False
        assert viewer_close_requested("not json\n{partial", "sid") is False

    def test_matching_among_multiple_buffered_lines(self):
        # a single recv() can deliver several newline-delimited messages.
        chunk = "garbage\n" + self._wire("other") + "\n" + self._wire("sid") + "\n"
        assert viewer_close_requested(chunk, "sid") is True


class TestViewerShellRequests:
    def _wire(self, operation, *, stream_id="sid", window_id=1, gen=7,
              x=0, y=0):
        return encode(ShellRequest(
            "shell_request", gen, window_id, stream_id, operation, x, y))

    def test_exact_live_identity_and_commands(self):
        chunk = "\n".join([
            self._wire(ShellOperation.MAXIMIZE),
            self._wire(ShellOperation.MOVE, x=40, y=-12),
            self._wire(ShellOperation.RESTORE),
        ])
        got = viewer_shell_requests(
            chunk, stream_id="sid", generation=7, window_id=1)
        assert [msg.operation for msg in got] == [
            ShellOperation.MAXIMIZE, ShellOperation.MOVE,
            ShellOperation.RESTORE,
        ]
        assert shell_request_commands(got[0]) == ("max 1",)
        assert shell_request_commands(got[1]) == ("move 1 40 -12",)
        assert shell_request_commands(got[2]) == (
            "fullscreen 1 0", "restore 1", "raise 1")

    def test_stale_mismatched_and_malformed_requests_rejected(self):
        wires = [
            self._wire(ShellOperation.MINIMIZE, stream_id="old"),
            self._wire(ShellOperation.MINIMIZE, window_id=2),
            self._wire(ShellOperation.MINIMIZE, gen=6),
            self._wire(ShellOperation.MINIMIZE, x=1),
            self._wire(ShellOperation.MOVE, x=40000),
            "not-json",
        ]
        assert viewer_shell_requests(
            "\n".join(wires), stream_id="sid", generation=7,
            window_id=1) == []

    def test_minimize_command(self):
        msg = viewer_shell_requests(
            self._wire(ShellOperation.MINIMIZE), stream_id="sid",
            generation=7, window_id=1)[0]
        assert shell_request_commands(msg) == ("min 1",)


class TestWatchViewerClose:
    def test_viewer_close_request_closes_source_and_sets_reason(self):
        """A CloseRequest fires on_viewer_data, which (in the live shell) stops the
        marker unit; the NEXT liveness poll sees it dead and the Closed carries the
        upgraded 'viewer-close' reason — the source-mediated close path."""
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        sent = []
        # alive (top) → DATA poll fires on_viewer_data (which "stops" the marker)
        # → next top sees it dead → Closed.
        alive = iter([True, False])
        ev = iter([VIEWER_DATA])
        closed_marker = {"stopped": False}

        def on_data():
            closed_marker["stopped"] = True
            return "viewer-close"

        reason = watch(cs, is_source_alive=lambda: next(alive),
                       poll_viewer=lambda: next(ev), send=sent.append,
                       on_viewer_data=on_data)
        assert closed_marker["stopped"] is True
        assert reason == "viewer-close"
        assert isinstance(sent[-1], Closed) and sent[-1].reason == "viewer-close"
        assert sent[-1].stream_id == "sid"

    def test_non_close_data_keeps_source_exit_reason(self):
        """Upstream data that is NOT a close (on_viewer_data returns None) leaves the
        reason as a spontaneous source-exit."""
        cs = ControlSource.from_source(_src(), generation=7, stream_id="sid")
        sent = []
        alive = iter([True, False])
        ev = iter([VIEWER_DATA])
        reason = watch(cs, is_source_alive=lambda: next(alive),
                       poll_viewer=lambda: next(ev), send=sent.append,
                       on_viewer_data=lambda: None)
        assert reason == "source-exit"
        assert isinstance(sent[-1], Closed) and sent[-1].reason == "source-exit"
