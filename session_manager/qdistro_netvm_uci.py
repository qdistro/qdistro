"""Egress-policy -> OpenWrt UCI compiler for the net-VM backend (task 4).

This is the *second* backend for the same silo egress contract the interim
host-netns backend (``qdistro_silo_egress``) already implements: **each silo
sees one default route and one resolver; policy decides where they go.** Where
the interim backend emits ``ip``/``wg``/``nft`` calls on the host, this compiles
the broker-approved per-silo ``EgressPolicy`` into the declarative OpenWrt UCI
fragments (PBR + fw4 + per-VLAN dnsmasq) the net VM consumes — *the same policy
store, a different rendering target* (task 4 piece 4).

It is **pure**: ``compile_silo`` is a deterministic function from a
``SiloNet`` spec to UCI text, with no I/O. The control-plane client (task 4
piece 3) ships the rendered fragments to the VM over the Probe-2-chosen
transport (rpcd JSON-RPC on the management vif); that lives elsewhere. Keeping
the compiler pure makes the whole policy->config mapping golden-file testable
headless, exactly like the interim backend's unit tests.

Design faithful to ``02-RESULTS.md`` (the OpenWrt-under-KVM probe):

  * **Per-silo L3 net** on its own virtio NIC: silo ``i`` is ``silo<i>`` on
    ``10.10.<i+1>.0/24`` with the router at ``.1`` (matches the probe's
    ``10.10.1.0/24`` for the first silo).
  * **Kill-switch by construction at fw4**: every silo zone is ``forward
    REJECT`` by default; a ``wg:`` silo gets a single forwarding to the ``vpn``
    zone (the wg interface) and *nothing* to ``wan``. Tunnel down => the wg
    interface is down => no route => silo dark, never a WAN fallback. This is
    the net-VM analogue of the interim backend's "only wg+lo in the netns".
  * **PBR** sends the silo's subnet out its tunnel's table (``ip rule from
    <subnet> lookup <table>``), with the **intra-subnet exception** the probe's
    finding 1 demands installed at a *higher* priority (``from <subnet> to
    <subnet> lookup main``) so silo<->router (DHCP/DNS) traffic is not sucked
    into the tunnel table. Finding 2 (a link bounce flushes the table's default
    route) is handled at runtime by the control-plane reload, not the static
    config — noted on ``table_route``.
  * **Per-VLAN dnsmasq**: one resolver instance bound to the silo interface,
    forwarding to the tunnel's DNS (wg) or a configured upstream (direct). No
    cross-silo resolver leak.

The three policy modes map to:

  * ``none``   -> zone with NO forwarding (default-deny dark); resolver still
                 answers locally so the silo gets one resolver, but it resolves
                 nothing reachable. Mirrors the interim ``none`` dark state.
  * ``direct`` -> forwarding to ``wan``; resolver upstream is the WAN resolver.
  * ``wg:<n>`` -> a wireguard interface + peer, PBR to its table, forwarding to
                 the ``vpn`` zone only, resolver upstream is the tunnel DNS.
"""
from __future__ import annotations

from dataclasses import dataclass

from qdistro_silo_egress import EgressPolicy, EgressError

# Base subnet block for per-silo L3 nets (matches Probe 2's 10.10.x.0/24).
_SUBNET_PREFIX = "10.10"
# Routing-table + ip-rule priority bases. The intra-subnet exception MUST sit
# at a lower (higher-priority) number than the tunnel rule so it wins.
_TABLE_BASE = 100
_RULE_PRIO_LOCAL = 1000        # intra-subnet -> main (finding 1)
_RULE_PRIO_TUNNEL = 2000       # subnet -> tunnel table
# Zone names declared by the image (not by this compiler): the WAN uplink and
# the umbrella zone the wireguard interfaces live in.
_WAN_ZONE = "wan"
_VPN_ZONE = "vpn"


