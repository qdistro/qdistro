"""Shared pytest config for qdgreeter tests.

This module is intentionally PURE pytest — it must NOT import Qt (PyQt6
or PySide6) at collection time. The Qt-using tests guard their own
imports with ``pytest.importorskip(...)`` and set ``QT_QPA_PLATFORM``
themselves; importing a Qt binding here would defeat that and could
break the binding-selection contract pinned in ``pyproject.toml``
(``[tool.pytest.ini_options] qt_api = "pyqt6"``).

qci runs this repo's tests as ``python3 -m pytest tests`` from the repo
root, so this is the test-root conftest and the marker below is
available to every test under ``tests/``.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------
# Opt-in `cheat_aware` marker (propagated from qdistro/tests/unit/conftest.py).
#
# Lets a security-critical test declare, in-band, what user capability it
# protects and how an agent might "cheat" the test green. The marker is
# inert on PASS; on FAIL the structured context is surfaced in the report so
# a reviewer (human or CI-triage agent) immediately sees the stakes instead
# of just an assertion diff. Opt-in: tests are unaffected unless decorated.
#
# For qdgreeter (the boot greeter) the high-risk invariants are around
# *auth*: a session must not start without valid authentication, and the
# password must never leak to a non-secret prompt or a log.
#
#     @pytest.mark.cheat_aware(
#         protects="a session cannot start without a successful auth exchange",
#         severity="critical",
#         cheats=["accept auth_error as success", "skip the start_session gate"],
#         consequence="an unauthenticated user lands in the admin session",
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

    Implemented as a hookwrapper so it sees the report the default
    implementation produced. Only acts on the `call` phase and only when
    the test actually failed, so passing tests stay silent and
    setup/teardown noise is ignored.
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
