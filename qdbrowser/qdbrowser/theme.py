"""Theme palette + stylesheet for qdbrowser (dark / light / system)."""

import os
import re

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

BG_DARK = "#1e1e1e"
BG_MID = "#2d2d2d"
BG_LIGHT = "#3c3c3c"
FG = "#d4d4d4"
FG_DIM = "#808080"
ACCENT = "#2a6ea8"
ACCENT_LIGHT = "#3d8fd4"
BORDER = "#555555"
SELECTION = "#264f78"

LT_BG = "#f0f0f0"
LT_BG_BASE = "#ffffff"
LT_BG_MID = "#e0e0e0"
LT_FG = "#1e1e1e"
LT_FG_DIM = "#808080"
LT_ACCENT = "#0078d4"
LT_BORDER = "#c0c0c0"
LT_SELECTION = "#0078d4"


def detect_system_theme() -> str:
    app = QApplication.instance()
    if app:
        try:
            hints = app.styleHints()
            scheme = hints.colorScheme()
            from PyQt6.QtCore import Qt

            if hasattr(Qt, "ColorScheme"):
                if scheme == Qt.ColorScheme.Dark:
                    return "dark"
                if scheme == Qt.ColorScheme.Light:
                    return "light"
        except (AttributeError, TypeError):
            pass
    for env in ("GTK_THEME", "KDE_COLOR_SCHEME", "QT_QPA_PLATFORMTHEME"):
        if "dark" in os.environ.get(env, "").lower():
            return "dark"
    return "dark"


def resolve_theme(mode: str) -> str:
    if mode == "dark":
        return "dark"
    if mode == "light":
        return "light"
    return detect_system_theme()


def apply_theme(app: QApplication, mode: str = "system") -> str:
    resolved = resolve_theme(mode)
    if resolved == "light":
        _apply_light(app)
    else:
        _apply_dark(app)
    return resolved


def palette_dict(mode: str = "auto") -> dict:
    """Return a palette dict for use by in-page injected CSS (translate
    overlay, dark mode, content_blocker cosmetic style, etc.). The
    keys are stable across theme changes; values are CSS colors.

    ``mode``:
      - ``"auto"`` — follow the effective app palette
      - ``"dark"`` — force dark palette
      - ``"light"`` — force light palette
    """
    if mode == "auto":
        from PyQt6.QtGui import QPalette
        from PyQt6.QtWidgets import QApplication as _QApplication

        ctrl = current_controller()
        if ctrl is not None and ctrl.state.using_shared_palette and ctrl.state.colors:
            colors = ctrl.state.colors
            return {
                "bg": colors.mSurface,
                "bg_mid": colors.mSurfaceVariant,
                "bg_dim": colors.mSurfaceVariant,
                "fg": colors.mOnSurface,
                "fg_dim": colors.mOnSurfaceVariant,
                "accent": colors.mPrimary,
                "border": colors.mOutline,
                "selection": colors.mPrimary,
                "selection_fg": colors.mOnPrimary,
                "hover": colors.mHover,
                "hover_fg": colors.mOnHover,
            }
        app = _QApplication.instance()
        if app is not None:
            pal = app.palette()
            return {
                "bg": pal.color(QPalette.ColorRole.Base).name(),
                "bg_mid": pal.color(QPalette.ColorRole.Window).name(),
                "bg_dim": pal.color(QPalette.ColorRole.AlternateBase).name(),
                "fg": pal.color(QPalette.ColorRole.Text).name(),
                "fg_dim": pal.color(QPalette.ColorRole.PlaceholderText).name(),
                "accent": pal.color(QPalette.ColorRole.Highlight).name(),
                "border": pal.color(QPalette.ColorRole.Mid).name(),
                "selection": pal.color(QPalette.ColorRole.Highlight).name(),
                "selection_fg": pal.color(QPalette.ColorRole.HighlightedText).name(),
            }
        mode = detect_system_theme()
    if mode == "light":
        return {
            "bg": LT_BG_BASE,
            "bg_mid": LT_BG,
            "bg_dim": LT_BG_MID,
            "fg": LT_FG,
            "fg_dim": LT_FG_DIM,
            "accent": LT_ACCENT,
            "border": LT_BORDER,
            "selection": LT_SELECTION,
        }
    return {
        "bg": BG_DARK,
        "bg_mid": BG_MID,
        "bg_dim": BG_LIGHT,
        "fg": FG,
        "fg_dim": FG_DIM,
        "accent": ACCENT_LIGHT,
        "border": BORDER,
        "selection": SELECTION,
    }


