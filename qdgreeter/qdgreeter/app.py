"""qdgreeter entry point.

Run by greetd as the configured `greeter`. Inherits $GREETD_SOCK
from greetd; exits when the controller emits `succeeded` (greetd
then takes over and starts the session).
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import logging
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO

from PyQt6.QtCore import QCoreApplication, QEvent, QObject, QSocketNotifier, Qt, QUrl
from PyQt6.QtGui import QGuiApplication, QKeyEvent
from PyQt6.QtQml import QQmlApplicationEngine

from .controller import GreetController

log = logging.getLogger("qdgreeter.app")

# The QML ships INSIDE the package (qdgreeter/qml, pyproject package-data),
# so a pip install carries it: with the earlier top-level qml/ every wheel
# shipped no QML and the greeter died at boot with "Main.qml: No such file"
# on any pip-installed machine (found by image/verify.sh, todo/iso/14 Phase D;
# the VM harness had hidden it by copying the whole source tree to /opt).
QML_ROOT = Path(__file__).resolve().parent / "qml"

_EV_KEY = 1
_KEY_RELEASE = 0
_KEY_PRESS = 1
_KEY_REPEAT = 2
_EVENT_STRUCT = struct.Struct("llHHI")
_EVIOCGRAB = 0x40044590

_PLAIN_KEYS = {
    2: "1",
    3: "2",
    4: "3",
    5: "4",
    6: "5",
    7: "6",
    8: "7",
    9: "8",
    10: "9",
    11: "0",
    12: "-",
    13: "=",
    16: "q",
    17: "w",
    18: "e",
    19: "r",
    20: "t",
    21: "y",
    22: "u",
    23: "i",
    24: "o",
    25: "p",
    26: "[",
    27: "]",
    30: "a",
    31: "s",
    32: "d",
    33: "f",
    34: "g",
    35: "h",
    36: "j",
    37: "k",
    38: "l",
    39: ";",
    40: "'",
    41: "`",
    43: "\\",
    44: "z",
    45: "x",
    46: "c",
    47: "v",
    48: "b",
    49: "n",
    50: "m",
    51: ",",
    52: ".",
    53: "/",
    57: " ",
}

_SHIFT_KEYS = {
    2: "!",
    3: "@",
    4: "#",
    5: "$",
    6: "%",
    7: "^",
    8: "&",
    9: "*",
    10: "(",
    11: ")",
    12: "_",
    13: "+",
    26: "{",
    27: "}",
    39: ":",
    40: '"',
    41: "~",
    43: "|",
    51: "<",
    52: ">",
    53: "?",
}

_SHIFT_CODES = {42, 54}
_CTRL_CODES = {29, 97}
_ALT_CODES = {56, 100}

# Finding 06: evdev scancode -> character decoding. The greeter EVIOCGRABs the
# keyboard on eglfs and must decode scancodes itself. The primary decoder uses
# libxkbcommon with the system XKB keymap so NON-US layouts (AZERTY/QWERTZ/...)
# can type layout-sensitive password characters at the GUI login — there is no
# tty2 text fallback (recovery is GRUB-only), so a US-only decoder would lock
# those users out. _UsTableDecoder is the graceful fallback if libxkbcommon is
# unavailable. Both expose the same interface (update/is_ctrl/is_alt/char) and
# only ONE is active per bridge, so there is no parallel modifier state.

# Linux evdev keycodes are XKB keycodes minus 8.
_EVDEV_XKB_OFFSET = 8
# enum xkb_key_direction
_XKB_KEY_UP = 0
_XKB_KEY_DOWN = 1
# enum xkb_state_component: XKB_STATE_MODS_EFFECTIVE
_XKB_STATE_MODS_EFFECTIVE = 1 << 3


class _XkbRuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


def _parse_vconsole_xkb(path: str) -> dict:
    """Read XKBLAYOUT/XKBVARIANT/XKBMODEL/XKBOPTIONS from a vconsole.conf-style
    KEY=VALUE file (systemd-localed output). KEYMAP= is a CONSOLE keymap name, a
    different namespace from XKB layouts, so it is intentionally NOT used."""
    out: dict = {}
    keys = {
        "XKBLAYOUT": "layout",
        "XKBVARIANT": "variant",
        "XKBMODEL": "model",
        "XKBOPTIONS": "options",
    }
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                field = keys.get(k.strip())
                if field:
                    out[field] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return {k: v for k, v in out.items() if v}


def _parse_xorg_xkb(path: str) -> dict:
    """Read XkbLayout/XkbVariant/XkbModel/XkbOptions from an
    /etc/X11/xorg.conf.d/00-keyboard.conf (localectl set-x11-keymap output);
    parses without an X server running."""
    out: dict = {}
    fields = {
        "xkblayout": "layout",
        "xkbvariant": "variant",
        "xkbmodel": "model",
        "xkboptions": "options",
    }
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    for m in re.finditer(r'Option\s+"(\w+)"\s+"([^"]*)"', text):
        field = fields.get(m.group(1).lower())
        if field and m.group(2):
            out[field] = m.group(2)
    return out


def _read_xkb_config() -> dict:
    """Resolve XKB rules/model/layout/variant/options for the system keymap.
    Order: XKB_DEFAULT_* env -> /etc/X11/xorg.conf.d/00-keyboard.conf ->
    /etc/vconsole.conf -> {} (libxkbcommon compiled defaults, usually `us`)."""
    env = {
        field: os.environ.get("XKB_DEFAULT_" + field.upper())
        for field in ("rules", "model", "layout", "variant", "options")
    }
    if env.get("layout"):
        return {k: v for k, v in env.items() if v}
    cfg = _parse_xorg_xkb("/etc/X11/xorg.conf.d/00-keyboard.conf")
    if cfg.get("layout"):
        return cfg
    cfg = _parse_vconsole_xkb("/etc/vconsole.conf")
    if cfg.get("layout"):
        return cfg
    log.info("no XKB layout configured; using libxkbcommon defaults")
    return {}


class _XkbDecoder:
    """libxkbcommon-backed evdev decoder. Handles shift levels, Caps Lock, AltGr
    and arbitrary layouts. `available` is False if any setup step fails, so the
    bridge can fall back to _UsTableDecoder.

    Dead-key compose (e.g. dead-acute + e -> e-acute) is NOT handled in v1: it
    needs xkb_compose_state. Layout/level/AltGr — the high-impact cases — work.
    """

    def __init__(self) -> None:
        self.available = False
        self._state = None
        try:
            self._lib = ctypes.CDLL("libxkbcommon.so.0")
        except OSError:
            log.warning("libxkbcommon.so.0 unavailable; raw keyboard falls back "
                        "to the US keymap (non-US layouts may mistype)")
            return
        try:
            self._bind()
            ctx = self._lib.xkb_context_new(0)
            if not ctx:
                return
            names = self._rule_names()
            keymap = self._lib.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
            if not keymap:
                log.warning("xkb keymap build failed; US keymap fallback")
                return
            state = self._lib.xkb_state_new(keymap)
            if not state:
                return
            # Keep refs so they are not GC'd while the state is in use.
            self._ctx = ctx
            self._keymap = keymap
            self._state = state
            self.available = True
            log.info("raw keyboard decode via libxkbcommon (layout=%s)",
                     self._layout or "default")
        except Exception:  # noqa: BLE001 - any ctypes/setup failure -> fallback
            log.warning("libxkbcommon setup failed; US keymap fallback",
                        exc_info=True)
            self.available = False

    def _bind(self) -> None:
        lib = self._lib
        vp = ctypes.c_void_p
        lib.xkb_context_new.restype = vp
        lib.xkb_context_new.argtypes = [ctypes.c_int]
        lib.xkb_keymap_new_from_names.restype = vp
        lib.xkb_keymap_new_from_names.argtypes = [vp, ctypes.POINTER(_XkbRuleNames),
                                                  ctypes.c_int]
        lib.xkb_state_new.restype = vp
        lib.xkb_state_new.argtypes = [vp]
        lib.xkb_state_update_key.restype = ctypes.c_int
        lib.xkb_state_update_key.argtypes = [vp, ctypes.c_uint32, ctypes.c_int]
        lib.xkb_state_key_get_utf8.restype = ctypes.c_int
        lib.xkb_state_key_get_utf8.argtypes = [vp, ctypes.c_uint32, ctypes.c_char_p,
                                               ctypes.c_size_t]
        lib.xkb_state_mod_name_is_active.restype = ctypes.c_int
        lib.xkb_state_mod_name_is_active.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]

    def _rule_names(self) -> _XkbRuleNames:
        cfg = _read_xkb_config()
        self._layout = cfg.get("layout")

        def enc(value):
            return value.encode("utf-8") if value else None

        return _XkbRuleNames(
            rules=enc(cfg.get("rules")),
            model=enc(cfg.get("model")),
            layout=enc(cfg.get("layout")),
            variant=enc(cfg.get("variant")),
            options=enc(cfg.get("options")),
        )

    def update(self, code: int, value: int) -> None:
        # Only PHYSICAL transitions change modifier state; autorepeat (value 2)
        # must not be replayed as a new press.
        if value == _KEY_PRESS:
            self._lib.xkb_state_update_key(self._state, code + _EVDEV_XKB_OFFSET,
                                           _XKB_KEY_DOWN)
        elif value == _KEY_RELEASE:
            self._lib.xkb_state_update_key(self._state, code + _EVDEV_XKB_OFFSET,
                                           _XKB_KEY_UP)

    def _mod_active(self, name: bytes) -> bool:
        return self._lib.xkb_state_mod_name_is_active(
            self._state, name, _XKB_STATE_MODS_EFFECTIVE) == 1

    def is_ctrl(self) -> bool:
        return self._mod_active(b"Control")

    def is_alt(self) -> bool:
        # Mod1 only — NOT Mod5/Level3 (AltGr), so AltGr text entry (@ { } \\ € on
        # many layouts) is not suppressed as if it were an Alt shortcut.
        return self._mod_active(b"Mod1")

    def char(self, code: int) -> str | None:
        buf = ctypes.create_string_buffer(16)
        n = self._lib.xkb_state_key_get_utf8(
            self._state, code + _EVDEV_XKB_OFFSET, buf, ctypes.sizeof(buf))
        if n <= 0:
            return None
        try:
            text = buf.value.decode("utf-8")
        except UnicodeDecodeError:
            return None
        # Non-empty printable only (accents are multi-byte but single grapheme);
        # control chars (Ctrl+key) are non-printable and dropped here too.
        if not text or not text.isprintable():
            return None
        return text


class _UsTableDecoder:
    """Hardcoded US-QWERTY fallback used only when libxkbcommon is unavailable.
    Preserves the pre-finding-06 behavior. Tracks its own shift/ctrl/alt state."""

    available = True

    def __init__(self) -> None:
        self._shift = False
        self._ctrl = False
        self._alt = False

    def update(self, code: int, value: int) -> None:
        pressed = value in (_KEY_PRESS, _KEY_REPEAT)
        if code in _SHIFT_CODES:
            if value in (_KEY_PRESS, _KEY_RELEASE):
                self._shift = pressed
        elif code in _CTRL_CODES:
            if value in (_KEY_PRESS, _KEY_RELEASE):
                self._ctrl = pressed
        elif code in _ALT_CODES:
            if value in (_KEY_PRESS, _KEY_RELEASE):
                self._alt = pressed

    def is_ctrl(self) -> bool:
        return self._ctrl

    def is_alt(self) -> bool:
        return self._alt

    def char(self, code: int) -> str | None:
        char = _SHIFT_KEYS.get(code) if self._shift else None
        if char is None:
            char = _PLAIN_KEYS.get(code)
            if char and self._shift and char.isalpha():
                char = char.upper()
        return char


def _make_keymap_decoder():
    """Pick the layout-correct libxkbcommon decoder, falling back to the US
    table if libxkbcommon cannot be set up."""
    decoder = _XkbDecoder()
    if decoder.available:
        return decoder
    return _UsTableDecoder()


def _raw_keyboard_candidates() -> list[str]:
    configured = os.environ.get("QDGREETER_RAW_KEYBOARD")
    if configured:
        return [configured]
    if configured == "":
        return []

    by_path = sorted(Path("/dev/input/by-path").glob("*-event-kbd"))
    if by_path:
        return [str(path) for path in by_path]
    return ["/dev/input/event0"]


class _GreeterKeyFilter(QObject):
    def __init__(self, controller: GreetController) -> None:
        super().__init__()
        self._controller = controller

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        if event is None or event.type() != QEvent.Type.KeyPress:
            return False
        if not isinstance(event, QKeyEvent):
            return False

        key = event.key()
        modifiers = event.modifiers()

        if modifiers & Qt.KeyboardModifier.ControlModifier and modifiers & Qt.KeyboardModifier.AltModifier:
            if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F12:
                return self._controller.switchToTty(key - Qt.Key.Key_F1 + 1)

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._controller.submit()
            return True

        if key == Qt.Key.Key_Backspace:
            self._controller.currentText = self._controller.currentText[:-1]
            return True

        if key == Qt.Key.Key_Escape:
            self._controller.currentText = ""
            return True

        text = event.text()
        if text and text.isprintable() and not (modifiers & Qt.KeyboardModifier.ControlModifier):
            self._controller.currentText = self._controller.currentText + text
            return True

        return False


class _RawKeyboardBridge(QObject):
    def __init__(
        self,
        device: str,
        event_file: BinaryIO,
        controller: GreetController,
    ) -> None:
        super().__init__()
        self._device = device
        self._event_file = event_file
        self._controller = controller
        # Finding 06: layout-correct decode (libxkbcommon) with a US fallback.
        self._decoder = _make_keymap_decoder()
        os.set_blocking(self._event_file.fileno(), False)
        self._notifier = QSocketNotifier(
            self._event_file.fileno(),
            QSocketNotifier.Type.Read,
            self,
        )
        self._notifier.activated.connect(self._read_available)
        self._released = False
        log.debug("raw keyboard bridge active for %s", self._device)

    def release(self) -> None:
        """Explicitly drop the exclusive keyboard grab and close the fd.

        Idempotent and error-tolerant: it runs on the login-shutdown path
        (controller.succeeded / app.aboutToQuit) and must never abort it.
        Disable+disconnect the notifier before closing the fd so Qt does
        not observe readiness on a descriptor about to be closed/reused.
        """
        if self._released or self._event_file is None:
            return
        self._released = True
        # Every step below is defensively guarded: release runs on the
        # login-shutdown path and must never raise, even if the notifier
        # wrapper or file object is already torn down.
        try:
            self._notifier.setEnabled(False)
            self._notifier.activated.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            fd = self._event_file.fileno()
            fcntl.ioctl(fd, _EVIOCGRAB, struct.pack("i", 0))
        except (OSError, ValueError):
            log.debug("raw keyboard ungrab failed for %s", self._device, exc_info=True)
        try:
            self._event_file.close()
        except OSError:
            log.debug("raw keyboard close failed for %s", self._device, exc_info=True)
        self._event_file = None
        log.debug("raw keyboard bridge released for %s", self._device)

    def _read_available(self, _fd: int | None = None) -> None:
        while True:
            # release() may run mid-loop if a handled key drives the auth to
            # success synchronously; it closes the fd and clears _event_file.
            # Bail out rather than dereference a closed/None descriptor.
            if self._event_file is None:
                return
            try:
                data = os.read(self._event_file.fileno(), _EVENT_STRUCT.size)
            except BlockingIOError:
                return
            except OSError:
                log.exception("raw keyboard reader failed for %s", self._device)
                self._notifier.setEnabled(False)
                return
            if len(data) != _EVENT_STRUCT.size:
                return
            _sec, _usec, event_type, code, value = _EVENT_STRUCT.unpack(data)
            if event_type != _EV_KEY:
                continue
            log.debug("raw keyboard event code=%s value=%s", code, value)
            self._handle_key(code, value)

    def _handle_key(self, code: int, value: int) -> None:
        # Feed every event to the decoder so it tracks modifier state
        # (shift/caps/ctrl/alt/altgr). Modifier keys produce no character, so
        # they fall through harmlessly without an explicit early return.
        self._decoder.update(code, value)
        pressed = value in (_KEY_PRESS, _KEY_REPEAT)
        if not pressed:
            return

        ctrl = self._decoder.is_ctrl()
        alt = self._decoder.is_alt()

        # Physical (layout-independent) keys: VT switch, submit, backspace, clear.
        if ctrl and alt and 59 <= code <= 70:
            self._controller.switchToTty(code - 58)
            return
        if code == 28:
            self._controller.submit()
            return
        if code == 14:
            self._controller.backspace()
            return
        if code == 1:
            self._controller.clearText()
            return

        char = self._decoder.char(code)
        # Suppress real Ctrl/Alt shortcuts (NOT AltGr, which is text entry).
        if char and not ctrl and not alt:
            log.debug("raw keyboard appending text from code=%s", code)
            self._controller.appendText(char)


def _running_virtualized() -> bool:
    """Best-effort: are we inside a VM/container?

    Mirrors qdistro deploy/qdistro-startlxqtwayland.sh, which only forces
    software cursors (``WLR_NO_HARDWARE_CURSORS=1``) under
    ``systemd-detect-virt --quiet``. If the tool is missing or errors we
    assume virtualized: an invisible pointer on a login screen is a worse
    failure than an unnecessary software cursor on bare metal.
    """
    try:
        return (
            subprocess.run(
                ["systemd-detect-virt", "--quiet"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    except OSError:
        return True


def _ensure_eglfs_software_cursor() -> None:
    """Make the eglfs/KMS pointer cursor visible on virtual GPUs.

    eglfs_kms draws the pointer on a DRM *hardware* cursor plane by
    default. The qdistro VM template (QEMU/KVM) — like most virtual GPUs
    — does not scan that plane out, so the cursor is invisible even
    though Qt thinks it is drawing one. This is the same class of bug the
    wlroots fallback session sidesteps with ``WLR_NO_HARDWARE_CURSORS=1``
    (see qdistro deploy/qdistro-startlxqtwayland.sh) — a fix that was
    applied to that path but never to this eglfs greeter.

    The eglfs equivalent is a KMS config file with ``"hwcursor": false``,
    which makes Qt composite the cursor into the framebuffer with the GL
    renderer instead — that always shows. We write a minimal config (no
    device/outputs, so Qt keeps auto-probing) into ``XDG_RUNTIME_DIR`` and
    point ``QT_QPA_EGLFS_KMS_CONFIG`` at it.

    Like the wlroots precedent, this only kicks in under virtualization,
    so real hardware keeps its (working, cheaper) hardware cursor plane.

    Guard rails:
    - no-op unless we are actually on an eglfs platform, so a desktop
      ``offscreen``/``wayland`` test run is untouched;
    - never clobbers an operator-supplied ``QT_QPA_EGLFS_KMS_CONFIG`` —
      if someone already tuned the KMS config we defer to it entirely;
    - no-op on bare metal (see _running_virtualized).

    Note this only renders the cursor; it must still be *driven*. eglfs
    only creates a cursor at all when an input device exists, so the
    greetd command must NOT pass ``QT_QPA_EGLFS_DISABLE_INPUT`` — Qt's
    libinput pointer is what gives the cursor a position. The keyboard is
    still owned by the raw-evdev bridge (it EVIOCGRABs the keyboard before
    Qt starts), so re-enabling Qt input does not double-type.
    """
    if "eglfs" not in os.environ.get("QT_QPA_PLATFORM", ""):
        return
    if os.environ.get("QT_QPA_EGLFS_KMS_CONFIG"):
        log.debug("eglfs KMS config already set; leaving cursor handling to it")
        return
    if not _running_virtualized():
        log.debug("bare metal: keeping eglfs hardware cursor")
        return

    # Never fall back to /tmp: the path is fed to Qt's QT_QPA_EGLFS_KMS_CONFIG,
    # so a world-writable, predictable location is a config-injection vector.
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        log.warning("XDG_RUNTIME_DIR unset; skipping eglfs software cursor")
        return

    # Validate the runtime dir before writing: it must exist, be a directory we
    # own, and not be accessible to group/other. Cheap defense in depth even
    # though the env is _greeter-private.
    try:
        st = os.stat(runtime_dir)
    except OSError:
        log.warning(
            "XDG_RUNTIME_DIR %s not statable; skipping eglfs software cursor",
            runtime_dir,
            exc_info=True,
        )
        return
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.geteuid()
        or stat.S_IMODE(st.st_mode) & 0o077 != 0
    ):
        log.warning(
            "XDG_RUNTIME_DIR %s failed ownership/permission check; "
            "skipping eglfs software cursor",
            runtime_dir,
        )
        return

    # Write atomically and symlink-safely: mkstemp gives an O_EXCL fd we own,
    # and os.replace onto the destination swaps the directory entry rather than
    # writing through a planted symlink at the predictable path.
    config_path = Path(runtime_dir) / "qdgreeter-eglfs-kms.json"
    # mkstemp itself can fail (ENOSPC, EMFILE, ACL/LSM denial); keep it inside
    # the guarded block so a cursor-config failure degrades gracefully (warn +
    # leave the env unset) rather than aborting greeter startup.
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=runtime_dir, prefix="qdgreeter-eglfs-kms.", suffix=".json"
        )
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"hwcursor": False}))
        os.replace(tmp, str(config_path))
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        log.warning(
            "could not write eglfs KMS config to %s; cursor may be invisible",
            config_path,
            exc_info=True,
        )
        return
    os.environ["QT_QPA_EGLFS_KMS_CONFIG"] = str(config_path)
    log.debug("eglfs software cursor enabled via %s", config_path)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("QDGREETER_LOG", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    argv = argv if argv is not None else sys.argv
    QCoreApplication.setOrganizationName("qdistro")
    QCoreApplication.setApplicationName("qdgreeter")
    # Must run before QGuiApplication: Qt reads QT_QPA_EGLFS_KMS_CONFIG
    # when the eglfs platform plugin initialises during construction.
    _ensure_eglfs_software_cursor()
    raw_keyboard_file = None
    raw_keyboard_device = ""
    for candidate in _raw_keyboard_candidates():
        try:
            raw_keyboard_file = open(candidate, "rb", buffering=0)
            fcntl.ioctl(raw_keyboard_file.fileno(), _EVIOCGRAB, struct.pack("i", 1))
            raw_keyboard_device = candidate
            log.debug("pre-grabbed raw keyboard device %s", raw_keyboard_device)
            break
        except OSError:
            log.debug("could not pre-grab raw keyboard device %s", candidate, exc_info=True)
            raw_keyboard_file = None

    app = QGuiApplication(argv)

    controller = GreetController()
    key_filter = _GreeterKeyFilter(controller)
    app.installEventFilter(key_filter)
    raw_keyboard = None
    if raw_keyboard_device and raw_keyboard_file is not None:
        raw_keyboard = _RawKeyboardBridge(raw_keyboard_device, raw_keyboard_file, controller)
        # Connect release BEFORE app.quit so it runs first in emission order
        # (Qt invokes directly-connected slots in connection order), and also
        # on aboutToQuit as a belt-and-braces fallback. release() is idempotent.
        controller.succeeded.connect(raw_keyboard.release)
        app.aboutToQuit.connect(raw_keyboard.release)
    controller.succeeded.connect(app.quit)

    try:
        engine = QQmlApplicationEngine()
        engine.addImportPath(str(QML_ROOT))
        engine.rootContext().setContextProperty("controller", controller)
        engine.rootContext().setContextProperty("greetController", controller)
        engine.load(QUrl.fromLocalFile(str(QML_ROOT / "Main.qml")))

        if not engine.rootObjects():
            log.error("QML failed to load")
            return 2

        app._qdgreeter_key_filter = key_filter  # type: ignore[attr-defined]
        app._qdgreeter_raw_keyboard = raw_keyboard  # type: ignore[attr-defined]
        return app.exec()
    finally:
        if raw_keyboard is not None:
            raw_keyboard.release()


if __name__ == "__main__":
    raise SystemExit(main())
