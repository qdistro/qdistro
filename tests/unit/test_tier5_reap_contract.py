"""Regression contracts for complete Tier-5 libvirt teardown."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPAWN = (ROOT / "tier5-vm/spawn-tier5.sh").read_text()
GUI_SCENARIO = (
    ROOT / "tests/integration/permissions-gui/21-tier5-close-cleanup.md"
).read_text()


def test_undefine_retries_with_ephemeral_state_and_metadata_cleanup():
    assert 'virsh undefine "$VM_NAME"' in SPAWN
    assert "--managed-save --snapshots-metadata --checkpoints-metadata" in SPAWN
    assert "--nvram --tpm" in SPAWN
    # the defined-state check must distinguish "not found" from a
    # libvirt query outage — only confirmed absence ends the loop (sol r7)
    assert "domain_defined_state; _ds=$?" in SPAWN
    assert '[ "$_ds" -eq 1 ] && return 0' in SPAWN
    assert 'still defined or state unverifiable' in SPAWN


def test_overlay_is_not_unlinked_while_domain_definition_remains():
    assert 'reap_domain "$force" || domain_reaped=0' in SPAWN
    assert '[ "$domain_reaped" = "1" ]' in SPAWN
    assert 'retaining overlay $DISK — $VM_NAME still defined' in SPAWN
    assert 'cleanup 0 || { [ "$rc" -ne 0 ] || rc=5; }' in SPAWN
    assert "trap cleanup_on_exit EXIT" in SPAWN


def test_gui_reap_probes_use_the_product_libvirt_session():
    assert "runuser -u admin -- virsh" not in GUI_SCENARIO
    assert GUI_SCENARIO.count("LIBVIRT_DEFAULT_URI=qemu:///session") >= 5
    assert GUI_SCENARIO.count("XDG_RUNTIME_DIR=/run/user/1000") >= 5
