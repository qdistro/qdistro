"""Unit tests for the standalone Phase-1 status surface model (codex impl-8 §2).

The QML view is the untested-by-unit-tests shell; the model that maps a viewer
``status()`` dict onto the display rows is pure and tested here. It drives the
exact impl-8 disclosure fields end-to-end from a real ``RemoteViewer.status()``.
"""
from __future__ import annotations

from multimachine.status_surface import (
    REMOTE_DISCLOSURE, model_from_status, render_text,
)
from multimachine.viewer import RemoteViewer
from multimachine.sidechannel import Disconnect, RemoteWindowMeta, Announce


class _FakeProc:
    def poll(self): return None
    def terminate(self): pass


def _viewer(gen=7):
    return RemoteViewer(gen, lambda ann: _FakeProc(), source_machine="server")


def _announce(gen=7, wid=1, sid="vs-1-a", title="Build", **kw):
    meta = RemoteWindowMeta(window_id=wid, source_machine="server", stream_id=sid,
                            title=title, app_id="org.x.term", req_w=800,
                            req_h=600, **kw)
    return Announce("announce", gen, meta)


class TestStatusModel:
    def test_connected_window_maps_all_impl8_fields(self):
        v = _viewer()
        v.on_message(_announce(security_label="u:r:term:s0"))
        model = model_from_status(v.status())
        assert model.status == "connected" and model.generation == 7
        assert len(model.rows) == 1
        row = model.rows[0]
        assert row.title == "Build" and row.app_id == "org.x.term"
        assert row.source_machine == "server" and row.generation == 7
        assert row.disclosure == REMOTE_DISCLOSURE and row.remote is True
        assert row.security_label == "u:r:term:s0"   # opaque display text only

    def test_disconnected_blanks_rows(self):
        v = _viewer()
        v.on_message(_announce())
        v.on_message(Disconnect("disconnect", 7, "link dropped"))
        model = model_from_status(v.status())
        assert model.status == "disconnected" and model.rows == []

    def test_capacity_status_surfaced(self):
        v = _viewer()
        v.on_message(Disconnect("disconnect", 7, "no free pipewire output"))
        model = model_from_status(v.status())
        assert model.status == "capacity-exceeded"

    def test_empty_or_malformed_status_is_idle_not_misleading(self):
        # a blank/garbage status file must NOT render a false "connected".
        assert model_from_status({}).status == "idle"
        assert model_from_status({"status": "bogus"}).status == "idle"
        assert model_from_status({}).rows == []

    def test_untitled_window_shows_placeholder(self):
        v = _viewer()
        v.on_message(_announce(title=""))
        model = model_from_status(v.status())
        assert model.rows[0].title == "(untitled)"

    def test_render_text_shows_remote_disclosure_and_fields(self):
        v = _viewer()
        v.on_message(_announce())
        text = render_text(model_from_status(v.status()))
        assert REMOTE_DISCLOSURE in text
        assert "Build" in text and "org.x.term" in text
        assert "from=server" in text and "gen=7" in text
        assert "connected" in text

    def test_render_text_no_windows(self):
        text = render_text(model_from_status({"status": "idle", "generation": 3}))
        assert "no remote windows" in text
        assert "generation=3" in text
