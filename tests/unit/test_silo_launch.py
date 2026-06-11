"""Unit tests for qdistro-silo-launch (fableplan2 task 04 CLI).

The CLI is a thin D-Bus wrapper, but the D-Bus signatures are load-bearing:
StopSilo's signature is "si" (name + grace_s), so a one-arg call raises
"More items found in D-Bus signature than in Python arguments" at call time
and the stop silently fails (the caller `|| true`s it). These tests pin the
exact (method, args) the CLI emits so that arity regression is caught here,
off the VM.
"""
from __future__ import annotations

import qdistro_silo_launch as sl


class _RecordingMgr:
    """Stand-in for the dbus.Interface proxy: records (method, args)."""
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def StartSilo(self, *args):
        self.calls.append(("StartSilo", args))

    def StopSilo(self, *args):
        self.calls.append(("StopSilo", args))


def _run(monkeypatch, argv):
    mgr = _RecordingMgr()
    monkeypatch.setattr(sl, "_session_manager", lambda: mgr)
    rc = sl.main(argv)
    return rc, mgr


def test_start_calls_startsilo_with_name_only(monkeypatch):
    rc, mgr = _run(monkeypatch, ["work"])
    assert rc == 0
    assert mgr.calls == [("StartSilo", ("work",))]


def test_stop_passes_name_and_grace(monkeypatch):
    # The D-Bus signature is "si": StopSilo MUST get the grace int too, or the
    # proxy raises and the stop is a no-op. Default grace mirrors the daemon's
    # DEFAULT_STOP_GRACE_S (5).
    rc, mgr = _run(monkeypatch, ["--stop", "work"])
    assert rc == 0
    assert mgr.calls == [("StopSilo", ("work", 5))]


def test_stop_honours_explicit_grace(monkeypatch):
    rc, mgr = _run(monkeypatch, ["--stop", "--grace", "30", "work"])
    assert rc == 0
    assert mgr.calls == [("StopSilo", ("work", 30))]


def test_dbus_error_is_surfaced_as_nonzero(monkeypatch, capsys):
    class _Boom:
        def StartSilo(self, *a):
            raise RuntimeError("bus exploded")
    monkeypatch.setattr(sl, "_session_manager", lambda: _Boom())
    rc = sl.main(["work"])
    assert rc == 1
    assert "FATAL" in capsys.readouterr().err


def test_missing_silo_is_a_usage_error(monkeypatch):
    # No silo and no --status: argparse exits non-zero (SystemExit).
    import pytest
    with pytest.raises(SystemExit):
        sl.main([])
