"""Resolve terminal content font and ANSI scheme from profile + presentation.

Chrome (window/tab/menu) follows PresentationController. Terminal *content*
is independent: profile ``font_source`` / ``color_source`` plus a per-terminal
zoom delta. UI scale is not terminal scale.
"""

from __future__ import annotations

import logging

from PyQt6.QtGui import QFont, QFontDatabase

log = logging.getLogger(__name__)

MIN_TERMINAL_POINT_SIZE = 6.0
NATIVE_FIXED_POINT_SIZE = 11.0

# Preview only: QTermWidget scheme files are not a public color API.
_SCHEME_PREVIEW = {
    "Linux": ("#000000", "#00ff00"),
    "BlackOnWhite": ("#ffffff", "#000000"),
    "WhiteOnBlack": ("#000000", "#ffffff"),
    "BlackOnLightYellow": ("#ffffdd", "#000000"),
}


def native_fixed_font() -> QFont:
    font = QFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    if not font.family():
        font.setFamily("monospace")
    font.setPointSizeF(NATIVE_FIXED_POINT_SIZE)
    return font


def resolve_terminal_font(profile: dict) -> QFont:
    """Base content font for a profile, ignoring the transient zoom delta."""
    ligatures = bool(profile.get("font_ligatures", False))
    source = profile.get("font_source") or "local"
    if source == "desktop":
        font = _desktop_fixed_font()
    else:
        family = profile.get("font_family") or native_fixed_font().family()
        size = float(profile.get("font_size") or NATIVE_FIXED_POINT_SIZE)
        font = QFont(family)
        font.setPointSizeF(max(MIN_TERMINAL_POINT_SIZE, size))
    if ligatures:
        font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
    return font


def _desktop_fixed_font() -> QFont:
    native = native_fixed_font()
    try:
        from qterminator.theme import current_controller

        ctrl = current_controller()
        state = None if ctrl is None else ctrl.state
    except Exception:  # noqa: BLE001
        state = None
    if state is None or not state.desktop_available:
        return native
    family = state.fixed_family or native.family()
    size = float(state.content_fixed_point_size or NATIVE_FIXED_POINT_SIZE)
    font = QFont(family)
    font.setPointSizeF(max(MIN_TERMINAL_POINT_SIZE, size))
    return font


def effective_appearance_mode() -> str:
    """``dark`` or ``light`` for appearance-mode ANSI selection."""
    from qterminator.theme import current_controller, detect_system_theme

    ctrl = current_controller()
    if ctrl is None:
        return detect_system_theme()
    try:
        state = ctrl.state
    except Exception:  # noqa: BLE001
        return detect_system_theme()
    if state.using_shared_palette and state.snapshot is not None:
        mode = state.snapshot.mode
        if mode in ("dark", "light"):
            return mode
    if ctrl.theme_mode in ("dark", "light"):
        return ctrl.theme_mode
    return detect_system_theme()


def resolve_color_scheme(profile: dict, config) -> str:
    """Profile scheme, or the general dark/light scheme when opted in."""
    fallback = profile.get("color_scheme") or "Linux"
    if (profile.get("color_source") or "profile") != "appearance-mode":
        return fallback
    mode = effective_appearance_mode()
    key = "light_color_scheme" if mode == "light" else "dark_color_scheme"
    scheme = config.get("general", key, default="") or ""
    if not scheme:
        log.warning("appearance-mode %s is empty; keeping profile scheme", key)
        return fallback
    return scheme


def preview_scheme_colors(scheme: str) -> tuple[str, str]:
    """Return (background, foreground) CSS colors for the font preview."""
    if scheme in _SCHEME_PREVIEW:
        return _SCHEME_PREVIEW[scheme]
    lower = (scheme or "").lower()
    if "light" in lower or lower.endswith("onwhite") or "yellow" in lower:
        return ("#ffffdd", "#000000")
    return ("#1e1e1e", "#d3d7cf")
