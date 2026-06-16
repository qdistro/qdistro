"""Dry-run tests for the peer-side remote viewer supervisor (Phase 1, impl-8).

Drives :class:`multimachine.viewer.RemoteViewer` with a FAKE process launcher
and scripted control messages — NO ``sdl-freerdp``, NO VM. Pins the
cross-machine lifecycle the live two-VM gate then confirms: Announce launches a
managed viewer toplevel, stale-generation / stream-id-mismatch control is
rejected, Close/Disconnect tear down and fail safe, a viewer-originated close
emits CloseRequest upstream, decoder exit maps to Disconnect, and capacity
denial surfaces in status.
"""
from __future__ import annotations

import pytest

from multimachine.bridge import (
    SourceWindowInfo, ViewStreamApproved, bridge_approved,
)
from multimachine.sidechannel import (
    Announce, Closed, Configure, Disconnect, Focus, RemoteWindowMeta,
    TitleChanged,
)
from multimachine.viewer import RemoteViewer, ViewerStatus


class FakeProc:
    """Stand-in for the launched sdl-freerdp decoder."""

    def __init__(self) -> None:
        self._rc: int | None = None
        self.terminated = 0

    def poll(self) -> int | None:
        return self._rc

    def terminate(self) -> None:
        self.terminated += 1
        self._rc = -15

    def die(self, rc: int = 0) -> None:
        self._rc = rc


def _viewer(gen=7):
    launched: list[Announce] = []

    def launcher(ann):
        launched.append(ann)
        return FakeProc()

    v = RemoteViewer(gen, launcher, source_machine="server")
    return v, launched


def _announce(gen=7, wid=1, sid="vs-1-a", **meta_kw):
    meta = RemoteWindowMeta(window_id=wid, source_machine="server",
                            stream_id=sid, title="Build", app_id="org.x.term",
                            req_w=800, req_h=600, **meta_kw)
    return Announce("announce", gen, meta)


class TestAnnounceLaunch:
    def test_announce_launches_managed_toplevel(self):
        v, launched = _viewer()
        assert v.on_message(_announce())
        assert len(launched) == 1
        assert v.status_value is ViewerStatus.CONNECTED
        st = v.status()
        assert st["status"] == "connected"
        assert st["windows"][0]["window_id"] == 1
        assert st["windows"][0]["remote"] is True          # visible disclosure
        assert st["windows"][0]["source_machine"] == "server"

    def test_announce_from_bridge_helper(self):
        # the source-side bridge_approved() Announce drives the viewer end-to-end.
        v, launched = _viewer(gen=12)
        approved = ViewStreamApproved("pw-0", 3401, "/c.pem", "OTP123")
        src = SourceWindowInfo(window_id=5, source_machine="alice",
                               title="Term", app_id="org.x.term",
                               req_w=640, req_h=480)
        ann = bridge_approved(approved, src, generation=12)
        assert v.on_message(ann)
        assert launched[0].meta.window_id == 5
        assert v.status()["windows"][0]["app_id"] == "org.x.term"

    def test_stale_generation_announce_rejected(self):
        v, launched = _viewer(gen=7)
        assert not v.on_message(_announce(gen=6))          # old dock generation
        assert not launched
        assert v.status_value is ViewerStatus.IDLE
        assert ("stale-generation" in r for r in
                [x[2] for x in v.state.rejected])


class TestLifecycle:
    def test_focus_and_title_and_configure_apply(self):
        v, _ = _viewer()
        v.on_message(_announce())
        assert v.on_message(Focus("focus", 7, window_id=1, focused=True,
                                  stream_id="vs-1-a"))
        assert v.state.windows[1].focused
        assert v.on_message(TitleChanged("title", 7, window_id=1,
                                         title="Build *", stream_id="vs-1-a"))
        assert v.state.windows[1].title == "Build *"
        assert v.on_message(Configure("configure", 7, window_id=1, w=900, h=700,
                                      stream_id="vs-1-a"))
        assert (v.state.windows[1].w, v.state.windows[1].h) == (900, 700)

    def test_stream_id_mismatch_control_rejected(self):
        v, _ = _viewer()
        v.on_message(_announce(sid="vs-1-a"))
        # a delayed Focus from a prior export reusing window_id but a stale sid
        assert not v.on_message(Focus("focus", 7, window_id=1, focused=True,
                                      stream_id="vs-1-OLD"))
        assert any(r[2] == "stream-id-mismatch" for r in v.state.rejected)

    def test_closed_removes_proxy_and_kills_decoder(self):
        v, _ = _viewer()
        v.on_message(_announce(wid=1, sid="vs-1-a"))
        proc = v.procs[1]
        assert v.on_message(Closed("closed", 7, window_id=1, reason="source",
                                   stream_id="vs-1-a"))
        assert 1 not in v.state.windows
        assert 1 not in v.procs
        assert proc.terminated == 1                        # decoder torn down

    def test_close_slot_released_then_reannounce_with_new_stream(self):
        v, launched = _viewer()
        v.on_message(_announce(wid=1, sid="vs-1-a"))
        v.on_message(Closed("closed", 7, window_id=1, reason="source",
                            stream_id="vs-1-a"))
        # a fresh export of the same window (new stream_id) re-launches cleanly.
        assert v.on_message(_announce(wid=1, sid="vs-1-b"))
        assert len(launched) == 2
        assert v.status_value is ViewerStatus.CONNECTED


