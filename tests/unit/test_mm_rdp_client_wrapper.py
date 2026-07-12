"""Unit tests for the durable pixel-backend supervisor (codex impl-34 Q5).

Exercises the :class:`RdpClientWrapper` contract headless with a fake spawn — no
real FreeRDP / secctx-exec: spec validation (fail closed), the
secctx-exec-wrapped windowed argv, process truth, and the broker-token-gated
teardown (the close button never reaches here — only a post-Closed broker
teardown with the right token does).
"""
from __future__ import annotations

import pytest

from multimachine.rdp_client_wrapper import (
    RdpClientWrapper, SpecError, StreamSpec, dispatch_request,
)


def _spec(**over) -> StreamSpec:
    base = dict(origin="vm-a", stream_id="sid-abc", generation=51,
                app_id="qdistro.mm.vm-a.streamA", instance_id="vm-a-streamA-1",
                rdp_host="10.0.2.2", rdp_port=5555, width=640, height=400,
                rdp_user="mm", allow_input=1)
    base.update(over)
    return StreamSpec(**base)


class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid
        self._exit = None
        self.terminated = False

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = -15

    def wait(self, timeout=None):
        return self._exit


def _wrapper(spec=None, *, token="tok-secret", proc=None, rdp_client="sdl-freerdp"):
    proc = proc or _FakeProc()
    spawned = {}

    def spawn(argv):
        spawned["argv"] = argv
        return proc

    w = RdpClientWrapper(spec or _spec(), teardown_token=token, spawn=spawn,
                         rdp_client=rdp_client)
    return w, proc, spawned


class TestSpecValidation:
    def test_bad_app_id_prefix_fails_closed(self):
        with pytest.raises(SpecError):
            _spec(app_id="qdistro.tier4.vm-a").validate()
        # the origin must match the app_id segment too.
        with pytest.raises(SpecError):
            _spec(origin="vm-b", app_id="qdistro.mm.vm-a.streamA").validate()

    def test_empty_or_bad_fields_fail_closed(self):
        for over in (dict(stream_id=""), dict(generation=0), dict(instance_id=""),
                     dict(rdp_port=0), dict(width=0), dict(origin="")):
            with pytest.raises(SpecError):
                _spec(**over).validate()

    def test_good_spec_validates(self):
        _spec().validate()      # no raise

    def test_allow_input_is_boolean_integer(self):
        for value in (-1, 2, "1", None, False, True):
            with pytest.raises(SpecError):
                _spec(allow_input=value).validate()

    def test_dotted_or_ambiguous_stream_label_fails_closed(self):
        for app_id in ("qdistro.mm.vm-a.a.b", "qdistro.mm.vm-a.bad label",
                       "qdistro.mm.vm-a."):
            with pytest.raises(SpecError):
                _spec(app_id=app_id).validate()

    def test_empty_token_is_rejected(self):
        with pytest.raises(SpecError):
            RdpClientWrapper(_spec(), teardown_token="", spawn=lambda a: _FakeProc())


class TestArgv:
    def test_argv_wraps_secctx_exec_windowed(self):
        w, _, _ = _wrapper(rdp_client="wlfreerdp")
        argv = w.build_argv("otp123")
        assert argv[0] == "qdistro-secctx-exec"
        assert "--sandbox-engine" in argv
        i = argv.index("--sandbox-engine")
        assert argv[i + 1] == "qdistro.mm"
        assert "qdistro.mm.vm-a.streamA" in argv          # exact secctx app_id
        assert "vm-a-streamA-1" in argv                   # instance id
        # everything after "--" is the FreeRDP child argv.
        client = argv[argv.index("--") + 1:]
        assert client[0] == "wlfreerdp"                   # resolved windowed client
        assert "/p:otp123" not in client                  # OTP absent from every argv
        assert "/args-from:fd:0" in client                # delivered via inherited fd
        assert len(client) == 2                            # args-from must stand alone
        fd_args = w.build_fd_args("otp123").decode().splitlines()
        assert "/f" not in fd_args                        # windowed, never fullscreen
        assert "/size:640x400" in fd_args
        assert "/p:otp123" in fd_args                     # secret only in inherited fd
        assert any(a.startswith("/v:10.0.2.2:5555") for a in fd_args)

    def test_empty_otp_fails_closed(self):
        w, _, _ = _wrapper()
        with pytest.raises(SpecError):
            w.build_argv("")


