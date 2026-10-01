"""Trusted presentation adapter for the lock surface.

Reads the managed snapshot only (developer path overrides are ignored).
A missing package, missing file, or invalid document keeps the built-in
dark defaults so the locker never depends on qdshell or this library
to acquire a secure surface.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject
from PyQt6.QtQml import QQmlPropertyMap

log = logging.getLogger("qdlocker.presentation")

# Built-in dark defaults matching qml/shim/Color.qml and Style.qml at
# scale/radius 1.0. Keep these even if qdistro_presentation is absent.
_DEFAULT_COLORS: dict[str, str] = {
    "mPrimary": "#fff59b",
    "mOnPrimary": "#0e0e43",
    "mSecondary": "#a9aefe",
    "mOnSecondary": "#0e0e43",
    "mTertiary": "#9BFECE",
    "mOnTertiary": "#0e0e43",
    "mError": "#FD4663",
    "mOnError": "#0e0e43",
    "mSurface": "#070722",
    "mOnSurface": "#f3edf7",
    "mSurfaceVariant": "#11112d",
    "mOnSurfaceVariant": "#7c80b4",
    "mOutline": "#21215F",
    "mShadow": "#070722",
    "mHover": "#9BFECE",
    "mOnHover": "#0e0e43",
}

_FONT_BASES: dict[str, float] = {
    "fontSizeXXS": 8.0,
    "fontSizeXS": 9.0,
    "fontSizeS": 10.0,
    "fontSizeM": 11.0,
    "fontSizeL": 13.0,
    "fontSizeXL": 16.0,
    "fontSizeXXL": 18.0,
    "fontSizeXXXL": 24.0,
}

_RADIUS_BASES: dict[str, int] = {
    "radiusXXXS": 3,
    "radiusXXS": 4,
    "radiusXS": 8,
    "radiusS": 12,
    "radiusM": 16,
    "radiusL": 20,
}

_IRADIUS_BASES: dict[str, int] = {
    "iRadiusXXXS": 3,
    "iRadiusXXS": 4,
    "iRadiusXS": 8,
    "iRadiusS": 12,
    "iRadiusM": 16,
    "iRadiusL": 20,
}

_BORDER_BASES: dict[str, int] = {
    "borderS": 1,
    "borderM": 2,
    "borderL": 3,
}

_MARGIN_BASES: dict[str, int] = {
    "marginXXS": 2,
    "marginXS": 4,
    "marginS": 6,
    "marginM": 9,
    "marginL": 13,
    "marginXL": 18,
}

_ANIMATION_BASES: dict[str, int] = {
    "animationFaster": 75,
    "animationFast": 150,
    "animationNormal": 300,
    "animationSlow": 450,
    "animationSlowest": 750,
}

_STATIC_STYLE: dict[str, int | float] = {
    "fontWeightRegular": 400,
    "fontWeightMedium": 500,
    "fontWeightSemiBold": 600,
    "fontWeightBold": 700,
    "screenRadius": 20,
    "opacityNone": 0.0,
    "opacityLight": 0.25,
    "opacityMedium": 0.5,
    "opacityHeavy": 0.75,
    "opacityAlmost": 0.95,
    "opacityFull": 1.0,
    "shadowOpacity": 0.85,
    "shadowBlur": 1.0,
    "shadowBlurMax": 22,
    "shadowHorizontalOffset": 0.0,
    "shadowVerticalOffset": 0.0,
    "tooltipDelay": 300,
    "tooltipDelayLong": 1200,
    "pillDelay": 500,
    "baseWidgetSize": 33.0,
    "sliderWidth": 200.0,
}


def try_load_trusted_snapshot():
    """Return the managed snapshot or None. Never raises to the locker."""
    try:
        from qdistro_presentation.model import SnapshotError, SnapshotPathError
        from qdistro_presentation.paths import load_snapshot, resolve_snapshot_path
    except ImportError:
        return None
    try:
        resolved = resolve_snapshot_path(role="locker")
        if resolved is None:
            return None
        snapshot, _identity = load_snapshot(resolved)
    except (OSError, SnapshotError, SnapshotPathError, RecursionError, UnicodeDecodeError):
        return None
    return snapshot


class LockerPresentation(QQmlPropertyMap):
    """QML `presentation` context property. Frozen for each lock cycle."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._has_snapshot = False
        self._apply_defaults()

    @property
    def has_snapshot(self) -> bool:
        return self._has_snapshot

    def reload_trusted(self) -> None:
        """One-shot read. Invalid/missing data keeps last-known-good or defaults."""
        snapshot = try_load_trusted_snapshot()
        if snapshot is None:
            if not self._has_snapshot:
                self._apply_defaults()
            return
        self._apply_snapshot(snapshot)

    def freeze_for_lock(self) -> None:
        """Re-read the trusted snapshot at lock entry, then keep it frozen."""
        self.reload_trusted()

    def _apply_defaults(self) -> None:
        for key, value in _DEFAULT_COLORS.items():
            self.insert(key, value)
        for key, value in _FONT_BASES.items():
            self.insert(key, value)
        for key, value in _RADIUS_BASES.items():
            self.insert(key, value)
        for key, value in _IRADIUS_BASES.items():
            self.insert(key, value)
        for key, value in _BORDER_BASES.items():
            self.insert(key, value)
        for key, value in _MARGIN_BASES.items():
            self.insert(key, value)
        for key, value in _ANIMATION_BASES.items():
            self.insert(key, value)
        for key, value in _STATIC_STYLE.items():
            self.insert(key, value)
        self.insert("hasSnapshot", False)
        self._has_snapshot = False

    def _apply_snapshot(self, snapshot) -> None:
        try:
            from qdistro_presentation.model import (
                animation_ms,
                card_radius_px,
                resolve_presentation,
            )
        except ImportError:
            self._apply_defaults()
            return
        try:
            resolved = resolve_presentation(
                theme_mode="system",
                snapshot=snapshot,
                native_ui_family="Sans Serif",
                native_fixed_family="monospace",
                native_icon_theme="",
            )
        except Exception:
            log.debug("locker presentation resolve failed", exc_info=True)
            if not self._has_snapshot:
                self._apply_defaults()
            return
        if not resolved.using_shared_palette or resolved.colors is None:
            self._apply_defaults()
            return
        colors = resolved.colors.as_dict()
        for key in _DEFAULT_COLORS:
            self.insert(key, colors[key])
        factor = resolved.ui_point_size / 11.0
        for key, base in _FONT_BASES.items():
            self.insert(key, float(base) * factor)
        for key, base in _RADIUS_BASES.items():
            self.insert(key, card_radius_px(base, resolved.radius_ratio))
        for key, base in _IRADIUS_BASES.items():
            self.insert(key, int(round(base * resolved.input_radius_ratio)))
        self.insert("screenRadius", card_radius_px(20, resolved.radius_ratio))
        for key, base in _BORDER_BASES.items():
            self.insert(key, max(1, int(round(base * resolved.ui_scale))))
        for key, base in _MARGIN_BASES.items():
            self.insert(key, int(round(base * resolved.ui_scale)))
        for key, base in _ANIMATION_BASES.items():
            self.insert(key, animation_ms(base, resolved))
        for key, value in _STATIC_STYLE.items():
            if key == "screenRadius":
                continue
            self.insert(key, value)
        self.insert("hasSnapshot", True)
        self._has_snapshot = True
