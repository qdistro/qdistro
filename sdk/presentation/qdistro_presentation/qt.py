"""PyQt6 presentation controller. Import this module explicitly from GUI apps."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from PyQt6.QtCore import QEvent, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

from .model import (
    THEME_MODES,
    Colors,
    LocalOverrides,
    PresentationSnapshot,
    ResolvedPresentation,
    SnapshotError,
    SnapshotPathError,
    blend_hex,
    changed_fields,
    resolve_presentation,
)
from .paths import (
    ResolvedPath,
    load_snapshot,
    nearest_existing_parent,
    resolve_snapshot_path,
)

log = logging.getLogger("qdistro_presentation.qt")

LegacyMode = Literal["dark", "light"]
ApplyLegacy = Callable[[QApplication, LegacyMode], None]
ApplyFallback = Callable[[QApplication], None]


def _qcolor(hex_color: str) -> QColor:
    return QColor(hex_color)


def derive_bevels(button: str, surface: str) -> dict[str, str]:
    """Light/Midlight/Mid/Dark from the mapped button/surface colors."""
    return {
        "light": blend_hex("#ffffff", button, 0.35),
        "midlight": blend_hex("#ffffff", button, 0.18),
        "mid": blend_hex(surface, button, 0.35),
        "dark": blend_hex("#000000", button, 0.35),
    }


def snapshot_palette(colors: Colors) -> QPalette:
    pal = QPalette()
    mapping = {
        QPalette.ColorRole.Window: colors.mSurface,
        QPalette.ColorRole.Base: colors.mSurface,
        QPalette.ColorRole.AlternateBase: colors.mSurfaceVariant,
        QPalette.ColorRole.Button: colors.mSurfaceVariant,
        QPalette.ColorRole.ToolTipBase: colors.mSurfaceVariant,
        QPalette.ColorRole.WindowText: colors.mOnSurface,
        QPalette.ColorRole.Text: colors.mOnSurface,
        QPalette.ColorRole.ButtonText: colors.mOnSurface,
        QPalette.ColorRole.ToolTipText: colors.mOnSurface,
        QPalette.ColorRole.PlaceholderText: colors.mOnSurfaceVariant,
        QPalette.ColorRole.Highlight: colors.mPrimary,
        QPalette.ColorRole.HighlightedText: colors.mOnPrimary,
        QPalette.ColorRole.Link: colors.mPrimary,
        QPalette.ColorRole.LinkVisited: colors.mSecondary,
        QPalette.ColorRole.BrightText: colors.mError,
        QPalette.ColorRole.Shadow: colors.mShadow,
    }
    bevels = derive_bevels(colors.mSurfaceVariant, colors.mSurface)
    mapping[QPalette.ColorRole.Light] = bevels["light"]
    mapping[QPalette.ColorRole.Midlight] = bevels["midlight"]
    mapping[QPalette.ColorRole.Mid] = bevels["mid"]
    mapping[QPalette.ColorRole.Dark] = bevels["dark"]
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        for role, hex_color in mapping.items():
            pal.setColor(group, role, _qcolor(hex_color))
        pal.setColor(group, QPalette.ColorRole.NoRole, _qcolor(colors.mOutline))

    disabled_pairs = (
        (QPalette.ColorRole.WindowText, colors.mOnSurface, colors.mSurface),
        (QPalette.ColorRole.Text, colors.mOnSurface, colors.mSurface),
        (QPalette.ColorRole.ButtonText, colors.mOnSurface, colors.mSurfaceVariant),
        (QPalette.ColorRole.PlaceholderText, colors.mOnSurfaceVariant, colors.mSurfaceVariant),
        (QPalette.ColorRole.HighlightedText, colors.mOnPrimary, colors.mPrimary),
    )
    for role, fg, bg in disabled_pairs:
        pal.setColor(QPalette.ColorGroup.Disabled, role, _qcolor(blend_hex(fg, bg, 0.6)))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Window, _qcolor(colors.mSurface))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, _qcolor(colors.mSurface))
    pal.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Button,
        _qcolor(colors.mSurfaceVariant),
    )
    pal.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, _qcolor(colors.mPrimary)
    )
    pal.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.HighlightedText,
        _qcolor(colors.mOnPrimary),
    )
    return pal


def snapshot_qss(colors: Colors, *, input_radius_px: int) -> str:
    radius = max(0, input_radius_px)
    disabled_fg = blend_hex(colors.mOnSurface, colors.mSurface, 0.6)
    return f"""
