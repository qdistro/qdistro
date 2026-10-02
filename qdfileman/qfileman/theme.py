"""Application theme helpers.

``apply_theme`` keeps the explicit legacy palettes. Shared qdshell appearance
is attached with :func:`attach_presentation` after QApplication construction.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

_VALID_MODES = ("system", "light", "dark", "native")
_CONTROLLER = None
_NATIVE_STYLE: str | None = None
_NATIVE_PALETTE: QPalette | None = None
_NATIVE_STYLESHEET: str | None = None


def _capture_native(app: QApplication) -> None:
    global _NATIVE_STYLE, _NATIVE_PALETTE, _NATIVE_STYLESHEET
    if _NATIVE_PALETTE is not None:
        return
    _NATIVE_STYLE = app.style().objectName()
    _NATIVE_PALETTE = QPalette(app.palette())
    _NATIVE_STYLESHEET = app.styleSheet()


def _restore_native(app: QApplication) -> None:
    _capture_native(app)
    if _NATIVE_STYLE:
        app.setStyle(_NATIVE_STYLE)
    app.setPalette(QPalette(_NATIVE_PALETTE))
    app.setStyleSheet(_NATIVE_STYLESHEET or "")


def apply_theme(app: QApplication, mode: str = "system") -> str:
    """Apply dark, light, native, or system theme to ``app``.

    ``system`` without a presentation controller is a no-op so the host
    desktop theme is used. ``native`` restores the captured platform
    style, palette, and stylesheet even when the shared package is missing.
    """
    _capture_native(app)
    if mode not in _VALID_MODES:
        log.warning("unknown theme mode %r; falling back to 'system'", mode)
        mode = "system"

    if mode == "native":
        _restore_native(app)
        return "native"

    if mode == "system":
        return mode

    app.setStyle("Fusion")
    if mode == "dark":
        app.setPalette(_dark_palette())
        return "dark"

    app.setPalette(QPalette())
    return "light"


def _dark_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.Base, QColor(42, 42, 42))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(66, 66, 66))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.Text, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(212, 212, 212))
    return palette


def _legacy_apply(app: QApplication, mode: str) -> None:
    apply_theme(app, mode)


def _system_fallback(app: QApplication) -> None:
    apply_theme(app, "system")


def attach_presentation(app: QApplication, config: Any):
    """Attach the shared presentation controller. Missing package is non-fatal."""
    global _CONTROLLER
    _capture_native(app)
    try:
        from qdistro_presentation.model import LocalOverrides, parse_local_overrides
        from qdistro_presentation.qt import PresentationController
    except ImportError as exc:
        log.warning("qdistro_presentation not installed; using local theme only (%s)", exc)
        mode = config.get("general", "theme_mode", default="system")
        apply_theme(app, mode)
        return None

    theme_mode = config.get("general", "theme_mode", default="system")
    if theme_mode not in _VALID_MODES:
        log.warning("unknown theme mode %r; falling back to 'system'", theme_mode)
        theme_mode = "system"
    appearance = config.get("appearance", default={}) or {}
    try:
        local = parse_local_overrides(appearance)
    except Exception as exc:  # noqa: BLE001
        log.warning("invalid appearance overrides: %s", exc)
        local = LocalOverrides()

    ctrl = PresentationController(
        app,
        theme_mode=theme_mode,
        local=local,
        apply_legacy=_legacy_apply,
        apply_system_fallback=_system_fallback,
        watch=True,
    )
    ctrl.changed.connect(lambda *_args: refresh_windows(app))
    _CONTROLLER = ctrl
    return ctrl


def current_controller():
    return _CONTROLLER


def refresh_windows(app: QApplication) -> None:
    for widget in app.topLevelWidgets():
        method = getattr(widget, "apply_presentation_update", None)
        if callable(method):
            method()
        else:
            widget.update()
            for child in widget.findChildren(QWidget):
                child.update()


def reset_controller_for_tests() -> None:
    global _CONTROLLER, _NATIVE_STYLE, _NATIVE_PALETTE, _NATIVE_STYLESHEET
    if _CONTROLLER is not None:
        try:
            _CONTROLLER.stop()
        except Exception:  # noqa: BLE001
            pass
    _CONTROLLER = None
    _NATIVE_STYLE = None
    _NATIVE_PALETTE = None
    _NATIVE_STYLESHEET = None
    try:
        from qdistro_presentation.qt import reset_controller_for_tests as _reset

        _reset()
    except ImportError:
        pass
