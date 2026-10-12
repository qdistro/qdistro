"""Regression contracts for complete Tier-5 libvirt teardown."""

import subprocess
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
    assert 'domain_is_defined || return 0' in SPAWN
    assert 'domain $VM_NAME remains defined after teardown' in SPAWN


def test_overlay_is_not_unlinked_while_domain_definition_remains():
    assert 'reap_domain "$force" || domain_reaped=0' in SPAWN
    assert '[ "$domain_reaped" = "1" ]' in SPAWN
    assert 'retaining overlay $DISK because $VM_NAME is still defined' in SPAWN
    assert 'cleanup 0 || { [ "$rc" -ne 0 ] || rc=5; }' in SPAWN
    assert "trap cleanup_on_exit EXIT" in SPAWN


def test_gui_reap_probes_use_the_product_libvirt_session():
    assert "runuser -u admin -- virsh" not in GUI_SCENARIO
    assert GUI_SCENARIO.count("LIBVIRT_DEFAULT_URI=qemu:///session") >= 5
    assert GUI_SCENARIO.count("XDG_RUNTIME_DIR=/run/user/1000") >= 5


def test_gui_close_journal_matchers_accept_exact_handle_at_end_of_line():
    for event in ("request_close", "toplevel_removed"):
        prefix = f"grep -qE 'qdwin: {event} handle=$handle"
        source_line = next(line for line in GUI_SCENARIO.splitlines()
                           if prefix in line)
        # The scenario's host double quotes consume this escape before the
        # command reaches grep in the guest.
        pattern = source_line.split("grep -qE '", 1)[1].split("'", 1)[0]
        pattern = pattern.replace("$handle", "7").replace(r"\$", "$")
        for suffix, expected in (("", True), (" (nested-proxy)", True),
                                 ("0", False), ("x", False)):
            line = f"qdwin: {event} handle=7{suffix}\n"
            matched = subprocess.run(["grep", "-qE", pattern], input=line,
                                     text=True, check=False).returncode == 0
            assert matched is expected, (event, suffix, pattern)
