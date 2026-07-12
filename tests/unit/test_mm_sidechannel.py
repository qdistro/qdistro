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
    TitleChanged, decode, encode, map_torn_down,
)


def _meta(wid=1, **kw):
    kw.setdefault("stream_id", f"s{wid}")
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
        assert v.apply(Configure("configure", 4, 1, 1024, 768, "s1"))
        assert (v.windows[1].w, v.windows[1].h) == (1024, 768)
        assert v.apply(Focus("focus", 4, 1, True, "s1"))
        assert v.windows[1].focused

    def test_single_focused_proxy(self):
        v = RemoteViewerState(generation=1)
        v.apply(Announce("announce", 1, _meta(1)))
        v.apply(Announce("announce", 1, _meta(2)))
        v.apply(Focus("focus", 1, 1, True, "s1"))
        v.apply(Focus("focus", 1, 2, True, "s2"))
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
        assert v.apply(Closed("closed", 1, 1, "exited", "s1"))
        assert v.windows == {}

    def test_configure_unknown_window_is_noop(self):
        v = RemoteViewerState(generation=1)
        assert not v.apply(Configure("configure", 1, 99, 10, 10, "sX"))


class TestStreamJoinKey:
    def test_stream_id_roundtrips_and_looks_up(self):
        m = Announce("announce", 1, _meta(1, stream_id="abc123"))
        got = decode(encode(m))
        assert got.meta.stream_id == "abc123"
        v = RemoteViewerState(generation=1)
        v.apply(m)
        v.apply(Announce("announce", 1, _meta(2, stream_id="def456")))
        assert v.proxy_for_stream("abc123").meta.window_id == 1
        assert v.proxy_for_stream("def456").meta.window_id == 2
        assert v.proxy_for_stream("nope") is None

    def test_distinct_windows_have_distinct_streams(self):
        v = RemoteViewerState(generation=1)
        v.apply(Announce("announce", 1, _meta(1, stream_id="s1")))
        v.apply(Announce("announce", 1, _meta(2, stream_id="s2")))
        assert {w.stream_id for w in v.windows.values()} == {"s1", "s2"}


class TestStreamIdentitySafety:
    def test_announce_requires_non_empty_stream_id(self):
        v = RemoteViewerState(generation=1)
        assert not v.apply(Announce("announce", 1, _meta(1, stream_id="")))
        assert any(r[2] == "empty-stream-id" for r in v.rejected)

    def test_duplicate_live_stream_id_rejected(self):
        v = RemoteViewerState(generation=1)
        assert v.apply(Announce("announce", 1, _meta(1, stream_id="dup")))
        # a second announce reusing the same live stream_id is a replay/collision
        assert not v.apply(Announce("announce", 1, _meta(2, stream_id="dup")))
        assert any(r[2] == "duplicate-stream-id" for r in v.rejected)

    def test_control_for_wrong_stream_rejected(self):
        # window_id reused across exports in the SAME generation: a delayed
        # Configure/Closed from the prior export (different stream_id) must not
        # mutate/remove the new proxy (codex impl-3 finding 2).
        v = RemoteViewerState(generation=1)
        v.apply(Announce("announce", 1, _meta(1, stream_id="new")))
        # stale Closed from the prior export of window 1 (stream_id "old")
        assert not v.apply(Closed("closed", 1, 1, "exited", "old"))
        assert 1 in v.windows                      # new proxy survives
        assert not v.apply(Configure("configure", 1, 1, 9, 9, "old"))
        assert any(r[2] == "stream-id-mismatch" for r in v.rejected)

    def test_matching_stream_control_applies(self):
        v = RemoteViewerState(generation=1)
        v.apply(Announce("announce", 1, _meta(1, stream_id="abc")))
        assert v.apply(Configure("configure", 1, 1, 9, 9, "abc"))
        assert (v.windows[1].w, v.windows[1].h) == (9, 9)


class TestSecurityLabelSemantics:
    def test_label_is_opaque_and_optional(self):
        # empty is valid; arbitrary opaque string survives round-trip unparsed.
        m = Announce("announce", 1, _meta(1, security_label=""))
        assert decode(encode(m)).meta.security_label == ""
        m2 = Announce("announce", 1, _meta(1, security_label="u:silo:build:m=server"))
        assert decode(encode(m2)).meta.security_label == "u:silo:build:m=server"

    def test_label_not_used_for_authorization(self):
        # the viewer applies control regardless of the label value — Phase 1
        # makes NO authorization decision from it (codex impl-2).
        v = RemoteViewerState(generation=1)
        assert v.apply(Announce("announce", 1, _meta(1, security_label="anything")))
        assert 1 in v.windows


class TestGenerationMonotonicity:
    def test_redock_requires_strictly_newer_generation(self):
        v = RemoteViewerState(generation=5)
        with pytest.raises(ValueError, match="strictly newer"):
            v.set_generation(5)
        with pytest.raises(ValueError, match="strictly newer"):
            v.set_generation(4)
        v.set_generation(6)  # ok
        assert v.generation == 6


class TestTornDownMapping:
    def test_source_close_maps_to_closed(self):
        msg = map_torn_down("source toplevel closed", 3, 7)
        assert isinstance(msg, Closed) and msg.window_id == 7

    def test_link_teardown_maps_to_disconnect(self):
        for reason in ("subscriber disconnected", "admin revoked", "compositor locked", ""):
            assert isinstance(map_torn_down(reason, 3, 7), Disconnect)

    def test_torn_down_disconnect_blanks_via_viewer(self):
        v = RemoteViewerState(generation=3)
        v.apply(Announce("announce", 3, _meta(1)))
        v.apply(map_torn_down("admin revoked", 3, 1))
        assert v.windows == {} and not v.connected
