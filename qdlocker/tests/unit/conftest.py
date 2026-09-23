"""Shared pytest config for qdlocker unit tests.

Pure-pytest only — this file must NOT import Qt. qdlocker pins PyQt6
(see pyproject's `qt_api = "pyqt6"`), but the marker hook below has to
stay import-clean so it can never perturb the PyQt6-vs-PySide6 load
order that the rest of the suite depends on.

`sys.path` / module-import wiring for `qdlocker` is already handled by
pyproject's `[tool.pytest.ini_options] pythonpath = ["."]`; do not add
ad-hoc `sys.path` hacks here.

--------------------------------------------------------------------------
Opt-in `cheat_aware` marker.

Lets a security-critical test declare, in-band, what user capability it
protects and how an agent might "cheat" the test green. The marker is
inert on PASS; on FAIL the structured context is surfaced in the report
so a reviewer (human or CI-triage agent) immediately sees the stakes
instead of just an assertion diff. Opt-in: tests are unaffected unless
decorated.

    @pytest.mark.cheat_aware(
        protects="locked screen cannot be bypassed without real auth",
        severity="critical",
        cheats=["assert on a stub instead of the real gate",
                "widen the accepted uid set"],
        consequence="anyone reaches the unlocked session",
    )

The marker itself is registered in pyproject's
`[tool.pytest.ini_options] markers` list. All kwargs are optional and
the report block degrades gracefully if some are missing.
--------------------------------------------------------------------------
"""
from __future__ import annotations

import pytest


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

    Only acts on the `call` phase and only when the test actually
    failed, so passing tests stay silent and setup/teardown noise is
    ignored.
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
        ("cheat_aware: protected security invariant", body)
    )