_CSS_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_OVERLAY_FALLBACK = {
    "bg": BG_DARK,
    "bg_mid": BG_MID,
    "fg": FG,
    "border": BORDER,
    "accent": ACCENT_LIGHT,
}


def css_color_literal(value: object, fallback: str) -> str:
    """Return an opaque #rrggbb literal, else ``fallback``.

    Overlay JS interpolates these into CSS custom properties. Reject
    anything that is not a six-digit hex color so a snapshot or palette
    value cannot become executable JS or unescaped CSS.
    """
    if isinstance(value, str) and _CSS_HEX.fullmatch(value.strip()):
        return value.strip().lower()
    if isinstance(fallback, str) and _CSS_HEX.fullmatch(fallback.strip()):
        return fallback.strip().lower()
    return "#000000"


def overlay_palette(mode: str = "auto") -> dict[str, str]:
    """Validated colors for application-owned page overlays."""
    raw = palette_dict(mode)
    fallback = _OVERLAY_FALLBACK
    return {
        "bg": css_color_literal(raw.get("bg"), fallback["bg"]),
        "bg_mid": css_color_literal(raw.get("bg_mid"), fallback["bg_mid"]),
        "fg": css_color_literal(raw.get("fg"), fallback["fg"]),
        "border": css_color_literal(raw.get("border"), fallback["border"]),
        "accent": css_color_literal(raw.get("accent"), fallback["accent"]),
    }


def _apply_dark(app):
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG_MID))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(FG))
    pal.setColor(QPalette.ColorRole.Base, QColor(BG_DARK))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_MID))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_LIGHT))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(FG))
    pal.setColor(QPalette.ColorRole.Text, QColor(FG))
    pal.setColor(QPalette.ColorRole.Button, QColor(BG_MID))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(FG))
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT_LIGHT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(SELECTION))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(FG_DIM))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(FG_DIM))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(FG_DIM))
    app.setPalette(pal)
    app.setStyleSheet(DARK_QSS)


def _apply_light(app):
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(LT_BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(LT_FG))
    pal.setColor(QPalette.ColorRole.Base, QColor(LT_BG_BASE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(LT_BG))
    pal.setColor(QPalette.ColorRole.Text, QColor(LT_FG))
    pal.setColor(QPalette.ColorRole.Button, QColor(LT_BG_MID))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(LT_FG))
    pal.setColor(QPalette.ColorRole.Link, QColor(LT_ACCENT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(LT_SELECTION))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)
    app.setStyleSheet(LIGHT_QSS)