QToolTip {{
    background-color: {colors.mSurfaceVariant};
    color: {colors.mOnSurface};
    border: 1px solid {colors.mOutline};
}}
QMenu {{
    background-color: {colors.mSurfaceVariant};
    color: {colors.mOnSurface};
    border: 1px solid {colors.mOutline};
}}
QMenu::item:selected {{
    background-color: {colors.mPrimary};
    color: {colors.mOnPrimary};
}}
QMenu::item:disabled {{
    color: {disabled_fg};
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit {{
    background-color: {colors.mSurface};
    color: {colors.mOnSurface};
    border: 1px solid {colors.mOutline};
    border-radius: {radius}px;
    selection-background-color: {colors.mPrimary};
    selection-color: {colors.mOnPrimary};
}}
QComboBox QAbstractItemView {{
    background-color: {colors.mSurfaceVariant};
    color: {colors.mOnSurface};
    selection-background-color: {colors.mPrimary};
    selection-color: {colors.mOnPrimary};
}}
QPushButton {{
    background-color: {colors.mSurfaceVariant};
    color: {colors.mOnSurface};
    border: 1px solid {colors.mOutline};
    border-radius: {radius}px;
}}
QPushButton:hover {{
    background-color: {colors.mHover};
    color: {colors.mOnHover};
}}
QPushButton:disabled {{
    color: {disabled_fg};
}}
QHeaderView::section {{
    background-color: {colors.mSurfaceVariant};
    color: {colors.mOnSurface};
    border: 1px solid {colors.mOutline};
}}
"""


def pick_family(requested: str, *, fallback: str, fixed: bool) -> str:
    families = set(QFontDatabase.families())
    if requested in families:
        return requested
    if fixed:
        return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    return fallback


class _TooltipFilter(QObject):
    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # noqa: ARG002
        if event is not None and event.type() == QEvent.Type.ToolTip:
            return True
        return False


class PresentationController(QObject):
    """One controller per QApplication. GUI-thread apply and watch."""

    changed = pyqtSignal(object, object, object)

    def __init__(
        self,
        app: QApplication,
        *,
        theme_mode: str = "system",
        local: LocalOverrides | None = None,
        role: Literal["ordinary", "polkit", "locker"] = "ordinary",
        watch: bool = True,
        snapshot_path: ResolvedPath | None = None,
        apply_legacy: ApplyLegacy | None = None,
        apply_system_fallback: ApplyFallback | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if theme_mode not in THEME_MODES:
            raise SnapshotError(f"unknown theme_mode {theme_mode!r}")
        self._app = app
        self._theme_mode = theme_mode
        self._local = local or LocalOverrides()
        self._role = role
        self._watch_enabled = watch and role == "ordinary"
        self._forced_path = snapshot_path
        self._apply_legacy = apply_legacy
        self._apply_system_fallback = apply_system_fallback
        self._native_style = app.style().objectName()
        self._native_palette = QPalette(app.palette())
        self._native_font = QFont(app.font())
        self._native_icon_theme = QIcon.themeName()
        self._native_stylesheet = app.styleSheet()
        self._state: ResolvedPresentation | None = None
        self._snapshot: PresentationSnapshot | None = None
        self._identity: tuple[int, int, int, int] | None = None
        self._applied_generation: str | None = None
        self._applied_signature: tuple | None = None
        self._tooltip_filter = _TooltipFilter(self)
        self._tooltip_installed = False
        self._watcher = None
        self._coalesce = QTimer(self)
        self._coalesce.setSingleShot(True)
        self._coalesce.setInterval(100)
        self._coalesce.timeout.connect(self._reload)
        self._retry = QTimer(self)
        self._retry.setInterval(2000)
        self._retry.timeout.connect(self._retry_absent)
        self._watched_dir = ""
        self._reload(initial=True)

    @property
    def state(self) -> ResolvedPresentation:
        assert self._state is not None
        return self._state

    @property
    def theme_mode(self) -> str:
        return self._theme_mode

    @property
    def local(self) -> LocalOverrides:
        return self._local

    def set_theme_mode(self, theme_mode: str) -> None:
        if theme_mode not in THEME_MODES:
            raise SnapshotError(f"unknown theme_mode {theme_mode!r}")
        if theme_mode == self._theme_mode:
            return
        self._theme_mode = theme_mode
        self._apply_resolved(self._snapshot, emit=True)

    def set_local(self, local: LocalOverrides) -> None:
        self._local = local
        self._apply_resolved(self._snapshot, emit=True)

    def stop(self) -> None:
        self._coalesce.stop()
        self._retry.stop()
        if self._watcher is not None:
            self._watcher.deleteLater()
            self._watcher = None
        if self._tooltip_installed:
            self._app.removeEventFilter(self._tooltip_filter)
            self._tooltip_installed = False

    def _resolved_path(self) -> ResolvedPath | None:
        if self._forced_path is not None:
            return self._forced_path
        return resolve_snapshot_path(role=self._role)

    def _load_current(self) -> PresentationSnapshot | None:
        resolved = self._resolved_path()
        if resolved is None:
            self._identity = None
            return None
        try:
            snapshot, identity = load_snapshot(resolved)
        except (OSError, SnapshotError, SnapshotPathError) as exc:
            log.debug("presentation snapshot unread: %s", exc)
            return self._snapshot
        if self._identity == identity and self._snapshot is not None:
            return self._snapshot
        self._identity = identity
        return snapshot

    def _reload(self, initial: bool = False) -> None:
        loaded = self._load_current()
        if loaded is None and not initial and self._snapshot is not None:
            # Missing/bad file: retain last-known-good.
            self._arm_watch()
            return
        if loaded is self._snapshot and not initial:
            self._arm_watch()
            return
        previous_good = self._snapshot
        try:
            self._apply_resolved(loaded, emit=not initial)
        except SnapshotError as exc:
            log.debug("presentation apply rejected: %s", exc)
            self._snapshot = previous_good
        self._arm_watch()

    def _retry_absent(self) -> None:
        resolved = self._resolved_path()
        if resolved is None:
            return
        import os

        if os.path.lexists(resolved.path):
            self._retry.stop()
            self._reload()

    def _arm_watch(self) -> None:
        if not self._watch_enabled:
            return
        from PyQt6.QtCore import QFileSystemWatcher

        resolved = self._resolved_path()
        if resolved is None:
            return
        path = resolved.path
        import os

        watch_dir = os.path.dirname(path)
        if not os.path.isdir(watch_dir):
            watch_dir = nearest_existing_parent(path)
            if not self._retry.isActive():
                self._retry.start()
        else:
            self._retry.stop()
        if self._watcher is None:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.directoryChanged.connect(self._on_fs_event)
            self._watcher.fileChanged.connect(self._on_fs_event)
        current_dirs = set(self._watcher.directories())
        current_files = set(self._watcher.files())
        wanted_dir = watch_dir
        if wanted_dir not in current_dirs:
            for extra in current_dirs:
                self._watcher.removePath(extra)
            if wanted_dir:
                self._watcher.addPath(wanted_dir)
        if os.path.isfile(path):
            if path not in current_files:
                for extra in current_files:
                    self._watcher.removePath(extra)
                self._watcher.addPath(path)
        else:
            for extra in current_files:
                self._watcher.removePath(extra)
        self._watched_dir = watch_dir
        # Recheck after the read/watch setup race.
        if os.path.isfile(path):
            try:
                snapshot, identity = load_snapshot(resolved)
            except (OSError, SnapshotError, SnapshotPathError):
                return
            if identity != self._identity:
                self._identity = identity
                self._apply_resolved(snapshot, emit=True)

    def _on_fs_event(self, _path: str) -> None:
        self._coalesce.start()

    def _apply_resolved(self, snapshot: PresentationSnapshot | None, *, emit: bool) -> None:
        native_ui = self._native_font.family()
        native_fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        resolved = resolve_presentation(
            theme_mode=self._theme_mode,
            snapshot=snapshot,
            local=self._local,
            native_ui_family=native_ui,
            native_fixed_family=native_fixed,
            native_icon_theme=self._native_icon_theme,
            native_ui_point_size=float(self._native_font.pointSizeF() or 11.0),
        )
        signature = (
            resolved.generation,
            resolved.theme_mode,
            resolved.using_shared_palette,
            resolved.field_map(),
        )
        if signature == self._applied_signature:
            self._snapshot = snapshot
            self._state = resolved
            return
        old = self._state
        self._snapshot = snapshot
        self._state = resolved
        self._apply_to_app(resolved)
        self._applied_signature = signature
        self._applied_generation = resolved.generation
        if emit:
            fields = changed_fields(old, resolved)
            if fields:
                self.changed.emit(old, resolved, fields)
                self._notify_widgets()

    def _apply_to_app(self, resolved: ResolvedPresentation) -> None:
        app = self._app
        if resolved.theme_mode == "native":
            app.setStyle(self._native_style)
            app.setPalette(QPalette(self._native_palette))
            app.setStyleSheet(self._native_stylesheet)
        elif resolved.using_shared_palette and resolved.colors is not None:
            app.setStyle("Fusion")
            app.setPalette(snapshot_palette(resolved.colors))
            app.setStyleSheet(
                snapshot_qss(resolved.colors, input_radius_px=resolved.input_radius_px)
            )
        elif resolved.theme_mode in ("dark", "light") and self._apply_legacy is not None:
            self._apply_legacy(app, resolved.theme_mode)  # type: ignore[arg-type]
        elif resolved.theme_mode == "system" and self._apply_system_fallback is not None:
            self._apply_system_fallback(app)
        elif resolved.theme_mode == "system":
            app.setStyle(self._native_style)
            app.setPalette(QPalette(self._native_palette))
            app.setStyleSheet(self._native_stylesheet)

        ui_family = pick_family(
            resolved.ui_family, fallback=self._native_font.family(), fixed=False
        )
        font = QFont(app.font())
        font.setFamily(ui_family)
        font.setPointSizeF(resolved.ui_point_size)
        font.setWeight(QFont.Weight.Normal)
        app.setFont(font)

        if resolved.icon_theme:
            QIcon.setThemeName(resolved.icon_theme)
        else:
            QIcon.setThemeName(self._native_icon_theme)

        if not resolved.tooltips_enabled and not self._tooltip_installed:
            app.installEventFilter(self._tooltip_filter)
            self._tooltip_installed = True
        elif resolved.tooltips_enabled and self._tooltip_installed:
            app.removeEventFilter(self._tooltip_filter)
            self._tooltip_installed = False

    def _notify_widgets(self) -> None:
        for widget in self._app.topLevelWidgets():
            self._polish(widget)

    def _polish(self, widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()
        for child in widget.findChildren(QWidget):
            child_style = child.style()
            child_style.unpolish(child)
            child_style.polish(child)
            child.update()


_CONTROLLER: PresentationController | None = None


def attach_controller(
    app: QApplication,
    **kwargs: object,
) -> PresentationController:
    """Attach the single PresentationController for this QApplication."""
    global _CONTROLLER
    if _CONTROLLER is not None:
        return _CONTROLLER
    _CONTROLLER = PresentationController(app, **kwargs)  # type: ignore[arg-type]
    return _CONTROLLER


def current_controller() -> PresentationController | None:
    return _CONTROLLER


def reset_controller_for_tests() -> None:
    global _CONTROLLER
    if _CONTROLLER is not None:
        _CONTROLLER.stop()
    _CONTROLLER = None
