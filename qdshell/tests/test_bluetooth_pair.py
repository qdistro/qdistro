"""F10: bluetooth-pair MAC validation rejects smuggled control characters."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HELPER = (
    Path(__file__).resolve().parents[1]
    / "Scripts" / "python" / "src" / "network" / "bluetooth-pair.py"
)

spec = importlib.util.spec_from_file_location("bluetooth_pair", HELPER)
assert spec and spec.loader
bluetooth_pair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bluetooth_pair)


@pytest.mark.parametrize("addr", [
    "AA:BB:CC:DD:EE:FF",
    "00:11:22:33:44:55",
    "a0:b1:c2:d3:e4:f5",
])
def test_accepts_canonical_mac(addr):
    # The helper uses fullmatch (see bluetooth-pair.py); mirror that here.
    assert bluetooth_pair.MAC_RE.fullmatch(addr)


@pytest.mark.parametrize("addr", [
    "",
    "AA:BB:CC:DD:EE",                 # too short
    "AA:BB:CC:DD:EE:FF:00",           # too long
    "AABBCCDDEEFF",                   # no separators
    "AA:BB:CC:DD:EE:GG",              # non-hex
    "AA:BB:CC:DD:EE:FF\nconnect XX",  # newline command smuggling (mid-string)
    "AA:BB:CC:DD:EE:FF\n",            # single trailing newline (the `$` foot-gun)
    "AA:BB:CC:DD:EE:FF ",            # trailing space
    "AA:BB:CC:DD:EE:FF;quit",
])
def test_rejects_noncanonical_mac(addr):
    assert not bluetooth_pair.MAC_RE.fullmatch(addr)
