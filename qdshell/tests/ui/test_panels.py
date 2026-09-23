"""One parametrized test per slide-out panel."""

import pytest

from . import runner
from .manifests import PANEL_SURFACES, NO_IPC


@pytest.mark.cheat_aware(
    protects=(
        "user-facing panels render their real content — notably the "
        "notifications panel, the surface through which the user sees and "
        "dismisses notifications that gate trust decisions"
    ),
    severity="high",
    cheats=[
        "weaken runner.judge() so any framebuffer scores PASS",
        "convert a real regression into pytest.skip() to dodge red",
        "turn the existing xfail (NO_IPC) into a blanket xfail to hide breakage",
    ],
    consequence=(
        "a panel (e.g. notifications) could silently regress to blank while CI "
        "stays green, hiding security-relevant prompts from the user"
    ),
)
@pytest.mark.parametrize("surface", PANEL_SURFACES, ids=lambda s: s.id)
def test_panel(capture, surface):
    if surface.open_cmd is NO_IPC:
        pytest.xfail(
            f"{surface.id}: no IPC handle in current qdshell; add one to test"
        )
    png, actual = capture(surface)
    assert png.exists()
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
