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
