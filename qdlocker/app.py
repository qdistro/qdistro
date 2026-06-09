"""qdlocker entry point.

PyQt6 QGuiApplication + QML engine, with the qdwin_locker_v1
binding running on a worker thread. Wayland events are funneled into
the main thread via Qt's QueuedConnection signals so the controller
stays single-threaded.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import tomllib
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import (
    QCoreApplication,
    QObject,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine, qmlRegisterUncreatableType

from .auth import AuthBackend
from .controller import LockController
from .ctrl import CtrlSocket
from .idle import IdleWatcher
from .logind import LogindWatcher
from .wayland import LockerClient, LockerEvents

log = logging.getLogger("qdlocker.app")

PACKAGE_ROOT = Path(__file__).resolve().parent
QML_ROOT = PACKAGE_ROOT / "qml"

REASON_NAMES = {
    0: "idle",
    1: "lid",
    2: "suspend",
    3: "manual",
}


# Schema for /etc/qdistro/locker.conf. Each entry: (type, validator).
_CONFIG_SCHEMA = {
    "idle_timeout_s": (int, lambda v: 0 < v <= 86400),
    "lid_action": (str, lambda v: v in ("lock", "ignore")),
    "fprintd_enabled": (bool, lambda v: True),
    "fprintd_max_failures": (int, lambda v: 1 <= v <= 100),
    "fprintd_timeout_s": (int, lambda v: 1 <= v <= 600),
}

_DEFAULT_CONFIG: dict = {
    "idle_timeout_s": 300,
    "lid_action": "lock",
    "fprintd_enabled": True,
    "fprintd_max_failures": 3,
    "fprintd_timeout_s": 10,
}


def _qdshell_import_path() -> Path | None:
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


def _validate_config(file_config: dict, source: str) -> dict:
    """Filter file_config through the schema. Unknown keys are
    logged at WARNING and discarded. Type/range errors are logged
    and the offending key is dropped (defaults stay in place)."""
    cleaned: dict = {}
    for key, value in file_config.items():
        if key not in _CONFIG_SCHEMA:
            log.warning("%s: ignoring unknown config key '%s'", source, key)
            continue
        expected_type, validator = _CONFIG_SCHEMA[key]
        if not isinstance(value, expected_type) or isinstance(value, bool) != (expected_type is bool):
            log.error(
                "%s: key '%s' has wrong type %s (expected %s); ignoring",
                source, key, type(value).__name__, expected_type.__name__,
            )
            continue
        if not validator(value):
            log.error("%s: key '%s' value %r out of range; ignoring",
                      source, key, value)
            continue
        cleaned[key] = value
    return cleaned


def _system_config_is_trusted(path: str) -> bool:
    """Reject the system config path unless it is a regular file owned
    by root with no group/world write bits."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        log.warning("config path %s is not a regular file; refusing", path)
        return False
    if st.st_uid != 0:
        log.warning("config path %s not owned by root (uid=%d); refusing",
                    path, st.st_uid)
        return False
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        log.warning("config path %s is group/world-writable; refusing", path)
        return False
    return True


