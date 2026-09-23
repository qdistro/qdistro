"""Raw evdev keyboard bridge tests for qdgreeter (_RawKeyboardBridge).

The greetd protocol / auth-flow suites already cover the controller's
round-trip. This file covers the one untested hop *before* that flow:
the raw-evdev keyboard bridge that owns the keyboard (it EVIOCGRABs the
device before Qt starts) and translates raw Linux input keycodes into
controller actions. This is the most exposed pre-auth boundary — every
keystroke a user types at the login screen passes through
``_RawKeyboardBridge._handle_key`` before any authentication happens.

We exercise ``_handle_key(code, value)`` directly against a FAKE
controller. The bridge's ``__init__`` wires a real ``QSocketNotifier``
to a file descriptor, which we don't want (and can't reliably get a real
evdev device on a CI host), so we construct the object via ``__new__``
and set only the modifier-state attributes ``_handle_key`` reads. The
keycode constants and method calls below mirror qdgreeter/app.py
exactly — they are NOT guessed.

Linux evdev value semantics (from app.py):
  value 0 = release, 1 = press, 2 = autorepeat.
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

_HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


PyQt6 = pytest.importorskip("PyQt6", reason="PyQt6 not installed")
from PyQt6.QtCore import QCoreApplication  # noqa: E402
from qdgreeter import app as qdgreeter_app  # noqa: E402
from qdgreeter.app import (  # noqa: E402
    _EVIOCGRAB,
    _RawKeyboardBridge,
    _UsTableDecoder,
)

# evdev keycodes used by app.py's _handle_key (kept here so the test
# documents the contract it pins). These match the real source.
_KEY_RELEASE = 0
_KEY_PRESS = 1
_KEY_REPEAT = 2

_CODE_ESC = 1
_CODE_BACKSPACE = 14
_CODE_ENTER = 28
_CODE_LEFTCTRL = 29
_CODE_LEFTALT = 56
_CODE_LEFTSHIFT = 42
_CODE_F1 = 59  # F1..F12 == 59..70; TTY = code - 58
_CODE_F2 = 60
_CODE_A = 30  # _PLAIN_KEYS[30] == "a"
_CODE_1 = 2   # plain "1" / shift "!"


class _FakeController:
    """Records every action the bridge drives, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self) -> None:
        self.calls.append(("submit",))

    def backspace(self) -> None:
        self.calls.append(("backspace",))

    def clearText(self) -> None:
        self.calls.append(("clearText",))

    def appendText(self, char: str) -> None:
        self.calls.append(("appendText", char))

    def switchToTty(self, tty: int) -> bool:
        self.calls.append(("switchToTty", tty))
        return True


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def _make_bridge(controller: _FakeController) -> _RawKeyboardBridge:
    """Build a bridge without running __init__ (which needs a real fd /
    QSocketNotifier). ``_handle_key`` delegates modifier/character decode to
    ``self._decoder``; these tests pin the US-table fallback decoder (the path
    used when libxkbcommon is unavailable), so they assert US-layout behavior.
    The libxkbcommon path (non-US layouts) is covered in
    test_qdgreeter_xkb_decode.py."""
    bridge = _RawKeyboardBridge.__new__(_RawKeyboardBridge)
    bridge._controller = controller
    bridge._device = "fake-kbd"
    bridge._decoder = _UsTableDecoder()
    return bridge


def _press(bridge: _RawKeyboardBridge, code: int) -> None:
    bridge._handle_key(code, _KEY_PRESS)


