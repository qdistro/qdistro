"""Application appearance preferences stored in QSettings.

Theme mode and optional UI-font overrides live under ``appearance/``.
The legacy ``dark_mode`` boolean is retained for downgrade compatibility
and is never the live authority once ``appearance/version`` exists.

This module is QtCore-only: it must not attach a presentation watcher
or import widgets.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QSettings

APPEARANCE_VERSION = 1
VALID_MODES = ("system", "dark", "light", "native")

THEME_LABEL_TO_KEY = {
    "Follow desktop": "system",
    "Native": "native",
    "Light": "light",
    "Dark": "dark",
}
THEME_KEY_TO_LABEL = {v: k for k, v in THEME_LABEL_TO_KEY.items()}

_ORG = "qnotebook"
_APP = "qnotebook"


def default_settings() -> QSettings:
    return QSettings(_ORG, _APP)


def migrate_appearance(settings: QSettings) -> str:
    """Ensure ``appearance/version`` exists and return the live theme mode.

    Migration runs only when the version key is absent:

    * ``dark_mode`` present and true → ``dark``
    * ``dark_mode`` present and false → ``native``
    * ``dark_mode`` absent → ``system``
    """
    if settings.contains("appearance/version"):
        mode = str(settings.value("appearance/theme_mode", "system") or "system")
        if mode not in VALID_MODES:
            mode = "system"
            settings.setValue("appearance/theme_mode", mode)
            settings.sync()
        return mode

    if settings.contains("dark_mode"):
        mode = "dark" if bool(settings.value("dark_mode", False, type=bool)) else "native"
    else:
        mode = "system"
    settings.setValue("appearance/version", APPEARANCE_VERSION)
    settings.setValue("appearance/theme_mode", mode)
    settings.sync()
    return mode


def load_theme_mode(settings: QSettings | None = None) -> str:
    return migrate_appearance(settings or default_settings())


def save_theme_mode(
    settings: QSettings,
    mode: str,
    *,
    update_legacy: bool,
) -> None:
    if mode not in VALID_MODES:
        mode = "system"
    settings.setValue("appearance/version", APPEARANCE_VERSION)
    settings.setValue("appearance/theme_mode", mode)
    if update_legacy:
        if mode == "dark":
            settings.setValue("dark_mode", True)
        elif mode in ("native", "light"):
            settings.setValue("dark_mode", False)
        # ``system`` must not overwrite the stored boolean with the inherited mode.
    settings.sync()


def load_overrides(settings: QSettings | None = None) -> dict[str, Any]:
    settings = settings or default_settings()
    payload: dict[str, Any] = {"version": APPEARANCE_VERSION}
    family = settings.value("appearance/ui_font_family", None)
    if family:
        payload["ui_font_family"] = str(family)
    if settings.contains("appearance/ui_font_size_pt"):
        raw = settings.value("appearance/ui_font_size_pt")
        try:
            payload["ui_font_size_pt"] = float(raw)
        except (TypeError, ValueError):
            pass
    return payload


def load_use_desktop_document_fonts(settings: QSettings | None = None) -> bool:
    settings = settings or default_settings()
    return bool(settings.value("appearance/use_desktop_document_fonts", False, type=bool))


def save_use_desktop_document_fonts(settings: QSettings, enabled: bool) -> None:
    settings.setValue("appearance/version", APPEARANCE_VERSION)
    settings.setValue("appearance/use_desktop_document_fonts", bool(enabled))
    settings.sync()


def save_overrides(settings: QSettings, appearance: dict[str, Any]) -> None:
    settings.setValue("appearance/version", int(appearance.get("version", APPEARANCE_VERSION)))
    family = appearance.get("ui_font_family")
    if family:
        settings.setValue("appearance/ui_font_family", str(family))
    else:
        settings.remove("appearance/ui_font_family")
    if "ui_font_size_pt" in appearance and appearance["ui_font_size_pt"] is not None:
        settings.setValue("appearance/ui_font_size_pt", float(appearance["ui_font_size_pt"]))
    else:
        settings.remove("appearance/ui_font_size_pt")
    settings.sync()


class SettingsAdapter:
    """Minimal Config-shaped reader for :func:`qnotebook.theme.attach_presentation`."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or default_settings()

    def get(self, *keys: str, default: Any = None) -> Any:
        if keys[:2] == ("general", "theme_mode"):
            return load_theme_mode(self._settings)
        if keys == ("appearance",):
            return load_overrides(self._settings)
        return default
