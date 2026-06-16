"""Static guards for the broker installer dependency set."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO / "scripts" / "install" / "install-broker-for-qdwin.sh"
_BROKER_SRC = _REPO / "broker"
_POLICY = _BROKER_SRC / "org.qdistro.AdminBroker1.conf"


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


def test_broker_installer_ships_commit_lineage_dependencies() -> None:
    """The commit-lineage chokepoint handler and its transitive deps (guard
    registry + metadata schema + the record_chokepoint engine) must ship so the
    RecordCommitLineage broker method can import them at runtime."""
    text = _INSTALLER.read_text()
    for name in (
        "qdistro_commit_lineage.py",
        "qdistro_lineage.py",
        "qdistro_guard_registry.py",
        "qdistro_metadata_schema.py",
    ):
        assert name in text


def _installed_broker_modules() -> set[str]:
    """The .py broker modules the installer copies into DEST (parsed from the
    two `install`/`for f in ... .py` blocks). Used to reconstruct the runtime
    sys.path the broker service actually has."""
    text = _INSTALLER.read_text()
    return set(re.findall(r"qdistro_[a-z0-9_]+\.py", text))


def test_commit_lineage_imports_from_installed_module_set() -> None:
    """Smoke: the modules the installer ships must be import-closed for the
    commit-lineage entry point — i.e. importing ``qdistro_commit_lineage`` with
    ONLY the installed broker modules on sys.path must succeed. This catches a
    missing transitive dependency in the installer list (the failure mode the
    name-presence assert above cannot see) before broker startup hits it."""
    import tempfile

    shipped = _installed_broker_modules()
    # Stage a DEST-like dir containing only the installer-listed broker modules.
    with tempfile.TemporaryDirectory() as dest:
        dest_path = Path(dest)
        for name in shipped:
            src = _BROKER_SRC / name
            if src.exists():
                (dest_path / name).write_bytes(src.read_bytes())
        # Import in a clean subprocess so the test process's already-populated
        # broker sys.path cannot mask a missing dependency.
        proc = subprocess.run(
            [sys.executable, "-c", "import qdistro_commit_lineage"],
            cwd=str(dest_path),
            env={"PYTHONPATH": str(dest_path), "PATH": ""},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            "qdistro_commit_lineage failed to import from the installed module "
            f"set (missing transitive dep?):\n{proc.stderr}"
        )


def test_policy_denies_record_commit_lineage_in_default_context() -> None:
    """The D-Bus policy must explicitly deny RecordCommitLineage in the
    ``context="default"`` block (root-only, mirroring RecordExportLineage), so a
    future policy edit cannot leave only the in-method root gate. We assert the
    deny member is present under a default-context policy."""
    import xml.etree.ElementTree as ET

    root = ET.parse(_POLICY).getroot()
    denied_default = set()
    for policy in root.findall("policy"):
        if policy.get("context") != "default":
            continue
        for deny in policy.findall("deny"):
            member = deny.get("send_member")
            if member:
                denied_default.add(member)
    assert "RecordCommitLineage" in denied_default
    # Parity sanity: the sibling lineage methods are denied the same way.
    assert "RecordExportLineage" in denied_default
