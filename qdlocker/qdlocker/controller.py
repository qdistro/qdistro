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
        # Submit-intent latch. Set ONLY when the user presses Return to
        # submit (tryUnlock → start_pam). A PAM "Password" prompt auto-
        # submits the typed buffer only while this is set; a prompt that
        # arrives WITHOUT it — e.g. the fprintd fallback starting PAM on its
        # own — instead parks in waitingForPassword and waits for an explicit
        # Return. This stops the locker from firing a half-typed password the
        # instant the fallback prompt lands mid-typing.
        self._submit_requested = False
        self._unlock_in_progress = False
        self._show_failure = False
        self._show_info = False
        self._error_message = ""
        self._info_message = ""
        self._pam_ready = False
        # Stale-outcome race guard. `_unlocked` latches once any auth path
        # succeeds for the current lock; a slower loser (e.g. a laggy PAM
        # FAILED that raced in after fprintd already unlocked) is then
        # dropped instead of flashing a spurious failure or corrupting
        # state. Reset on each fresh lock via notify_lock_begin().
        self._unlocked = False

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
            # Deliberately do NOT abort a running PAM conversation on each
            # keystroke. When fprintd is unavailable, PAM is started by the
            # backend (lock-begin or fallback) and sits waiting on the typed
            # password; aborting it per-keystroke killed that worker, and
            # occupy_fingerprint_sensor(True) below would immediately respawn
            # it — re-emitting the "Password" prompt every keystroke, which
            # (with the old auto-submit) fired the half-typed buffer on a loop
            # (the observed 1,2,3,4,0,… reset). A stale worker from a prior
            # lock is already neutralised by the per-session generation guard,
            # and start_pam() suppresses duplicate workers, so there is nothing
            # left for a per-keystroke abort to usefully cancel.
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
        fprintd-failure counter, then arms the fingerprint sensor so a
        touch-to-unlock works on a FRESH lock with an empty password
        field — matching the spec's "fingerprint = the owner is present"
        path (sessions.md). Previously the sensor was only armed lazily
        from the currentText setter once the user typed, so an idle lock
        left fprintd dormant and a finger-only unlock never fired."""
        self._unlocked = False
        # Drop any stale submit-intent so a Return pressed against a previous
        # lock can't auto-submit into the fresh lock's first PAM prompt.
        self._submit_requested = False
        # reset_session() bumps the generation and clears the per-session
        # fprintd-unavailable latch; arm AFTER it so the worker captures
        # the fresh generation and a transient wedge from a prior lock is
        # retried. occupy_fingerprint_sensor(True) is idempotent for the
        # session (the _fprintd_busy guard makes a later keystroke-driven
        # call a no-op while the worker is in flight).
        self._auth.reset_session()
        self._auth.occupy_fingerprint_sensor(True)

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
        # No prompt is showing yet: the user pressed Return to submit before
        # PAM asked. Latch the intent so the buffer is auto-submitted when the
        # prompt arrives, then kick off the conversation.
        self._submit_requested = True
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
            # Auto-submit the typed buffer ONLY if the user explicitly asked
            # to submit (pressed Return → _submit_requested). A prompt that
            # fires on its own — the fprintd fallback started PAM, or PAM
            # re-prompted — must NOT fire a half-typed buffer; it parks in
            # waitingForPassword and waits for an explicit Return. Consume the
            # intent either way so a later spontaneous re-prompt can't reuse it.
            submit = self._submit_requested
            self._submit_requested = False
            if submit and self._current_text:
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

    def _on_auth_outcome(self, payload: object) -> None:
        # Outcomes from the real backend always arrive tagged with the
        # session generation they belong to: (AuthOutcome, generation).
        # Strictly reject anything else — an untagged or malformed outcome
        # would bypass the anti-replay generation check below, so drop it
        # and stay locked (fail closed).
        # `type(...) is int` deliberately rejects bool: bool subclasses int,
        # so isinstance(True, int) is True and a stray (AuthOutcome.SUCCESS,
        # True) would otherwise validate and unlock whenever the current
        # generation happens to be 1 (True == 1). Demand a real int.
        if (
            not isinstance(payload, tuple)
            or len(payload) != 2
            or not isinstance(payload[0], AuthOutcome)
            or type(payload[1]) is not int
        ):
            log.warning(
                "dropping untagged/malformed auth outcome (type=%s); "
                "expected (AuthOutcome, int)",
                type(payload).__name__,
            )
            return
        outcome, generation = payload

        # Drop a stale outcome from a superseded lock session: a slow PAM
        # or fprintd worker may deliver its result after the next lock
        # already bumped the generation. Acting on it would corrupt the
        # fresh session's state.
        current = self._auth._current_generation()
        if generation != current:
            log.info(
                "dropping stale auth outcome %s (gen=%s, current=%s)",
                getattr(outcome, "name", outcome), generation, current,
            )
            return

        # Drop a losing outcome that raced in after this lock already
        # unlocked (e.g. PAM FAILED arriving just after fprintd SUCCESS in
        # the same generation). Without this, the queued FAILED would flash
        # a spurious "Authentication failed" over an already-unlocked
        # session.
        if self._unlocked:
            log.info(
                "dropping auth outcome %s; session already unlocked",
                getattr(outcome, "name", outcome),
            )
            return

        self._set_unlock_in_progress(False)
        # This attempt is resolved; clear any submit-intent so it can't bleed
        # into the next prompt (e.g. a worker that FAILED before prompting).
        self._submit_requested = False
        if outcome is AuthOutcome.SUCCESS:
            log.info("authentication successful")
            self._unlocked = True
            self.currentText = ""
            self.unlocked.emit()
            return
        log.info("authentication failed: %s", outcome.name)
        # Route through the setter (not a direct `_current_text` write) so the
        # empty-value side effect fires: releasing the fingerprint-sensor
        # occupier via occupy_fingerprint_sensor(False). The worker that
        # produced this FAILED has already exited, so there is no PAM attempt
        # left to cancel here.
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
