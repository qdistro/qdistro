"""Static guards for the broker installer dependency set."""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO / "scripts" / "install" / "install-broker-for-qdwin.sh"


def test_broker_installer_ships_export_lineage_dependencies() -> None:
    text = _INSTALLER.read_text()
    for name in (
        "qdistro_export_lineage.py",
        "qdistro_lineage_store.py",
        "qdistro_lineage_receipts.py",
        "qdistro_disposables.py",
        "qdistro_disposable_classes.py",
    ):
        assert name in text
    assert "/var/lib/qdistro/lineage" in text


def test_broker_installer_ships_upload_lineage_dependencies() -> None:
    """The RecordUploadLineage entry point imports the upload chokepoint and
    its transitive deps (guard registry + chokepoint + metadata schema); the
    broker must ship them or the daemon fails to import."""
    text = _INSTALLER.read_text()
    for name in (
        "qdistro_upload_lineage.py",
        "qdistro_upload_lineage_entry.py",
        "qdistro_lineage.py",
        "qdistro_guard_registry.py",
        "qdistro_metadata_schema.py",
    ):
        assert name in text
