"""Regression contracts for the tier-5 nested-VM cold-boot probes.

The spawn's own bounded qga deadline is executed in test_tier5_lifecycle.py.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROBE = (ROOT / "tests/integration/vm/s45-tier5-vm.sh").read_text()
LOOPBACK_PROBE = (ROOT / "tests/integration/vm/s43-tier5-loopback.sh").read_text()


def test_vm_probe_retains_boot_evidence_until_assertions_finish():
    assert "TIER5_QGA_TIMEOUT_SECS=180" in PROBE
    assert 'TIER5_SERIAL_LOG="$SERIAL_LOG"' in PROBE
    assert "TIER5_KEEP_DOMAIN=1" in PROBE
    assert 'tail -200 "$SERIAL_LOG"' in PROBE
    assert PROBE.count('kill -0 "$SPAWN_PID"') == 2


def test_vm_probe_uses_suspend_resilient_deadlines():
    assert PROBE.count("qd_deadline_start ") == 3
    assert PROBE.count("while qd_deadline_pending; do") == 3
    assert "date +%s" not in PROBE


def test_standalone_probes_stage_spawn_common_beside_tier5():
    for source in (PROBE, LOOPBACK_PROBE):
        assert 'cp -r "$SRC/tier5-vm" "$TIER5_DIR/tier5-vm"' in source
        assert 'cp -r "$SRC/lib" "$TIER5_DIR/lib"' in source
        assert 'SPAWN="$TIER5_DIR/tier5-vm/spawn-tier5.sh"' in source
