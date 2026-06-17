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
    RdpClientWrapper, SpecError, StreamSpec,
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
        assert "/f" not in client                         # windowed, never fullscreen
        assert "/size:640x400" in client
        assert "/p:otp123" in client                      # single-use OTP on the child
        assert any(a.startswith("/v:10.0.2.2:5555") for a in client)

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
