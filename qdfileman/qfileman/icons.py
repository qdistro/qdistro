"""Named theme icons and file-icon cache helpers.

Plan 03: live presentation updates re-query retained ``QIcon.fromTheme``
icons and invalidate ``QFileIconProvider`` caches without rebuilding
directory listings.
"""

from __future__ import annotations

from PyQt6.QtCore import QFileInfo, QObject
from PyQt6.QtGui import QIcon, QPixmapCache
from PyQt6.QtWidgets import QFileIconProvider

THEME_ICON_PROPERTY = "qfilemanThemeIcon"


def set_named_icon(target: QObject, name: str) -> None:
    """Remember a freedesktop icon name and apply it from the current theme."""
    target.setProperty(THEME_ICON_PROPERTY, name)
    setter = getattr(target, "setIcon", None)
    if setter is None:
        raise TypeError(f"{type(target).__name__} has no setIcon")
    setter(QIcon.fromTheme(name))


def refresh_named_icons(root: QObject) -> None:
    """Re-query ``QIcon.fromTheme`` on ``root`` and descendants that retained a name."""
    objects: list[QObject] = [root]
    objects.extend(root.findChildren(QObject))
    for obj in objects:
        name = obj.property(THEME_ICON_PROPERTY)
        if not name:
            continue
        setter = getattr(obj, "setIcon", None)
        if callable(setter):
            setter(QIcon.fromTheme(str(name)))


def invalidate_file_icon_cache() -> None:
    """Drop Qt's pixmap cache so themed and file icons can be re-rasterized."""
    QPixmapCache.clear()


def file_icon(provider: QFileIconProvider, path: str) -> QIcon:
    """Icon for ``path`` from ``provider`` (a fresh provider has an empty cache)."""
    return provider.icon(QFileInfo(path))
