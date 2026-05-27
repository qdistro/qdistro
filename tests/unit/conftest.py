"""Shared fixtures for phase1 tests.

These tests are pure-Python (no D-Bus, no Qt). They exercise the cache,
audit log, scope-mapping helpers, and CLI behavior against temporary
sqlite databases. For the in-VM integration walk see the playbooks
under tasks/.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# test_permission.py is the in-VM acceptance script (deployed to
# /usr/local/bin/qdistro-test-permission); not a unit test.
collect_ignore = ["test_permission.py"]

# Make broker + cli + pwd + print modules importable as top-level for the tests.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))
sys.path.insert(0, str(_ROOT / "cli"))
sys.path.insert(0, str(_ROOT / "pwd"))
sys.path.insert(0, str(_ROOT / "print"))
sys.path.insert(0, str(_ROOT / "snapshots"))
sys.path.insert(0, str(_ROOT / "recall"))
sys.path.insert(0, str(_ROOT / "phone"))
sys.path.insert(0, str(_ROOT / "browser_bridge"))
sys.path.insert(0, str(_ROOT / "games"))
sys.path.insert(0, str(_ROOT / "daemons"))


@pytest.fixture
def cache_db(tmp_path: Path) -> str:
    """Temp path for an ApprovalCache sqlite db."""
    return str(tmp_path / "approvals.sqlite")


@pytest.fixture
def audit_db(tmp_path: Path) -> str:
    """Temp path for an AuditLog sqlite db."""
    return str(tmp_path / "audit.sqlite")
