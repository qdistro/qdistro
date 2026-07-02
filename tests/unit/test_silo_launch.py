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
    assert "exec env -i" in src
    assert 'TIER2_SILO="$TIER2_SILO"' in src
    assert 'TIER2_NETWORK="$TIER2_NETWORK"' in src
    assert "safe_source_profile /etc/qdistro/profile" in src
    assert 'QDISTRO_PROFILE="$QDISTRO_PROFILE"' in src
    assert "TIER2_ALLOW_PRIVESC=" not in src
    assert "TIER2_KEEP_CAPS=" not in src
    assert "TIER2_SECCOMP_PROFILE=" not in src
    # missing-user and uid-0 guards both exit nonzero before the spawn exec.
    assert "does not exist" in src and "exit 6" in src
    assert 'ADMIN_UID" != "0"' in src
    # The spawn must run UNDER the guards (exec is the last statement).
    exec_idx = src.rindex("exec env")
    guard_idx = src.index("ADMIN_UID=\"$(id -u")
    assert guard_idx < exec_idx, "admin-uid guard must precede the spawn exec"
    # Root-TCB: the env file is `.`-sourced as root, so the helper must verify
    # ownership/mode BEFORE sourcing — a refactor must not drop this guard.
    assert "require_trusted_env" in src
    src_idx = src.index('. "$ENV_FILE"')
    assert src.index("require_trusted_env \"$ENV_FILE\"") < src_idx, \
        "the trusted-env guard must run BEFORE the source"


def test_stop_helper_fails_closed_on_uid_0(tmp_path: Path) -> None:
    """ExecStop must drop to a NON-root admin uid to reach the admin-rootless
    container. If the fixed admin resolves to uid 0 it must refuse rather
    than 'stop' against root's empty store (which would orphan the container)."""
    env = _farm_with_id(tmp_path, id_output="0")
    res = subprocess.run(
        ["/bin/bash", str(_STOP), "work"],
        env=env, capture_output=True, text=True, check=False, timeout=30)
    assert res.returncode != 0
    assert "uid 0" in res.stderr


def test_stop_helper_fails_closed_on_missing_admin_user(tmp_path: Path) -> None:
    env = _farm_with_id(tmp_path, id_output=None)
    res = subprocess.run(
        ["/bin/bash", str(_STOP), "work"],
        env=env, capture_output=True, text=True, check=False, timeout=30)
    assert res.returncode != 0
    assert "does not exist" in res.stderr


def test_helpers_are_executable_shell() -> None:
    for p in (_LAUNCH, _STOP):
        assert p.exists(), p
        assert p.stat().st_mode & stat.S_IXUSR, f"{p} not executable"
        head = p.read_text().splitlines()[0]
        assert head.startswith("#!/bin/bash"), head


# --------------------------------------------------------------------------
# fixed admin-user launch env behavior.
#
# The daemon writes per-silo launch metadata into
# /run/qdistro/silo-launch/<name>.env (root-owned 0600); both helpers SOURCE it
# for silo/container metadata. The admin user itself is fixed to `admin`.
# Because the helpers `.`-source the file AS ROOT, they refuse a file not owned
# by the sourcing uid or group/other-writable (root TCB).
#
# Host tests run unprivileged, so they redirect the env dir
# (QDISTRO_SILO_LAUNCH_ENV_DIR, a TEST-ONLY override) into a tmp dir and write
# the env file owned by the test user (== the sourcing uid; the euid-relative
# guard is satisfied). A fake `id` resolves `id -u <user>` for chosen users while
# deferring the bare `id -u` euid self-check to the real id, and a recording fake
# spawn captures the env the launch helper hands spawn-tier2.
# --------------------------------------------------------------------------

def _farm(tmp_path: Path, *, users: dict[str, str] | None = None,
          missing: bool = False) -> tuple[Path, dict[str, str]]:
    """A PATH front dir with a fake `id` (and, for the launch helper, a recording
    fake spawn). `users` maps a username -> the uid `id -u <name>` should print;
    an unlisted name (or any name when `missing` is True) makes `id -u <name>`
    fail (rc 1, no output) — a non-existent admin user. The BARE `id -u` (euid
    self-check) always defers to the real id so the guard sees the true caller."""
    farm = tmp_path / "bin"
    farm.mkdir()
    users = users or {}
    # Build a case arm per known user; default arm fails (missing user).
    arms = "\n".join(
        f'        {u}) printf "%s\\n" "{uid}"; exit 0 ;;' for u, uid in users.items()
    )
    fake_id = farm / "id"
    fake_id.write_text(
        "#!/bin/bash\n"
        '# Bare `id -u` (euid self-check) -> real id (true caller uid).\n'
        'if [ "$1" = "-u" ] && [ "$#" -eq 1 ]; then exec /usr/bin/id -u; fi\n'
        'if [ "$1" = "-u" ] && [ "$#" -eq 2 ]; then\n'
        + ("    exit 1\n" if missing else
           "    case \"$2\" in\n" + arms + "\n        *) exit 1 ;;\n    esac\n")
        + 'fi\n'
        'exec /usr/bin/id "$@"\n')
    fake_id.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{farm}:{env.get('PATH', '')}"
    return farm, env


