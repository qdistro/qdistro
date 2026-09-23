"""Theme support for QTerminator (dark, light, and system detection)."""

import os

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

# -- Dark palette colors --
BG_DARK = "#1e1e1e"
BG_MID = "#2d2d2d"
BG_LIGHT = "#3c3c3c"
FG = "#d4d4d4"
FG_DIM = "#808080"
ACCENT = "#2a6ea8"
ACCENT_LIGHT = "#3d8fd4"
BORDER = "#555555"
SELECTION = "#264f78"

# -- Light palette colors --
LT_BG = "#f0f0f0"
LT_BG_BASE = "#ffffff"
LT_BG_MID = "#e0e0e0"
LT_FG = "#1e1e1e"
LT_FG_DIM = "#808080"
LT_ACCENT = "#0078d4"
LT_ACCENT_DARK = "#005a9e"
LT_BORDER = "#c0c0c0"
LT_SELECTION = "#0078d4"


def detect_system_theme() -> str:
    """Detect whether the OS prefers dark or light theme.

    Uses Qt's QStyleHints.colorScheme() if available (Qt 6.5+),
    otherwise falls back to environment variables.
    Returns "dark" or "light".
    """
    app = QApplication.instance()
    if app:
        try:
            hints = app.styleHints()
            scheme = hints.colorScheme()
            # Qt.ColorScheme.Dark == 2, Light == 1, Unknown == 0
            from PyQt6.QtCore import Qt as _Qt
            if hasattr(_Qt, "ColorScheme"):
                if scheme == _Qt.ColorScheme.Dark:
                    return "dark"
                elif scheme == _Qt.ColorScheme.Light:
                    return "light"
        except (AttributeError, TypeError):
            pass

    # Fallback: check common environment variables
    # GTK/GNOME
    gtk_theme = os.environ.get("GTK_THEME", "")
    if "dark" in gtk_theme.lower():
        return "dark"

    # KDE
    kde_scheme = os.environ.get("KDE_COLOR_SCHEME", "")
    if "dark" in kde_scheme.lower():
        return "dark"

    # Generic freedesktop
    color_scheme = os.environ.get("QT_QPA_PLATFORMTHEME", "")
    if "dark" in color_scheme.lower():
        return "dark"

    # Default to dark (QTerminator's original default)
    return "dark"


def resolve_theme(theme_mode: str) -> str:
    """Resolve theme_mode config value to 'dark' or 'light'."""
    if theme_mode == "dark":
        return "dark"
    elif theme_mode == "light":
        return "light"
    else:  # "system" or unknown
        return detect_system_theme()


def apply_dark_theme(app: QApplication):
    """Apply a dark color palette and stylesheet to the application."""
    palette = QPalette()

    palette.setColor(QPalette.ColorRole.Window, QColor(BG_MID))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(FG))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_DARK))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_MID))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_LIGHT))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(FG))
    palette.setColor(QPalette.ColorRole.Text, QColor(FG))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_MID))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(FG))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT_LIGHT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(SELECTION))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))

    # Disabled colors
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(FG_DIM))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(FG_DIM))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(FG_DIM))

    app.setPalette(palette)

    app.setStyleSheet(STYLESHEET)


def apply_light_theme(app: QApplication):
    """Apply a light color palette and stylesheet to the application."""
    palette = QPalette()

    palette.setColor(QPalette.ColorRole.Window, QColor(LT_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(LT_FG))
    palette.setColor(QPalette.ColorRole.Base, QColor(LT_BG_BASE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(LT_BG))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(LT_BG_BASE))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(LT_FG))
    palette.setColor(QPalette.ColorRole.Text, QColor(LT_FG))
    palette.setColor(QPalette.ColorRole.Button, QColor(LT_BG_MID))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(LT_FG))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#000000"))
    palette.setColor(QPalette.ColorRole.Link, QColor(LT_ACCENT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(LT_SELECTION))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))

    # Disabled colors
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(LT_FG_DIM))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(LT_FG_DIM))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(LT_FG_DIM))

    app.setPalette(palette)

    app.setStyleSheet(LIGHT_STYLESHEET)


def apply_theme(app: QApplication, theme_mode: str = "system"):
    """Apply theme based on mode. Returns the resolved theme ('dark' or 'light')."""
    resolved = resolve_theme(theme_mode)
    if resolved == "light":
        apply_light_theme(app)
    else:
        apply_dark_theme(app)
    return resolved


