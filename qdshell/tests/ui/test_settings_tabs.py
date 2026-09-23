"""One parametrized test per Settings tab."""

from pathlib import Path

import pytest

from . import runner
from .manifests import SETTINGS_SURFACES


@pytest.mark.cheat_aware(
    protects=(
        "security-relevant Settings surfaces render their real controls — "
        "notably the Hooks tab (the HooksGate approval/allow-deny config) and "
        "the Lock Screen tab — so a regression that blanks or strips those "
        "controls is caught, not silently passed"
    ),
    severity="high",
    cheats=[
        "weaken runner.judge() so any framebuffer scores PASS",
        "convert a real regression into pytest.skip()/xfail to dodge red",
        "loosen the expectation .md so missing controls still match",
    ],
    consequence=(
        "the HooksGate / lock-screen configuration UI could regress to a "
        "blank or wrong surface while CI stays green, hiding a gate the user "
        "can no longer see or trust"
    ),
)
@pytest.mark.parametrize("surface", SETTINGS_SURFACES, ids=lambda s: s.id)
def test_settings_tab(capture, surface):
    png, actual = capture(surface)
    assert png.exists(), f"no screenshot for {surface.id}"
    reference = (runner.EXPECTATIONS_DIR / surface.expectation).read_text()
    verdict = runner.judge(reference, actual)
    if verdict.verdict == "SKIP":
        pytest.skip(verdict.raw)
    assert verdict.verdict == "PASS", (
        f"{surface.id} regressed.\n"
        f"  missing: {verdict.missing}\n"
        f"  extra:   {verdict.extra}\n"
        f"  judge:   {verdict.raw}\n"
        f"  png:     {png}\n"
        f"  actual described as:\n{actual}"
    )
