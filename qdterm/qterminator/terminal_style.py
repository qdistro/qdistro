"""Resolve terminal content font and ANSI scheme from profile + presentation.

Chrome (window/tab/menu) follows PresentationController. Terminal *content*
is independent: profile ``font_source`` / ``color_source`` plus a per-terminal
zoom delta. UI scale is not terminal scale.
"""

from __future__ import annotations

import configparser
import logging
import os

from PyQt6.QtGui import QFont, QFontDatabase

log = logging.getLogger(__name__)

MIN_TERMINAL_POINT_SIZE = 6.0
NATIVE_FIXED_POINT_SIZE = 11.0

# Fallback when the matching .colorscheme file is not installed.
_SCHEME_PREVIEW = {
    "Linux": ("#000000", "#00ff00"),
    "BlackOnWhite": ("#ffffff", "#000000"),
    "WhiteOnBlack": ("#000000", "#ffffff"),
    "BlackOnLightYellow": ("#ffffdd", "#000000"),
}

_SCHEME_DATA_SUBDIRS = (
    ("qtermwidget6", "color-schemes"),
    ("qtermwidget5", "color-schemes"),
    ("konsole",),
)


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
    parsed = _colors_from_scheme_file(scheme)
    if parsed is not None:
        return parsed
    if scheme in _SCHEME_PREVIEW:
        return _SCHEME_PREVIEW[scheme]
    lower = (scheme or "").lower()
    if "light" in lower or lower.endswith("onwhite") or "yellow" in lower:
        return ("#ffffdd", "#000000")
    return ("#1e1e1e", "#d3d7cf")


def parse_colorscheme_file(path: str) -> tuple[str, str] | None:
    """Read Background/Foreground ``Color=r,g,b`` from a Konsole scheme file."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        loaded = parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    if not loaded:
        return None

    def _hex(section: str) -> str | None:
        if not parser.has_section(section) or not parser.has_option(section, "Color"):
            return None
        parts = [p.strip() for p in parser.get(section, "Color").split(",")]
        if len(parts) < 3:
            return None
        try:
            rgb = [max(0, min(255, int(float(p)))) for p in parts[:3]]
        except ValueError:
            return None
        return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

    background = _hex("Background")
    foreground = _hex("Foreground")
    if background and foreground:
        return (background, foreground)
    return None


def _scheme_search_dirs() -> list[str]:
    dirs: list[str] = []
    try:
        from PyQt6.QtCore import QStandardPaths

        roots = QStandardPaths.standardLocations(
            QStandardPaths.StandardLocation.GenericDataLocation
        )
    except Exception:  # noqa: BLE001
        roots = []
    for root in roots:
        for parts in _SCHEME_DATA_SUBDIRS:
            dirs.append(os.path.join(root, *parts))
    for extra in (
        "/usr/share/qtermwidget6/color-schemes",
        "/usr/share/qtermwidget5/color-schemes",
    ):
        if extra not in dirs:
            dirs.append(extra)
    return dirs


def _colors_from_scheme_file(scheme: str) -> tuple[str, str] | None:
    name = (scheme or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    filename = f"{name}.colorscheme"
    for directory in _scheme_search_dirs():
        path = os.path.join(directory, filename)
        if os.path.isfile(path):
            parsed = parse_colorscheme_file(path)
            if parsed is not None:
                return parsed
    return None
