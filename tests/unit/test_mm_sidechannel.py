"""Tests for the remote whole-window viewer control side-channel (Phase 1).

Pins the metadata/control contract: messages round-trip through JSON, the
viewer-side state applies them, stale-generation control is rejected, focus is
single-seat, and Disconnect blanks all proxies (detach, source survives).
"""
from __future__ import annotations

import pytest

from multimachine.sidechannel import (
    Announce, Closed, CloseRequest, Configure, Disconnect, Focus,
    PointerPolicy, RemoteViewerState, RemoteWindowMeta, Sensitivity,
    TitleChanged, decode, encode,
)


def _meta(wid=1, **kw):
    return RemoteWindowMeta(window_id=wid, source_machine="server",
                            title="Build", app_id="org.x.term",
                            req_w=800, req_h=600, **kw)


class TestEncoding:
    def test_announce_roundtrip(self):
        m = Announce("announce", 3, _meta(sensitivity=Sensitivity.SENSITIVE,
                                          pointer_policy=PointerPolicy.LOCAL_ONLY))
        got = decode(encode(m))
        assert isinstance(got, Announce)
        assert got.generation == 3
        assert got.meta.sensitivity is Sensitivity.SENSITIVE
        assert got.meta.pointer_policy is PointerPolicy.LOCAL_ONLY
        assert got.meta.title == "Build" and got.meta.window_id == 1

    @pytest.mark.parametrize("msg", [
        Configure("configure", 2, 5, 1024, 768),
        Focus("focus", 2, 5, True),
        TitleChanged("title", 2, 5, "new"),
        CloseRequest("close_request", 2, 5),
        Closed("closed", 2, 5, "app exited"),
        Disconnect("disconnect", 2, "link lost"),
    ])
    def test_message_roundtrip(self, msg):
        assert decode(encode(msg)) == msg

    def test_bad_version_rejected(self):
        raw = encode(Focus("focus", 1, 1, True)).replace('"v": 1', '"v": 9')
        with pytest.raises(ValueError, match="version"):
            decode(raw)

    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError, match="unknown control"):
            decode('{"v": 1, "type": "bogus", "generation": 1}')


class TestViewerState:
    def test_announce_then_configure_and_focus(self):
        v = RemoteViewerState(generation=4)
        assert v.apply(Announce("announce", 4, _meta(1)))
        assert 1 in v.windows
        assert v.apply(Configure("configure", 4, 1, 1024, 768))
        assert (v.windows[1].w, v.windows[1].h) == (1024, 768)
        assert v.apply(Focus("focus", 4, 1, True))
        assert v.windows[1].focused

    def test_single_focused_proxy(self):
        v = RemoteViewerState(generation=1)
        v.apply(Announce("announce", 1, _meta(1)))
        v.apply(Announce("announce", 1, _meta(2)))
        v.apply(Focus("focus", 1, 1, True))
        v.apply(Focus("focus", 1, 2, True))
        assert v.windows[2].focused and not v.windows[1].focused

    def test_stale_generation_control_rejected(self):
        v = RemoteViewerState(generation=5)
        assert not v.apply(Announce("announce", 4, _meta(1)))
        assert 1 not in v.windows
        assert any(r[2] == "stale-generation" for r in v.rejected)

    def test_disconnect_blanks_all_proxies(self):
        v = RemoteViewerState(generation=2)
        v.apply(Announce("announce", 2, _meta(1)))
        v.apply(Announce("announce", 2, _meta(2)))
        assert len(v.windows) == 2
        assert v.apply(Disconnect("disconnect", 2, "undock"))
        assert v.windows == {}        # blanked
        assert not v.connected
        # after disconnect, further control is rejected until redock.
        assert not v.apply(Announce("announce", 2, _meta(3)))

    def test_redock_bumps_generation_and_clears(self):
        v = RemoteViewerState(generation=2)
        v.apply(Announce("announce", 2, _meta(1)))
        v.set_generation(3)
        assert v.windows == {} and v.connected
        assert v.apply(Announce("announce", 3, _meta(9)))
        assert 9 in v.windows

    def test_closed_removes_window(self):
        v = RemoteViewerState(generation=1)
        v.apply(Announce("announce", 1, _meta(1)))
        assert v.apply(Closed("closed", 1, 1, "exited"))
        assert v.windows == {}

    def test_configure_unknown_window_is_noop(self):
        v = RemoteViewerState(generation=1)
        assert not v.apply(Configure("configure", 1, 99, 10, 10))
