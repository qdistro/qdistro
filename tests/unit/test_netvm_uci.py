"""Unit tests for the egress-policy -> OpenWrt UCI compiler (task 4 piece 4).

The compiler is pure, so we assert the load-bearing CONTRACT properties (the
kill-switch fw4 default-deny, the PBR intra-subnet exception, per-silo resolver
isolation) plus a golden snapshot that locks the rendered format. Mirrors the
interim backend's test_silo_egress.py style.
"""
from __future__ import annotations

import pytest

from qdistro_silo_egress import EgressPolicy, EgressError
from qdistro_netvm_uci import (
    SiloNet, compile_silo, compile_all, compile_firewall, compile_network,
)


def _wg(index=0, name="work"):
    return SiloNet(
        name=name, index=index, policy=EgressPolicy.parse("wg:" + name),
        wg_peer_public_key="PEERPUB=", wg_endpoint="vpn.example:51820",
        wg_address="10.200.0.1/24", wg_dns="10.200.0.1")


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------

class TestDerivations:
    def test_per_silo_subnet_and_table(self):
        a, b = _wg(0), _wg(1, "play")
        assert a.subnet == "10.10.1.0/24" and a.router_ip == "10.10.1.1"
        assert b.subnet == "10.10.2.0/24" and b.table == 101
        assert a.subnet != b.subnet                # disjoint per-silo nets

    def test_index_bounds_rejected(self):
        with pytest.raises(EgressError):
            SiloNet(name="x", index=-1, policy=EgressPolicy.parse("none"))
        with pytest.raises(EgressError):
            SiloNet(name="x", index=999, policy=EgressPolicy.parse("none"))

    def test_wg_requires_peer_fields(self):
        with pytest.raises(EgressError):
            SiloNet(name="x", index=0, policy=EgressPolicy.parse("wg:x"))


# ---------------------------------------------------------------------------
# Kill-switch by construction (fw4 default-deny forward)
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_every_zone_default_denies_forward(self):
        for spec in ("none", "direct"):
            fw = compile_firewall(
                SiloNet(name="s", index=0, policy=EgressPolicy.parse(spec)))
            assert "option forward 'REJECT'" in fw
        assert "option forward 'REJECT'" in compile_firewall(_wg())

    def test_wg_forwards_only_to_vpn_never_wan(self):
        fw = compile_firewall(_wg())
        assert "option dest 'vpn'" in fw
        assert "option dest 'wan'" not in fw      # no WAN fallback => kill-switch

    def test_direct_forwards_to_wan(self):
        fw = compile_firewall(
            SiloNet(name="s", index=0, policy=EgressPolicy.parse("direct")))
        assert "option dest 'wan'" in fw

    def test_none_has_no_forwarding(self):
        fw = compile_firewall(
            SiloNet(name="s", index=0, policy=EgressPolicy.parse("none")))
        assert "config forwarding" not in fw      # dark: forwards nowhere


# ---------------------------------------------------------------------------
# PBR intra-subnet exception (Probe 2 finding 1)
# ---------------------------------------------------------------------------

class TestPBR:
    def test_intra_subnet_exception_outranks_tunnel_rule(self):
        s = _wg(0)
        net = compile_network(s)
        # The local (main-table) rule must have a numerically LOWER priority
        # than the tunnel rule so silo<->router DNS/DHCP is not tunnel-bound.
        local_prio = 1000 + s.index
        tunnel_prio = 2000 + s.index
        assert f"option priority '{local_prio}'" in net
        assert f"option priority '{tunnel_prio}'" in net
        assert local_prio < tunnel_prio
        # the local rule keeps intra-subnet traffic on the main table
        assert "option lookup 'main'" in net
        assert f"option lookup '{s.table}'" in net

    def test_none_and_direct_have_no_pbr(self):
        for spec in ("none", "direct"):
            net = compile_network(
                SiloNet(name="s", index=0, policy=EgressPolicy.parse(spec)))
            assert net == ""                       # PBR is wg-only


# ---------------------------------------------------------------------------
# Per-silo resolver isolation
# ---------------------------------------------------------------------------

class TestResolver:
    def test_each_silo_gets_its_own_dnsmasq_instance(self):
        merged = compile_all([_wg(0, "a"), _wg(1, "b")])
        assert "config dnsmasq 'silo0'" in merged["dhcp"]
        assert "config dnsmasq 'silo1'" in merged["dhcp"]

    def test_wg_resolver_upstream_is_tunnel_dns(self):
        dhcp = compile_silo(_wg(0))["dhcp"]
        assert "list server '10.200.0.1'" in dhcp

    def test_every_resolver_is_noresolv_no_wan_leak(self):
        # noresolv on EVERY instance: a per-silo resolver must never fall back to
        # the VM's /etc/resolv.conf (WAN), which would leak a wg silo's DNS.
        for spec, kw in (("none", {}), ("direct", {}),):
            dhcp = compile_silo(
                SiloNet(name="s", index=0,
                        policy=EgressPolicy.parse(spec)))["dhcp"]
            assert "option noresolv '1'" in dhcp
        assert "option noresolv '1'" in compile_silo(_wg(0))["dhcp"]
        # interface binding is a list option on OpenWrt.
        assert "list interface 'silo0'" in compile_silo(_wg(0))["dhcp"]

    def test_none_resolver_has_no_upstream(self):
        dhcp = compile_silo(
            SiloNet(name="s", index=0, policy=EgressPolicy.parse("none")))["dhcp"]
        assert "option noresolv '1'" in dhcp       # answers, forwards nothing
        assert "list server" not in dhcp


# ---------------------------------------------------------------------------
# Golden snapshot — locks the rendered format for one wg silo
# ---------------------------------------------------------------------------

GOLDEN_WG_FIREWALL = """\
config zone
\toption name 'silo0'
\toption input 'REJECT'
\toption output 'ACCEPT'
\toption forward 'REJECT'
\tlist network 'silo0'
config rule
\toption name 'silo0-dhcp-dns'
\toption src 'silo0'
\toption dest_port '53 67'
\toption target 'ACCEPT'
config forwarding
\toption src 'silo0'
\toption dest 'vpn'
"""


class TestGolden:
    def test_wg_firewall_golden(self):
        assert compile_firewall(_wg(0)) == GOLDEN_WG_FIREWALL

    def test_compile_all_stable_order(self):
        # index order is stable regardless of input order.
        a = compile_all([_wg(1, "b"), _wg(0, "a")])
        b = compile_all([_wg(0, "a"), _wg(1, "b")])
        assert a == b
        assert a["firewall"].index("silo0") < a["firewall"].index("silo1")
