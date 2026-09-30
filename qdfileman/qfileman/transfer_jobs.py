"""One bounded receipt-aware transfer per window, with Qt-only presentation."""
from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

_WORKERS = threading.BoundedSemaphore(2)
_DISCOVERY_WORKERS = threading.BoundedSemaphore(2)


class TransferSender(QObject):
    changed = pyqtSignal(str)
    result = pyqtSignal(int, object, bool)
    POLL_SECONDS = 60.0
    POLL_INTERVAL = 1.0

    def __init__(self, parent, integration):
        super().__init__(parent)
        self.integration = integration
        self._generation = 0
        self._busy = False
        self._closed = False
        self._cancel = threading.Event()
        self.result.connect(self._present, Qt.ConnectionType.QueuedConnection)

    def start(self, uid, service, expected_instance, payload):
        if self._busy or self._closed or not _WORKERS.acquire(blocking=False):
            return False
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._cancel = threading.Event()
        self.changed.emit("Sending text; awaiting receiver disposition.")
        threading.Thread(target=self._run,
                         args=(generation, self._cancel, uid, service, expected_instance, payload),
                         daemon=True, name="qfileman-transfer").start()
        return True

    def close(self):
        self._closed = True
        self._generation += 1
        self._cancel.set()

    def _emit(self, generation, receipt, done):
        try:
            self.result.emit(generation, receipt, done)
        except RuntimeError:
            pass  # Window/controller was destroyed while the bounded read ran.

    def _run(self, generation, cancel, uid, service, expected_instance, payload):
        try:
            caps = self.integration.get_transfer_capabilities(uid, service)
            if cancel.is_set():
                return
            if caps.get("version") != 1 or caps.get("instance_id") != expected_instance:
                receipt = {"state": "unknown", "reason": "Receiver restarted or capabilities unavailable; nothing resent"}
            elif (not caps.get("available") or "text/plain" not in caps.get("kinds", [])
                  or caps.get("encoding") != "utf-8"
                  or len(payload.encode("utf-8")) > int(caps.get("max_bytes", 0))):
                receipt = {"state": "rejected", "reason": caps.get("reason") or "Receiver cannot accept this text"}
            else:
                receipt = self.integration.send_transfer(
                    uid, service, expected_instance, "text/plain", payload, timeout=60)
                if (receipt.get("state") in {"staged", "applied", "declined", "failed"}
                        and receipt.get("instance_id") != expected_instance):
                    receipt = {"state": "unknown", "reason": "Receiver instance changed"}
            payload = None  # Polling retains only the private receipt handle.
            deadline = time.monotonic() + self.POLL_SECONDS
            handle = receipt.get("transfer_id")
            while receipt.get("state") == "staged" and not cancel.is_set():
                self._emit(generation, receipt, False)
                if not handle or cancel.wait(self.POLL_INTERVAL) or time.monotonic() >= deadline:
                    receipt = {"state": "unknown", "reason": "Confirmation outcome not known within the waiting limit"}
                    break
                receipt = self.integration.get_transfer_status(handle, timeout=3)
                if (receipt.get("instance_id") != expected_instance
                        or receipt.get("transfer_id") != handle):
                    receipt = {"state": "unknown", "reason": "Receiver restarted or receipt identity changed"}
            if not cancel.is_set():
                self._emit(generation, receipt, True)
        except Exception:
            if not cancel.is_set():
                self._emit(generation, {"state": "unknown", "reason": "Transport failed or timed out"}, True)
        finally:
            _WORKERS.release()

    @pyqtSlot(int, object, bool)
    def _present(self, generation, receipt, done):
        if self._closed or generation != self._generation:
            return
        if done:
            self._busy = False
        state = receipt.get("state", "unknown")
        messages = {
            "staged": "Text staged at receiver; awaiting confirmation.",
            "applied": "Text inserted into receiver editor; saving is separate.",
            "declined": "Receiver declined the text; it was not inserted.",
            "rejected": "Receiver rejected the text; it was not inserted.",
            "failed": "Receiver failed to insert the text.",
            "unknown": "Transfer outcome unknown; text may have arrived. Nothing was resent.",
        }
        message = messages.get(state, messages["unknown"])
        reason = receipt.get("reason", "")
        if reason:
            message += " " + str(reason)
        self.changed.emit(message)


class CapabilityDiscovery(QObject):
    """Enrich a menu without holding Qt or retaining any selected contents."""

    enriched = pyqtSignal(int, str, object)
    result = pyqtSignal(int, object, bool)
    MAX_TARGETS = 16
    BUDGET_SECONDS = 15.0

    def __init__(self, parent, integration):
        super().__init__(parent)
        self.integration = integration
        self._generation = 0
        self._busy = False
        self._closed = False
        self._cancel = threading.Event()
        self.result.connect(self._present, Qt.ConnectionType.QueuedConnection)

    def start(self, targets):
        self._generation += 1
        if self._closed or self._busy or not targets or not _DISCOVERY_WORKERS.acquire(blocking=False):
            return
        self._busy = True
        self._cancel = threading.Event()
        threading.Thread(target=self._run,
                         args=(self._generation, list(targets[:self.MAX_TARGETS]), self._cancel),
                         daemon=True, name="qfileman-capabilities").start()

    def close(self):
        self._closed = True
        self._generation += 1
        self._cancel.set()

    def _emit(self, generation, result, done):
        try:
            self.result.emit(generation, result, done)
        except RuntimeError:
            pass

    def _run(self, generation, targets, cancel):
        deadline = time.monotonic() + self.BUDGET_SECONDS
        try:
            for uid, service in targets:
                if cancel.is_set() or time.monotonic() >= deadline:
                    break
                try:
                    caps = self.integration.get_transfer_capabilities(uid, service)
                except Exception:
                    caps = {"version": 0}
                if not cancel.is_set():
                    self._emit(generation, (uid, service, caps), False)
        finally:
            _DISCOVERY_WORKERS.release()
            self._emit(generation, None, True)

    @pyqtSlot(int, object, bool)
    def _present(self, generation, result, done):
        if done:
            self._busy = False
        if self._closed or generation != self._generation or done:
            return
        self.enriched.emit(*result)
