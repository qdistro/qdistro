"""Tests for the shipped-RDP <-> side-channel bridge (Phase-1 viewer core)."""
from __future__ import annotations

from multimachine.bridge import (
    SourceWindowInfo, ViewStreamApproved, bridge_approved, bridge_torn_down,
    mint_stream_id, rdp_client_argv, rdp_password,
)
from multimachine.sidechannel import (
    Announce, Closed, Disconnect, RemoteViewerState,
)


def _approved(port=43210):
    return ViewStreamApproved("weston.pipewire-3", port, "/tmp/c.pem", "otp123")


def _source(wid=7):
    return SourceWindowInfo(window_id=wid, source_machine="server",
                            title="Build", app_id="org.x.term",
                            req_w=800, req_h=600)


class TestBridgeApproved:
    def test_builds_announce_with_metadata(self):
        ann = bridge_approved(_approved(), _source(7), generation=2)
        assert isinstance(ann, Announce)
        assert ann.generation == 2
        assert ann.meta.window_id == 7
        assert ann.meta.title == "Build"
        assert ann.meta.stream_id  # minted

    def test_no_rdp_credentials_in_sidechannel(self):
        ann = bridge_approved(_approved(), _source(), generation=1)
        blob = str(ann.meta.__dict__)
        assert "otp123" not in blob          # password never crosses the control channel
        assert "/tmp/c.pem" not in blob

    def test_fresh_nonce_per_export_even_for_identical_endpoint(self):
        # codex impl-3 finding 1: same window + same endpoint must still mint a
        # DIFFERENT stream_id each export (replay / rapid close-reopen safety).
        a = bridge_approved(_approved(43210), _source(7), generation=1)
        b = bridge_approved(_approved(43210), _source(7), generation=1)
        assert a.meta.stream_id != b.meta.stream_id

    def test_injected_nonce_is_used(self):
        sid = mint_stream_id(7, nonce=lambda: "deadbeef")
        assert sid == "vs-7-deadbeef"

    def test_announce_applies_to_viewer_and_correlates_by_stream(self):
        ann = bridge_approved(_approved(), _source(7), generation=3)
        v = RemoteViewerState(generation=3)
        assert v.apply(ann)
        assert v.proxy_for_stream(ann.meta.stream_id).meta.window_id == 7


class TestBridgeTornDown:
    def test_source_close_to_closed(self):
        assert isinstance(bridge_torn_down("source toplevel closed", 1, 7), Closed)

    def test_link_to_disconnect(self):
        assert isinstance(bridge_torn_down("subscriber disconnected", 1, 7), Disconnect)


class TestRdpClientArgv:
    def test_forces_no_scaling_and_no_password_on_argv(self):
        argv = rdp_client_argv(_approved(43210), host="10.0.2.2")
        assert argv[0] == "sdl-freerdp"
        assert "/v:10.0.2.2:43210" in argv
        assert "/scale:100" in argv          # no hidden client-side scaling
        assert "/from-stdin" in argv         # OTP read from stdin, not argv
        # the OTP must NOT appear anywhere in argv (finding 5: argv leak).
        assert not any("otp123" in a for a in argv)
        assert "/cert:ignore" in argv        # TOFU, default

    def test_password_supplied_via_stdin_helper(self):
        assert rdp_password(_approved(43210)) == "otp123"

    def test_verify_cert_pins_cert_path(self):
        argv = rdp_client_argv(_approved(), verify_cert=True)
        assert any(a.startswith("/cert:name:/tmp/c.pem") for a in argv)
        assert "/cert:ignore" not in argv

    def test_size_optional(self):
        argv = rdp_client_argv(_approved(), width=1280, height=720)
        assert "/size:1280x720" in argv

    def test_fullscreen_knob_adds_f(self):
        # impl-9 Q2: the live managed gate decodes fullscreen at the kiosk origin.
        assert "/f" not in rdp_client_argv(_approved())
        argv = rdp_client_argv(_approved(), fullscreen=True)
        assert "/f" in argv
        assert "/from-stdin" in argv          # still OTP-on-stdin, no argv leak
        assert not any("otp123" in a for a in argv)

    def test_username_knob_optional(self):
        # default: no /u (the OTP on stdin authenticates).
        assert not any(a.startswith("/u:") for a in rdp_client_argv(_approved()))
        argv = rdp_client_argv(_approved(), username="mm")
        assert "/u:mm" in argv
        # /u precedes /from-stdin (the proven decoder argv order).
        assert argv.index("/u:mm") < argv.index("/from-stdin")
