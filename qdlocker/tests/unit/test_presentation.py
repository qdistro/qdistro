"""Locker trusted presentation: freeze on lock, ignore overrides, fail open."""

from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtGui")
pytest.importorskip("PyQt6.QtQml")

from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
from PyQt6.QtCore import QUrl  # noqa: E402

from qdlocker.app import WaylandBridge  # noqa: E402
from qdlocker.presentation import LockerPresentation, try_load_trusted_snapshot  # noqa: E402


@pytest.fixture
def qgui():
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([])
    return app


def _distinct_snapshot():
    from qdistro_presentation.model import example_snapshot, with_generation

    colors = replace(example_snapshot().colors, mSurface="#112233", mOnSurface="#eeeeee")
    return with_generation(replace(example_snapshot(), colors=colors))


def _force_absent_managed(monkeypatch, tmp_path):
    from qdistro_presentation import paths as paths_mod

    real = paths_mod.resolve_snapshot_path
    absent = str(tmp_path / "no-managed")

    def wrapped(*, role="ordinary", environ=None, managed_dir=paths_mod.MANAGED_DIR):
        return real(role=role, environ=environ, managed_dir=absent)

    monkeypatch.setattr(paths_mod, "resolve_snapshot_path", wrapped)


def test_override_env_is_ignored(qgui, tmp_path, monkeypatch):
    from qdistro_presentation.model import example_snapshot
    from qdistro_presentation.publish import write_snapshot

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv("QDISTRO_PRESENTATION_FILE", str(tmp_path / "current.json"))
    _force_absent_managed(monkeypatch, tmp_path)
    assert try_load_trusted_snapshot() is None
    pres = LockerPresentation()
    pres.reload_trusted()
    assert pres.has_snapshot is False
    assert pres.value("mSurface") in ("#070722",)


