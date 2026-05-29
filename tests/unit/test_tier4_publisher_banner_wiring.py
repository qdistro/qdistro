"""Shell-level wiring tests for the tier-4 publisher identity banner.

These exercise the actual ``qdistro-tier4-publisher.sh`` (guest) and
``spawn-tier4.sh`` (host) scripts in dry/source-only modes — no VM — to
pin that:

  * the guest publisher reports the instance token + banner path it would
    publish (dry-run dump);
  * the host script ships the verification gate, the qga token injection,
    and a dev-only opt-out, and resolves the shared identity helper.

Pairs with test_tier4_publisher_identity.py (pure-logic banner contract).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = REPO_ROOT / "tier4-vm-guest" / "qdistro-tier4-publisher.sh"
SPAWN = REPO_ROOT / "tier4-vm" / "spawn-tier4.sh"
IDENTITY_PY = REPO_ROOT / "tier4-vm" / "tier4_publisher_identity.py"


def test_identity_helper_module_present_and_importable():
    # The helper lives alongside spawn-tier4.sh so the resolver's first
    # candidate ($SCRIPT_DIR/tier4_publisher_identity.py) hits in-tree.
    assert IDENTITY_PY.is_file()
    cp = subprocess.run(
        ["python3", str(IDENTITY_PY), "build", "vm1", "vm1-" + "a" * 32,
         "7879"],
        text=True, capture_output=True, timeout=10)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.startswith("QDISTRO-TIER4-PUBLISHER v1 ")


def test_publisher_dry_run_reports_instance_and_banner(tmp_path: Path):
    dry = tmp_path / "dry.log"
    sock_dir = tmp_path / "run"
    sock_dir.mkdir()
    (sock_dir / "wayland-0").touch()
    env = os.environ.copy()
    env.update({
        "QDISTRO_TIER4_DRY_RUN": "1",
        "QDISTRO_TIER4_DRY_OUT": str(dry),
        "XDG_RUNTIME_DIR": str(sock_dir),
        "QDISTRO_TIER4_INSTANCE": "s110vm-" + "a" * 32,
        "QDISTRO_TIER4_VM": "s110vm",
        "QDISTRO_TIER4_BANNER_PATH": str(tmp_path / "banner"),
    })
    cp = subprocess.run(
        ["bash", str(PUBLISHER), "7879"],
        env=env, text=True, capture_output=True, timeout=10)
    assert cp.returncode == 0, cp.stderr
    out = dry.read_text()
    assert "instance=s110vm-" + "a" * 32 in out
    assert "banner_path=" + str(tmp_path / "banner") in out


def test_publisher_dry_run_does_not_write_real_banner(tmp_path: Path):
    # Dry-run must be side-effect-free: it must NOT touch the banner path
    # (the real default is unwritable in the test sandbox anyway).
    dry = tmp_path / "dry.log"
    sock_dir = tmp_path / "run"
    sock_dir.mkdir()
    (sock_dir / "wayland-0").touch()
    banner = tmp_path / "banner"
    env = os.environ.copy()
    env.update({
        "QDISTRO_TIER4_DRY_RUN": "1",
        "QDISTRO_TIER4_DRY_OUT": str(dry),
        "XDG_RUNTIME_DIR": str(sock_dir),
        "QDISTRO_TIER4_INSTANCE": "s110vm-" + "a" * 32,
        "QDISTRO_TIER4_BANNER_PATH": str(banner),
    })
    subprocess.run(["bash", str(PUBLISHER), "7879"],
                   env=env, text=True, capture_output=True, timeout=10)
    assert not banner.exists()


def _spawn_text() -> str:
    return SPAWN.read_text()


def test_spawn_ships_identity_verification_gate():
    txt = _spawn_text()
    # The gate function and its fail-closed exit must be present.
    assert "verify_publisher_identity()" in txt
    assert "publisher identity banner" in txt
    # The verification gate exits non-zero (fail closed) on mismatch.
    assert "QDISTRO_TIER4_ALLOW_UNVERIFIED_PUBLISHER" in txt


def test_spawn_injects_instance_token_into_guest():
    txt = _spawn_text()
    assert "QDISTRO_TIER4_INSTANCE='$SECCTX_INSTANCE'" in txt


def test_spawn_resolves_identity_helper_candidates():
    txt = _spawn_text()
    assert "tier4_publisher_identity.py" in txt


def test_source_only_still_short_circuits():
    # Adding the gate must not have broken the --source-only contract the
    # other unit tests rely on (sourcing the script must not run launch
    # logic / require root).
    cp = subprocess.run(
        ["bash", "-c", f". {SPAWN} --source-only && echo SOURCED_OK"],
        text=True, capture_output=True, timeout=10)
    assert "SOURCED_OK" in cp.stdout, cp.stderr