# --------------------------------------------------------------------------
# Core key -> action mapping.
# --------------------------------------------------------------------------
@pytest.mark.cheat_aware(
    protects="pressing Enter at the login screen drives controller.submit() "
    "(the one action that initiates the greetd auth round-trip)",
    severity="high",
    cheats=[
        "assert len(calls) >= 0 (always true)",
        "treat any keypress as submit so the test can't tell Enter apart",
    ],
    consequence="the raw keyboard bridge could fail to wire Enter to submit, "
    "leaving the boot login screen unable to authenticate at all",
)
def test_enter_submits(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    _press(bridge, _CODE_ENTER)
    assert ctl.calls == [("submit",)]


def test_printable_char_appends_password_char(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    _press(bridge, _CODE_A)
    assert ctl.calls == [("appendText", "a")]


def test_shift_uppercases_letter(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_LEFTSHIFT, _KEY_PRESS)
    _press(bridge, _CODE_A)
    assert ctl.calls == [("appendText", "A")]


def test_shift_maps_symbol_row(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_LEFTSHIFT, _KEY_PRESS)
    _press(bridge, _CODE_1)  # plain "1" -> shift "!"
    assert ctl.calls == [("appendText", "!")]


def test_backspace(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    _press(bridge, _CODE_BACKSPACE)
    assert ctl.calls == [("backspace",)]


def test_escape_clears_text(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    _press(bridge, _CODE_ESC)
    assert ctl.calls == [("clearText",)]


def test_autorepeat_press_still_types(qapp):
    """value==2 (autorepeat) counts as pressed for typing, like a real
    held key."""
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_A, _KEY_REPEAT)
    assert ctl.calls == [("appendText", "a")]


def test_key_release_is_ignored_for_typing(qapp):
    """A bare key-release (value==0) must not type a character."""
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_A, _KEY_RELEASE)
    assert ctl.calls == []


def test_keycode_outside_mapped_range_is_ignored(qapp):
    """Codes with no mapping (and not a modifier / control key) are
    silently dropped — no spurious controller calls."""
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    # 200 is well outside _PLAIN_KEYS / _SHIFT_KEYS / the F-key TTY range
    # and is not a shift/ctrl/alt code.
    _press(bridge, 200)
    # KEY_RESERVED-ish low code 15 (Tab) is also unmapped here.
    _press(bridge, 15)
    assert ctl.calls == []


# --------------------------------------------------------------------------
# Ctrl+Alt+Fx -> TTY switch. The modifier state is REQUIRED.
# --------------------------------------------------------------------------
@pytest.mark.cheat_aware(
    protects="a TTY switch (chvt) only happens on Ctrl+Alt+Fx — a plain Fx "
    "keystroke while typing a password must NOT switch VTs",
    severity="high",
    cheats=[
        "route every F-key to switchToTty regardless of modifier state",
        "drop the plain-F2 negative assertion so the modifier gate is "
        "untested",
        "assert switchToTty was 'called or not' (tautology)",
    ],
    consequence="without the Ctrl+Alt gate, an F-key typed as part of input "
    "would yank the user to a raw VT mid-login — or, worse, the gate could "
    "be removed and a password keystroke would silently change VTs",
)
def test_ctrl_alt_f2_switches_to_tty2(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_LEFTCTRL, _KEY_PRESS)
    bridge._handle_key(_CODE_LEFTALT, _KEY_PRESS)
    _press(bridge, _CODE_F2)
    # F2 == code 60; tty == code - 58 == 2.
    assert ctl.calls == [("switchToTty", 2)]


def test_ctrl_alt_f1_switches_to_tty1(qapp):
    """Lower bound of the F-key range: F1 == 59 -> tty 1."""
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_LEFTCTRL, _KEY_PRESS)
    bridge._handle_key(_CODE_LEFTALT, _KEY_PRESS)
    _press(bridge, _CODE_F1)
    assert ctl.calls == [("switchToTty", 1)]


@pytest.mark.cheat_aware(
    protects="modifier state is mandatory for the TTY path: a plain F2 (no "
    "Ctrl+Alt) typed during password entry must NOT switch VTs",
    severity="high",
    cheats=[
        "ignore the modifier flags and switch TTY on any F-key",
        "weaken the assertion to allow switchToTty to be called",
    ],
    consequence="a bare F-key while typing would drop the user out of the "
    "greeter into a raw console, abandoning the login attempt",
)
def test_plain_f2_does_not_switch_tty(qapp):
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    # No ctrl/alt pressed.
    _press(bridge, _CODE_F2)
    assert all(c[0] != "switchToTty" for c in ctl.calls), (
        f"plain F2 must not switch TTY; got {ctl.calls!r}"
    )
    # And it certainly must not be typed as a password character either.
    assert ctl.calls == []


def test_ctrl_only_f2_does_not_switch_tty(qapp):
    """Ctrl alone (no Alt) must not satisfy the TTY gate."""
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_LEFTCTRL, _KEY_PRESS)
    _press(bridge, _CODE_F2)
    assert all(c[0] != "switchToTty" for c in ctl.calls), (
        f"Ctrl-only F2 must not switch TTY; got {ctl.calls!r}"
    )


def test_ctrl_suppresses_text_entry(qapp):
    """A printable code held with Ctrl (e.g. Ctrl+A) must NOT be typed
    into the password — app.py only appends when neither ctrl nor alt is
    held."""
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_LEFTCTRL, _KEY_PRESS)
    _press(bridge, _CODE_A)
    assert ("appendText", "a") not in ctl.calls
    assert all(c[0] == "switchToTty" or c == () for c in ctl.calls) or ctl.calls == []
    assert ctl.calls == []


def test_modifier_release_clears_state(qapp):
    """Releasing Ctrl/Alt must drop the modifier flags, so a later F2 is
    treated as plain again (no TTY switch)."""
    ctl = _FakeController()
    bridge = _make_bridge(ctl)
    bridge._handle_key(_CODE_LEFTCTRL, _KEY_PRESS)
    bridge._handle_key(_CODE_LEFTALT, _KEY_PRESS)
    bridge._handle_key(_CODE_LEFTCTRL, _KEY_RELEASE)
    bridge._handle_key(_CODE_LEFTALT, _KEY_RELEASE)
    _press(bridge, _CODE_F2)
    assert ctl.calls == []


