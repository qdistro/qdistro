"""Tests for the SDK helper qdistro_app.open_in_disposable (07-plan P2).

The helper is a CONVENIENCE over the shipped trusted launch binary — it is NOT
the security boundary (the binary re-does every gate). These tests pin its
client-side path validation, the class->workload resolution against the REAL
shipped registry resolver, the argv it builds, and that it surfaces a refusal
from the binary as an OpenInDisposableError.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("dbus")

REPO_ROOT = Path(__file__).resolve().parents[2]
_SDK = REPO_ROOT / "sdk"
sys.path.insert(0, str(_SDK))
import qdistro_app  # noqa: E402

RESOLVER = REPO_ROOT / "session_manager" / "qdistro_disposable_classes.py"
REGISTRY = REPO_ROOT / "session_manager" / "disposable-classes.toml"


def _fake_spawn_bin(tmp_path: Path, *, rc: int = 0,
                    stdout: str = "", stderr: str = "") -> Path:
    """A fake qdistro-tier2-spawn that records its argv + env to files and
    emits a canned contract / error."""
    bin_path = tmp_path / "qdistro-tier2-spawn"
    argv_file = tmp_path / "spawn-argv"
    env_file = tmp_path / "spawn-env"
    bin_path.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        f'env > "{env_file}"\n'
        f'printf "%s" "{stdout}"\n'
        f'printf "%s" "{stderr}" >&2\n'
        f"exit {rc}\n"
    )
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return bin_path


def _base_env() -> dict[str, str]:
    # Point the SDK's class resolver at the in-tree resolver + registry.
    return {
        "QDISTRO_DISPOSABLE_CLASSES_RESOLVER": str(RESOLVER),
        "QDISTRO_DISPOSABLE_CLASSES": str(REGISTRY),
    }


def test_open_rejects_relative_path(tmp_path):
    with pytest.raises(qdistro_app.OpenInDisposableError, match="absolute"):
        qdistro_app.open_in_disposable("rel/path", class_name="agent-scratch")


def test_open_rejects_missing_path(tmp_path):
    with pytest.raises(qdistro_app.OpenInDisposableError, match="does not exist"):
        qdistro_app.open_in_disposable(str(tmp_path / "nope.txt"),
                                       class_name="agent-scratch")


def test_open_rejects_empty_class(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(qdistro_app.OpenInDisposableError, match="class_name"):
        qdistro_app.open_in_disposable(str(f), class_name="")


def test_open_disabled_class_refused_via_resolver(tmp_path):
    """A hostile class (pdf) is rejected by the shipped resolver (exit 4) — the
    SDK surfaces it as an error WITHOUT spawning."""
    f = tmp_path / "a.pdf"
    f.write_text("x")
    spawn = _fake_spawn_bin(tmp_path)
    with pytest.raises(qdistro_app.OpenInDisposableError, match="not openable"):
        qdistro_app.open_in_disposable(
            str(f), class_name="pdf", spawn_bin=str(spawn),
            extra_env=_base_env())
    # The spawn binary was never invoked (no argv file written).
    assert not (tmp_path / "spawn-argv").exists()


def test_open_unknown_class_refused_via_resolver(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    spawn = _fake_spawn_bin(tmp_path)
    with pytest.raises(qdistro_app.OpenInDisposableError):
        qdistro_app.open_in_disposable(
            str(f), class_name="not-a-class", spawn_bin=str(spawn),
            extra_env=_base_env())


def test_open_builds_correct_invocation(tmp_path):
    """An enabled class resolves to its workload, and the SDK execs the binary
    with --disposable <workload> + TIER2_OPEN_CLASS + TIER2_RO_INPUT set."""
    f = tmp_path / "note.txt"
    f.write_text("hi")
    contract = "LAUNCH_TOKEN=abc\nCONTAINER=disp-weston-terminal-x\nIMAGE=img\nAPP_ID=qdistro.disp.deadbeef\n"
    spawn = _fake_spawn_bin(tmp_path, rc=0, stdout=contract)
    out = qdistro_app.open_in_disposable(
        str(f), class_name="agent-scratch", spawn_bin=str(spawn),
        extra_env=_base_env())
    # Returned contract is parsed.
    assert out["CONTAINER"] == "disp-weston-terminal-x"
    assert out["LAUNCH_TOKEN"] == "abc"
    # argv: --disposable weston-terminal -- ...
    argv = (tmp_path / "spawn-argv").read_text().splitlines()
    assert argv[0] == "--disposable"
    assert argv[1] == "weston-terminal"  # resolved from agent-scratch
    assert "--" in argv
    # env: the open class + RO input + detach are passed to the binary.
    env_lines = (tmp_path / "spawn-env").read_text().splitlines()
    env = dict(ln.split("=", 1) for ln in env_lines if "=" in ln)
    assert env["TIER2_OPEN_CLASS"] == "agent-scratch"
    assert env["TIER2_RO_INPUT"] == os.path.realpath(str(f))
    assert env["TIER2_DETACH"] == "1"


def test_open_surfaces_binary_refusal(tmp_path):
    """If the binary refuses (e.g. the broker open gate denies), the SDK raises
    with the binary's stderr."""
    f = tmp_path / "note.txt"
    f.write_text("hi")
    spawn = _fake_spawn_bin(
        tmp_path, rc=2,
        stderr="broker has no allow rule ... decision=unknown")
    with pytest.raises(qdistro_app.OpenInDisposableError, match="decision=unknown"):
        qdistro_app.open_in_disposable(
            str(f), class_name="agent-scratch", spawn_bin=str(spawn),
            extra_env=_base_env())


def test_open_dir_input_allowed(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    (d / "f.txt").write_text("x")
    spawn = _fake_spawn_bin(tmp_path, rc=0, stdout="CONTAINER=disp-x\n")
    out = qdistro_app.open_in_disposable(
        str(d), class_name="agent-scratch", spawn_bin=str(spawn),
        extra_env=_base_env())
    assert out["CONTAINER"] == "disp-x"
    env_lines = (tmp_path / "spawn-env").read_text().splitlines()
    env = dict(ln.split("=", 1) for ln in env_lines if "=" in ln)
    assert env["TIER2_RO_INPUT"] == os.path.realpath(str(d))
