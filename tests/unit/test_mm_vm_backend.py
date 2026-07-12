"""Unit tests for the QciVMBackend pure helpers (no VM/subprocess).

The protocol-touching methods (spin/exec/screenshot/...) are exercised by the
live-gated integration run; here we pin the parsing/argv logic that decides what
those methods do, since a regression there silently mis-drives a real VM.
"""
from __future__ import annotations

import pytest

from multimachine.harness.vm_backend import (
    arg_value, hostfwd_add_hmp, hostfwd_present, is_marker_argv, parse_approved)


# Real `info usernet` output (qemu:///session, virtio-net user hub) — the format
# the host-forward detection must parse. The port is a BARE column field.
_USERNET = (
    "Hub -1 (hostnet0):\n"
    "  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ\n"
    "  TCP[HOST_FORWARD] 138       127.0.0.1  5555       10.0.2.15  5555     0     0\n"
    "  UDP[211 sec]      137       10.0.2.15 43673   188.68.34.173   123     0     0\n")
_USERNET_NOFWD = (
    "Hub -1 (hostnet0):\n"
    "  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ\n"
    "  UDP[211 sec]      137       10.0.2.15 43673   188.68.34.173   123     0     0\n")


class TestParseApproved:
    SAMPLE = (
        "qdwin-bystander: bound qdwin_shell_v1 v26\n"
        "qdwin-bystander: view_stream approved handle=1 pw=weston.pipewire-0 port=3401\n"
        "HANDLE=1\nPIPEWIRE_NODE_NAME=weston.pipewire-0\nRDP_PORT=3401\n"
        "RDP_CERT_PATH=\nRDP_PASSWORD=0e363c6088ae44f0\n")

    def test_parses_port_password_node(self):
        info = parse_approved(self.SAMPLE)
        assert info["rdp_port"] == 3401
        assert info["password"] == "0e363c6088ae44f0"
        assert info["pw_node"] == "weston.pipewire-0"

    def test_raises_when_not_approved(self):
        with pytest.raises(ValueError):
            parse_approved("qdwin-bystander: view_stream denied handle=1 reason=...\n")

    def test_missing_password_is_empty_not_error(self):
        info = parse_approved("RDP_PORT=42\nPIPEWIRE_NODE_NAME=n\n")
        assert info["rdp_port"] == 42 and info["password"] == ""


class TestHostfwd:
    def test_hmp_command_shape(self):
        assert (hostfwd_add_hmp("hostnet0", 5555)
                == "hostfwd_add hostnet0 tcp:127.0.0.1:5555-:5555")

    def test_custom_host_addr(self):
        assert "tcp:0.0.0.0:7000-:7000" in hostfwd_add_hmp("hostnet0", 7000, "0.0.0.0")

    def test_present_detects_existing_forward(self):
        # the bare-column `info usernet` format — NOT ":5555" (the old buggy
        # token check the session-3 live re-validation caught).
        assert hostfwd_present(_USERNET, 5555)
        assert hostfwd_present(_USERNET, 5555, "127.0.0.1")

    def test_present_false_when_no_forward(self):
        assert not hostfwd_present(_USERNET_NOFWD, 5555)

    def test_present_false_for_other_port(self):
        assert not hostfwd_present(_USERNET, 6000)

    def test_present_ignores_non_hostforward_lines(self):
        # a UDP session to a host on port 5555 must NOT be read as a forward.
        udp = ("  UDP[200 sec] 9  10.0.2.15 40000  1.2.3.4  5555  0  0\n")
        assert not hostfwd_present(udp, 5555)


class TestArgv:
    def test_is_marker_argv(self):
        assert is_marker_argv(["qdwin-marker-client", "--width", "1280"])
        assert not is_marker_argv(["pgrep", "-f", "qdwin-marker-client"])
        assert not is_marker_argv([])

    def test_arg_value(self):
        argv = ["qdwin-marker-client", "--width", "1280", "--generation", "20"]
        assert arg_value(argv, "--generation") == "20"
        assert arg_value(argv, "--width") == "1280"
        assert arg_value(argv, "--frame", "0") == "0"   # default when absent
        assert arg_value(argv, "--missing") is None
