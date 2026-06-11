"""Unit tests for the pure per-silo egress backend (qdistro_silo_egress).

The backend is exercised against a `_RecordOps` adapter that records the
ip/wg/nft/veth calls in order and pretends success. This lets us assert the
*sequence* that yields kill-switch-by-construction (wg device born in the init
netns, configured, then moved into the silo netns) without root or real
namespaces. Mirrors the `_FakeOps` pattern in test_session_manager.py.
"""
from __future__ import annotations

import pytest

import qdistro_silo_egress as eg
from qdistro_silo_egress import (
    EgressBackend, EgressError, EgressPolicy, KeyUnavailable, TunnelConfig,
    link_up_event, validate_egress, wg_ifname, veth_host_ifname,
    veth_silo_ifname,
)


# ---------------------------------------------------------------------------
# Recording ops adapter
# ---------------------------------------------------------------------------

class _RecordOps:
    """Records every backend->ops call as a (op, *args) tuple in `calls`, and
    tracks which links 'exist' so teardown/idempotency can be asserted."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.links: set[tuple[str | None, str]] = set()   # (ns, ifname)
        self.resolv: dict[str, list[str]] = {}
        self.skuid_drop: dict[int, bool] = {}
        self.nat: dict[str, bool] = {}
        self.ip_forward = False
        self.dns: dict[str, tuple] = {}            # ns -> (host_if, host_ip)

    def _rec(self, *a):
        self.calls.append(a)

    # ---- link / addr / route ----
    def link_up(self, ns, ifname):
        self._rec("link_up", ns, ifname)

    def link_del(self, ns, ifname):
        # idempotent: record only when something was actually there, but always
        # log the attempt so order is visible.
        self._rec("link_del", ns, ifname)
        self.links.discard((ns, ifname))

    def link_set_netns(self, ifname, ns):
        self._rec("link_set_netns", ifname, ns)
        # the device leaves the init netns and appears in `ns`
        self.links.discard((None, ifname))
        self.links.add((ns, ifname))

    def addr_add(self, ns, ifname, address):
        self._rec("addr_add", ns, ifname, address)

    def route_add_default_dev(self, ns, ifname):
        self._rec("route_add_default_dev", ns, ifname)

    def route_add_default_via(self, ns, gateway):
        self._rec("route_add_default_via", ns, gateway)

    def ipv6_disable(self, ns, ifname):
        self._rec("ipv6_disable", ns, ifname)

    # ---- wg ----
    def wg_add_dev(self, ifname):
        self._rec("wg_add_dev", ifname)
        self.links.add((None, ifname))            # created in the init netns

    def wg_configure(self, ifname, *, private_key, peer_public_key, endpoint,
                     allowed_ips, keepalive):
        self._rec("wg_configure", ifname, private_key, peer_public_key,
                  endpoint, allowed_ips, keepalive)

    # ---- veth / nat ----
    def veth_create(self, host_if, peer_if):
        self._rec("veth_create", host_if, peer_if)
        self.links.add((None, host_if))
        self.links.add((None, peer_if))

    def nat_masquerade(self, subnet, enable):
        self._rec("nat_masquerade", subnet, enable)
        self.nat[subnet] = bool(enable)

    # ---- direct: forwarding + per-silo resolver ----
    def enable_ip_forward(self):
        self._rec("enable_ip_forward")
        self.ip_forward = True

    def dns_start(self, ns, host_if, host_ip):
        self._rec("dns_start", ns, host_if, host_ip)
        self.dns[ns] = (host_if, host_ip)

    def dns_stop(self, ns):
        self._rec("dns_stop", ns)
        self.dns.pop(ns, None)

    # ---- nft backstop / resolver ----
    def nft_skuid_drop(self, uid, enable):
        self._rec("nft_skuid_drop", uid, enable)
        self.skuid_drop[int(uid)] = bool(enable)

    def write_netns_resolv(self, ns, nameservers):
        self._rec("write_netns_resolv", ns, list(nameservers))
        self.resolv[ns] = list(nameservers)

    def remove_netns_resolv(self, ns):
        self._rec("remove_netns_resolv", ns)
        self.resolv.pop(ns, None)

    # ---- helpers for assertions ----
    def ops_only(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def ops():
    return _RecordOps()


@pytest.fixture
def backend():
    return EgressBackend()


NS = "qd-work"
UID = 2001
TUN = TunnelConfig(
    name="work", peer_public_key="PUBKEYbase64==",
    endpoint="vpn.example:51820", address="10.7.0.2/32", dns="10.7.0.1",
    keepalive=25)


# ---------------------------------------------------------------------------
# Policy parsing / validation
# ---------------------------------------------------------------------------

class TestPolicyParse:
    def test_none(self):
        p = EgressPolicy.parse("none")
        assert p.mode == "none" and p.tunnel is None and p.spec == "none"

    def test_direct(self):
        assert EgressPolicy.parse("direct").mode == "direct"

    def test_wg(self):
        p = EgressPolicy.parse("wg:work")
        assert p.mode == "wg" and p.tunnel == "work" and p.spec == "wg:work"

    @pytest.mark.parametrize("bad", [
        "", "wg:", "wg:..", "wg:Work", "wg:a b", "wg:../etc", "bogus",
        "wg:" + "x" * 32, None, 123, "none ",
    ])
    def test_malformed_rejected(self, bad):
        # Probe 1 A1: untrusted egress input must never be smuggled into the
        # privileged path; reject everything that isn't exactly valid.
        with pytest.raises(EgressError):
            EgressPolicy.parse(bad)

    def test_validate_egress_none_passthrough(self):
        assert validate_egress(None) is None      # legacy/unmanaged state

    def test_validate_egress_canonicalises(self):
        assert validate_egress("wg:work") == "wg:work"
        assert validate_egress("none") == "none"
        with pytest.raises(EgressError):
            validate_egress("wg:Bad")

    def test_validate_egress_accepts_direct(self):
        # Opt 3-A completed `direct` (forwarding + per-silo resolver +
        # forward-isolation), so the create/load/set chokepoint now accepts it
        # and canonicalises to "direct".
        assert validate_egress("direct") == "direct"


# ---------------------------------------------------------------------------
# Interface naming (IFNAMSIZ bound — S1)
# ---------------------------------------------------------------------------

class TestNaming:
    def test_ifnames_bounded_for_max_uid(self):
        # The worst case is the largest uid; all derived ifnames must fit the
        # 15-char usable IFNAMSIZ budget, regardless of how long the silo NAME
        # is (names go to 32 chars, which is why ifnames are uid-derived).
        for uid in (2000, 60000):
            for fn in (wg_ifname, veth_host_ifname, veth_silo_ifname):
                assert len(fn(uid)) <= 15, (fn.__name__, uid)

    def test_netns_name_uses_silo_name(self):
        assert eg.netns_name("a-very-long-silo-name-thats-32c") == \
            "qd-a-very-long-silo-name-thats-32c"


# ---------------------------------------------------------------------------
# wg apply: kill-switch by construction
# ---------------------------------------------------------------------------

class TestWgApply:
    def test_born_in_init_then_moved_then_routed(self, backend, ops):
        res = backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                            tunnel=TUN, keyfn=lambda name: "PRIVKEY=")
        assert not res.dark and res.mode == "wg"
        seq = ops.ops_only()
        ifn = wg_ifname(UID)
        # The load-bearing order: device created in the INIT netns (wg_add_dev),
        # configured there (so its socket binds to init), THEN moved into the
        # silo netns, THEN addr/up/default-route inside the netns.
        i_add = seq.index("wg_add_dev")
        i_cfg = seq.index("wg_configure")
        i_move = seq.index("link_set_netns")
        i_addr = seq.index("addr_add")
        i_route = seq.index("route_add_default_dev")
        assert i_add < i_cfg < i_move < i_addr < i_route

    def test_only_lo_and_wg_no_other_egress_device(self, backend, ops):
        # The essence of kill-switch-by-construction: the only devices touched
        # inside the silo netns are lo and wg-<uid>; no veth, no physical NIC,
        # so there is no fallback route to flush to.
        backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                      tunnel=TUN, keyfn=lambda n: "PRIVKEY=")
        in_ns_links = {ifn for (ns, ifn) in ops.links if ns == NS}
        assert in_ns_links == {wg_ifname(UID)}        # plus lo (link_up, not tracked)
        assert "route_add_default_via" not in ops.ops_only()  # no veth gateway

    def test_dns_is_tunnel_resolver(self, backend, ops):
        backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                      tunnel=TUN, keyfn=lambda n: "PRIVKEY=")
        assert ops.resolv[NS] == ["10.7.0.1"]         # the tunnel-side DNS

    def test_private_key_from_keyfn_only(self, backend, ops):
        # The key reaches wg_configure verbatim from keyfn and nowhere else —
        # the backend never derives a path under a silo home.
        seen = {}
        def keyfn(name):
            seen["name"] = name
            return "SECRETKEY="
        backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                      tunnel=TUN, keyfn=keyfn)
        assert seen["name"] == "work"
        cfg = [c for c in ops.calls if c[0] == "wg_configure"][0]
        assert cfg[2] == "SECRETKEY="                 # private_key arg

    def test_backstop_enabled(self, backend, ops):
        backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                      tunnel=TUN, keyfn=lambda n: "PRIVKEY=")
        assert ops.skuid_drop[UID] is True

    def test_locked_vault_comes_up_dark_not_fail(self, backend, ops):
        # B3: a locked/pre-auth vault must not fail the start. Come up dark
        # (no wg device, backstop on), retriable.
        def keyfn(name):
            raise KeyUnavailable("vault-locked")
        res = backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                            tunnel=TUN, keyfn=keyfn)
        assert res.dark and res.pending == "vault-locked"
        assert "wg_add_dev" not in ops.ops_only()
        assert ops.skuid_drop[UID] is True

    def test_missing_key_comes_up_dark(self, backend, ops):
        res = backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                            tunnel=TUN, keyfn=lambda n: None)
        assert res.dark and res.pending == "no-key"
        assert "wg_add_dev" not in ops.ops_only()

    def test_wg_without_tunnel_is_error(self, backend, ops):
        with pytest.raises(EgressError):
            backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                          tunnel=None, keyfn=lambda n: "k")

    def test_apply_teardown_stale_first(self, backend, ops):
        # S4: a re-apply over a restart-surviving/half-configured netns must
        # delete stale devices before adding, so `ip link add` can't hit EEXIST.
        backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                      tunnel=TUN, keyfn=lambda n: "k")
        seq = ops.ops_only()
        assert seq.index("link_del") < seq.index("wg_add_dev")

    def test_teardown_deletes_init_netns_wg(self, backend, ops):
        # B1: a `wg set` failure leaves the device orphaned in the INIT netns
        # before the move; teardown MUST delete it there too, or the next start
        # hits `ip link add wg-<uid>` -> EEXIST and wedges forever.
        backend.teardown(NS, UID, EgressPolicy.parse("wg:work"), ops)
        init_wg_deletes = [c for c in ops.calls
                           if c == ("link_del", None, wg_ifname(UID))]
        assert init_wg_deletes, "teardown must delete wg in the init netns"

    def test_apply_recovers_from_orphaned_init_device(self, backend, ops):
        # Simulate a prior failed apply that left wg-<uid> in the init netns,
        # then a fresh apply: teardown-stale-first deletes it before re-adding.
        ops.links.add((None, wg_ifname(UID)))     # orphan from a failed move
        backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                      tunnel=TUN, keyfn=lambda n: "k")
        seq = ops.ops_only()
        assert seq.index("link_del") < seq.index("wg_add_dev")


# ---------------------------------------------------------------------------
# reattach (Probe 2 finding 1: reinstall addr+route on link-up)
# ---------------------------------------------------------------------------

class TestReattach:
    def test_reinstalls_addr_and_route(self, backend, ops):
        backend.reattach(NS, UID, EgressPolicy.parse("wg:work"), ops, tunnel=TUN)
        seq = ops.ops_only()
        assert seq == ["addr_add", "link_up", "route_add_default_dev"]

    def test_noop_for_none(self, backend, ops):
        backend.reattach(NS, UID, EgressPolicy.parse("none"), ops)
        assert ops.ops_only() == []


# ---------------------------------------------------------------------------
# link_up_event: the pure parser feeding the reattach watcher (Opt 3-C)
# ---------------------------------------------------------------------------

class TestLinkUpEvent:
    def test_admin_up_for_our_device(self):
        line = "5: wg-2099: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 ..."
        assert link_up_event(line, "wg-2099") is True

    def test_down_is_not_up(self):
        line = "5: wg-2099: <POINTOPOINT,NOARP> mtu 1420 state DOWN ..."
        assert link_up_event(line, "wg-2099") is False

    def test_other_device_ignored(self):
        line = "3: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ..."
        assert link_up_event(line, "wg-2099") is False

    def test_deletion_ignored(self):
        line = "Deleted 5: wg-2099: <POINTOPOINT,UP,LOWER_UP> mtu 1420 ..."
        assert link_up_event(line, "wg-2099") is False

    def test_lower_up_alone_is_not_admin_up(self):
        # LOWER_UP without the bare UP flag must not count (token-exact match).
        line = "5: wg-2099: <POINTOPOINT,NOARP,LOWER_UP> mtu 1420 ..."
        assert link_up_event(line, "wg-2099") is False

    def test_peer_suffix_device_matches(self):
        line = "5: vpeer-2099@if4: <BROADCAST,UP,LOWER_UP> mtu 1500 ..."
        assert link_up_event(line, "vpeer-2099") is True

    def test_blank_or_garbage(self):
        assert link_up_event("", "wg-2099") is False
        assert link_up_event("nonsense", "wg-2099") is False


# ---------------------------------------------------------------------------
# none: dark by construction
# ---------------------------------------------------------------------------

class TestNone:
    def test_no_egress_device_no_route_no_resolver(self, backend, ops):
        res = backend.apply(NS, UID, EgressPolicy.parse("none"), ops)
        assert res.dark and res.mode == "none"
        seq = ops.ops_only()
        assert "wg_add_dev" not in seq and "veth_create" not in seq
        assert "route_add_default_dev" not in seq
        assert "route_add_default_via" not in seq
        # EMPTY resolv (not absent): an absent file makes `ip netns exec` skip
        # the bind-mount, leaking the host's /etc/resolv.conf into the silo.
        assert ops.resolv[NS] == []
        assert ops.skuid_drop[UID] is True            # backstop on


# ---------------------------------------------------------------------------
# direct: per-silo routed /30, no shared bridge (S2)
# ---------------------------------------------------------------------------

class TestDirect:
    def test_routed_veth_with_nat_and_ra_off(self, backend, ops):
        res = backend.apply(NS, UID, EgressPolicy.parse("direct"), ops)
        assert not res.dark and res.mode == "direct"
        seq = ops.ops_only()
        assert "veth_create" in seq
        assert "ipv6_disable" in seq                  # S7: no SLAAC around NAT
        assert seq.count("nat_masquerade") == 1
        host_ip, silo_ip, subnet = eg.direct_addrs(UID)
        # resolver must be reachable from the netns -> the veth host address,
        # never the host's 127.0.0.53 stub.
        assert ops.resolv[NS] == [host_ip]
        assert ops.nat[subnet] is True

    def test_forwarding_and_per_silo_resolver(self, backend, ops):
        # Opt 3-A: direct now enables host forwarding and starts a per-silo
        # resolver pinned to the veth host address.
        backend.apply(NS, UID, EgressPolicy.parse("direct"), ops)
        host_ip, _silo_ip, _subnet = eg.direct_addrs(UID)
        assert ops.ip_forward is True
        assert ops.dns[NS] == (eg.veth_host_ifname(UID), host_ip)

    def test_ip_forward_before_resolver_binds(self, backend, ops):
        # Ordering matters: forwarding on first; the resolver only after the
        # veth host end carries its address (so the bind to host_ip succeeds).
        backend.apply(NS, UID, EgressPolicy.parse("direct"), ops)
        seq = ops.ops_only()
        assert seq.index("enable_ip_forward") < seq.index("veth_create")
        host_ip, _silo_ip, _subnet = eg.direct_addrs(UID)
        addr_i = seq.index("addr_add")               # first addr_add = host end
        assert seq.index("dns_start") > addr_i

    def test_per_silo_subnets_disjoint(self):
        # Two different silos get non-overlapping /30s -> no L2 cross-talk.
        a = eg.direct_addrs(2001)
        b = eg.direct_addrs(2002)
        assert a[2] != b[2]


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------

class TestTeardown:
    def test_removes_devices_backstop_resolver(self, backend, ops):
        backend.apply(NS, UID, EgressPolicy.parse("wg:work"), ops,
                      tunnel=TUN, keyfn=lambda n: "k")
        ops.calls.clear()
        backend.teardown(NS, UID, EgressPolicy.parse("wg:work"), ops)
        seq = ops.ops_only()
        assert "link_del" in seq
        assert ("nft_skuid_drop", UID, False) in ops.calls
        assert "remove_netns_resolv" in seq

    def test_idempotent_on_never_applied(self, backend, ops):
        # Safe to tear down a silo whose egress was never applied.
        backend.teardown(NS, UID, EgressPolicy.parse("none"), ops)
        backend.teardown(NS, UID, None, ops)          # policy unknown
        assert ops.skuid_drop[UID] is False

    def test_direct_teardown_disables_nat_and_stops_resolver(self, backend, ops):
        backend.apply(NS, UID, EgressPolicy.parse("direct"), ops)
        backend.teardown(NS, UID, EgressPolicy.parse("direct"), ops)
        assert ops.nat[eg.direct_addrs(UID)[2]] is False
        assert NS not in ops.dns                       # per-silo dnsmasq stopped

    def test_force_clear_stops_resolver(self, backend, ops):
        # The policy=None force-clear path (used when a legacy silo has stale
        # state) must still stop a surviving per-silo dnsmasq.
        backend.apply(NS, UID, EgressPolicy.parse("direct"), ops)
        backend.teardown(NS, UID, None, ops)
        assert NS not in ops.dns