class TestTeardownFailsSafe:
    def test_disconnect_blanks_all_and_kills(self):
        v, _ = _viewer()
        v.on_message(_announce(wid=1, sid="vs-1-a"))
        v.on_message(_announce(wid=2, sid="vs-2-a"))
        procs = list(v.procs.values())
        assert v.on_message(Disconnect("disconnect", 7, "link dropped"))
        assert v.state.windows == {}
        assert v.procs == {}
        assert all(p.terminated == 1 for p in procs)
        assert v.status_value is ViewerStatus.DISCONNECTED
        # post-disconnect control is rejected (fail safe).
        assert not v.on_message(Focus("focus", 7, window_id=1, focused=True,
                                      stream_id="vs-1-a"))

    def test_decoder_exit_maps_to_disconnect(self):
        v, _ = _viewer()
        v.on_message(_announce(wid=1, sid="vs-1-a"))
        v.procs[1].die(0)                                  # sdl-freerdp exits
        exited = v.reap()
        assert exited == [1]
        assert v.status_value is ViewerStatus.DISCONNECTED
        assert v.state.windows == {}

    def test_capacity_denial_surfaces_in_status(self):
        v, _ = _viewer()
        # source could not grant a stream (e.g. "no free pipewire output").
        assert v.on_message(Disconnect("disconnect", 7,
                                       "no free pipewire output"))
        assert v.status_value is ViewerStatus.CAPACITY_EXCEEDED
        assert v.status()["status"] == "capacity-exceeded"


class TestControlChannelRoundTrip:
    """The wire is newline-delimited ``sidechannel.encode`` JSON (impl-8). Prove
    the full source->wire->viewer path in memory: a source emits Announce then a
    teardown; the viewer consumes the exact bytes and ends correctly."""

    def test_announce_then_disconnect_over_jsonlines(self):
        from multimachine.sidechannel import decode, encode
        approved = ViewStreamApproved("pw-0", 3401, "/c.pem", "OTP")
        src = SourceWindowInfo(window_id=9, source_machine="bob",
                               title="Editor", app_id="org.x.ed",
                               req_w=1024, req_h=768)
        # source side serialises the control sequence to a JSON-lines stream.
        wire = "".join(encode(m) + "\n" for m in (
            bridge_approved(approved, src, generation=3, stream_id="vs-9-z"),
            Disconnect("disconnect", 3, "admin revoked"),
        ))
        v, launched = _viewer(gen=3)
        for line in wire.splitlines():
            v.on_message(decode(line))
        assert len(launched) == 1 and launched[0].meta.title == "Editor"
        assert v.status_value is ViewerStatus.DISCONNECTED
        assert v.procs == {} and v.state.windows == {}

    def test_upstream_close_request_serialises_back(self):
        from multimachine.sidechannel import decode, encode
        v, _ = _viewer(gen=4)
        v.on_message(_announce(gen=4, wid=2, sid="vs-2-q"))
        assert v.request_close(2)
        # the transport would write each upstream message back on the socket;
        # it must decode to an equivalent CloseRequest the source can act on.
        back = decode(encode(v.upstream[0]))
        assert back.type == "close_request" and back.window_id == 2
        assert back.stream_id == "vs-2-q" and back.generation == 4


class TestViewerOriginatedClose:
    def test_user_close_emits_close_request_upstream(self):
        v, _ = _viewer()
        v.on_message(_announce(wid=1, sid="vs-1-a"))
        assert v.request_close(1)
        msg = v.upstream.pop()
        assert msg.type == "close_request"
        assert msg.window_id == 1 and msg.stream_id == "vs-1-a"
        assert msg.generation == 7

    def test_close_request_for_unknown_window_is_noop(self):
        v, _ = _viewer()
        assert not v.request_close(99)
        assert not v.upstream