@dataclass(frozen=True)
class SiloNet:
    """A silo's net-VM-side egress spec. ``index`` is the stable per-silo slot
    (0..N) that derives the interface, subnet, router IP, and routing table —
    assigned by the caller (the egress policy store), never by the silo."""

    name: str
    index: int
    policy: EgressPolicy
    # wg fields (required iff policy.mode == "wg"). The PRIVATE key is never in
    # this spec or in UCI: the net VM holds it (provisioned out of band) or the
    # control-plane injects it; only the non-secret peer/endpoint/address/dns
    # are config. ``wg_address`` is the ROUTER-side tunnel address.
    wg_peer_public_key: str | None = None
    wg_endpoint: str | None = None          # host:port
    wg_address: str | None = None           # e.g. "10.200.0.1/24"
    wg_dns: str | None = None               # tunnel-side resolver
    # direct upstream resolver (mode == "direct"); defaults to the VM's WAN
    # resolver if unset.
    direct_dns: str = "1.1.1.1"

    def __post_init__(self) -> None:
        if self.index < 0:
            raise EgressError(f"silo index {self.index} must be >= 0")
        if self.index > 244:
            # 10.10.<index+1>.0/24 must stay a valid third octet (<=255).
            raise EgressError(f"silo index {self.index} exhausts the /16 block")
        if self.policy.mode == "wg":
            for field in ("wg_peer_public_key", "wg_endpoint", "wg_address"):
                if not getattr(self, field):
                    raise EgressError(
                        f"wg silo {self.name!r} missing {field}")

    # ---- derived, deterministic ------------------------------------------
    @property
    def ifname(self) -> str:
        return f"silo{self.index}"

    @property
    def octet(self) -> int:
        return self.index + 1

    @property
    def subnet(self) -> str:
        return f"{_SUBNET_PREFIX}.{self.octet}.0/24"

    @property
    def router_ip(self) -> str:
        return f"{_SUBNET_PREFIX}.{self.octet}.1"

    @property
    def table(self) -> int:
        return _TABLE_BASE + self.index

    @property
    def wg_ifname(self) -> str:
        return f"wg_{self.name}"

    @property
    def zone(self) -> str:
        return f"silo{self.index}"


def _section(kind: str, name: str | None, options: list[tuple[str, str]],
             lists: list[tuple[str, list[str]]] | None = None) -> str:
    """Render one UCI ``config`` section deterministically."""
    head = f"config {kind}" + (f" '{name}'" if name else "")
    lines = [head]
    for k, v in options:
        lines.append(f"\toption {k} '{v}'")
    for k, vs in (lists or []):
        for v in vs:
            lines.append(f"\tlist {k} '{v}'")
    return "\n".join(lines) + "\n"


def compile_network(s: SiloNet) -> str:
    """``/etc/config/network`` fragments: the wg interface (wg only), the PBR
    rules, and the tunnel table route (wg only)."""
    out: list[str] = []
    if s.policy.mode == "wg":
        out.append(_section(
            "interface", s.wg_ifname,
            [("proto", "wireguard")],
            [("addresses", [s.wg_address or ""])],   # list option on OpenWrt
        ))
        out.append(_section(
            f"wireguard_{s.wg_ifname}", None,
            [("public_key", s.wg_peer_public_key or ""),
             ("endpoint_host", (s.wg_endpoint or "").rsplit(":", 1)[0]),
             ("endpoint_port", (s.wg_endpoint or ":").rsplit(":", 1)[1]),
             ("route_allowed_ips", "0"),     # PBR owns routing, not wg's own
             ("persistent_keepalive", "25")],
            [("allowed_ips", ["0.0.0.0/0"])],
        ))
        # Intra-subnet exception FIRST (finding 1): silo<->router stays on main.
        out.append(_section(
            "rule", None,
            [("src", s.subnet), ("dest", s.subnet),
             ("lookup", "main"),
             ("priority", str(_RULE_PRIO_LOCAL + s.index))],
        ))
        # Then the tunnel rule: everything else from the subnet -> its table.
        out.append(_section(
            "rule", None,
            [("src", s.subnet), ("lookup", str(s.table)),
             ("priority", str(_RULE_PRIO_TUNNEL + s.index))],
        ))
        # Default route in the tunnel's table. NOTE: a wg link bounce flushes
        # THIS route (Probe 2 finding 2) — the control-plane reattaches it on
        # link-up; the static config seeds it.
        out.append(_section(
            "route", None,
            [("interface", s.wg_ifname), ("table", str(s.table)),
             ("target", "0.0.0.0/0")],
        ))
    return "".join(out)


