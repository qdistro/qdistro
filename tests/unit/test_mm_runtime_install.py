"""Static guard for the inert multi-machine VM runtime installer.

The broker is copied as a plain Python package rather than installed from a
wheel. Every sibling module imported by the broker therefore has to appear in
the install script, or the first live launch fails before claiming its D-Bus
name. Keep the paired-origin authority load-bearing in that deployed closure.
"""
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts/install/install-multimachine-for-vm.sh"


def test_installer_ships_paired_origin_authority() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "origin_authority.py" in text, (
        "mm_broker imports multimachine.origin_authority, so the VM runtime "
        "installer must copy origin_authority.py or the broker cannot start"
    )


def test_installer_ships_trusted_session_launcher() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "mm_session_launcher.py" in text
    assert '"$SRC/qdistro-mm-session-launcher"' in text
    assert "/usr/local/bin/qdistro-mm-session-launcher" in text


def test_installer_ships_remote_adapter_core() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "remote_adapter.py" in text
    assert "remote_nested_protocol.py" in text
    assert "remote_nested_service.py" in text
    assert "remote_nested_supervisor.py" in text
    assert "remote_nested_registry.py" in text
    assert '"$SRC/qdistro-mm-remote-nested-controller"' in text
    assert "/usr/local/bin/qdistro-mm-remote-nested-controller" in text
    assert '"$SRC/qdistro-mm-remote-nested-session"' in text
    assert "/usr/local/bin/qdistro-mm-remote-nested-session" in text


def test_installer_ships_remote_session_authority_and_launcher() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "mm_remote_session_authority.py" in text
    assert "mm_remote_session_launcher.py" in text
    assert '"$SRC/qdistro-mm-remote-session-launcher"' in text
    assert "/usr/local/bin/qdistro-mm-remote-session-launcher" in text
    assert "remote_adapter_transport.py" in text
    assert '"$SRC/qdistro-mm-remote-adapter"' in text
    assert "/usr/local/bin/qdistro-mm-remote-adapter" in text


def test_installer_ships_r9_display_authority_and_slot_controller() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "mm_display_authority.py" in text
    assert "remote_display_slot.py" in text
