"""§1 malformed user-config recovery + schema baseline — VM-only.

Pairs with the host-runnable tests/test_settings_recovery.js (which pins the
pure parse/merge/recover RULES). This proves the END-TO-END behavior in a live
qdshell: a corrupt settings.json on disk must NOT brick the shell — qdshell
recovers to defaults, comes back, answers IPC, and writes a well-formed config
again.

VM-only: needs the live user systemd unit + on-disk config + restart.
"""

import json
import time

import pytest

from . import runner


@pytest.mark.cheat_aware(
    protects=(
        "a malformed settings.json does not brick the shell — qdshell recovers "
        "to a usable (default-merged) config and restarts cleanly"
    ),
    severity="critical",
    cheats=[
        "restore a good config before restart so 'recovery' never actually runs",
        "assert only that the process is alive, not that config was rewritten valid",
        "turn a real start-on-corrupt-config failure into skip/xfail",
    ],
    consequence=(
        "a single bad write / hand-edit could leave the user with no working "
        "shell at next login"
    ),
)
def test_malformed_config_recovers(vm_session):
    s = vm_session
    # 1. Snapshot the current (good) config so we can restore it afterwards.
    original = runner.read_settings_vm(s)
    original_text = json.dumps(original) if original is not None else None

    try:
        # 2. Plant a truncated / corrupt settings.json and restart qdshell.
        runner.write_settings_vm(s, '{"bar": {"position": "to')  # truncated JSON
        runner.restart_qdshell_vm(s)

        # 3. The shell must be alive and answering IPC (recovered to defaults).
        runner.ipc_vm(s, "bar", "showBar")

        # 4. A setting change must now succeed and produce VALID json on disk —
        #    proving the corrupt file was replaced, not left in place.
        runner.ipc_vm(s, "darkMode", "toggle")
        time.sleep(1.5)
        recovered = runner.read_settings_vm(s)
        assert recovered is not None, "settings.json must exist after recovery + a write"
        assert isinstance(recovered, dict), "recovered config must be a JSON object"
        # A recovered config carries the schema baseline + the toggled value.
        assert "colorSchemes" in recovered, (
            "recovered config should contain default sections (merged from defaults)"
        )
    finally:
        # Restore the user's original config (best-effort) and restart so the
        # VM is left in a clean state for subsequent tests.
        if original_text is not None:
            try:
                runner.write_settings_vm(s, original_text)
                runner.restart_qdshell_vm(s)
            except Exception:
                pass


@pytest.mark.cheat_aware(
    protects="an empty / zero-length settings.json is treated as a fresh install, not a crash",
    severity="high",
    cheats=["pre-seed a valid config so the empty-file path never executes"],
    consequence="a zero-length config file (interrupted write) bricks the shell",
)
def test_empty_config_recovers(vm_session):
    s = vm_session
    original = runner.read_settings_vm(s)
    original_text = json.dumps(original) if original is not None else None
    try:
        runner.write_settings_vm(s, "")  # zero-length file
        runner.restart_qdshell_vm(s)
        runner.ipc_vm(s, "bar", "showBar")  # alive
        # writes a valid config back
        runner.ipc_vm(s, "darkMode", "toggle")
        time.sleep(1.5)
        recovered = runner.read_settings_vm(s)
        assert isinstance(recovered, dict) and recovered, (
            "empty config must recover to a populated default config"
        )
    finally:
        if original_text is not None:
            try:
                runner.write_settings_vm(s, original_text)
                runner.restart_qdshell_vm(s)
            except Exception:
                pass
