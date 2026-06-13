"""Unit tests for qdistro-silo-launch (fableplan2 task 04 CLI).

The CLI is a thin D-Bus wrapper, but the D-Bus signatures are load-bearing:
StopSilo's signature is "si" (name + grace_s), so a one-arg call raises
"More items found in D-Bus signature than in Python arguments" at call time
and the stop silently fails (the caller `|| true`s it). These tests pin the
exact (method, args) the CLI emits so that arity regression is caught here,
off the VM.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import qdistro_silo_launch as sl

_REPO = Path(__file__).resolve().parents[2]
_LAUNCH = _REPO / "session_manager" / "qdistro-tier2-silo-launch"
_STOP = _REPO / "session_manager" / "qdistro-tier2-silo-stop"


def _farm_with_id(tmp_path: Path, id_output: str | None) -> dict[str, str]:
    """Build a PATH front dir whose `id` prints id_output (and rc 0), or — if
    id_output is None — fails (simulating a missing admin user). Real coreutils
    stay reachable behind it so the scripts still find env/runuser/podman."""
    farm = tmp_path / "bin"
    farm.mkdir()
    fake_id = farm / "id"
    if id_output is None:
        fake_id.write_text("#!/bin/bash\nexit 1\n")
    else:
        # Only intercept `id -u <user>`; defer anything else to real id so we
        # don't break unrelated callers.
        fake_id.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "-u" ]; then printf "%s\\n" ' f"'{id_output}'" "; exit 0; fi\n"
            'exec /usr/bin/id "$@"\n')
    fake_id.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{farm}:{env.get('PATH', '')}"
    return env


def test_launch_helper_admin_resolution_is_fail_closed_in_source() -> None:
    """The launcher hands spawn-tier2 the root-launcher mode + the resolved
    admin uid; a missing user or uid 0 must EXIT NONZERO (no un-tagged
    fallback). The full path needs /run + a real admin session, so it is proven
    end-to-end by the VM lane tier2-silo-secctx-wiretag.bats; here we pin the
    fail-closed guards + the root-launcher hand-off in the shipped source so a
    refactor cannot quietly drop them."""
    src = _LAUNCH.read_text()
    assert "TIER2_ROOT_LAUNCHER=1" in src, \
        "launcher must hand spawn-tier2 the root-launcher mode"
    assert 'TIER2_ADMIN_UID="$ADMIN_UID"' in src
    # missing-user and uid-0 guards both exit nonzero before the spawn exec.
    assert "does not exist" in src and "exit 6" in src
    assert 'ADMIN_UID" != "0"' in src
    # The spawn must run UNDER the guards (exec is the last statement).
    exec_idx = src.rindex("exec env")
    guard_idx = src.index("ADMIN_UID=\"$(id -u")
    assert guard_idx < exec_idx, "admin-uid guard must precede the spawn exec"


def test_stop_helper_fails_closed_on_uid_0(tmp_path: Path) -> None:
    """ExecStop must drop to a NON-root admin uid to reach the admin-rootless
    container. If the configured admin resolves to uid 0 it must refuse rather
    than 'stop' against root's empty store (which would orphan the container)."""
    env = _farm_with_id(tmp_path, id_output="0")
    env["QDISTRO_ADMIN_USER"] = "root"
    res = subprocess.run(
        ["/bin/bash", str(_STOP), "work"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=30)
    assert res.returncode != 0
    assert "uid 0" in res.stderr


def test_stop_helper_fails_closed_on_missing_admin_user(tmp_path: Path) -> None:
    env = _farm_with_id(tmp_path, id_output=None)
    env["QDISTRO_ADMIN_USER"] = "nope"
    res = subprocess.run(
        ["/bin/bash", str(_STOP), "work"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, timeout=30)
    assert res.returncode != 0
    assert "does not exist" in res.stderr


def test_helpers_are_executable_shell() -> None:
    for p in (_LAUNCH, _STOP):
        assert p.exists(), p
        assert p.stat().st_mode & stat.S_IXUSR, f"{p} not executable"
        head = p.read_text().splitlines()[0]
        assert head.startswith("#!/bin/bash"), head


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