STYLESHEET = f"""
QMainWindow {{
    background-color: {BG_MID};
}}

QMenuBar {{
    background-color: {BG_MID};
    color: {FG};
    border-bottom: 1px solid {BORDER};
}}

QMenuBar::item:selected {{
    background-color: {BG_LIGHT};
}}

QMenu {{
    background-color: {BG_MID};
    color: {FG};
    border: 1px solid {BORDER};
}}

QMenu::item:selected {{
    background-color: {SELECTION};
}}

QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

QTabWidget::pane {{
    border: none;
}}

QTabBar {{
    background-color: {BG_DARK};
}}

QTabBar::tab {{
    background-color: {BG_DARK};
    color: {FG_DIM};
    padding: 4px 12px;
    border: none;
    border-right: 1px solid {BORDER};
    min-width: 80px;
}}

QTabBar::tab:selected {{
    background-color: {BG_MID};
    color: {FG};
}}

QTabBar::tab:hover {{
    background-color: {BG_LIGHT};
    color: {FG};
}}

QTabBar::close-button {{
    subcontrol-position: right;
    padding: 2px;
}}

QTabBar::close-button:hover {{
    background-color: {BG_LIGHT};
    border-radius: 3px;
}}

QSplitter::handle {{
    background-color: {BORDER};
}}

QDialog {{
    background-color: {BG_MID};
    color: {FG};
}}

QGroupBox {{
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 12px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    padding: 0 4px;
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {BG_DARK};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 2px 4px;
}}

QComboBox::drop-down {{
    border: none;
}}

QPushButton {{
    background-color: {BG_LIGHT};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 4px 12px;
}}

QPushButton:hover {{
    background-color: {ACCENT};
}}

QPushButton:pressed {{
    background-color: {ACCENT_LIGHT};
}}

QCheckBox {{
    color: {FG};
}}

QLabel {{
    color: {FG};
}}

QScrollBar:vertical {{
    background-color: {BG_DARK};
    width: 10px;
    border: none;
}}

QScrollBar::handle:vertical {{
    background-color: {BG_LIGHT};
    border-radius: 4px;
    min-height: 20px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {FG_DIM};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""

LIGHT_STYLESHEET = f"""
QMainWindow {{
    background-color: {LT_BG};
}}

QMenuBar {{
    background-color: {LT_BG};
    color: {LT_FG};
    border-bottom: 1px solid {LT_BORDER};
}}

QMenuBar::item:selected {{
    background-color: {LT_BG_MID};
}}

QMenu {{
    background-color: {LT_BG_BASE};
    color: {LT_FG};
    border: 1px solid {LT_BORDER};
}}

QMenu::item:selected {{
    background-color: {LT_SELECTION};
    color: #ffffff;
}}

QMenu::separator {{
    height: 1px;
    background: {LT_BORDER};
    margin: 4px 8px;
}}

QTabWidget::pane {{
    border: none;
}}

QTabBar {{
    background-color: {LT_BG_MID};
}}

QTabBar::tab {{
    background-color: {LT_BG_MID};
    color: {LT_FG_DIM};
    padding: 4px 12px;
    border: none;
    border-right: 1px solid {LT_BORDER};
    min-width: 80px;
}}

QTabBar::tab:selected {{
    background-color: {LT_BG};
    color: {LT_FG};
}}

QTabBar::tab:hover {{
    background-color: {LT_BG_BASE};
    color: {LT_FG};
}}

QTabBar::close-button {{
    subcontrol-position: right;
    padding: 2px;
}}

QTabBar::close-button:hover {{
    background-color: {LT_BG_MID};
    border-radius: 3px;
}}

QSplitter::handle {{
    background-color: {LT_BORDER};
}}

QDialog {{
    background-color: {LT_BG};
    color: {LT_FG};
}}

QGroupBox {{
    color: {LT_FG};
    border: 1px solid {LT_BORDER};
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 12px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    padding: 0 4px;
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {LT_BG_BASE};
    color: {LT_FG};
    border: 1px solid {LT_BORDER};
    border-radius: 3px;
    padding: 2px 4px;
}}

QComboBox::drop-down {{
    border: none;
}}

QPushButton {{
    background-color: {LT_BG_MID};
    color: {LT_FG};
    border: 1px solid {LT_BORDER};
    border-radius: 3px;
    padding: 4px 12px;
}}

QPushButton:hover {{
    background-color: {LT_ACCENT};
    color: #ffffff;
}}

QPushButton:pressed {{
    background-color: {LT_ACCENT_DARK};
    color: #ffffff;
}}

QCheckBox {{
    color: {LT_FG};
}}

QLabel {{
    color: {LT_FG};
}}

QScrollBar:vertical {{
    background-color: {LT_BG};
    width: 10px;
    border: none;
}}

QScrollBar::handle:vertical {{
    background-color: {LT_BG_MID};
    border-radius: 4px;
    min-height: 20px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {LT_FG_DIM};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""
