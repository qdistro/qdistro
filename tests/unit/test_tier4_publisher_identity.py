"""Unit tests for tier4_publisher_identity — identity-bound publisher
validation that closes the fixed-CID fail-open.

Source: ``todo/gpt-review/tier4-waypipe-display-tests.md`` §"Replace fixed
unauthenticated vsock success checks".

These are pure-logic tests (no VM, no vsock). They pin the contract that
the host's readiness probe only accepts the publisher endpoint when its
banner matches the launch record this spawn minted, and fails closed on a
stale, forged, wrong-instance, wrong-vm, or malformed banner — the exact
"a stale or wrong process bound to that port could satisfy the signal"
class the review flagged.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# tier4-vm/ is not on the conftest sys.path (it's a flat asset dir), so
# load the module by file location the same way test_bridge_identity
# loads the browser bridge.
_MOD = (Path(__file__).resolve().parent.parent.parent
        / "tier4-vm" / "tier4_publisher_identity.py")
_spec = importlib.util.spec_from_file_location(
    "tier4_publisher_identity", _MOD)
pi = importlib.util.module_from_spec(_spec)
sys.modules["tier4_publisher_identity"] = pi
_spec.loader.exec_module(pi)


def _rec(vm="s110vm", instance="s110vm-" + "a" * 32, port=7879):
    return pi.LaunchRecord(vm_name=vm, instance_id=instance, port=port)


# --- build -----------------------------------------------------------------

class TestBuild:
    def test_build_roundtrips_through_parse(self):
        rec = _rec()
        banner = pi.build_handshake(rec.vm_name, rec.instance_id, rec.port)
        assert banner.endswith("\n")
        fields = pi.parse_handshake(banner)
        assert fields == {
            "vm": rec.vm_name,
            "instance": rec.instance_id,
            "port": str(rec.port),
        }

    def test_build_rejects_bad_vm(self):
        with pytest.raises(pi.HandshakeError) as e:
            pi.build_handshake("../evil", "inst1", 7879)
        assert e.value.reason == "bad-field-chars"

    def test_build_rejects_newline_in_instance(self):
        # A banner field can never carry a newline back to the host parser.
        with pytest.raises(pi.HandshakeError):
            pi.build_handshake("vm1", "inst\n1", 7879)

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999, "x"])
    def test_build_rejects_bad_port(self, port):
        with pytest.raises(pi.HandshakeError) as e:
            pi.build_handshake("vm1", "inst1", port)
        assert e.value.reason == "bad-port"


# --- verify: the happy path ------------------------------------------------

class TestVerifyMatch:
    def test_exact_match_passes(self):
        rec = _rec()
        banner = pi.build_handshake(rec.vm_name, rec.instance_id, rec.port)
        # Returns None (no raise) on success.
        assert pi.verify_handshake(banner, rec) is None

    def test_match_tolerates_trailing_crlf(self):
        rec = _rec()
        banner = pi.build_handshake(rec.vm_name, rec.instance_id, rec.port)
        assert pi.verify_handshake(banner.rstrip("\n") + "\r\n", rec) is None


# --- verify: NEGATIVE cases that MUST fail closed --------------------------

class TestVerifyFailClosed:
    """Each of these is a 'wrong endpoint' the readiness probe used to
    accept. They MUST raise so the host refuses to attach the client."""

    def test_wrong_instance_fails_closed(self):
        # The load-bearing case: a stale prior-spawn publisher or a
        # co-tenant occupies CID:port but carries a DIFFERENT launch
        # token. Same vm/port, wrong instance -> deny.
        rec = _rec(instance="s110vm-" + "a" * 32)
        impostor = pi.build_handshake(
            rec.vm_name, "s110vm-" + "b" * 32, rec.port)
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(impostor, rec)
        assert e.value.reason == "instance-mismatch"

    def test_wrong_vm_fails_closed(self):
        rec = _rec(vm="s110vm")
        other = pi.build_handshake(
            "othervm", rec.instance_id, rec.port)
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(other, rec)
        assert e.value.reason == "vm-mismatch"

    def test_wrong_port_fails_closed(self):
        rec = _rec(port=7879)
        other = pi.build_handshake(rec.vm_name, rec.instance_id, 7880)
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(other, rec)
        assert e.value.reason == "port-mismatch"

    def test_empty_banner_fails_closed(self):
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake("", _rec())
        assert e.value.reason == "empty-banner"

    def test_none_banner_fails_closed(self):
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(None, _rec())
        assert e.value.reason == "missing-banner"

    def test_garbage_banner_fails_closed(self):
        # A waypipe stream's first bytes, or any non-banner noise.
        with pytest.raises(pi.HandshakeError):
            pi.verify_handshake("\x00\x01random-binary", _rec())

    def test_wrong_magic_fails_closed(self):
        bad = "SOMETHING-ELSE v1 vm=s110vm instance=x port=7879\n"
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(bad, _rec(vm="s110vm", instance="x"))
        assert e.value.reason == "bad-magic"

    def test_wrong_version_fails_closed(self):
        bad = "QDISTRO-TIER4-PUBLISHER v2 vm=s110vm instance=x port=7879\n"
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(bad, _rec(vm="s110vm", instance="x"))
        assert e.value.reason == "bad-version"

    def test_missing_field_fails_closed(self):
        bad = "QDISTRO-TIER4-PUBLISHER v1 vm=s110vm port=7879\n"
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(bad, _rec())
        # Token count is 4, not 5 -> caught at the structural gate.
        assert e.value.reason == "bad-token-count"

    def test_duplicate_field_fails_closed(self):
        bad = ("QDISTRO-TIER4-PUBLISHER v1 vm=s110vm "
               "instance=x port=7879 port=7879\n")
        with pytest.raises(pi.HandshakeError):
            pi.verify_handshake(bad, _rec())

    def test_multiline_banner_fails_closed(self):
        # A peer that tries to smuggle a second line (e.g. to confuse a
        # line-based host reader) is rejected outright.
        rec = _rec()
        good = pi.build_handshake(rec.vm_name, rec.instance_id, rec.port)
        with pytest.raises(pi.HandshakeError) as e:
            pi.verify_handshake(good + "EXTRA LINE\n", rec)
        assert e.value.reason == "multiline-banner"


# --- CLI shim (used by spawn-tier4.sh via python3) -------------------------

class TestCli:
    def test_cli_build_then_verify_ok(self, capsys):
        rc = pi._main(["build", "s110vm", "s110vm-" + "a" * 32, "7879"])
        assert rc == 0
        banner = capsys.readouterr().out.rstrip("\n")
        rc = pi._main(
            ["verify", "s110vm", "s110vm-" + "a" * 32, "7879", banner])
        assert rc == 0

    def test_cli_verify_mismatch_exit_3(self, capsys):
        rc = pi._main([
            "verify", "s110vm", "right-inst", "7879",
            "QDISTRO-TIER4-PUBLISHER v1 vm=s110vm "
            "instance=wrong-inst port=7879",
        ])
        assert rc == 3
        err = capsys.readouterr().err
        assert "reason=instance-mismatch" in err

    def test_cli_unknown_subcommand_exit_2(self):
        assert pi._main(["frobnicate"]) == 2