def _write_env_file(tmp_path: Path, name: str, *, mode: int = 0o600) -> Path:
    """Write a per-silo launch env file (test-user-owned) under a redirected env
    dir, mirroring the daemon's shlex-quoted KEY='VALUE' lines."""
    env_dir = tmp_path / "silo-launch"
    env_dir.mkdir(exist_ok=True)
    f = env_dir / f"{name}.env"
    lines = [
        "TIER2_SILO='work'",
        "TIER2_NETWORK='none'",
        "QD_WORKLOAD='browser'",
        "QD_CONTAINER='qdistro-silo-work'",
        "QD_APP_ARGV_JSON='[\"browser\"]'",
    ]
    f.write_text("\n".join(lines) + "\n")
    f.chmod(mode)
    return f


def _recording_spawn(farm: Path) -> Path:
    """Install a fake spawn-tier2 at /usr/bin/qdistro-tier2-spawn's FIRST helper
    candidate path is absolute, so we cannot front it on PATH. Instead the launch
    helper's THIRD candidate is <dir>/../tier2/spawn-tier2.sh; we cannot redirect
    that either. So we front `env` — the launch helper execs `env TIER2_*=.. SPAWN
    ..` and a recording `env` captures the resolved TIER2_ADMIN_UID without
    running the real spawn."""
    fake_env = farm / "env"
    rec = farm / "spawn-record"
    fake_env.write_text(
        "#!/bin/bash\n"
        f'rec="{rec}"\n'
        '# Record every NAME=VALUE assignment arg (TIER2_ROOT_LAUNCHER, '
        'TIER2_ADMIN_UID, ...) then STOP before exec-ing the real spawn.\n'
        ': >"$rec"\n'
        'for a in "$@"; do\n'
        '    [ "$a" = "-i" ] && continue\n'
        '    case "$a" in\n'
        '        *=*) printf "%s\\n" "$a" >>"$rec" ;;\n'
        '        *) break ;;\n'
        '    esac\n'
        'done\n'
        'exit 0\n')
    fake_env.chmod(0o755)
    return rec


def _run_launch(tmp_path: Path, name: str, env: dict[str, str]):
    env = dict(env)
    env["QDISTRO_SILO_LAUNCH_ENV_DIR"] = str(tmp_path / "silo-launch")
    return subprocess.run(
        ["/bin/bash", str(_LAUNCH), name],
        env=env, capture_output=True, text=True, check=False, timeout=30)


def _run_stop(tmp_path: Path, name: str, env: dict[str, str]):
    env = dict(env)
    env["QDISTRO_SILO_LAUNCH_ENV_DIR"] = str(tmp_path / "silo-launch")
    return subprocess.run(
        ["/bin/bash", str(_STOP), name],
        env=env, capture_output=True, text=True, check=False, timeout=30)


def test_launch_resolves_fixed_admin(tmp_path: Path) -> None:
    _write_env_file(tmp_path, "work")
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    rec = _recording_spawn(farm)
    res = _run_launch(tmp_path, "work", env)
    assert res.returncode == 0, res.stderr
    recorded = rec.read_text()
    assert "TIER2_ROOT_LAUNCHER=1" in recorded
    assert "TIER2_ADMIN_UID=1000" in recorded, recorded
    assert "TIER2_SILO=work" in recorded
    assert "TIER2_NETWORK=none" in recorded


def test_launch_scrubs_ambient_downgrade_env(tmp_path: Path) -> None:
    _write_env_file(tmp_path, "work")
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    rec = _recording_spawn(farm)
    env.update({
        "TIER2_ALLOW_PRIVESC": "1",
        "TIER2_KEEP_CAPS": "SYS_ADMIN",
        "TIER2_SECCOMP_PROFILE": "/tmp/empty.json",
        "TIER2_USE_SECCTX": "0",
    })
    res = _run_launch(tmp_path, "work", env)
    assert res.returncode == 0, res.stderr
    recorded = rec.read_text()
    assert "TIER2_ROOT_LAUNCHER=1" in recorded
    assert "TIER2_USE_SECCTX=" not in recorded
    assert "TIER2_ALLOW_PRIVESC=" not in recorded
    assert "TIER2_KEEP_CAPS=" not in recorded
    assert "TIER2_SECCOMP_PROFILE=" not in recorded


