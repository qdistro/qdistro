"""TCB no-network discipline for the broker (todo/fable-networking task 5).

These are the headless, runnable-here halves of the task-5 test matrix:

  * the systemd unit actually carries the no-network hardening (a regression
    tripwire — if someone drops PrivateNetwork, this fails);
  * the broker source stays AF_UNIX-only (no AF_INET/AF_INET6/AF_VSOCK creeps
    in) — the invariant the hardening relies on;
  * the SELinux module carries the neverallow ratchet.

The enforcing halves — a negative bats proving AF_INET socket() is denied from
the broker context, and the build-ratchet proving an intentional forbidden
`allow` fails the policy build — need an SELinux-enforcing VM and are staged in
the task file, not here.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _repo_file(rel: str) -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / rel
        if cand.exists():
            return cand
    raise FileNotFoundError(rel)


SERVICE = _repo_file("broker/qdistro-admin-broker.service")
BROKER_TE = _repo_file("selinux/broker/qdistro_broker.te")
BROKER_DIR = _repo_file("broker")


def _service_directives() -> dict[str, str]:
    d: dict[str, str] = {}
    for line in SERVICE.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        d[k.strip()] = v.strip()
    return d


class TestUnitHardening:
    def test_private_network_enabled(self):
        assert _service_directives().get("PrivateNetwork") == "yes"

    def test_restrict_address_families_unix_only(self):
        fams = _service_directives().get("RestrictAddressFamilies", "")
        assert "AF_UNIX" in fams
        # The whole point: no IP families.
        assert "AF_INET" not in fams and "AF_INET6" not in fams
        assert "AF_VSOCK" not in fams        # broker decision: not needed

    def test_ip_egress_denied(self):
        assert _service_directives().get("IPAddressDeny") == "any"


class TestBrokerSourceIsUnixOnly:
    def test_no_inet_or_vsock_in_broker_sources(self):
        # Match actual socket-family USE (`socket.AF_INET`), not bare mentions:
        # qdistro_vm_schema.py documents tier4/5 VM waypipe-over-AF_VSOCK
        # transports in comments without ever opening such a socket.
        offenders: list[str] = []
        for py in BROKER_DIR.glob("*.py"):
            text = py.read_text()
            for fam in ("AF_INET", "AF_INET6", "AF_VSOCK"):
                if f"socket.{fam}" in text:
                    offenders.append(f"{py.name}:socket.{fam}")
        assert not offenders, (
            "broker must stay AF_UNIX-only for the no-network discipline; "
            f"found {offenders}")


class TestSelinuxRatchet:
    def test_neverallow_pins_off_net_sockets(self):
        te = BROKER_TE.read_text()
        for cls in ("tcp_socket", "udp_socket", "rawip_socket",
                    "netlink_route_socket", "packet_socket"):
            assert f"neverallow qdistro_broker_t self:{cls}" in te, cls

    def test_module_version_bumped(self):
        assert "policy_module(qdistro_broker, 0.5.0)" in BROKER_TE.read_text()