def compile_firewall(s: SiloNet) -> str:
    """``/etc/config/firewall`` fragments: the silo zone (default-deny forward =
    kill-switch) + its single allowed forwarding, and the input allows for the
    silo's own DHCP/DNS to the router."""
    out = [_section(
        "zone", None,
        [("name", s.zone), ("input", "REJECT"), ("output", "ACCEPT"),
         ("forward", "REJECT")],            # default-deny: the kill-switch
        [("network", [s.ifname])],
    )]
    # Let the silo reach ONLY the router's DHCP+DNS on its own interface.
    out.append(_section(
        "rule", None,
        [("name", f"{s.zone}-dhcp-dns"), ("src", s.zone),
         ("dest_port", "53 67"), ("target", "ACCEPT")],
    ))
    if s.policy.mode == "direct":
        out.append(_section(
            "forwarding", None, [("src", s.zone), ("dest", _WAN_ZONE)]))
    elif s.policy.mode == "wg":
        # ONLY to the vpn zone (the wg interfaces). Never to wan. Tunnel down =>
        # wg iface down => this forwarding leads nowhere => dark.
        out.append(_section(
            "forwarding", None, [("src", s.zone), ("dest", _VPN_ZONE)]))
    # mode == "none": no forwarding at all (default-deny dark).
    return "".join(out)


def compile_dhcp(s: SiloNet) -> str:
    """``/etc/config/dhcp`` fragments: a per-silo dnsmasq instance bound to the
    silo interface, with its upstream set by policy (tunnel DNS for wg, the
    configured upstream for direct, none for a dark silo). One instance per silo
    => no cross-silo resolver leak.

    The dnsmasq instance and the dhcp pool MUST NOT share a UCI section name:
    they are different section types, and UCI rejects two same-named sections of
    different type in one package ("section of different type overwrites prior
    section with same name") — a parse error that bricks the whole ``dhcp``
    package. The dhcp pool keeps the silo's interface name (``silo<i>``); the
    dnsmasq instance is suffixed ``_dns`` and the pool references it via
    ``option instance``. (Found applying real output to live ``uci``; the pure
    golden test never fed the text to a parser.)"""
    instance = f"silo{s.index}_dns"
    servers: list[str] = []
    if s.policy.mode == "wg" and s.wg_dns:
        servers = [s.wg_dns]
    elif s.policy.mode == "direct":
        servers = [s.direct_dns]
    # mode == "none": no upstream — the resolver answers (so the silo has one
    # resolver) but forwards nothing, matching the interim dark state.
    #
    # `noresolv 1` on EVERY instance: a per-silo resolver must use ONLY its
    # explicit policy upstream (the tunnel DNS / configured upstream), never the
    # VM's /etc/resolv.conf — otherwise a wg silo's DNS could leak to the WAN
    # resolver, breaking the no-cross-leak + kill-switch contract. `interface`
    # is a list option on OpenWrt.
    opts = [("localuse", "0"), ("rebind_protection", "1"), ("noresolv", "1")]
    section_lists = [("interface", [s.ifname])]
    if servers:
        section_lists.append(("server", servers))
    out = [_section("dnsmasq", instance, opts, section_lists)]
    out.append(_section(
        "dhcp", s.ifname,
        [("interface", s.ifname), ("instance", instance),
         ("start", "100"), ("limit", "150"), ("leasetime", "12h")],
    ))
    return "".join(out)


def compile_silo(s: SiloNet) -> dict[str, str]:
    """Compile one silo's egress policy into its UCI fragments, keyed by the
    target ``/etc/config/<file>``. Deterministic; golden-file tested."""
    return {
        "network": compile_network(s),
        "firewall": compile_firewall(s),
        "dhcp": compile_dhcp(s),
    }


def compile_all(silos: list[SiloNet]) -> dict[str, str]:
    """Merge every silo's fragments per config file, in stable index order — the
    declarative overlay the image pipeline (task 4 piece 1) lays over the
    read-only base image on each rebuild."""
    merged: dict[str, list[str]] = {"network": [], "firewall": [], "dhcp": []}
    for s in sorted(silos, key=lambda x: x.index):
        frags = compile_silo(s)
        for f in merged:
            if frags[f]:
                merged[f].append(frags[f])
    return {f: "".join(parts) for f, parts in merged.items()}
