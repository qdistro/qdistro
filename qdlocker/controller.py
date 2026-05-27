"""LockController — Python mirror of qdshell/Modules/LockScreen/LockContext.qml.

Exposes the same properties/signals/slots as the QML LockContext so the
qdlocker QML can drop in qdshell's NText/NIcon widgets and styling
without reimplementing the controller flow.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, Qt, pyqtProperty, pyqtSignal, pyqtSlot

from .auth import AuthBackend, AuthOutcome
from .keysyms import XKB_BackSpace, XKB_Escape, XKB_Return, XKB_Tab

log = logging.getLogger("qdlocker.controller")


class LockController(QObject):
    """State + auth coordinator. QML-facing surface intentionally matches
    qdshell's LockContext.qml so styling/widgets port 1:1."""

    unlocked = pyqtSignal()
    failed = pyqtSignal()
    _currentTextChanged = pyqtSignal()
    _waitingForPasswordChanged = pyqtSignal()
    _unlockInProgressChanged = pyqtSignal()
    _showFailureChanged = pyqtSignal()
    _showInfoChanged = pyqtSignal()
    _errorMessageChanged = pyqtSignal()
    _infoMessageChanged = pyqtSignal()
    _pamReadyChanged = pyqtSignal()

    def __init__(self, auth: AuthBackend, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._auth = auth
        self._current_text = ""
        self._waiting_for_password = False
        self._unlock_in_progress = False
        self._show_failure = False
        self._show_info = False
        self._error_message = ""
        self._info_message = ""
        self._pam_ready = False

        # Auth signals fire from a worker thread; explicit
        # QueuedConnection routes them through the main thread's
        # event loop so the controller's mutators don't race with
        # the QML render thread.
        self._auth.outcome.connect(self._on_auth_outcome, Qt.ConnectionType.QueuedConnection)
        self._auth.message.connect(self._on_auth_message, Qt.ConnectionType.QueuedConnection)
        self._auth.ready.connect(self._on_auth_ready, Qt.ConnectionType.QueuedConnection)
        self._auth.probe_pam()

    @pyqtProperty(str, notify=_currentTextChanged)
    def currentText(self) -> str:
        return self._current_text

    @currentText.setter  # type: ignore[no-redef]
    def currentText(self, value: str) -> None:
        if value == self._current_text:
            return
        self._current_text = value
        self._currentTextChanged.emit()
        if value:
            self._set_show_info(False)
            self._set_show_failure(False)
            if not self._waiting_for_password:
                self._auth.abort_pam()
            self._auth.occupy_fingerprint_sensor(True)
        else:
            self._auth.occupy_fingerprint_sensor(False)

    @pyqtProperty(bool, notify=_waitingForPasswordChanged)
    def waitingForPassword(self) -> bool:
        return self._waiting_for_password

    @pyqtProperty(bool, notify=_unlockInProgressChanged)
    def unlockInProgress(self) -> bool:
        return self._unlock_in_progress

    @pyqtProperty(bool, notify=_showFailureChanged)
    def showFailure(self) -> bool:
        return self._show_failure

    @pyqtProperty(bool, notify=_showInfoChanged)
    def showInfo(self) -> bool:
        return self._show_info

    @pyqtProperty(str, notify=_errorMessageChanged)
    def errorMessage(self) -> str:
        return self._error_message

    @pyqtProperty(str, notify=_infoMessageChanged)
    def infoMessage(self) -> str:
        return self._info_message

    @pyqtProperty(bool, notify=_pamReadyChanged)
    def pamReady(self) -> bool:
        return self._pam_ready

    def notify_lock_begin(self) -> None:
        """Called by WaylandBridge on every fresh lock_requested. Resets
        per-session auth state so the next lock cycle has a clean
        fprintd-failure counter."""
        self._auth.reset_session()

    @pyqtSlot()
    def tryUnlock(self) -> None:
        if not self._pam_ready:
            log.warning("PAM not ready yet, ignoring unlock attempt")
            return
        if self._waiting_for_password:
            self._auth.respond_pam(self._current_text)
            self._set_unlock_in_progress(True)
            self._set_waiting_for_password(False)
            self._set_show_info(False)
            return
        log.info("starting PAM authentication")
        self._auth.start_pam()

    def _on_auth_ready(self) -> None:
        if self._pam_ready:
            return
        self._pam_ready = True
        self._pamReadyChanged.emit()

    def _on_auth_message(self, text: str, is_error: bool, response_required: bool) -> None:
        log.info("PAM message: %r err=%s resp=%s", text, is_error, response_required)
        if response_required:
            if self._current_text:
                self._auth.respond_pam(self._current_text)
                self._set_unlock_in_progress(True)
            else:
                self._set_waiting_for_password(True)
                self._info_message = "Password"
                self._infoMessageChanged.emit()
                self._set_show_info(True)
        elif is_error:
            self._error_message = text
            self._errorMessageChanged.emit()
            self._set_show_failure(True)
        else:
            self._info_message = text
            self._infoMessageChanged.emit()
            self._set_show_info(True)

    def _on_auth_outcome(self, outcome: AuthOutcome) -> None:
        self._set_unlock_in_progress(False)
        if outcome is AuthOutcome.SUCCESS:
            log.info("authentication successful")
            self.currentText = ""
            self.unlocked.emit()
            return
        log.info("authentication failed: %s", outcome.name)
        # Route through the setter so the side effects fire:
        # aborting any still-running PAM attempt and releasing the
        # fingerprint sensor occupier. Setting `_current_text`
        # directly skips that cleanup and leaks worker threads.
        self.currentText = ""
        self._error_message = "Authentication failed"
        self._errorMessageChanged.emit()
        self._set_show_failure(True)
        self.failed.emit()

    def _set_show_info(self, value: bool) -> None:
        if value == self._show_info:
            return
        self._show_info = value
        self._showInfoChanged.emit()
        if value and self._show_failure:
            self._show_failure = False
            self._showFailureChanged.emit()

    def _set_show_failure(self, value: bool) -> None:
        if value == self._show_failure:
            return
        self._show_failure = value
        self._showFailureChanged.emit()
        if value and self._show_info:
            self._show_info = False
            self._showInfoChanged.emit()

    def _set_unlock_in_progress(self, value: bool) -> None:
        if value == self._unlock_in_progress:
            return
        self._unlock_in_progress = value
        self._unlockInProgressChanged.emit()

    def _set_waiting_for_password(self, value: bool) -> None:
        if value == self._waiting_for_password:
            return
        self._waiting_for_password = value
        self._waitingForPasswordChanged.emit()

    def handle_overlay_key(self, sym: int, utf8: str) -> None:
        """Called by the Wayland glue when qdwin forwards a key via
        `qdwin_locker_v1.overlay_key`. Keys arrive here because the
        compositor installs a keyboard grab while locked; the lock
        surface itself never receives wl_keyboard events.

        Hoisted out of a per-call lazy import; the keysyms module is
        tiny and avoiding the import-machinery hit on every keystroke
        is a free win."""
        if sym == XKB_Return:
            self.tryUnlock()
            return
        if sym == XKB_BackSpace:
            if self._current_text:
                self.currentText = self._current_text[:-1]
            return
        if sym == XKB_Escape:
            self.currentText = ""
            return
        if sym == XKB_Tab:
            return
        if utf8:
            self.currentText = self._current_text + utf8
