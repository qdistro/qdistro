"""Lock qdwin_shell_v1 v19 hotkey surface in the protocol XML.

Static assertions only — runtime behaviour (Super+Space → launcher
toggle) requires a live qdwin compositor and lives in the in-VM bats
suite. Here we just guard against accidental rename / version
regression of the v19 additions:

  * interface bumped to version="19"
  * request register_hotkey(id, modifiers, key) since="19"
  * request unregister_hotkey(id) since="19"
  * event hotkey_pressed(id) since="19"
  * enum modifier with ctrl/alt/super/shift bits
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

XML_PATH = (
    Path(__file__).resolve().parents[2]
    .parent / "qdwin" / "qdwin" / "qdwin-shell-v1.xml"
)


@pytest.fixture(scope="module")
def shell_iface() -> ET.Element:
    tree = ET.parse(XML_PATH)
    iface = tree.getroot().find("./interface[@name='qdwin_shell_v1']")
    assert iface is not None, "qdwin_shell_v1 interface missing"
    return iface


def test_interface_version_at_least_19(shell_iface: ET.Element) -> None:
    assert int(shell_iface.attrib["version"]) >= 19


def test_register_hotkey_request(shell_iface: ET.Element) -> None:
    req = shell_iface.find("./request[@name='register_hotkey']")
    assert req is not None
    assert req.attrib.get("since") == "19"
    args = [(a.attrib["name"], a.attrib["type"]) for a in req.findall("arg")]
    assert args == [("id", "uint"), ("modifiers", "uint"), ("key", "uint")]


def test_unregister_hotkey_request(shell_iface: ET.Element) -> None:
    req = shell_iface.find("./request[@name='unregister_hotkey']")
    assert req is not None
    assert req.attrib.get("since") == "19"
    args = [(a.attrib["name"], a.attrib["type"]) for a in req.findall("arg")]
    assert args == [("id", "uint")]


def test_hotkey_pressed_event(shell_iface: ET.Element) -> None:
    ev = shell_iface.find("./event[@name='hotkey_pressed']")
    assert ev is not None
    assert ev.attrib.get("since") == "19"
    args = [(a.attrib["name"], a.attrib["type"]) for a in ev.findall("arg")]
    assert args == [("id", "uint")]


def test_modifier_enum(shell_iface: ET.Element) -> None:
    enum = shell_iface.find("./enum[@name='modifier']")
    assert enum is not None
    assert enum.attrib.get("bitfield") == "true"
    entries = {
        e.attrib["name"]: int(e.attrib["value"])
        for e in enum.findall("entry")
    }
    # Must mirror the four bits the C handler decodes.
    assert entries == {"ctrl": 1, "alt": 2, "super": 4, "shift": 8}


def test_modifier_bits_match_c_handler() -> None:
    """C side maps qdwin_shell_v1.modifier bits → weston_keyboard_modifier.

    If the bit layout in the XML drifts from the C decoder in
    qdwin_hotkey_mods_to_weston(), shells will fire wrong combos. Lock
    both sides by reading the source.
    """
    src = (
        Path(__file__).resolve().parents[2]
        .parent / "qdwin" / "qdwin" / "qdwin.c"
    ).read_text()
    # Each line of the form `if (mods & N) wmods |= MODIFIER_X;` must
    # appear exactly once with the expected bit→name pairing.
    expected = [
        ("1", "MODIFIER_CTRL"),
        ("2", "MODIFIER_ALT"),
        ("4", "MODIFIER_SUPER"),
        ("8", "MODIFIER_SHIFT"),
    ]
    for bit, mod in expected:
        needle = f"if (mods & {bit}) wmods |= {mod};"
        assert needle in src, f"missing decoder line: {needle}"
