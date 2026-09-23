#!/usr/bin/env python3
"""Pin the compositor-authoritative RDP input lease gate."""
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def body(source: str, name: str) -> str:
    match = re.search(rf"\n{name}\s*\([^;]*?\)\s*\{{", source, re.DOTALL)
    if not match:
        raise ValueError(f"{name} definition not found")
    start = source.index("{", match.start())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise ValueError(f"{name} has unbalanced braces")


def main() -> int:
    if len(sys.argv) != 5:
        return fail("usage: test_rdp_input_gate.py rdp.c backend-rdp.h qdwin.c xml")
    rdp = Path(sys.argv[1]).read_text(encoding="utf-8")
    header = Path(sys.argv[2]).read_text(encoding="utf-8")
    qdwin = Path(sys.argv[3]).read_text(encoding="utf-8")
    protocol = Path(sys.argv[4]).read_text(encoding="utf-8")

    if 'WESTON_RDP_OUTPUT_API_NAME "weston_rdp_output_api_v2"' not in header:
        return fail("RDP output API broke the stock frontend ABI name")
    if "output_set_input_enabled" not in header:
        return fail("RDP output API does not expose the input gate")
    if "b->input_enabled = false;" not in rdp:
        return fail("RDP backend input does not start fail-closed")

    try:
        release = body(rdp, "rdp_peer_release_input")
        backend_gate = body(rdp, "rdp_output_set_input_enabled")
        shell_gate = body(qdwin, "qdwin_handle_set_remote_output_input")
        drain = body(qdwin, "qdwin_handle_drain_remote_output_state")
        shell_destroy = body(qdwin, "qdwin_shell_resource_destroy")
        input_handlers = [
            body(rdp, name) for name in (
                "xf_mouseEvent", "xf_extendedMouseEvent",
                "xf_input_synchronize_event", "xf_input_keyboard_event")
        ]
    except ValueError as error:
        return fail(str(error))

    if not all("rdp_peer_input_allowed" in handler for handler in input_handlers):
        return fail("an RDP input callback bypasses the backend gate")
    for token in (
        "notify_button", "WL_POINTER_BUTTON_STATE_RELEASED",
        "notify_key", "WL_KEYBOARD_KEY_STATE_RELEASED",
        "STATE_UPDATE_AUTOMATIC",
    ):
        if token not in release:
            return fail(f"held-input release is missing {token}")
    if "rdp_peer_release_input" not in backend_gate:
        return fail("disabling RDP input does not release held state")

    for token in (
        "qdwin_shell_require_bound", "qdwin_remote_output_name_valid",
        "enabled && qdwin->locked", "qdwin_rdp_output_get_api",
        "output_set_input_enabled",
    ):
        if token not in shell_gate and token not in qdwin:
            return fail(f"qdwin remote input gate is missing {token}")
    if "qdwin_set_remote_output_input" not in shell_destroy or "false" not in shell_destroy:
        return fail("shell loss does not revoke RDP input")
    interface = re.search(
        r'interface name="qdwin_shell_v1" version="(\d+)"', protocol)
    if interface is None or int(interface.group(1)) < 34:
        return fail("qdwin shell protocol does not include the v34 input gate")
    if 'request name="set_remote_output_input" since="34"' not in protocol:
        return fail("qdwin shell protocol lacks the v34 input request")
    if 'event name="remote_output_input_result" since="34"' not in protocol:
        return fail("qdwin shell protocol lacks the authoritative input result")
    if "send_remote_output_input_result" not in shell_gate:
        return fail("qdwin does not acknowledge the backend input transition")
    if 'interface name="qdwin_shell_v1" version="34"' not in protocol:
        return fail("qdwin shell protocol was not bumped for safety drain")
    if 'request name="drain_remote_output_state" since="34"' not in protocol:
        return fail("qdwin shell protocol lacks the safety drain request")
    for token in (
        "qdwin_shell_require_bound", "qdwin_remote_output_name_valid",
        "qdwin_output_boundary_cancel_state",
        "send_remote_output_drain_result",
    ):
        if token not in drain:
            return fail(f"qdwin remote output drain is missing {token}")
    advertised = re.search(
        r"wl_global_create\(ec->wl_display,\s*"
        r"&qdwin_shell_v1_interface,\s*(\d+),",
        qdwin,
    )
    if advertised is None or int(advertised.group(1)) < 34:
        return fail("qdwin does not advertise the v34 safety boundary")

    # Merge compatibility: preserve every shipped main opcode while appending
    # the remote controls. Reusing feature-branch v31-v33 would misdispatch
    # existing capture requests and events on an older shell connection.
    iface = ET.fromstring(protocol).find("interface")
    for kind, expected in {
        "request": {36: "request_set_position", 37: "prepare_output_capture",
                    38: "set_remote_output_input", 39: "drain_remote_output_state"},
        "event": {30: "toplevel_app_id", 31: "capture_served_stale",
                  32: "nested_proxy_remote_identity", 33: "remote_output_input_result",
                  34: "remote_output_drain_result"},
    }.items():
        entries = iface.findall(kind)
        for opcode, name in expected.items():
            if entries[opcode].get("name") != name:
                return fail(f"{kind} opcode {opcode} must remain {name}")
    for handler in (shell_gate, drain):
        if "wl_resource_get_version(resource) < 34" not in handler:
            return fail("remote requests must reject callers bound below v34")

    print("PASS: RDP input is fail-closed and bound-shell controlled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