def test_missing_library_keeps_defaults(qgui, monkeypatch):
    pres = LockerPresentation()
    real_import = __import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("qdistro_presentation"):
            raise ImportError("missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake)
    pres.reload_trusted()
    assert pres.has_snapshot is False
    assert pres.value("mSurface") == "#070722"


def test_invalid_snapshot_keeps_defaults(qgui, monkeypatch):
    from qdistro_presentation.model import SnapshotError

    def bad_load(*_a, **_k):
        raise SnapshotError("truncated")

    monkeypatch.setattr("qdistro_presentation.paths.resolve_snapshot_path", lambda **_k: object())
    monkeypatch.setattr("qdistro_presentation.paths.load_snapshot", bad_load)
    pres = LockerPresentation()
    pres.reload_trusted()
    assert pres.has_snapshot is False
    assert pres.value("fontSizeM") == 11.0


def test_valid_snapshot_applies_and_caps_scale(qgui, monkeypatch):
    from qdistro_presentation.model import example_snapshot, with_generation
    from qdistro_presentation.paths import ResolvedPath

    fonts = replace(example_snapshot().fonts, ui_scale=1.25)
    metrics = replace(example_snapshot().metrics, ui_scale=1.2, radius_ratio=2.0)
    snap = with_generation(replace(example_snapshot(), fonts=fonts, metrics=metrics))
    monkeypatch.setattr(
        "qdistro_presentation.paths.resolve_snapshot_path",
        lambda **_k: ResolvedPath(path="/tmp/x", kind="override", expected_uid=None, watch=False),
    )
    monkeypatch.setattr(
        "qdistro_presentation.paths.load_snapshot",
        lambda _resolved: (snap, (1, 2, 3, 4)),
    )
    pres = LockerPresentation()
    pres.reload_trusted()
    assert pres.has_snapshot is True
    assert pres.value("mSurface") == example_snapshot().colors.mSurface
    # 11 * 1.25 * 1.2 = 16.5
    assert pres.value("fontSizeM") == pytest.approx(16.5)
    assert pres.value("radiusXS") == 16  # round(8 * 2.0)


def test_enabled_false_resets_to_defaults(qgui, monkeypatch):
    from qdistro_presentation.model import example_snapshot, with_generation
    from qdistro_presentation.paths import ResolvedPath

    enabled = _distinct_snapshot()
    disabled = with_generation(replace(example_snapshot(), enabled=False))
    snaps = [enabled, disabled]
    monkeypatch.setattr(
        "qdistro_presentation.paths.resolve_snapshot_path",
        lambda **_k: ResolvedPath(path="/tmp/x", kind="override", expected_uid=None, watch=False),
    )
    monkeypatch.setattr(
        "qdistro_presentation.paths.load_snapshot",
        lambda _resolved: (snaps.pop(0), (1, 2, 3, 4)),
    )
    pres = LockerPresentation()
    pres.reload_trusted()
    assert pres.has_snapshot is True
    assert pres.value("mSurface") == "#112233"
    pres.reload_trusted()
    assert pres.has_snapshot is False
    assert pres.value("mSurface") == "#070722"


def test_failed_reload_keeps_last_good(qgui, monkeypatch):
    from qdistro_presentation.model import SnapshotError
    from qdistro_presentation.paths import ResolvedPath

    snap = _distinct_snapshot()
    monkeypatch.setattr(
        "qdistro_presentation.paths.resolve_snapshot_path",
        lambda **_k: ResolvedPath(path="/tmp/x", kind="override", expected_uid=None, watch=False),
    )
    monkeypatch.setattr(
        "qdistro_presentation.paths.load_snapshot",
        lambda _resolved: (snap, (1, 2, 3, 4)),
    )
    pres = LockerPresentation()
    pres.reload_trusted()
    assert pres.value("mSurface") == "#112233"

    def bad_load(*_a, **_k):
        raise SnapshotError("gone")

    monkeypatch.setattr("qdistro_presentation.paths.load_snapshot", bad_load)
    pres.freeze_for_lock()
    assert pres.has_snapshot is True
    assert pres.value("mSurface") == "#112233"


def test_lock_request_freezes_before_visibility(qgui, monkeypatch):
    from dataclasses import replace

    from PyQt6.QtCore import QCoreApplication
    from qdistro_presentation.model import with_generation
    from qdistro_presentation.paths import ResolvedPath

    snap_a = _distinct_snapshot()
    snap_b = with_generation(
        replace(snap_a, colors=replace(snap_a.colors, mSurface="#445566"))
    )
    current = [snap_a]
    loads: list[str] = []

    def load(_resolved):
        snap = current[0]
        loads.append(snap.colors.mSurface)
        return snap, (1, 2, 3, 4)

    monkeypatch.setattr(
        "qdistro_presentation.paths.resolve_snapshot_path",
        lambda **_k: ResolvedPath(path="/tmp/x", kind="override", expected_uid=None, watch=False),
    )
    monkeypatch.setattr("qdistro_presentation.paths.load_snapshot", load)
    pres = LockerPresentation()
    assert pres.has_snapshot is False
    controller = MagicMock()
    bridge = WaylandBridge(controller)
    bridge.set_presentation(pres)
    order = []
    bridge.lockedChanged.connect(
        lambda locked: order.append(("visible", locked, pres.value("mSurface")))
    )
    bridge.inject_lock_requested(3)
    QCoreApplication.processEvents()
    assert pres.has_snapshot is True
    assert order[0][0] == "visible"
    assert order[0][2] == "#112233"
    assert loads == ["#112233"]
    controller.notify_lock_begin.assert_called()

    current[0] = snap_b
    bridge._on_locked_changed(True)
    assert pres.value("mSurface") == "#112233"
    assert loads == ["#112233"]

    bridge._on_unlocked()
    QCoreApplication.processEvents()
    bridge.inject_lock_requested(3)
    QCoreApplication.processEvents()
    assert pres.value("mSurface") == "#445566"
    assert loads == ["#112233", "#445566"]


def test_lock_ui_defaults_without_adapter(qgui):
    from pathlib import Path

    qml_root = str(Path(__file__).resolve().parents[2] / "qdlocker" / "qml")
    engine = QQmlEngine()
    engine.addImportPath(qml_root)
    component = QQmlComponent(engine, QUrl.fromLocalFile(qml_root + "/LockUI.qml"))
    obj = component.create()
    assert obj is not None, [e.toString() for e in component.errors()]
    # Keep engine alive.
    obj._engine = engine
    obj._component = component
    banner = obj.findChild(type(obj), "securityBanner")
    assert banner is not None


def test_lock_ui_follows_frozen_snapshot(qgui, monkeypatch):
    from pathlib import Path

    from PyQt6.QtGui import QColor
    from qdistro_presentation.paths import ResolvedPath

    snap = _distinct_snapshot()
    monkeypatch.setattr(
        "qdistro_presentation.paths.resolve_snapshot_path",
        lambda **_k: ResolvedPath(path="/tmp/x", kind="override", expected_uid=None, watch=False),
    )
    monkeypatch.setattr(
        "qdistro_presentation.paths.load_snapshot",
        lambda _resolved: (snap, (1, 2, 3, 4)),
    )
    pres = LockerPresentation()
    pres.freeze_for_lock()
    qml_root = str(Path(__file__).resolve().parents[2] / "qdlocker" / "qml")
    engine = QQmlEngine()
    engine.addImportPath(qml_root)
    engine.rootContext().setContextProperty("presentation", pres)
    component = QQmlComponent(engine)
    component.setData(
        b'import QtQuick\nimport shim\nItem { objectName: "probe"; property color surface: Color.mSurface }\n',
        QUrl(),
    )
    obj = component.create()
    assert obj is not None, [e.toString() for e in component.errors()]
    obj._engine = engine
    obj._component = component
    color = obj.property("surface")
    assert color is not None
    assert QColor(color).name().lower() == "#112233"


def test_busy_indicator_not_gated_on_animation_style():
    from pathlib import Path

    ui = (Path(__file__).resolve().parents[2] / "qdlocker" / "qml" / "LockUI.qml").read_text()
    assert "BusyIndicator" in ui
    assert "running: visible" in ui
    assert "Style.animation" not in ui
    assert "animationDisabled" not in ui
