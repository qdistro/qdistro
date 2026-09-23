"""Test fixtures: tmp_notebook, qapp.

Also registers the opt-in ``cheat_aware`` marker (see below). The marker
registration and report hook are PURE pytest — they do not import Qt — so
they keep working even if the Qt fixtures below are unavailable.
"""

from __future__ import annotations

import gc
import os
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# Opt-in `cheat_aware` marker.
#
# Lets a correctness/data-safety-critical test declare, in-band, what user
# capability it protects and how an agent might "cheat" the test green. The
# marker is inert on PASS; on FAIL the structured context is surfaced in the
# report so a reviewer (human or CI-triage agent) immediately sees the stakes
# instead of just an assertion diff. Opt-in: tests are unaffected unless
# decorated.
#
#     @pytest.mark.cheat_aware(
#         protects="a concurrent external edit is never silently dropped",
#         severity="critical",
#         cheats=["assert only result.ok and drop the byte check",
#                 "widen the merge acceptance to status in (ok, conflict)"],
#         consequence="user loses note content written by another process",
#     )
#
# All kwargs are optional and the report block degrades gracefully if some
# are missing. This block is intentionally Qt-free: do NOT import Qt here.
# --------------------------------------------------------------------------
def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "cheat_aware(protects, severity, cheats, consequence): correctness/"
        "data-safety-critical test; on failure prints what capability it "
        "protects, how the test could be cheated green, and the consequence "
        "of a false pass.",
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
    report.sections.append(
        ("cheat_aware: protected correctness invariant", body)
    )


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def tmp_notebook(tmp_path: Path) -> Path:
    """Create a small markdown notebook with a few pages."""
    root = tmp_path / "nb"
    root.mkdir()
    (root / "Home.md").write_text(
        "# Home\n\nWelcome. See [[Sub:Child]] and [[Other]].\n",
        encoding="utf-8",
    )
    sub = root / "Sub"
    sub.mkdir()
    (root / "Sub.md").write_text("# Sub\n\n- item\n- item2\n", encoding="utf-8")
    (sub / "Child.md").write_text(
        "# Child\n\nLinks back to [[Home]].\n", encoding="utf-8"
    )
    (root / "Other.md").write_text(
        "# Other\n\n**bold** and _italic_.\n", encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def _flush_qt(qapp):
    yield
    qapp.processEvents()
    gc.collect()
    qapp.processEvents()
