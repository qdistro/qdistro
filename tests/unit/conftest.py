"""Shared fixtures for phase1 tests.

These tests are pure-Python (no D-Bus, no Qt). They exercise the cache,
audit log, scope-mapping helpers, and CLI behavior against temporary
sqlite databases. For the in-VM integration walk see the playbooks
under tasks/.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# test_permission.py is the in-VM acceptance script (deployed to
# /usr/local/bin/qdistro-test-permission); not a unit test.
collect_ignore = ["test_permission.py"]

# CI hardening (Phase 4): the admin-app Qt tests use
# `pytest.importorskip("PyQt6.QtWidgets")`, so an environment where
# PySide6's libQt6Core shadows PyQt6 would SILENTLY skip ~150 tests
# instead of failing. When QDISTRO_REQUIRE_PYQT6=1 (set by qci) make an
# unimportable PyQt6 a hard collection error so the regression is loud.
if os.environ.get("QDISTRO_REQUIRE_PYQT6") == "1":
    try:
        import PyQt6.QtWidgets  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "QDISTRO_REQUIRE_PYQT6=1 but PyQt6.QtWidgets is unimportable "
            f"({e!r}). PySide6 likely shadows PyQt6 here; set "
            "PYTEST_QT_API=pyqt6 or remove PySide6 from this image."
        ) from e

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
sys.path.insert(0, str(_ROOT / "workflow"))


# --------------------------------------------------------------------------
# Opt-in `cheat_aware` marker (synthesis.md item 3).
#
# Lets a security-critical test declare, in-band, what user capability it
# protects and how an agent might "cheat" the test green. The marker is
# inert on PASS; on FAIL the structured context is surfaced in the report so
# a reviewer (human or CI-triage agent) immediately sees the stakes instead
# of just an assertion diff. Opt-in: tests are unaffected unless decorated.
#
#     @pytest.mark.cheat_aware(
#         protects="qsu approval cannot be broadened silently",
#         severity="critical",
#         cheats=["change expected denial to skip", "broaden argv match"],
#         consequence="admin approval authorizes the wrong command",
#     )
#
# All kwargs are optional and the report block degrades gracefully if some
# are missing.
# --------------------------------------------------------------------------
def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "cheat_aware(protects, severity, cheats, consequence): security-"
        "critical test; on failure prints what capability it protects, how "
        "the test could be cheated green, and the consequence of a false pass.",
    )


def _format_cheat_aware_block(kwargs: dict) -> str:
    """Render the marker kwargs into a human-readable failure block.

    Degrades gracefully: only fields that were supplied are shown.
    """
    lines: list[str] = []
    protects = kwargs.get("protects")
    severity = kwargs.get("severity")
    cheats = kwargs.get("cheats")
    consequence = kwargs.get("consequence")

    if severity is not None:
        lines.append(f"severity:    {severity}")
    if protects is not None:
        lines.append(f"protects:    {protects}")
    if consequence is not None:
        lines.append(f"consequence: {consequence}")
    if cheats:
        # `cheats` is meant to be a list, but tolerate a bare string.
        if isinstance(cheats, str):
            cheats = [cheats]
        lines.append("cheats (do NOT do these to make this pass):")
        for c in cheats:
            lines.append(f"  - {c}")

    if not lines:
        lines.append(
            "(no structured fields supplied on the cheat_aware marker)"
        )
    return "\n".join(lines)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Surface cheat_aware context when a marked test FAILS.

    Only acts on the `call` phase and only when the test actually failed,
    so passing tests stay silent and setup/teardown noise is ignored.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or report.outcome != "failed":
        return
    marker = item.get_closest_marker("cheat_aware")
    if marker is None:
        return
    body = _format_cheat_aware_block(marker.kwargs)
    report.sections.append(("cheat_aware: protected security invariant", body))


@pytest.fixture
def cache_db(tmp_path: Path) -> str:
    """Temp path for an ApprovalCache sqlite db."""
    return str(tmp_path / "approvals.sqlite")


@pytest.fixture
def audit_db(tmp_path: Path) -> str:
    """Temp path for an AuditLog sqlite db."""
    return str(tmp_path / "audit.sqlite")