class TestLifecycleAndProcessTruth:
    def test_start_spawns_and_reports_truth(self):
        w, proc, spawned = _wrapper()
        pid = w.start("otp123")
        assert pid == proc.pid
        assert spawned["argv"][0] == "qdistro-secctx-exec"
        t = w.process_truth()
        assert t["pid"] == proc.pid and t["alive"] and t["exit_status"] is None
        assert t["stream_id"] == "sid-abc" and t["generation"] == 51

    def test_double_start_rejected(self):
        w, _, _ = _wrapper()
        w.start("otp123")
        with pytest.raises(SpecError):
            w.start("otp123")

    def test_backend_exit_is_process_truth_not_close(self):
        # a FreeRDP that dies on its own is pixel_backend_lost evidence — the
        # wrapper reports the exit status; it does NOT close/teardown anything.
        w, proc, _ = _wrapper()
        w.start("otp123")
        proc._exit = 1                       # FreeRDP crashed
        assert not w.alive()
        assert w.exit_status() == 1
        assert not proc.terminated           # wrapper did NOT terminate it


class TestTokenGatedTeardown:
    def test_correct_token_tears_down(self):
        w, proc, _ = _wrapper(token="tok-secret")
        w.start("otp123")
        assert w.teardown("sid-abc", 51, "tok-secret") is True
        assert proc.terminated

    def test_wrong_token_rejected(self):
        w, proc, _ = _wrapper(token="tok-secret")
        w.start("otp123")
        assert w.teardown("sid-abc", 51, "WRONG") is False
        assert not proc.terminated

    def test_wrong_stream_or_generation_rejected(self):
        w, proc, _ = _wrapper(token="tok-secret")
        w.start("otp123")
        assert w.teardown("sid-OTHER", 51, "tok-secret") is False
        assert w.teardown("sid-abc", 99, "tok-secret") is False
        assert not proc.terminated


class TestDispatchRequestFailClosed:
    """The socket adapter's pure core (codex impl-35 HIGH): malformed broker IPC
    must FAIL CLOSED — never crash, never tear down FreeRDP without a valid token."""

    def test_status_request(self):
        w, proc, _ = _wrapper()
        w.start("otp123")
        resp, accepted = dispatch_request(w, {"cmd": "status"})
        assert accepted is False and resp["pid"] == proc.pid and resp["alive"]

    def test_malformed_generation_rejected_not_torn_down(self):
        w, proc, _ = _wrapper(token="tok-secret")
        w.start("otp123")
        # non-integer generation must NOT raise and must NOT tear down.
        resp, accepted = dispatch_request(
            w, {"cmd": "teardown", "stream_id": "sid-abc",
                "generation": "x", "token": "tok-secret"})
        assert accepted is False and resp == {"accepted": False}
        assert not proc.terminated

    def test_missing_fields_rejected(self):
        w, proc, _ = _wrapper(token="tok-secret")
        w.start("otp123")
        for req in ({"cmd": "teardown"},
                    {"cmd": "teardown", "token": "tok-secret"},
                    {}, {"cmd": "bogus"}, None):
            _resp, accepted = dispatch_request(w, req)
            assert accepted is False
        assert not proc.terminated

    def test_valid_teardown_accepted(self):
        w, proc, _ = _wrapper(token="tok-secret")
        w.start("otp123")
        resp, accepted = dispatch_request(
            w, {"cmd": "teardown", "stream_id": "sid-abc",
                "generation": 51, "token": "tok-secret"})
        assert accepted is True and resp == {"accepted": True}
        assert proc.terminated