def _read_toml_no_follow(path: str) -> dict:
    """Open with O_NOFOLLOW so a symlink swap fails closed."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as f:
            return tomllib.load(f)
    except Exception:  # noqa: BLE001 - re-raised after explicit close
        raise


def load_config() -> dict:
    """Load /etc/qdistro/locker.conf (preferred) or
    ~/.config/qdistro/locker.conf (only if no system config exists at all)."""
    config = dict(_DEFAULT_CONFIG)

    system_path = "/etc/qdistro/locker.conf"
    user_path = os.path.expanduser("~/.config/qdistro/locker.conf")

    system_exists = os.path.exists(system_path)
    chosen: tuple[str, bool] | None = None  # (path, is_system)
    if system_exists:
        if _system_config_is_trusted(system_path):
            chosen = (system_path, True)
        else:
            log.warning(
                "system config %s exists but is not trustworthy; "
                "refusing to fall back to user config", system_path
            )
            chosen = None
    elif os.path.exists(user_path):
        # Per-user override only allowed when no system config exists.
        try:
            st = os.lstat(user_path)
            if not stat.S_ISREG(st.st_mode):
                log.warning("user config %s is not a regular file; ignoring",
                            user_path)
            elif st.st_uid != os.getuid():
                log.warning("user config %s not owned by current uid; ignoring",
                            user_path)
            else:
                chosen = (user_path, False)
        except OSError:
            log.debug("user config stat failed", exc_info=True)

    if chosen is None:
        log.info("using built-in defaults (no trusted config file found)")
        return config

    path, _is_system = chosen
    try:
        file_config = _read_toml_no_follow(path)
    except FileNotFoundError:
        return config
    except tomllib.TOMLDecodeError as e:
        # Log only exception class name to avoid leaking file contents.
        log.error("config %s: parse failed (%s); using defaults",
                  path, e.__class__.__name__)
        return config
    except OSError as e:
        log.error("config %s: open failed (%s)", path, e.__class__.__name__)
        return config

    cleaned = _validate_config(file_config, path)
    config.update(cleaned)
    log.info("loaded config from %s (%d keys)", path, len(cleaned))
    return config


class WaylandBridge(QObject):
    """Cross-thread bridge between the pywayland worker and the Qt
    main thread."""

    _readySignal = pyqtSignal(bool)
    _lockedChangedSignal = pyqtSignal(bool)
    _lockRequestedSignal = pyqtSignal(int)
    _overlayKeySignal = pyqtSignal(int, str)
    lockedChanged = pyqtSignal(bool)
    initiallyLockedChanged = pyqtSignal(bool)
    lockedChangedForCtrl = pyqtSignal(bool)

    def __init__(
        self,
        controller: LockController,
        idle_watcher: IdleWatcher | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._client: LockerClient | None = None
        self._idle_watcher = idle_watcher
        self._initially_locked = False
        self._locked = False
        # `_locked` mirrors *intent*: it is set True on the request path
        # (_on_lock_requested) BEFORE the compositor has actually committed
        # the lock surface, for idempotency. `_compositor_locked` is the
        # stricter flag: it is True only after the compositor's own
        # locked_changed(1) confirmation. The suspend delay inhibitor must
        # gate on the strict flag, never on mere intent, or it could be
        # released before the LOCK-layer frame is painted (the flash race
        # this whole feature exists to close).
        self._compositor_locked = False
        # Optional thread-safe callback fired when the COMPOSITOR confirms
        # it has entered the locked state (qdwin_locker_v1.locked_changed=1).
        # Used by the logind suspend-path delay inhibitor to release the
        # sleep inhibitor only once the lock surface is actually committed.
        self._lock_confirmed_cb: Callable[[], None] | None = None
        controller.unlocked.connect(self._on_unlocked)
        self._readySignal.connect(self._on_ready, Qt.ConnectionType.QueuedConnection)
        self._lockedChangedSignal.connect(self._on_locked_changed, Qt.ConnectionType.QueuedConnection)
        self._lockRequestedSignal.connect(self._on_lock_requested, Qt.ConnectionType.QueuedConnection)
        self._overlayKeySignal.connect(self._on_overlay_key, Qt.ConnectionType.QueuedConnection)

    @pyqtProperty(bool, notify=lockedChanged)
    def locked(self) -> bool:
        return self._locked

    @property
    def initially_locked(self) -> bool:
        return self._initially_locked

    @pyqtProperty(bool, notify=initiallyLockedChanged)
    def initiallyLocked(self) -> bool:
        return self._initially_locked

    def attach(self, client: LockerClient) -> None:
        self._client = client

    def set_idle_watcher(self, watcher: IdleWatcher) -> None:
        self._idle_watcher = watcher

    def set_lock_confirmed_cb(self, cb: Callable[[], None] | None) -> None:
        """Register a thread-safe callback fired when the compositor
        confirms the locked state. Used to release the logind sleep
        delay inhibitor only after the lock surface is committed."""
        self._lock_confirmed_cb = cb

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

    @pyqtSlot(bool)
    def _on_ready(self, initially_locked: bool) -> None:
        log.info("locker bound; initially_locked=%s", initially_locked)
        # If the compositor reports it is already locked at bind time, that
        # IS a compositor-confirmed locked state — seed the strict flag so a
        # suspend arriving before any fresh locked_changed(1) confirms
        # immediately instead of waiting out the inhibitor timeout.
        self._compositor_locked = initially_locked
        if self._initially_locked != initially_locked:
            self._initially_locked = initially_locked
            self.initiallyLockedChanged.emit(initially_locked)
        if self._locked != initially_locked:
            self._locked = initially_locked
            self.lockedChanged.emit(initially_locked)
        self.lockedChangedForCtrl.emit(initially_locked)

    @pyqtSlot(bool)
    def _on_locked_changed(self, locked: bool) -> None:
        log.info("compositor locked_changed=%s", locked)
        # This is the compositor's authoritative lock state — track it
        # separately from the intent mirror `_locked`.
        self._compositor_locked = locked
        if self._locked != locked:
            self._locked = locked
            self.lockedChanged.emit(locked)
        self.lockedChangedForCtrl.emit(locked)
        # The compositor has confirmed it entered the locked state: the
        # lock surface is now committed on the LOCK layer. Let the logind
        # suspend path know so it can release the sleep delay inhibitor.
        if locked and self._lock_confirmed_cb is not None:
            try:
                self._lock_confirmed_cb()
            except Exception:
                log.exception("lock_confirmed callback raised")

    @pyqtSlot(int)
    def _on_lock_requested(self, reason: int) -> None:
        reason_name = REASON_NAMES.get(reason, f"unknown({reason})")
        # Idempotency: if we're already locked (per the bridge's
        # mirror of compositor state), don't re-send set_locked or
        # lock_acknowledged — the compositor may treat duplicate acks
        # as a protocol error and kill the locker.
        if self._locked:
            log.info("lock_requested reason=%s (already locked; ignoring)",
                     reason_name)
            # Confirm-immediately is ONLY safe when the COMPOSITOR has
            # already committed the lock surface (_compositor_locked). If
            # we merely mirror intent from an in-flight earlier lock
            # request whose locked_changed(1) has not arrived yet, firing
            # the confirm here would release the suspend inhibitor before
            # the LOCK-layer frame is painted — the exact flash race we
            # are guarding against. In that case do nothing: the pending
            # compositor locked_changed(1) will fire the confirm via
            # _on_locked_changed once the surface is actually committed.
            if self._compositor_locked and self._lock_confirmed_cb is not None:
                try:
                    self._lock_confirmed_cb()
                except Exception:
                    log.exception("lock_confirmed callback raised")
            return
        # Notify the controller so it can reset per-lock-session state
        # (e.g. fprintd failure counter).
        try:
            self._controller.notify_lock_begin()
        except Exception:
            log.exception("controller.notify_lock_begin raised")
        log.info("lock_requested reason=%s", reason_name)
        # Mirror intent locally BEFORE issuing the requests so a
        # second lock_requested arriving on the same event-loop tick
        # (compositor + client-side idle racing) gets caught by the
        # `if self._locked` guard above. The compositor will follow
        # up with a locked_changed=true event that confirms it.
        self._locked = True
        self.lockedChanged.emit(True)
        self.lockedChangedForCtrl.emit(True)
        if self._client:
            self._client.set_locked(True)
            self._client.lock_acknowledged(reason)

    @pyqtSlot(int, str)
    def _on_overlay_key(self, sym: int, utf8: str) -> None:
        self._controller.handle_overlay_key(sym, utf8)

    # Public for tests / ctrl-socket synthetic lock injection.
    def inject_lock_requested(self, reason: int) -> None:
        self._lockRequestedSignal.emit(reason)

    @pyqtSlot()
    def _on_unlocked(self) -> None:
        # We are leaving the locked state: drop the compositor-confirmed
        # flag so a subsequent suspend waits for a fresh locked_changed(1).
        self._compositor_locked = False
        if self._locked:
            self._locked = False
            self.lockedChanged.emit(False)
        self.lockedChangedForCtrl.emit(False)
        if self._client:
            self._client.set_locked(False)
        # Re-arm idle notification so the next idle period fires again.
        if self._idle_watcher is not None:
            try:
                self._idle_watcher.rearm()
            except Exception:
                log.exception("idle rearm failed")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("QDLOCKER_LOG", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    argv = argv if argv is not None else sys.argv
    QCoreApplication.setOrganizationName("qdistro")
    QCoreApplication.setApplicationName("qdlocker")
    app = QGuiApplication(argv)

    config = load_config()

    qmlRegisterUncreatableType(
        LockController, "Qdistro.Locker", 1, 0,
        "LockController is provided as the `controller` context property; "
        "do not instantiate from QML.",
        "LockController",
    )

    auth = AuthBackend(
        max_fprintd_failures=int(config["fprintd_max_failures"]),
        fprintd_timeout_s=float(config["fprintd_timeout_s"]),
        fprintd_enabled=bool(config["fprintd_enabled"]),
    )
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

    if os.environ.get("QDLOCKER_NO_WAYLAND") != "1":
        if not client.connect():
            log.error("Wayland connect failed; running in detached mode")
    else:
        log.info("QDLOCKER_NO_WAYLAND=1: skipping compositor binding (dev mode)")

    # Idle watcher — bind ext-idle-notify-v1 on the locker's shared
    # display, serialized with the poll thread via _display_lock.
    idle_timeout_s = int(config["idle_timeout_s"])
    idle_timeout_ms = int(os.environ.get(
        "QDLOCKER_IDLE_MS", str(idle_timeout_s * 1000)
    ))
    _idle = IdleWatcher(timeout_ms=idle_timeout_ms)
    _idle.on_idle(lambda: bridge.inject_lock_requested(0))
    bridge.set_idle_watcher(_idle)
    if os.environ.get("QDLOCKER_NO_WAYLAND") != "1" and client._display is not None:
        try:
            _idle.start(client._display, display_lock=client._display_lock)
        except Exception:
            log.exception("idle watcher start failed; idle auto-lock disabled")

    # Logind subscription — covers HandleLidSwitch=lock (Session.Lock)
    # and PrepareForSleep(start=True) (suspend pre-lock).
    _logind: LogindWatcher | None = None
    if config.get("lid_action", "lock") == "lock":
        _logind = LogindWatcher(
            on_lock=bridge.inject_lock_requested,
        )
        # Release the suspend delay inhibitor only once the compositor
        # confirms the lock surface is committed.
        bridge.set_lock_confirmed_cb(_logind.notify_lock_confirmed)
        _logind.start()
    else:
        log.info("lid_action=ignore: skipping logind subscription")

    # Keep a strong ref so the ctrl socket isn't GC'd while
    # app.exec() runs. Parented on `app` for cleanup on quit.
    ctrl: CtrlSocket | None = None
    if os.environ.get("QDLOCKER_CTRL_SOCKET", "1") != "0":
        ctrl = CtrlSocket(controller, bridge, parent=app)

    app.aboutToQuit.connect(client.disconnect)
    if ctrl is not None:
        app.aboutToQuit.connect(ctrl.close)
    if _logind is not None:
        app.aboutToQuit.connect(_logind.stop)
    app.aboutToQuit.connect(_idle.stop)

    _notify_ready()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
