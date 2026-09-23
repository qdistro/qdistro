"""libxkbcommon raw-evdev decode tests (finding 06).

The greeter EVIOCGRABs the keyboard on eglfs and decodes scancodes itself.
Before finding 06 it used hardcoded US-QWERTY tables, so a non-US-layout user
could not type a layout-sensitive password at the GUI login (and there is no
tty2 text fallback — recovery is GRUB-only). These tests pin that the
libxkbcommon decoder is LAYOUT-CORRECT: the same physical scancodes produce
different characters under `us` vs `de` (QWERTZ), shift/levels work, and the
raw bridge appends the layout-correct character.

Skipped entirely if libxkbcommon.so.0 is unavailable; the US-table fallback is
covered by test_qdgreeter_keyboard_bridge.py.
"""

from __future__ import annotations

import os
import sys

import pytest

_HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6", reason="PyQt6 not installed")
from PyQt6.QtCore import QCoreApplication  # noqa: E402
from qdgreeter.app import _RawKeyboardBridge, _XkbDecoder  # noqa: E402

_KEY_PRESS = 1

# Physical evdev scancodes (layout-independent positions).
_CODE_Y_POS = 21   # US 'y'; on de (QWERTZ) -> 'z'
_CODE_Z_POS = 44   # US 'z'; on de (QWERTZ) -> 'y'
_CODE_MINUS = 12   # US '-'; on de -> 'ß'
_CODE_Q = 16       # 'q' on both
_CODE_LEFTSHIFT = 42


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def _decoder(monkeypatch, layout: str, variant: str | None = None) -> _XkbDecoder:
    for var in ("RULES", "MODEL", "LAYOUT", "VARIANT", "OPTIONS"):
        monkeypatch.delenv("XKB_DEFAULT_" + var, raising=False)
    monkeypatch.setenv("XKB_DEFAULT_LAYOUT", layout)
    if variant:
        monkeypatch.setenv("XKB_DEFAULT_VARIANT", variant)
    dec = _XkbDecoder()
    if not dec.available:
        pytest.skip("libxkbcommon.so.0 not available")
    return dec


class _FakeController:
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


def test_us_layout_matches_qwerty(qapp, monkeypatch):
    dec = _decoder(monkeypatch, "us")
    assert dec.char(_CODE_Y_POS) == "y"
    assert dec.char(_CODE_Z_POS) == "z"
    assert dec.char(_CODE_Q) == "q"


@pytest.mark.cheat_aware(
    protects="the raw keyboard decoder is layout-correct via libxkbcommon, so a "
    "non-US (here German QWERTZ) user types the right password characters at "
    "the GUI login instead of US-misinterpreted ones",
    severity="medium",
    cheats=[
        "decode every layout through the hardcoded US tables",
        "ignore XKB_DEFAULT_LAYOUT and always build a `us` keymap",
        "assert only that some char comes back, not the layout-correct one",
    ],
    consequence="non-US-layout users cannot type their password at the only "
    "interactive qdistro login (no tty2 fallback) and are locked out",
)
def test_de_layout_swaps_z_y_and_maps_eszett(qapp, monkeypatch):
    dec = _decoder(monkeypatch, "de")
    # QWERTZ: the physical 'y' and 'z' positions are swapped vs US.
    assert dec.char(_CODE_Y_POS) == "z", "de physical-Y position must produce 'z'"
    assert dec.char(_CODE_Z_POS) == "y", "de physical-Z position must produce 'y'"
    # The key right of '0' is 'ß' on de, '-' on us.
    assert dec.char(_CODE_MINUS) == "ß"
    # Letters in the same place stay the same.
    assert dec.char(_CODE_Q) == "q"


def test_shift_applies_level_on_de(qapp, monkeypatch):
    dec = _decoder(monkeypatch, "de")
    dec.update(_CODE_LEFTSHIFT, _KEY_PRESS)
    # Shift+physical-Y -> 'Z' on de.
    assert dec.char(_CODE_Y_POS) == "Z"
    # Shift + ß key -> '?' on de.
    assert dec.char(_CODE_MINUS) == "?"


def test_bridge_types_layout_correct_char(qapp, monkeypatch):
    """End-to-end through _handle_key with an xkb (de) decoder."""
    dec = _decoder(monkeypatch, "de")
    ctl = _FakeController()
    bridge = _RawKeyboardBridge.__new__(_RawKeyboardBridge)
    bridge._controller = ctl
    bridge._device = "fake-kbd"
    bridge._decoder = dec
    bridge._handle_key(_CODE_Y_POS, _KEY_PRESS)
    assert ctl.calls == [("appendText", "z")]


def test_altgr_is_not_treated_as_alt_shortcut(qapp, monkeypatch):
    """AltGr (ISO_Level3, Mod5) must NOT register as Alt, so AltGr-composed
    characters are typed rather than suppressed as an Alt shortcut."""
    dec = _decoder(monkeypatch, "de")
    _CODE_RIGHTALT = 100  # AltGr
    dec.update(_CODE_RIGHTALT, _KEY_PRESS)
    assert dec.is_alt() is False, "AltGr must not count as Alt (Mod1)"
    assert dec.is_ctrl() is False
