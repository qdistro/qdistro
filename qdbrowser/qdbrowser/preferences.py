"""Preferences dialog.

qdbrowser has no generic settings dump; the command palette is the
entrypoint. This dialog holds the Appearance section required by the
shared presentation contract and writes through the existing Config
TOML file.
"""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from qdbrowser.config import Config

log = logging.getLogger("qdbrowser.preferences")

THEME_LABEL_TO_KEY = {
    "Follow desktop": "system",
    "Native": "native",
    "Light": "light",
    "Dark": "dark",
}
THEME_KEY_TO_LABEL = {v: k for k, v in THEME_LABEL_TO_KEY.items()}


class PreferencesDialog(QDialog):
    """Settings dialog backed by :class:`qdbrowser.config.Config`."""

    def __init__(self, config: Config | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.resize(420, 280)
        self.config = config or Config()
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        appearance = QGroupBox("Appearance", self)
        form = QFormLayout(appearance)

        self.combo_theme = QComboBox()
        self.combo_theme.addItems(list(THEME_LABEL_TO_KEY))
        form.addRow("Application appearance:", self.combo_theme)

        self.cb_desktop_fonts = QCheckBox("Use desktop fonts")
        form.addRow(self.cb_desktop_fonts)

        self.lbl_desktop_status = QLabel("")
        self.lbl_desktop_status.setObjectName("lbl_desktop_status")
        self.lbl_desktop_status.setWordWrap(True)
        form.addRow(self.lbl_desktop_status)

        self.combo_ui_font = QFontComboBox()
        form.addRow("UI font:", self.combo_ui_font)
        self._ui_font_family_dirty = False
        self.combo_ui_font.currentTextChanged.connect(self._mark_ui_font_family_dirty)

        self.spin_ui_font_size = QSpinBox()
        self.spin_ui_font_size.setRange(6, 48)
        form.addRow("UI font size:", self.spin_ui_font_size)
        self._ui_font_size_dirty = False
        self.spin_ui_font_size.valueChanged.connect(self._mark_ui_font_size_dirty)

        self.cb_desktop_fonts.toggled.connect(self._on_desktop_fonts_toggled)
        self.combo_theme.currentTextChanged.connect(
            lambda *_args: self._refresh_desktop_status()
        )

        layout.addWidget(appearance)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply,
            parent=self,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        layout.addWidget(btns)

    def _effective_ui_font(self) -> tuple[str, int]:
        try:
            from qdbrowser.theme import current_controller

            ctrl = current_controller()
            if ctrl is not None:
                family = str(ctrl.state.ui_family or "")
                size = int(ctrl.state.ui_point_size or 11)
                if family:
                    return family, size
        except Exception:  # noqa: BLE001
            pass
        app = QApplication.instance()
        if app is not None:
            font = app.font()
            return font.family(), int(font.pointSize() or 11)
        return "Sans Serif", 11

    def _load_values(self) -> None:
        theme = self.config.get("general", "theme_mode", default="system")
        self.combo_theme.setCurrentText(
            THEME_KEY_TO_LABEL.get(theme, "Follow desktop")
        )
        appearance = self.config.get("appearance", default={}) or {}
        has_font_override = bool(
            appearance.get("ui_font_family") or appearance.get("ui_font_size_pt")
        )
        self.cb_desktop_fonts.setChecked(not has_font_override)
        effective_family, effective_size = self._effective_ui_font()
        self.combo_ui_font.blockSignals(True)
        if appearance.get("ui_font_family"):
            self.combo_ui_font.setCurrentText(str(appearance["ui_font_family"]))
        else:
            self.combo_ui_font.setCurrentText(effective_family)
        self.combo_ui_font.blockSignals(False)
        self._ui_font_family_dirty = False
        self.spin_ui_font_size.blockSignals(True)
        if appearance.get("ui_font_size_pt"):
            self.spin_ui_font_size.setValue(int(appearance["ui_font_size_pt"]))
        else:
            self.spin_ui_font_size.setValue(effective_size)
        self.spin_ui_font_size.blockSignals(False)
        self._ui_font_size_dirty = False
        self._on_desktop_fonts_toggled(self.cb_desktop_fonts.isChecked())
        self._refresh_desktop_status()

    def _apply(self) -> None:
        theme_mode = THEME_LABEL_TO_KEY[self.combo_theme.currentText()]
        self.config.set("general", "theme_mode", theme_mode)
        appearance = dict(self.config.get("appearance", default={}) or {})
        appearance["version"] = 1
        if self.cb_desktop_fonts.isChecked():
            appearance.pop("ui_font_family", None)
            appearance.pop("ui_font_size_pt", None)
        else:
            existing = self.config.get("appearance", default={}) or {}
            if self._ui_font_family_dirty:
                appearance["ui_font_family"] = self.combo_ui_font.currentText()
            elif existing.get("ui_font_family"):
                appearance["ui_font_family"] = str(existing["ui_font_family"])
            else:
                appearance.pop("ui_font_family", None)
            if self._ui_font_size_dirty:
                appearance["ui_font_size_pt"] = float(self.spin_ui_font_size.value())
            elif existing.get("ui_font_size_pt") is not None:
                appearance["ui_font_size_pt"] = float(existing["ui_font_size_pt"])
            else:
                appearance.pop("ui_font_size_pt", None)
        self.config.set("appearance", appearance)
        self.config.save()
        self._apply_live(theme_mode, appearance)

    def _apply_live(self, theme_mode: str, appearance: dict) -> None:
        try:
            from qdistro_presentation.model import parse_local_overrides

            from qdbrowser.theme import apply_theme, current_controller, refresh_windows

            ctrl = current_controller()
            if ctrl is not None:
                ctrl.set_theme_mode(theme_mode)
                ctrl.set_local(parse_local_overrides(appearance))
                return
            app = QApplication.instance()
            if app is not None:
                apply_theme(app, theme_mode)
                refresh_windows(app)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not apply appearance: %s", exc)
            try:
                from qdbrowser.theme import apply_theme, refresh_windows

                app = QApplication.instance()
                if app is not None:
                    apply_theme(app, theme_mode)
                    refresh_windows(app)
            except Exception as inner:  # noqa: BLE001
                log.warning("legacy appearance apply failed: %s", inner)

    def _mark_ui_font_family_dirty(self, _value: str) -> None:
        self._ui_font_family_dirty = True

    def _mark_ui_font_size_dirty(self, _value: int) -> None:
        self._ui_font_size_dirty = True

    def _on_desktop_fonts_toggled(self, checked: bool) -> None:
        self.combo_ui_font.setEnabled(not checked)
        self.spin_ui_font_size.setEnabled(not checked)
        self._refresh_desktop_status()

    def _refresh_desktop_status(self) -> None:
        follow = self.combo_theme.currentText() == "Follow desktop"
        use_fonts = self.cb_desktop_fonts.isChecked()
        try:
            from qdistro_presentation.model import desktop_status_text

            from qdbrowser.theme import current_controller

            ctrl = current_controller()
            state = ctrl.state if ctrl is not None else None
            text = desktop_status_text(
                state, follow_desktop=follow, use_desktop_fonts=use_fonts
            )
        except Exception:  # noqa: BLE001
            text = (
                "desktop settings unavailable" if follow or use_fonts else ""
            )
        self.lbl_desktop_status.setText(text)
        self.lbl_desktop_status.setVisible(bool(text))
        if use_fonts:
            family, size = self._effective_ui_font()
            self.combo_ui_font.blockSignals(True)
            if family:
                self.combo_ui_font.setCurrentText(family)
            self.combo_ui_font.blockSignals(False)
            self._ui_font_family_dirty = False
            self.spin_ui_font_size.blockSignals(True)
            self.spin_ui_font_size.setValue(size)
            self.spin_ui_font_size.blockSignals(False)
            self._ui_font_size_dirty = False

    def apply_presentation_update(self) -> None:
        self._refresh_desktop_status()
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def accept(self) -> None:
        self._apply()
        super().accept()