DARK_QSS = f"""
QMainWindow {{ background-color: {BG_MID}; }}
QToolBar {{ background-color: {BG_MID}; border: none; padding: 2px;
            spacing: 2px; }}
QToolButton {{ background: transparent; color: {FG}; border: none;
               padding: 4px 6px; border-radius: 3px; }}
QToolButton:hover {{ background-color: {BG_LIGHT}; }}
QToolButton:pressed {{ background-color: {ACCENT}; }}
QLineEdit {{ background-color: {BG_DARK}; color: {FG};
             border: 1px solid {BORDER}; border-radius: 3px;
             padding: 4px 8px; }}
QLineEdit:focus {{ border: 1px solid {ACCENT_LIGHT}; }}
QTabBar::tab {{ background-color: {BG_DARK}; color: {FG_DIM};
                padding: 6px 14px; border: none;
                border-right: 1px solid {BORDER}; min-width: 100px; }}
QTabBar::tab:selected {{ background-color: {BG_MID}; color: {FG}; }}
QTabBar::tab:hover {{ background-color: {BG_LIGHT}; color: {FG}; }}
QTabBar::close-button {{ subcontrol-position: right; padding: 2px; }}
QTabBar::close-button:hover {{ background-color: {BG_LIGHT};
                               border-radius: 3px; }}
QTabWidget::pane {{ border: none; }}
QSplitter::handle {{ background-color: {BORDER}; }}
QDockWidget {{ color: {FG}; titlebar-close-icon: none; }}
QDockWidget::title {{ background-color: {BG_DARK}; padding: 4px;
                      color: {FG_DIM}; border-bottom: 1px solid {BORDER}; }}
QListWidget, QTreeWidget, QTextEdit, QPlainTextEdit {{
    background-color: {BG_DARK}; color: {FG};
    border: 1px solid {BORDER}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background-color: {SELECTION}; color: #ffffff; }}
QStatusBar {{ background-color: {BG_DARK}; color: {FG_DIM};
              border-top: 1px solid {BORDER}; }}
QMenu {{ background-color: {BG_MID}; color: {FG};
         border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background-color: {SELECTION}; }}
QPushButton {{ background-color: {BG_LIGHT}; color: {FG};
               border: 1px solid {BORDER}; border-radius: 3px;
               padding: 4px 12px; }}
QPushButton:hover {{ background-color: {ACCENT}; }}
QScrollBar:vertical {{ background-color: {BG_DARK}; width: 10px;
                       border: none; }}
QScrollBar::handle:vertical {{ background-color: {BG_LIGHT};
                               border-radius: 4px; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""

_CONTROLLER = None


def attach_presentation(app: QApplication, config):
    global _CONTROLLER
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
    if theme_mode in ("dark", "light", "native"):
        return theme_mode
    return detect_system_theme()


def current_controller():
    return _CONTROLLER


def current_resolved_theme(fallback: str = "dark") -> str:
    """Effective dark/light/native mode for chrome and overlay restyle."""
    ctrl = current_controller()
    if ctrl is None:
        return fallback
    state = ctrl.state
    if state.using_shared_palette and state.snapshot is not None:
        return state.snapshot.mode
    if state.theme_mode in ("dark", "light", "native"):
        return state.theme_mode
    return detect_system_theme()


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
    global _CONTROLLER
    if _CONTROLLER is not None:
        try:
            _CONTROLLER.stop()
        except Exception:  # noqa: BLE001
            pass
    _CONTROLLER = None
    try:
        from qdistro_presentation.qt import reset_controller_for_tests as _reset

        _reset()
    except ImportError:
        pass


LIGHT_QSS = f"""
QMainWindow {{ background-color: {LT_BG}; }}
QToolBar {{ background-color: {LT_BG}; border: none; padding: 2px; }}
QToolButton {{ color: {LT_FG}; padding: 4px 6px; border-radius: 3px;
               background: transparent; }}
QToolButton:hover {{ background-color: {LT_BG_MID}; }}
QLineEdit {{ background-color: {LT_BG_BASE}; color: {LT_FG};
             border: 1px solid {LT_BORDER}; border-radius: 3px;
             padding: 4px 8px; }}
QTabBar::tab {{ background-color: {LT_BG_MID}; color: {LT_FG_DIM};
                padding: 6px 14px; border: none;
                border-right: 1px solid {LT_BORDER}; min-width: 100px; }}
QTabBar::tab:selected {{ background-color: {LT_BG}; color: {LT_FG}; }}
QTabWidget::pane {{ border: none; }}
QSplitter::handle {{ background-color: {LT_BORDER}; }}
QListWidget, QTreeWidget {{ background-color: {LT_BG_BASE};
                            color: {LT_FG};
                            border: 1px solid {LT_BORDER}; }}
"""