# --------------------------------------------------------------------------
# release(): explicit grab-drop + fd close on shutdown (harden-07).
#
# release() needs a REAL fd + QSocketNotifier, so unlike _make_bridge above
# these tests build the bridge through the real __init__ on an os.pipe() read
# end. A pipe fd cannot service EVIOCGRAB, so we monkeypatch
# qdgreeter.app.fcntl.ioctl to RECORD calls and return 0 — letting us assert
# the explicit ungrab (EVIOCGRAB 0) was attempted without a real evdev device.
# --------------------------------------------------------------------------
def _real_bridge_on_pipe(monkeypatch, controller: _FakeController):
    """Build a real-__init__ bridge on a pipe read fd.

    Returns (bridge, read_file, recorded_ioctls, close_write). The caller
    must call close_write() to release the pipe write end. The ioctl recorder
    captures (fd, request, arg) tuples and returns 0 so __init__/release never
    hit a real evdev syscall.
    """
    recorded: list[tuple] = []

    def _fake_ioctl(fd, request, arg):
        recorded.append((fd, request, arg))
        return 0

    monkeypatch.setattr(qdgreeter_app.fcntl, "ioctl", _fake_ioctl)
    r_fd, w_fd = os.pipe()
    read_file = os.fdopen(r_fd, "rb", buffering=0)
    bridge = _RawKeyboardBridge("fake-kbd", read_file, controller)

    def _close_write() -> None:
        try:
            os.close(w_fd)
        except OSError:
            pass

    return bridge, read_file, recorded, _close_write


@pytest.mark.cheat_aware(
    protects="the greeter explicitly releases its exclusive keyboard grab "
    "(EVIOCGRAB 0) and closes the raw input fd when it shuts down, instead "
    "of relying on implicit process-exit cleanup",
    severity="low",
    cheats=[
        "make release() a no-op and rely on process exit",
        "skip the EVIOCGRAB 0 ungrab and only close the fd",
        "assert release() was called without checking the grab was dropped",
    ],
    consequence="the greeter could finish still holding an exclusive grab on "
    "the keyboard device, and the explicit-cleanup guarantee would silently "
    "rot",
)
def test_release_ungrabs_and_closes_input_fd(qapp, monkeypatch):
    ctl = _FakeController()
    bridge, read_file, recorded, close_write = _real_bridge_on_pipe(monkeypatch, ctl)
    try:
        bridge.release()

        ungrabs = [
            arg
            for (_fd, request, arg) in recorded
            if request == _EVIOCGRAB and struct.unpack("i", arg)[0] == 0
        ]
        assert ungrabs, (
            "release() must EVIOCGRAB 0 to drop the exclusive grab; "
            f"recorded ioctls: {recorded!r}"
        )
        assert read_file.closed is True, "release() must close the raw input fd"
    finally:
        close_write()


def test_release_is_idempotent(qapp, monkeypatch):
    ctl = _FakeController()
    bridge, read_file, recorded, close_write = _real_bridge_on_pipe(monkeypatch, ctl)
    try:
        bridge.release()
        ungrab_count = sum(
            1
            for (_fd, request, arg) in recorded
            if request == _EVIOCGRAB and struct.unpack("i", arg)[0] == 0
        )
        # Second call must be a harmless no-op: no raise, no extra ungrab.
        bridge.release()
        ungrab_count_after = sum(
            1
            for (_fd, request, arg) in recorded
            if request == _EVIOCGRAB and struct.unpack("i", arg)[0] == 0
        )
        assert ungrab_count_after == ungrab_count, (
            "second release() must not re-issue the ungrab; "
            f"counts {ungrab_count} -> {ungrab_count_after}"
        )
        assert read_file.closed is True
    finally:
        close_write()


def test_release_disables_notifier(qapp, monkeypatch):
    ctl = _FakeController()
    bridge, read_file, recorded, close_write = _real_bridge_on_pipe(monkeypatch, ctl)
    try:
        assert bridge._notifier.isEnabled() is True
        bridge.release()
        assert bridge._notifier.isEnabled() is False, (
            "release() must disable the QSocketNotifier before the fd is closed"
        )
    finally:
        close_write()


def test_read_available_is_safe_after_release(qapp, monkeypatch):
    """If a handled key drives auth to success synchronously, release()
    runs mid-loop and clears _event_file; a resumed _read_available() must
    bail out cleanly rather than dereference the closed/None fd."""
    ctl = _FakeController()
    bridge, read_file, recorded, close_write = _real_bridge_on_pipe(monkeypatch, ctl)
    try:
        bridge.release()
        # Must not raise AttributeError on self._event_file.fileno().
        bridge._read_available()
    finally:
        close_write()
