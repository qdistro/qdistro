"""Qt controller: native capture, snapshot apply, last-known-good, watches."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QFont, QPalette
from PyQt6.QtWidgets import QApplication, QLabel, QWidget
from qdistro_presentation.model import example_snapshot, with_generation
from qdistro_presentation.paths import ResolvedPath
from qdistro_presentation.publish import write_disabled_envelope, write_snapshot
from qdistro_presentation.qt import PresentationController, snapshot_palette


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def _path(tmp_path) -> ResolvedPath:
    return ResolvedPath(
        path=str(tmp_path / "current.json"),
        kind="override",
        expected_uid=None,
        watch=True,
    )


def test_native_restore_and_snapshot_palette(qapp, tmp_path):
    native_window = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    snap_window = qapp.palette().color(QPalette.ColorRole.Window)
    assert snap_window.name() == example_snapshot().colors.mSurface
    highlight = qapp.palette().color(QPalette.ColorRole.HighlightedText).name()
    assert highlight == example_snapshot().colors.mOnPrimary
    ctrl.set_theme_mode("native")
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native_window
    ctrl.stop()


def test_enabled_false_clears_shared_stylesheet(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    assert "QMenu" in qapp.styleSheet()
    write_disabled_envelope(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl._reload()
    assert ctrl.state.using_shared_palette is False
    assert qapp.styleSheet() == ctrl._native_stylesheet
    ctrl.stop()


def test_explicit_light_clears_shared_stylesheet(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)

    def legacy(app, mode):
        from PyQt6.QtGui import QPalette

        app.setStyle("Fusion")
        app.setPalette(QPalette())

    ctrl = PresentationController(
        qapp,
        theme_mode="system",
        snapshot_path=_path(tmp_path),
        apply_legacy=legacy,
        watch=False,
    )
    assert "QMenu" in qapp.styleSheet()
    ctrl.set_theme_mode("light")
    assert "QMenu" not in qapp.styleSheet()
    ctrl.stop()


def test_selection_uses_on_primary_not_white():
    pal = snapshot_palette(example_snapshot().colors)
    assert pal.color(QPalette.ColorRole.HighlightedText).name() == "#0e0e43"
    assert pal.color(QPalette.ColorRole.Highlight).name() == "#fff59b"


def test_missing_file_uses_fallback_then_recovers(qapp, tmp_path):
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    assert ctrl.state.using_shared_palette is False
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl._reload()
    assert ctrl.state.using_shared_palette is True
    ctrl.stop()


def test_unrepresentable_number_keeps_last_good_then_recovers(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    generation = ctrl.state.generation
    payload = example_snapshot().to_dict()
    payload["fonts"]["basePointSize"] = int("1" + "0" * 400)
    (tmp_path / "current.json").write_text(json.dumps(payload), encoding="utf-8")
    ctrl._reload()
    assert ctrl.state.generation == generation
    recovered = with_generation(
        replace(example_snapshot(), fonts=replace(example_snapshot().fonts, ui_scale=1.25))
    )
    write_snapshot(
        str(tmp_path),
        recovered,
        require_unwritable_dirs=False,
        skip_unchanged=False,
    )
    ctrl._reload()
    assert ctrl.state.generation == recovered.generation
    assert ctrl.state.ui_point_size == pytest.approx(13.75)
    ctrl.stop()


def test_malformed_keeps_last_good(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    generation = ctrl.state.generation
    (tmp_path / "current.json").write_text("{not json", encoding="utf-8")
    ctrl._reload()
    assert ctrl.state.generation == generation
    ctrl.stop()


def test_deletion_keeps_last_good(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    (tmp_path / "current.json").unlink()
    ctrl._reload()
    assert ctrl.state.using_shared_palette is True
    ctrl.stop()


def test_local_override_survives_snapshot(qapp, tmp_path):
    from qdistro_presentation.model import LocalOverrides

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp,
        theme_mode="system",
        local=LocalOverrides(ui_font_family="DoesNotExistFontXYZ"),
        snapshot_path=_path(tmp_path),
        watch=False,
    )
    # Unavailable family falls back at apply time; resolved state keeps the
    # requested override so it can round-trip in app config.
    assert ctrl.state.ui_family == "DoesNotExistFontXYZ"
    ctrl.stop()


def test_changed_signal_and_existing_widget(qapp, tmp_path):
    label = QLabel("hello")
    seen = []
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="native", snapshot_path=_path(tmp_path), watch=False
    )
    ctrl.changed.connect(lambda old, new, fields: seen.append(fields))
    ctrl.set_theme_mode("system")
    assert seen
    assert "using_shared_palette" in seen[0]
    label.setParent(None)
    ctrl.stop()


def test_changed_handler_fonts_survive_polish(qapp, tmp_path):
    from dataclasses import replace

    from qdistro_presentation.model import with_generation

    class Host(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.label = QLabel("hello", self)
            heading = QFont(qapp.font())
            heading.setPointSizeF((heading.pointSizeF() or 11.0) * 14.0 / 11.0)
            self.label.setFont(heading)

        def apply_presentation_update(self, *_args: object) -> None:
            heading = QFont(qapp.font())
            heading.setPointSizeF((heading.pointSizeF() or 11.0) * 14.0 / 11.0)
            self.label.setFont(heading)

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    host = Host()
    ctrl.changed.connect(host.apply_presentation_update)
    fonts = replace(example_snapshot().fonts, ui_scale=1.25)
    second = with_generation(replace(example_snapshot(), fonts=fonts))
    write_snapshot(str(tmp_path), second, require_unwritable_dirs=False, skip_unchanged=False)
    ctrl._reload()
    assert ctrl.state.ui_point_size == pytest.approx(13.75)
    assert host.label.font().pointSizeF() == pytest.approx(13.75 * 14.0 / 11.0)
    host.setParent(None)
    ctrl.stop()


def test_unchanged_file_reload_is_noop(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    assert ctrl.state.using_shared_palette is True
    seen = []
    ctrl.changed.connect(lambda *_args: seen.append(True))
    ctrl._reload()
    assert seen == []
    ctrl.stop()


def test_same_generation_rewrite_skips_apply(qapp, tmp_path):
    snap = example_snapshot()
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    assert ctrl.state.using_shared_palette is True
    first_identity = ctrl._identity
    first_generation = ctrl.state.generation
    seen = []
    applies = []
    ctrl.changed.connect(lambda *_args: seen.append(True))
    real_apply = ctrl._apply_to_app

    def spy(resolved):
        applies.append(resolved.generation)
        real_apply(resolved)

    ctrl._apply_to_app = spy
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False, skip_unchanged=False)
    ctrl._reload()
    assert ctrl.state.generation == first_generation
    assert ctrl._identity != first_identity
    assert applies == []
    assert seen == []
    ctrl.stop()


def test_replacement_storm_last_valid_wins(qapp, tmp_path):
    from dataclasses import replace

    from qdistro_presentation.model import with_generation

    first = example_snapshot()
    write_snapshot(str(tmp_path), first, require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    (tmp_path / "current.json").write_text("{truncated", encoding="utf-8")
    ctrl._reload()
    assert ctrl.state.generation == first.generation
    second = with_generation(replace(first, mode="light"))
    write_snapshot(str(tmp_path), second, require_unwritable_dirs=False, skip_unchanged=False)
    ctrl._reload()
    assert ctrl.state.generation == second.generation
    assert ctrl.state.snapshot.mode == "light"
    ctrl.stop()


def test_active_and_inactive_palette_groups_match(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    pal = qapp.palette()
    surface = example_snapshot().colors.mSurface
    assert pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window).name() == surface
    assert pal.color(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Window).name() == surface
    assert pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Window).name() == surface
    ctrl.stop()


def test_ui_scale_applied_once_to_app_font(qapp, tmp_path):
    from qdistro_presentation.model import DEFAULT_DARK_COLORS, normalize_producer

    snap = normalize_producer(
        mode="dark",
        colors=DEFAULT_DARK_COLORS,
        settings={"ui": {"fontDefaultScale": 1.1}, "general": {"scaleRatio": 1.1}},
    )
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    assert qapp.font().pointSizeF() == pytest.approx(11 * 1.1 * 1.1)
    assert ctrl.state.ui_point_size == pytest.approx(11 * 1.1 * 1.1)
    assert ctrl.state.content_ui_point_size == pytest.approx(11 * 1.1)
    ctrl.stop()


def test_ui_font_ignores_device_pixel_ratio(qapp, tmp_path, monkeypatch):
    from qdistro_presentation.model import DEFAULT_DARK_COLORS, normalize_producer
    from qdistro_presentation.qt import apply_logical_ui_font

    snap = normalize_producer(
        mode="dark",
        colors=DEFAULT_DARK_COLORS,
        settings={"ui": {"fontDefaultScale": 1.0}, "general": {"scaleRatio": 1.2}},
    )
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    monkeypatch.setattr(type(qapp), "devicePixelRatio", lambda self: 2.0)
    monkeypatch.setattr(QWidget, "devicePixelRatio", lambda self: 2.0)
    if hasattr(QWidget, "devicePixelRatioF"):
        monkeypatch.setattr(QWidget, "devicePixelRatioF", lambda self: 2.0)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    want = 11 * 1.2
    assert float(qapp.devicePixelRatio()) == pytest.approx(2.0)
    assert qapp.font().pointSizeF() == pytest.approx(want)
    assert qapp.font().pointSizeF() != pytest.approx(want * 2.0)
    assert qapp.font().pixelSize() == -1
    apply_logical_ui_font(qapp, ctrl.state, native_family=qapp.font().family())
    assert qapp.font().pointSizeF() == pytest.approx(want)
    ctrl.stop()


def test_qt_scale_factor_does_not_multiply_point_size(tmp_path):
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    source = r"""
import os
import sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = "2"
from PyQt6.QtWidgets import QApplication
from qdistro_presentation.model import DEFAULT_DARK_COLORS, normalize_producer
from qdistro_presentation.paths import ResolvedPath
from qdistro_presentation.publish import write_snapshot
from qdistro_presentation.qt import PresentationController

snap = normalize_producer(
    mode="dark",
    colors=DEFAULT_DARK_COLORS,
    settings={"ui": {"fontDefaultScale": 1.0}, "general": {"scaleRatio": 1.2}},
)
write_snapshot(sys.argv[1], snap, require_unwritable_dirs=False)
app = QApplication(["presentation-device-scale"])
ctrl = PresentationController(
    app,
    theme_mode="system",
    snapshot_path=ResolvedPath(
        path=os.path.join(sys.argv[1], "current.json"),
        kind="override",
        expected_uid=None,
        watch=False,
    ),
    watch=False,
)
size = app.font().pointSizeF()
dpr = float(app.devicePixelRatio())
ctrl.stop()
if abs(size - 13.2) > 0.05:
    raise SystemExit(f"FAIL size={size} dpr={dpr} want=13.20")
if abs(size - 13.2 * dpr) <= 0.05 and dpr != 1.0:
    raise SystemExit(f"FAIL size multiplied by dpr size={size} dpr={dpr}")
print(f"ok size={size:.2f} dpr={dpr:.2f}")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get(
        "PYTHONPATH", ""
    )
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(
        [sys.executable, "-c", source, str(snap_dir)],
        timeout=20,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ok size=13.20")
