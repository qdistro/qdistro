"""Bar layout sanity check: take a clean shot with no panel open."""

from . import runner
from .manifests import BAR_SURFACES


def test_bar_idle(capture):
    surface = BAR_SURFACES[0]
    png, actual = capture(surface)
    assert png.exists()
    reference = (runner.EXPECTATIONS_DIR / surface.expectation).read_text()
    verdict = runner.judge(reference, actual)
    if verdict.verdict == "SKIP":
        import pytest
        pytest.skip(verdict.raw)
    assert verdict.verdict == "PASS", (
        f"bar regressed.\n"
        f"  missing: {verdict.missing}\n"
        f"  extra:   {verdict.extra}\n"
        f"  png:     {png}\n"
        f"  actual described as:\n{actual}"
    )
