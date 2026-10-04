"""Locker trusted presentation: freeze on lock, ignore overrides, fail open."""

from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtGui")
pytest.importorskip("PyQt6.QtQml")

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402
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


def _install_snapshot_loader(monkeypatch, current):
    from qdistro_presentation.paths import ResolvedPath

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
    return loads


def _bridge_with_pres(pres):
    controller = MagicMock()
    client = MagicMock()
    bridge = WaylandBridge(controller)
    bridge.set_presentation(pres)
    bridge.attach(client)
    return bridge, controller, client


@pytest.mark.cheat_aware(
    protects="a slow presentation snapshot cannot delay set_locked or lock_acknowledged",
    severity="high",
    cheats=[
        "call freeze_for_lock in _on_lock_requested before lock_acknowledged",
        "move the read one line after lock_acknowledged in the same slot",
    ],
    consequence="a hung /var/lib/qdistro/presentation read delays the lock surface",
)
def test_lock_request_acknowledges_before_freeze(qgui, monkeypatch):
    from dataclasses import replace

    from PyQt6.QtCore import QCoreApplication
    from qdistro_presentation.model import with_generation

    snap_a = _distinct_snapshot()
    snap_b = with_generation(
        replace(snap_a, colors=replace(snap_a.colors, mSurface="#445566"))
    )
    current = [snap_a]
    loads = _install_snapshot_loader(monkeypatch, current)
    pres = LockerPresentation()
    assert pres.has_snapshot is False
    bridge, controller, client = _bridge_with_pres(pres)
    order = []
    bridge.lockedChanged.connect(
        lambda locked: order.append(("visible", locked, pres.value("mSurface")))
    )
    client.set_locked.side_effect = lambda v: order.append(("set_locked", v))
    client.lock_acknowledged.side_effect = lambda reason: order.append(
        ("lock_acknowledged", reason)
    )

    bridge._on_lock_requested(3)
    assert order == [
        ("visible", True, "#070722"),
        ("set_locked", True),
        ("lock_acknowledged", 3),
    ]
    assert loads == []
    assert pres.has_snapshot is False
    assert bridge.locked is True
    controller.notify_lock_begin.assert_called()
    client.set_locked.assert_called_once_with(True)
    client.lock_acknowledged.assert_called_once_with(3)

    QCoreApplication.processEvents()
    assert pres.has_snapshot is True
    assert pres.value("mSurface") == "#112233"
    assert loads == ["#112233"]

    current[0] = snap_b
    bridge._on_locked_changed(True)
    QCoreApplication.processEvents()
    assert pres.value("mSurface") == "#112233"
    assert loads == ["#112233"]

    bridge._on_unlocked()
    QCoreApplication.processEvents()
    order.clear()
    bridge._on_lock_requested(3)
    assert order[0] == ("visible", True, "#112233")
    assert ("lock_acknowledged", 3) in order
    assert loads == ["#112233"]
    QCoreApplication.processEvents()
    assert pres.value("mSurface") == "#445566"
    assert loads == ["#112233", "#445566"]


@pytest.mark.cheat_aware(
    protects="a failing presentation freeze cannot prevent lock acknowledgement",
    severity="high",
    cheats=["let freeze_for_lock exceptions escape _on_lock_requested"],
    consequence="an unreadable snapshot leaves the compositor without lock_acknowledged",
)
def test_failing_freeze_does_not_block_lock(qgui, monkeypatch):
    from PyQt6.QtCore import QCoreApplication

    pres = LockerPresentation()
    calls = []

    def boom():
        calls.append("freeze")
        raise RuntimeError("snapshot hung")

    monkeypatch.setattr(pres, "freeze_for_lock", boom)
    bridge, controller, client = _bridge_with_pres(pres)
    bridge._on_lock_requested(3)
    client.set_locked.assert_called_once_with(True)
    client.lock_acknowledged.assert_called_once_with(3)
    assert bridge.locked is True
    assert calls == []
    QCoreApplication.processEvents()
    assert calls == ["freeze"]
    assert bridge.locked is True
    controller.notify_lock_begin.assert_called_once_with()
    client.lock_acknowledged.assert_called_once_with(3)


def test_lock_request_keeps_cached_appearance_until_queued_freeze(qgui, monkeypatch):
    from dataclasses import replace

    from PyQt6.QtCore import QCoreApplication
    from qdistro_presentation.model import with_generation

    snap_a = _distinct_snapshot()
    snap_b = with_generation(
        replace(snap_a, colors=replace(snap_a.colors, mSurface="#445566"))
    )
    current = [snap_a]
    _install_snapshot_loader(monkeypatch, current)
    pres = LockerPresentation()
    pres.reload_trusted()
    assert pres.value("mSurface") == "#112233"
    current[0] = snap_b
    bridge, _, client = _bridge_with_pres(pres)
    first_visible = []
    bridge.lockedChanged.connect(
        lambda locked: first_visible.append(pres.value("mSurface"))
    )
    bridge._on_lock_requested(3)
    assert first_visible == ["#112233"]
    client.lock_acknowledged.assert_called_once_with(3)
    assert pres.value("mSurface") == "#112233"
    QCoreApplication.processEvents()
    assert pres.value("mSurface") == "#445566"


@pytest.mark.cheat_aware(
    protects="compositor lock confirm is not delayed by a presentation freeze",
    severity="high",
    cheats=["call freeze_for_lock in _on_locked_changed before lock_confirmed_cb"],
    consequence="a hung snapshot read holds the logind sleep inhibitor",
)
def test_compositor_entry_confirms_before_freeze(qgui, monkeypatch):
    from PyQt6.QtCore import QCoreApplication

    current = [_distinct_snapshot()]
    loads = _install_snapshot_loader(monkeypatch, current)
    pres = LockerPresentation()
    bridge, _, _ = _bridge_with_pres(pres)
    confirmed = []

    def on_confirm():
        confirmed.append((pres.has_snapshot, pres.value("mSurface"), list(loads)))

    bridge.set_lock_confirmed_cb(on_confirm)
    bridge._on_locked_changed(True)
    assert confirmed == [(False, "#070722", [])]
    assert bridge.locked is True
    assert pres.has_snapshot is False
    assert loads == []
    QCoreApplication.processEvents()
    assert pres.has_snapshot is True
    assert pres.value("mSurface") == "#112233"
    assert loads == ["#112233"]


def test_initially_locked_ready_schedules_freeze_after_session_begin(qgui, monkeypatch):
    from PyQt6.QtCore import QCoreApplication

    current = [_distinct_snapshot()]
    loads = _install_snapshot_loader(monkeypatch, current)
    pres = LockerPresentation()
    bridge, controller, _ = _bridge_with_pres(pres)
    bridge._on_ready(True)
    controller.notify_lock_begin.assert_called_once_with()
    assert bridge.locked is True
    assert pres.has_snapshot is False
    assert loads == []
    QCoreApplication.processEvents()
    assert pres.has_snapshot is True
    assert pres.value("mSurface") == "#112233"
    assert loads == ["#112233"]


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
