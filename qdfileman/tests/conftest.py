"""Shared fixtures and cleanup for QFileMan tests.

Runs under Qt's offscreen platform plugin by default so tests don't pop
real windows on the active desktop. Override with
``QT_QPA_PLATFORM=xcb pytest`` if you need an on-screen run.
"""

import gc
import os

# Default to offscreen rendering. Must be set before QApplication is
# constructed, which happens lazily on first qtbot use.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication


# --------------------------------------------------------------------------
# Opt-in `cheat_aware` marker (ported from qdistro's tests/unit/conftest.py).
#
# Lets a correctness/security-critical test declare, in-band, what user
# capability it protects and how an agent might "cheat" the test green. The
# marker is inert on PASS; on FAIL the structured context is surfaced in the
# report so a reviewer (human or CI-triage agent) immediately sees the stakes
# instead of just an assertion diff. Opt-in: tests are unaffected unless
# decorated.
#
#     @pytest.mark.cheat_aware(
#         protects="a moved file's source is only removed after the copy lands",
#         severity="critical",
#         cheats=["drop the `not src.exists()` assertion", "stub the runner"],
#         consequence="Move silently deletes data without copying it",
#     )
#
# All kwargs are optional and the report block degrades gracefully if some
# are missing. The registration + hook below are PURE pytest (no Qt import),
# so they work even if the Qt fixtures above are unavailable.
# --------------------------------------------------------------------------
def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "cheat_aware(protects, severity, cheats, consequence): correctness/"
        "security-critical test; on failure prints what capability it "
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
        lines.append("(no structured fields supplied on the cheat_aware marker)")
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
    report.sections.append(("cheat_aware: protected invariant", body))


@pytest.fixture(autouse=True)
def _cleanup_after_test():
    """Clean up after every test to prevent memory leaks.

    Runs multiple processEvents+gc rounds to ensure deleteLater()
    calls are processed and C++ objects are freed.
    """
    yield
    app = QApplication.instance()
    if app:
        for _ in range(3):
            app.processEvents()
            gc.collect()
            app.processEvents()


@pytest.fixture
def tmp_dir(tmp_path):
    """Create a temporary directory with some files for testing."""
    test_dir = tmp_path / "test_files"
    test_dir.mkdir()

    # Create some test files with predictable content for sorting tests
    # file1.txt - smallest (8 bytes)
    (test_dir / "file1.txt").write_text("content1")
    # file2.txt - medium (8 bytes)
    (test_dir / "file2.txt").write_text("content2")
    # file3.md - different extension for type sorting
    (test_dir / "file3.md").write_text("# Markdown")

    # Create subdirectory (for navigation tests)
    subdir = test_dir / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested content")

    # Create hidden file (for hidden toggle tests)
    (test_dir / ".hidden").write_text("hidden")

    return test_dir


@pytest.fixture
def nested_tmp_dir(tmp_path):
    """Create a 2-level temp directory for navigation tests."""
    parent = tmp_path / "parent_dir"
    parent.mkdir()
    (parent / "parent_file.txt").write_text("parent")

    child = parent / "child_dir"
    child.mkdir()
    (child / "child_file.txt").write_text("child")

    return parent


@pytest.fixture
def tmp_tree(tmp_path):
    """Realistic sample tree used by search/preferences/theme tests.

    Mirrors the fixture used in the sibling qfileman variant so ported
    tests can be reused with minimal edits.
    """
    root = tmp_path / "tree"
    root.mkdir()
    (root / "file1.txt").write_text("hello\n", encoding="utf-8")
    (root / "file2.py").write_text("print('hi')\n", encoding="utf-8")
    sub = root / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested\n", encoding="utf-8")
    (root / ".hidden").write_text("secret\n", encoding="utf-8")
    dot_dir = root / ".dotdir"
    dot_dir.mkdir()
    (dot_dir / "inside.txt").write_text("hidden dir file\n", encoding="utf-8")
    return root
