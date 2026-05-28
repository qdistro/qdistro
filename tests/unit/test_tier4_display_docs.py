from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VM_TESTS = REPO_ROOT / "tests" / "integration" / "vm"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_tier4_integration_scripts_syntax_check_without_vm():
    scripts = [
        VM_TESTS / "s42-tier4-spawn.sh",
        VM_TESTS / "s44-tier4-secctx-exec.sh",
        VM_TESTS / "s46-tier4-clipboard-gate.sh",
    ]
    for script in scripts:
        cp = subprocess.run(
            ["bash", "-n", str(script)],
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert cp.returncode == 0, cp.stderr


def test_tier4_pass_counts_are_documented_and_asserted():
    expected = {
        "s42-tier4-spawn.sh": 5,
        "s44-tier4-secctx-exec.sh": 7,
        "s46-tier4-clipboard-gate.sh": 8,
    }
    bats = _read(VM_TESTS / "tiered-isolation.bats")

    for script_name, count in expected.items():
        script = _read(VM_TESTS / script_name)
        stem = script_name.split("-", 1)[0]
        assert f"Expected PASS count on success: {count}." in script
        assert f'echo "[{stem}] $PASSCOUNT passes, 0 failures"' in script
        assert f'assert_output_contains "[{stem}] {count} passes, 0 failures"' in bats


def test_tier4_spawn_bats_name_no_longer_claims_full_waypipe_display():
    bats = _read(VM_TESTS / "tiered-isolation.bats")
    names = re.findall(r'@test "([^"]*tier4-spawn[^"]*)"', bats)
    assert names == [
        "phase7-tier4-spawn: qdistro-tier4-spawn defines and boots the tier-4 domain without launching a viewer"
    ]
    assert "waypipe display" not in names[0]


def test_spice_domdisplay_fallback_is_documented_as_retired():
    docs = "\n".join(
        [
            _read(REPO_ROOT / "tier4-vm" / "README.md"),
            _read(REPO_ROOT / "tier4-vm-guest" / "README.md"),
            _read(VM_TESTS / "tiered-isolation.bats"),
        ]
    )
    assert "SPICE/`domdisplay` fallback is retired" in docs
    assert "SPICE/domdisplay fallback coverage is intentionally retired" in docs
    assert "There is no SPICE/`domdisplay` fallback path." in docs
