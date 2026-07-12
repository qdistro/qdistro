"""Transactional inherited-config boundary for the live multi-machine broker."""
from __future__ import annotations

from copy import deepcopy

import pytest

from multimachine.mm_broker import parse_session_config


def _stream(label: str, origin: str, generation: int, port: int,
            control_port: int) -> dict:
    return {
        "label": label,
        "spec": {
            "origin": origin,
            "stream_id": f"source-{label}",
            "generation": generation,
            "app_id": f"qdistro.mm.{origin}.{label}",
            "instance_id": f"{origin}-{label}-1",
            "rdp_host": "10.0.2.2",
            "rdp_port": port,
            "width": 640,
            "height": 400,
            "allow_input": 1,
        },
        "control_port": control_port,
        "rdp_unit": f"mm-rdp-{label}",
        "marker_unit": f"mm-marker-{label}",
        "otp": f"otp-{label}",
        "control_capability": f"control-{label}",
    }


def _config() -> dict:
    return {
        "control_host": "127.0.0.1",
        "origins": [
            {
                "machine_id": "vm-a",
                "trust_domain_id": "owner-machines",
                "generation": 51,
                "capabilities": ["attach_ui", "receive_input"],
            },
            {
                "machine_id": "vm-b",
                "trust_domain_id": "owner-machines",
                "generation": 52,
                "capabilities": ["attach_ui", "receive_input"],
            },
        ],
        "streams": [
            _stream("a", "vm-a", 51, 5555, 5571),
            _stream("b", "vm-b", 52, 5560, 5572),
        ],
    }


def test_complete_config_is_parsed_and_authorized_before_startup() -> None:
    parsed = parse_session_config(_config())
    assert parsed.control_host == "127.0.0.1"
    assert [stream.label for stream in parsed.streams] == ["a", "b"]
    assert [stream.spec.origin for stream in parsed.streams] == ["vm-a", "vm-b"]
    assert [stream.connect_timeout for stream in parsed.streams] == [30.0, 30.0]
    assert parsed.shell_pid is None


@pytest.mark.parametrize("value", [0, -1, True, "123"])
def test_invalid_shell_pid_is_rejected(value) -> None:
    raw = _config()
    raw["shell_pid"] = value
    with pytest.raises(ValueError, match="shell_pid"):
        parse_session_config(raw)


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda c: c["streams"][1].update(label="a"), "duplicate stream label"),
    (lambda c: c["streams"][1].update(control_port=5571), "duplicate control_port"),
    (lambda c: c["streams"][1]["spec"].update(
        origin="vm-a", generation=51, app_id="qdistro.mm.vm-a.a"),
     "duplicate secctx app_id"),
    (lambda c: c["streams"][1]["spec"].update(
        rdp_host="10.0.2.2", rdp_port=5555), "duplicate RDP endpoint"),
    (lambda c: c["streams"][1]["spec"].update(generation=51), "generation"),
    (lambda c: c["streams"][1].update(control_port=True), "control_port"),
    (lambda c: c["streams"][1].update(control_capability=""),
     "control_capability"),
])
def test_bad_later_stream_fails_the_whole_snapshot(mutate, message) -> None:
    raw = deepcopy(_config())
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        parse_session_config(raw)


@pytest.mark.parametrize("raw", [None, {}, {"origins": [], "streams": []}])
def test_missing_authority_or_streams_fails_closed(raw) -> None:
    with pytest.raises(ValueError):
        parse_session_config(raw)
