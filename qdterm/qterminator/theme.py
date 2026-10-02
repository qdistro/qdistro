"""Theme support for QTerminator (dark, light, and system detection)."""

import os
import re

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

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
    """Resolve theme_mode config value to 'dark', 'light', or 'native'."""
    if theme_mode == "dark":
        return "dark"
    if theme_mode == "light":
        return "light"
    if theme_mode == "native":
        return "native"
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


_NATIVE_STYLE: str | None = None
_NATIVE_PALETTE: QPalette | None = None
_NATIVE_STYLESHEET: str | None = None


def _underlying_style_name(app: QApplication) -> str:
    """Factory style name, unwrapping QStyleSheetStyle's empty objectName."""
    qss = app.styleSheet()
    if not qss:
        return app.style().objectName()
    app.setStyleSheet("")
    try:
        return app.style().objectName()
    finally:
        app.setStyleSheet(qss)


def _capture_native(app: QApplication) -> None:
    global _NATIVE_STYLE, _NATIVE_PALETTE, _NATIVE_STYLESHEET
    if _NATIVE_PALETTE is not None:
        return
    _NATIVE_PALETTE = QPalette(app.palette())
    _NATIVE_STYLESHEET = app.styleSheet()
    _NATIVE_STYLE = _underlying_style_name(app)


def _restore_native(app: QApplication) -> None:
    _capture_native(app)
    if _NATIVE_STYLE:
        app.setStyle(_NATIVE_STYLE)
    app.setPalette(QPalette(_NATIVE_PALETTE))
    app.setStyleSheet(_NATIVE_STYLESHEET or "")


def apply_theme(app: QApplication, theme_mode: str = "system"):
    """Apply theme based on mode. Returns the resolved theme ('dark', 'light', or 'native')."""
    _capture_native(app)
    resolved = resolve_theme(theme_mode)
    if resolved == "native":
        _restore_native(app)
        return "native"
    if resolved == "light":
        apply_light_theme(app)
    else:
        apply_dark_theme(app)
    return resolved


_CONTROLLER = None


def attach_presentation(app: QApplication, config):
    global _CONTROLLER
    _capture_native(app)
    try:
        from qdistro_presentation.model import LocalOverrides, parse_local_overrides
        from qdistro_presentation.qt import PresentationController
    except ImportError:
        return apply_theme(app, config.get("general", "theme_mode", default="system"))

    theme_mode = config.get("general", "theme_mode", default="system")
    if theme_mode not in ("system", "dark", "light", "native"):
        theme_mode = "system"
    appearance = config.get("appearance", default={}) or {}
    try:
        local = parse_local_overrides(appearance)
    except Exception:
        local = LocalOverrides()
    ctrl = PresentationController(
        app,
        theme_mode=theme_mode,
        local=local,
        apply_legacy=lambda a, mode: apply_theme(a, mode),
        apply_system_fallback=lambda a: apply_theme(a, detect_system_theme()),
        watch=True,
    )
    ctrl.changed.connect(lambda *_args: refresh_windows(app))
    _CONTROLLER = ctrl
    if ctrl.state.using_shared_palette:
        return ctrl.state.snapshot.mode if ctrl.state.snapshot else "dark"
    if theme_mode in ("dark", "light"):
        return theme_mode
    if theme_mode == "native":
        return "native"
    return detect_system_theme()


def current_controller():
    return _CONTROLLER


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_FALLBACK_SURFACE = "#1e1e1e"
_FALLBACK_ON_SURFACE = "#d4d4d4"
_FALLBACK_SURFACE_VARIANT = "#2d2d2d"
_FALLBACK_ON_SURFACE_VARIANT = "#808080"
_FALLBACK_HOVER = "#3c3c3c"
_FALLBACK_OUTLINE = "#555555"


def _css_hex(value: object, fallback: str) -> str:
    if isinstance(value, str) and _HEX.fullmatch(value.strip()):
        return value.strip().lower()
    if isinstance(fallback, str) and _HEX.fullmatch(fallback.strip()):
        return fallback.strip().lower()
    return "#000000"


def _qcolor_hex(color: QColor, fallback: str) -> str:
    if color.isValid():
        return _css_hex(
            f"#{color.red():02x}{color.green():02x}{color.blue():02x}",
            fallback,
        )
    return _css_hex(fallback, "#000000")


def shared_palette_colors():
    """Snapshot colors when Follow desktop is using the shared palette."""
    try:
        ctrl = current_controller()
        state = getattr(ctrl, "state", None) if ctrl is not None else None
        snap = getattr(state, "snapshot", None) if state is not None else None
        if getattr(state, "using_shared_palette", False) and snap is not None:
            return snap.colors
    except Exception:  # noqa: BLE001
        return None
    return None


def pane_roles(widget: QWidget) -> dict[str, str]:
    """Tab/splitter chrome from the shared snapshot, else the widget palette."""
    colors = shared_palette_colors()
    if colors is not None:
        return {
            "surface": _css_hex(colors.mSurface, _FALLBACK_SURFACE),
            "on_surface": _css_hex(colors.mOnSurface, _FALLBACK_ON_SURFACE),
            "surface_variant": _css_hex(
                colors.mSurfaceVariant, _FALLBACK_SURFACE_VARIANT
            ),
            "on_surface_variant": _css_hex(
                colors.mOnSurfaceVariant, _FALLBACK_ON_SURFACE_VARIANT
            ),
            "hover": _css_hex(colors.mHover, _FALLBACK_HOVER),
            "on_hover": _css_hex(colors.mOnHover, _FALLBACK_ON_SURFACE),
            "outline": _css_hex(colors.mOutline, _FALLBACK_OUTLINE),
        }
    pal = widget.palette()
    return {
        "surface": _qcolor_hex(
            pal.color(QPalette.ColorRole.Window), _FALLBACK_SURFACE
        ),
        "on_surface": _qcolor_hex(
            pal.color(QPalette.ColorRole.WindowText), _FALLBACK_ON_SURFACE
        ),
        "surface_variant": _qcolor_hex(
            pal.color(QPalette.ColorRole.AlternateBase), _FALLBACK_SURFACE_VARIANT
        ),
        "on_surface_variant": _qcolor_hex(
            pal.color(QPalette.ColorRole.PlaceholderText),
            _FALLBACK_ON_SURFACE_VARIANT,
        ),
        "hover": _qcolor_hex(
            pal.color(QPalette.ColorRole.Highlight), _FALLBACK_HOVER
        ),
        "on_hover": _qcolor_hex(
            pal.color(QPalette.ColorRole.HighlightedText), _FALLBACK_ON_SURFACE
        ),
        "outline": _qcolor_hex(
            pal.color(QPalette.ColorRole.Mid), _FALLBACK_OUTLINE
        ),
    }


def using_shared_palette() -> bool:
    return shared_palette_colors() is not None


def refresh_windows(app: QApplication) -> None:
    for widget in app.topLevelWidgets():
        method = getattr(widget, "apply_presentation_update", None)
        if callable(method):
            method()
        else:
            widget.update()
            for child in widget.findChildren(QWidget):
                child.update()


def apply_profile_to_all_windows(app: QApplication, profile_name: str) -> None:
    for widget in app.topLevelWidgets():
        method = getattr(widget, "apply_profile_to_terminals", None)
        if callable(method):
            method(profile_name)


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
