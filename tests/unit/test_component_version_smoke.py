"""Per-component `--version` health smoke gate.

From the NixOS test-integrity review (item 7): there was no cheap smoke
check that each shipped, user-facing component is actually invokable and
reports a basic identity/version. This test fills that gap.

For every covered entrypoint it spawns ``python3 <entrypoint> --version``
as a subprocess and asserts:

  * exit code 0, and
  * stdout names the component and the ``(qdistro)`` marker.

The ``--version`` path on each component is deliberately *cheap and
side-effect-free*: it must not require root, a running D-Bus, a VM, or
hardware. That is what makes it a valid host-gate (qci ``host`` runs
``python3 -m pytest tests/unit``, so this test rides along with no extra
wiring). Each component prints ``<prog> (qdistro)``; the format is kept
identical across components so a future version bump is grep-able.

DELIBERATELY EXCLUDED (cannot be smoke-checked headless without faking a
pass — see the module-level EXCLUDED note below): the D-Bus/Qt daemons
and GUI apps whose only real entrypoint immediately connects to a bus,
claims a well-known name, or opens a display. Adding a fake ``--version``
to those would not exercise anything the smoke gate cares about and would
drift from their actual (argv-less) systemd ExecStart contract. They are
covered by the in-VM bats/integration gates instead.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# tests/unit/ -> tests/ -> repo root
_REPO = Path(__file__).resolve().parents[2]

# Component source dirs that the covered entrypoints import siblings from.
# Spawned subprocesses need these on PYTHONPATH because on the host the
# flat per-dir layout is not yet collapsed into one install prefix.
_PYTHONPATH = [
    "broker",
    # The broker imports two pure modules (qdistro_disposables,
    # qdistro_disposable_classes) that live under session_manager/ in the
    # source tree. Production flattens them beside the broker at install
    # time (scripts/install/install-broker-for-qdwin.sh), so the spawned
    # subprocess needs session_manager/ on PYTHONPATH to mirror that.
    "session_manager",
    "workflow",
    "cli",
    "recall",
    "snapshots",
    "phone",
    "qsu",
    "pwd",
    "daemons/forward",
    "tier4-vm",
]

# (repo-relative entrypoint, expected prog name in --version output)
COVERED = [
    ("broker/qdistro_admin_broker.py", "qdistro-admin-broker"),
    ("qsu/qsu.py", "qsu"),
    ("cli/qdistro_recall_cli.py", "qdistro-recall"),
    ("cli/qdistro_recall_admin_cli.py", "qdistro-recall-admin"),
    ("cli/qdistro_approvals.py", "qdistro-approvals"),
    ("phone/qdistro_phone_cli.py", "qdistro-phone"),
    ("pwd/qdistro-pwd-admin.py", "qdistro-pwd-admin"),
    ("daemons/forward/qdistro-forward.py", "qdistro-forward"),
    ("tier4-vm/tier4_control.py", "tier4_control"),
]


def _env():
    env = dict(os.environ)
    extra = ":".join(str(_REPO / p) for p in _PYTHONPATH)
    env["PYTHONPATH"] = (
        extra + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    return env


def _run_version(entry: str) -> subprocess.CompletedProcess:
    path = _REPO / entry
    assert path.is_file(), f"covered entrypoint missing: {entry}"
    return subprocess.run(
        [sys.executable, str(path), "--version"],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=30,
        # No stdin: a --version probe must never block on input.
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("entry,prog", COVERED, ids=[e for e, _ in COVERED])
def test_component_version_smoke(entry: str, prog: str) -> None:
    """Each covered component reports a version cheaply and exits 0."""
    cp = _run_version(entry)
    assert cp.returncode == 0, (
        f"{entry} --version exited {cp.returncode}\n"
        f"stdout={cp.stdout!r}\nstderr={cp.stderr!r}"
    )
    # argparse's version action writes to stdout; the broker prints to
    # stdout too. Accept either stream to stay robust, but require a sane,
    # non-empty identity line carrying the prog name + the qdistro marker.
    out = (cp.stdout + cp.stderr).strip()
    assert out, f"{entry} --version produced no output"
    assert prog in out, f"{entry} --version output {out!r} lacks prog {prog!r}"
    assert "(qdistro)" in out, (
        f"{entry} --version output {out!r} lacks the (qdistro) marker"
    )


def test_shipped_qsu_c_client_version(tmp_path: Path) -> None:
    """The installed qsu is the C client, so smoke that exact entrypoint."""
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("cc not available to build qsu C client")
    binary = tmp_path / "qsu"
    compile_cp = subprocess.run(
        [cc, "-O2", "-Wall", "-Wextra", "-o", str(binary), str(_REPO / "qsu/qsu.c")],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert compile_cp.returncode == 0, (
        f"qsu.c compile failed\nstdout={compile_cp.stdout!r}"
        f"\nstderr={compile_cp.stderr!r}"
    )
    cp = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert cp.returncode == 0, (
        f"compiled qsu --version exited {cp.returncode}\n"
        f"stdout={cp.stdout!r}\nstderr={cp.stderr!r}"
    )
    out = (cp.stdout + cp.stderr).strip()
    assert "qsu" in out
    assert "(qdistro)" in out


# The two covered CLIs whose real subcommands sit behind a root gate; the
# --version probe must bypass that gate. Only an UNPRIVILEGED probe proves
# the bypass — run as root the gate would pass regardless and prove nothing.
_ROOT_GATED = [
    ("cli/qdistro_approvals.py", "qdistro-approvals"),
    ("cli/qdistro_recall_admin_cli.py", "qdistro-recall-admin"),
]


def test_version_bypasses_root_gate() -> None:
    """--version must not require root for the root-gated CLIs.

    `qdistro-approvals` and `qdistro-recall-admin` refuse their real
    subcommands unless run as root. This guards against a regression that
    moves the root check ahead of the --version handler and makes the
    smoke gate need privileges. The proof is only meaningful when the
    probe itself is unprivileged, so skip loudly (never silently pass)
    when the suite happens to run as root.
    """
    if os.geteuid() == 0:
        pytest.skip(
            "running as root: a root probe cannot prove the --version "
            "bypass of the root gate (the gate would pass either way). "
            "Run this test unprivileged to exercise it."
        )
    for entry, prog in _ROOT_GATED:
        cp = _run_version(entry)
        assert cp.returncode == 0, (
            f"{entry} --version requires root it should not "
            f"(rc={cp.returncode}, stderr={cp.stderr!r})"
        )
        assert prog in (cp.stdout + cp.stderr)
