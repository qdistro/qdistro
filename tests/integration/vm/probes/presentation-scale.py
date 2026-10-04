#!/usr/bin/env python3
"""Live compositor scale dump for presentation-live.bats.

Runs as admin against the session Wayland display. Publishes a snapshot
whose user scale is not 1.0, attaches the installed Qt controller, and
asserts the application font stays in points (11 * fonts.uiScale *
metrics.uiScale) instead of device pixels. presentation-scale.sh runs it
once per compositor output scale and passes ``--expect-dpr``, so the
check is made at a real non-100% device scale, not only at 1.0. Also
imports the four first-party packages (and qterminator's QTermWidget
binding) so a missing bake shows up as FAIL, not skip.

Exit 0 only when every PASS line below is printed and no FAIL fired.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

os.environ.pop("QDISTRO_PRESENTATION_FILE", None)
os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

FAILS = 0


def pass_(msg: str) -> None:
    print(f"PASS: {msg}")


def fail(msg: str) -> None:
    global FAILS
    FAILS += 1
    print(f"FAIL: {msg}")


def _expected_dpr() -> float | None:
    if "--expect-dpr" in sys.argv:
        return float(sys.argv[sys.argv.index("--expect-dpr") + 1])
    return None


def main() -> int:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")
    sock = os.path.join(runtime, os.environ.get("WAYLAND_DISPLAY", "wayland-1"))
    if not os.path.exists(sock):
        fail(f"compositor socket missing: {sock}")
        return 1
    pass_(f"compositor socket exists: {sock}")

    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as exc:
        fail(f"PyQt6.QtWidgets import failed: {exc}")
        return 1

    app = QApplication.instance() or QApplication(["presentation-scale"])
    dpr = float(app.devicePixelRatio())
    pass_(f"devicePixelRatio={dpr}")
    want_dpr = _expected_dpr()
    if want_dpr is not None:
        if abs(dpr - want_dpr) > 0.01:
            fail(f"devicePixelRatio {dpr} != compositor scale {want_dpr}")
        else:
            pass_(f"devicePixelRatio matches compositor scale {want_dpr}")
    platform = app.platformName()
    pass_(f"qt platform={platform}")
    if platform != "wayland":
        fail(f"expected wayland QPA, got {platform}")

    try:
        from qdistro_presentation.model import example_snapshot
        from qdistro_presentation.publish import write_snapshot
        from qdistro_presentation.qt import PresentationController, reset_controller_for_tests
    except Exception as exc:
        fail(f"qdistro_presentation import failed: {exc}")
        return 1
    pass_("installed qdistro_presentation imports without PYTHONPATH")

    font_ui = 1.1
    metrics_ui = 1.1
    expected = 11.0 * font_ui * metrics_ui
    base = example_snapshot()
    snap = replace(
        base,
        fonts=replace(base.fonts, ui_scale=font_ui),
        metrics=replace(base.metrics, ui_scale=metrics_ui),
        enabled=True,
    )
    result = write_snapshot(
        "/var/lib/qdistro/presentation",
        snap,
        owner_uid=1000,
        skip_unchanged=False,
    )
    pass_(f"published generation={result.generation} expected_pt={expected}")

    reset_controller_for_tests()
    ctrl = PresentationController(app, theme_mode="system", watch=False, role="ordinary")
    if not ctrl.state.using_shared_palette:
        fail("controller did not apply the published snapshot")
    else:
        pass_("controller applied the published snapshot")

    painted = float(app.font().pointSizeF())
    pass_(f"app font pointSizeF={painted}")
    if abs(painted - expected) > 0.05:
        fail(f"app font pointSizeF {painted} != expected {expected}")
    else:
        pass_(f"user scale applied once in points ({painted})")
    if dpr != 1.0 and abs(painted - expected * dpr) <= 0.05:
        fail(f"app font pointSizeF {painted} looks like expected*{dpr} device pixels")
    else:
        pass_("UI font was not multiplied by devicePixelRatio")

    for mod in ("qfileman", "qterminator", "QTermWidget", "qdbrowser", "qnotebook"):
        try:
            __import__(mod)
        except Exception as exc:
            fail(f"import {mod}: {exc}")
        else:
            pass_(f"import {mod}")

    ctrl.stop()
    reset_controller_for_tests()
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
