"""Regression contracts for tier-5 nested-VM cold-boot diagnostics."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPAWN = (ROOT / "tier5-vm/spawn-tier5.sh").read_text()
PROBE = (ROOT / "tests/integration/vm/s45-tier5-vm.sh").read_text()


def test_spawn_has_bounded_configurable_qga_deadline():
    assert 'QGA_TIMEOUT_SECS="${TIER5_QGA_TIMEOUT_SECS:-180}"' in SPAWN
    assert "clamp_int QGA_TIMEOUT_SECS 180 30 600" in SPAWN
    assert "qga_deadline=$((SECONDS + QGA_TIMEOUT_SECS))" in SPAWN
    assert 'qemu-agent-command --timeout 5 "$VM_NAME"' in SPAWN
    assert "seq 1 90" not in SPAWN


def test_vm_probe_retains_boot_evidence_until_assertions_finish():
    assert "TIER5_QGA_TIMEOUT_SECS=180" in PROBE
    assert 'TIER5_SERIAL_LOG="$SERIAL_LOG"' in PROBE
    assert "TIER5_KEEP_DOMAIN=1" in PROBE
    assert 'tail -200 "$SERIAL_LOG"' in PROBE
    assert PROBE.count('kill -0 "$SPAWN_PID"') == 2
