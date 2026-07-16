"""Regression contracts for complete Tier-5 libvirt teardown."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPAWN = (ROOT / "tier5-vm/spawn-tier5.sh").read_text()


def test_undefine_retries_with_ephemeral_state_and_metadata_cleanup():
    assert 'virsh undefine "$VM_NAME"' in SPAWN
    assert "--managed-save --snapshots-metadata --checkpoints-metadata" in SPAWN
    assert "--nvram --tpm" in SPAWN
    assert 'domain_is_defined || return 0' in SPAWN
    assert 'domain $VM_NAME remains defined after teardown' in SPAWN


def test_overlay_is_not_unlinked_while_domain_definition_remains():
    assert 'reap_domain "$force" || domain_reaped=0' in SPAWN
    assert '[ "$domain_reaped" = "1" ]' in SPAWN
    assert 'retaining overlay $DISK because $VM_NAME is still defined' in SPAWN
    assert 'cleanup 0 || { [ "$rc" -ne 0 ] || rc=5; }' in SPAWN
    assert "trap cleanup_on_exit EXIT" in SPAWN
