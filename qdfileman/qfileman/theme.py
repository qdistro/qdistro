"""Application theme helpers.

A single entry point, :func:`apply_theme`, switches the running
``QApplication`` between three modes:

- ``"dark"``   — Fusion style with a hand-tuned dark palette.
- ``"light"``  — Fusion style with its default (light) palette.
- ``"system"`` — leave Qt's defaults alone so the host desktop theme
  (Breeze on KDE, Adwaita on GNOME, etc.) is used. We do not touch
  ``setStyle`` or ``setPalette`` in this mode.
"""

from __future__ import annotations

import logging

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

log = logging.getLogger(__name__)


_VALID_MODES = ("system", "light", "dark")


def apply_theme(app: QApplication, mode: str = "system") -> str:
    """Apply dark, light, or system theme to ``app``.

    Returns the resolved mode string. Unknown modes fall back to
    ``"system"`` with a warning logged.

    ``"system"`` is a deliberate no-op: it leaves Qt's platform
    integration in charge so the host desktop theme is used. ``"light"``
    and ``"dark"`` switch to Fusion so the dark palette renders the same
    way across desktops.
    """
    if mode not in _VALID_MODES:
        log.warning("unknown theme mode %r; falling back to 'system'", mode)
        mode = "system"

    if mode == "system":
        return "system"

    app.setStyle("Fusion")
    if mode == "dark":
        app.setPalette(_dark_palette())
        return "dark"

    # Light mode: Fusion's default palette.
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
