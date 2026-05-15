"""qdlocker entry point.

PySide6 QGuiApplication + QML engine, with the qdwin_locker_v1
binding running on a worker thread. Wayland events are funneled into
the main thread via Qt's QueuedConnection signals so the controller
stays single-threaded.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterUncreatableType

from .auth import AuthBackend
from .controller import LockController
from .ctrl import CtrlSocket
from .idle import IdleWatcher
from .wayland import LockerClient, LockerEvents

log = logging.getLogger("qdlocker.app")

# Package layout: this module lives at <pkg>/qdlocker/app.py. QML
# ships at <pkg>/qdlocker/qml/ (inside the package — not at the repo
# root — so setuptools' package_data globs pick it up).
PACKAGE_ROOT = Path(__file__).resolve().parent
QML_ROOT = PACKAGE_ROOT / "qml"


def _qdshell_import_path() -> Path | None:
    """qdshell is a sibling repo with its top-level Commons/ and
    Widgets/ directories. The actual styling reuse requires
    Quickshell at runtime — we add the path here for future use but
    LockUI.qml does NOT import qdshell directly until the
    Quickshell-free shim lands (see README §Status)."""
    explicit = os.environ.get("QDLOCKER_QDSHELL_PATH")
    if explicit:
        return Path(explicit)
    candidates = [PACKAGE_ROOT.parent.parent / "qdshell",
                  PACKAGE_ROOT.parent.parent.parent / "qdshell"]
    for c in candidates:
        if (c / "Commons" / "Style.qml").exists():
            return c
    return None


def _notify_ready() -> None:
    """sd_notify READY=1 so a Type=notify systemd unit knows the
    locker is up and accepting events. Silently no-op if not running
    under systemd."""
    if not os.environ.get("NOTIFY_SOCKET"):
        return
    try:
        import socket
        path = os.environ["NOTIFY_SOCKET"]
        if path.startswith("@"):
            path = "\0" + path[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            s.sendall(b"READY=1\n")
    except OSError:
        log.exception("sd_notify failed")


class WaylandBridge(QObject):
    """Cross-thread bridge between the pywayland worker and the Qt
    main thread.

    The LockerClient invokes our `_thread_*` methods on the worker
    thread; each emits a Signal that's connected with
    `Qt.QueuedConnection` so the corresponding `_on_*` slot runs on
    the main thread. Properties (`locked`, `initially_locked`) live
    here so ctrl.py can introspect live state without reaching into
    private fields.
    """

    _readySignal = Signal(bool)
    _lockedChangedSignal = Signal(bool)
    _lockRequestedSignal = Signal(int)
    _overlayKeySignal = Signal(int, str)

    def __init__(
        self, controller: LockController, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._client: LockerClient | None = None
        self._initially_locked = False
        self._locked = False
        controller.unlocked.connect(self._on_unlocked)
        # Wire worker→main with explicit QueuedConnection so this stays
        # safe even if QObject thread-affinity ever changes.
        self._readySignal.connect(self._on_ready, Qt.QueuedConnection)
        self._lockedChangedSignal.connect(self._on_locked_changed, Qt.QueuedConnection)
        self._lockRequestedSignal.connect(self._on_lock_requested, Qt.QueuedConnection)
        self._overlayKeySignal.connect(self._on_overlay_key, Qt.QueuedConnection)

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def initially_locked(self) -> bool:
        return self._initially_locked

    def attach(self, client: LockerClient) -> None:
        self._client = client

    # ---- worker-thread entry points (must be re-entrant-safe) ----

    def _thread_on_ready(self, initially_locked: bool) -> None:
        self._readySignal.emit(initially_locked)

    def _thread_on_locked_changed(self, locked: bool) -> None:
        self._lockedChangedSignal.emit(locked)

    def _thread_on_lock_requested(self, reason: int) -> None:
        self._lockRequestedSignal.emit(reason)

    def _thread_on_overlay_key(self, sym: int, utf8: str) -> None:
        self._overlayKeySignal.emit(sym, utf8)

    # ---- main-thread slots ----

    @Slot(bool)
    def _on_ready(self, initially_locked: bool) -> None:
        log.info("locker bound; initially_locked=%s", initially_locked)
        self._initially_locked = initially_locked
        self._locked = initially_locked

    @Slot(bool)
    def _on_locked_changed(self, locked: bool) -> None:
        log.info("compositor locked_changed=%s", locked)
        self._locked = locked

    @Slot(int)
    def _on_lock_requested(self, reason: int) -> None:
        log.info("lock_requested reason=%d", reason)
        if self._client:
            self._client.set_locked(True)
            self._client.lock_acknowledged(reason)

    @Slot(int, str)
    def _on_overlay_key(self, sym: int, utf8: str) -> None:
        self._controller.handle_overlay_key(sym, utf8)

    # Public for tests / ctrl-socket synthetic lock injection.
    def inject_lock_requested(self, reason: int) -> None:
        self._lockRequestedSignal.emit(reason)

    @Slot()
    def _on_unlocked(self) -> None:
        if self._client:
            self._client.set_locked(False)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("QDLOCKER_LOG", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    argv = argv if argv is not None else sys.argv
    QCoreApplication.setOrganizationName("qdistro")
    QCoreApplication.setApplicationName("qdlocker")
    app = QGuiApplication(argv)

    # `qmlRegisterType` would let QML instantiate `LockController` via
    # `LockController { }`, which calls the default constructor and
    # crashes because the controller's `__init__` requires an
    # `AuthBackend`. We expose the live instance via a context
    # property (see below) and register the *type* as uncreatable
    # only so QML can type-check property bindings.
    qmlRegisterUncreatableType(
        LockController, "Qdistro.Locker", 1, 0, "LockController",
        "LockController is provided as the `controller` context property; "
        "do not instantiate from QML."
    )

    auth = AuthBackend()
    controller = LockController(auth)
    bridge = WaylandBridge(controller)

    events = LockerEvents(
        on_ready=bridge._thread_on_ready,
        on_locked_changed=bridge._thread_on_locked_changed,
        on_lock_requested=bridge._thread_on_lock_requested,
        on_overlay_key=bridge._thread_on_overlay_key,
    )
    client = LockerClient(events)
    bridge.attach(client)

    engine = QQmlApplicationEngine()
    qdshell_path = _qdshell_import_path()
    if qdshell_path:
        engine.addImportPath(str(qdshell_path))
    engine.addImportPath(str(QML_ROOT))
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_ROOT / "Main.qml")))

    if not engine.rootObjects():
        log.error("QML failed to load")
        return 2

    # Connect to qdwin AFTER QML loaded so the controller is wired.
    if os.environ.get("QDLOCKER_NO_WAYLAND") != "1":
        if not client.connect():
            log.error("Wayland connect failed; running in detached mode")
    else:
        log.info("QDLOCKER_NO_WAYLAND=1: skipping compositor binding (dev mode)")

    # Idle path runs via qdwin's lock_requested(reason=0=idle) for now;
    # see qdlocker/idle.py for the local-subscription plan. The
    # IdleWatcher is intentionally NOT started here.
    _idle = IdleWatcher(
        timeout_ms=int(os.environ.get("QDLOCKER_IDLE_MS", str(10 * 60 * 1000)))
    )

    # Keep a strong ref so the ctrl socket isn't GC'd while
    # app.exec() runs. Parented on `app` for cleanup on quit.
    ctrl: CtrlSocket | None = None
    if os.environ.get("QDLOCKER_CTRL_SOCKET", "1") != "0":
        ctrl = CtrlSocket(controller, bridge, parent=app)

    app.aboutToQuit.connect(client.disconnect)
    if ctrl is not None:
        app.aboutToQuit.connect(ctrl.close)

    _notify_ready()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