def test_launch_scrubs_ambient_profile(tmp_path: Path) -> None:
    _write_env_file(tmp_path, "work")
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    rec = _recording_spawn(farm)
    env["QDISTRO_PROFILE"] = "dev"

    res = _run_launch(tmp_path, "work", env)

    assert res.returncode == 0, res.stderr
    recorded = rec.read_text()
    assert "QDISTRO_PROFILE=daily-driver" in recorded


def test_launch_accepts_trusted_env_file_profile(tmp_path: Path) -> None:
    f = _write_env_file(tmp_path, "work")
    with f.open("a", encoding="utf-8") as fh:
        fh.write("QDISTRO_PROFILE='dev'\n")
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    rec = _recording_spawn(farm)

    res = _run_launch(tmp_path, "work", env)

    assert res.returncode == 0, res.stderr
    recorded = rec.read_text()
    assert "QDISTRO_PROFILE=dev" in recorded


def test_launch_fails_closed_on_missing_admin(tmp_path: Path) -> None:
    _write_env_file(tmp_path, "work")
    farm, env = _farm(tmp_path, users={}, missing=True)
    rec = _recording_spawn(farm)
    res = _run_launch(tmp_path, "work", env)
    assert res.returncode == 6, (res.returncode, res.stderr)
    assert "does not exist" in res.stderr
    assert not rec.exists() or rec.read_text() == "", \
        "spawn must NOT have run for a missing admin user"


def test_launch_fails_closed_on_uid_0_admin(tmp_path: Path) -> None:
    """A fixed admin that resolves to uid 0 must EXIT NONZERO (rootless podman
    needs a non-root uid; a root spawn would not be the admin-rootless store).
    Dynamically pins the launch-side uid-0 guard that the VM fail-closed lane can
    no longer drive (b168138 fixed the admin identity — no env-file injection)."""
    _write_env_file(tmp_path, "work")
    farm, env = _farm(tmp_path, users={"admin": "0"})
    rec = _recording_spawn(farm)
    res = _run_launch(tmp_path, "work", env)
    assert res.returncode == 6, (res.returncode, res.stderr)
    assert "uid 0" in res.stderr
    assert not rec.exists() or rec.read_text() == "", \
        "spawn must NOT have run for a uid-0 admin"


def test_launch_refuses_group_or_other_writable_env_file(tmp_path: Path) -> None:
    """Root TCB: the helper `.`-sources the file as root, so a group/other-
    writable file (a non-owner could rewrite it) must be REFUSED, never sourced."""
    f = _write_env_file(tmp_path, "work", mode=0o660)
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    _recording_spawn(farm)
    res = _run_launch(tmp_path, "work", env)
    assert res.returncode != 0
    assert "writable" in res.stderr, res.stderr
    # And other-writable too.
    f.chmod(0o606)
    res = _run_launch(tmp_path, "work", env)
    assert res.returncode != 0
    assert "writable" in res.stderr


def test_stop_uses_fixed_admin(tmp_path: Path) -> None:
    _write_env_file(tmp_path, "work")
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    # Recording runuser captures its `-u <user>` target.
    rec = farm / "runuser-record"
    fake_runuser = farm / "runuser"
    fake_runuser.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$2" >"{rec}"\n'   # $1=-u $2=<user>
        'exit 0\n')
    fake_runuser.chmod(0o755)
    res = _run_stop(tmp_path, "work", env)
    assert res.returncode == 0, res.stderr
    assert rec.read_text().strip() == "admin"


def test_stop_missing_env_file_falls_back_best_effort(tmp_path: Path) -> None:
    """A MISSING env file at stop time is best-effort: the helper falls back to
    the fixed admin rather than failing (ExecStop is best-effort and the daemon
    re-verifies)."""
    # No env file written.
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    rec = farm / "runuser-record"
    fake_runuser = farm / "runuser"
    fake_runuser.write_text(
        "#!/bin/bash\n" f'printf "%s\\n" "$2" >"{rec}"\nexit 0\n')
    fake_runuser.chmod(0o755)
    res = _run_stop(tmp_path, "work", env)
    assert res.returncode == 0, res.stderr
    assert rec.read_text().strip() == "admin"


def test_stop_refuses_unsafe_env_file(tmp_path: Path) -> None:
    """Root TCB: a PRESENT but group/other-writable env file is a hard fail for
    the stop helper too (someone planted/rewrote it)."""
    _write_env_file(tmp_path, "work", mode=0o666)
    farm, env = _farm(tmp_path, users={"admin": "1000"})
    res = _run_stop(tmp_path, "work", env)
    assert res.returncode != 0
    assert "writable" in res.stderr, res.stderr


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
