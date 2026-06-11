"""Per-silo network-namespace egress backend (interim host implementation).

Honors the silo-facing contract from ``qdistro/doc/networking.md`` — *each silo
sees one default route and one resolver; policy decides where they go* — using
host primitives, before the OpenWrt net VM (task 4) exists.

This module is **pure**: it expresses the exact ``ip``/``wg``/``nft``/veth
sequence as calls on an injected ``ops`` object (the session-manager's
``_SystemOps`` in production, a fake in tests). It owns no I/O of its own and no
global state, so the whole kill-switch sequence is unit-testable headless.

Egress policy (the ``Silo.egress`` field) has four states:

  * ``None`` / absent  -> legacy host networking, **no netns** (backward compat).
                          Handled by the *caller* (session-manager), not here.
  * ``"none"``         -> netns with ``lo`` only. Dark by construction. Default-deny.
  * ``"direct"``       -> per-silo routed /30 veth to the host, NAT-masqueraded,
                          with host IPv4 forwarding, a per-silo resolver pinned to
                          the veth host address, and nft isolation (a ``forward``
                          rule + an ``input`` rule) so a ``direct`` silo reaches
                          the WAN but **not** other silos, the host LAN, or the
                          host's own services — only its resolver port on the
                          gateway. (IPv6 is disabled on the silo veth, so there is
                          no v6 path to filter.)
  * ``"wg:<name>"``    -> per-silo WireGuard tunnel; kill-switch by construction.

Kill-switch by construction (wg): the wg device is created in the **init**
netns, configured, then moved into the silo netns. WireGuard keeps its
encrypted UDP socket in the netns where the device was *created* (init), so
tunnel packets still egress via the host WAN — but inside the silo netns the
only devices are ``wg-<uid>`` + ``lo``. There is no other egress device, so a
tunnel down means no reachable route out: the silo goes dark, with **no firewall
rule needed** for processes inside the netns. DNS is tunnel-bound for the same
reason — any resolver the silo uses is reachable only through the tunnel
(structurally stronger than Probe 2's single-router PBR, which needed an
intra-subnet exception rule).

The ``nft skuid <uid> drop`` backstop applied here is *separate* defense in
depth: it catches any silo-uid process that was launched **outside** the netns
(a bypass), not the in-netns route topology.
"""
from __future__ import annotations

from dataclasses import dataclass
import re as _re

# uid range mirrors qdistro_session_manager.SILO_UID_MIN/MAX; duplicated as a
# module-local constant so this stays import-cycle-free.
_SILO_UID_MIN = 2000

EGRESS_NONE = "none"
EGRESS_DIRECT = "direct"
_WG_PREFIX = "wg:"

# A WireGuard tunnel name crosses into ip/wg argv and a config filename, so it
# is untrusted input — validate it tightly (Probe 1 A1: one bad rule must not
# be smuggled into the privileged path). Lowercase alnum + _/-, must start
# alnum, bounded; no path traversal.
_TUNNEL_NAME_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


class EgressError(ValueError):
    """A malformed egress spec or tunnel config. The session-manager translates
    this to ``BadArgument`` at its boundary."""


class KeyUnavailable(Exception):
    """The tunnel's private key could not be fetched (locked/pre-auth pwd
    vault, or no key provisioned). Signals the backend to bring the silo up
    **dark** — netns present, no wg device — rather than failing the start."""


@dataclass(frozen=True)
class EgressPolicy:
    """A parsed, validated egress policy. Every parsed policy is netns-backed;
    the ``None``/legacy state is represented by the *absence* of a policy and
    is handled by the caller, not this type."""

    mode: str                      # "none" | "direct" | "wg"
    tunnel: str | None = None      # tunnel name iff mode == "wg"

    @classmethod
    def parse(cls, spec: object) -> "EgressPolicy":
        if not isinstance(spec, str) or not spec:
            raise EgressError(f"egress must be a non-empty string, got {spec!r}")
        if spec == EGRESS_NONE:
            return cls(mode="none")
        if spec == EGRESS_DIRECT:
            return cls(mode="direct")
        if spec.startswith(_WG_PREFIX):
            name = spec[len(_WG_PREFIX):]
            if not _TUNNEL_NAME_RE.match(name) or ".." in name:
                raise EgressError(
                    f"egress tunnel name {name!r} is invalid (lowercase "
                    f"alnum/underscore/dash, must start alnum, <=31 chars)")
            return cls(mode="wg", tunnel=name)
        raise EgressError(
            f"egress {spec!r} must be 'none', 'direct', or 'wg:<name>'")

    @property
    def spec(self) -> str:
        if self.mode == "wg":
            return f"{_WG_PREFIX}{self.tunnel}"
        return self.mode


def validate_egress(value: object) -> str | None:
    """Normalise the persisted ``Silo.egress`` field. ``None``/absent means the
    legacy un-managed state (no netns); any other value must parse to a valid
    netns-backed policy. Returns the canonical string (or ``None``).

    All three netns-backed modes — ``none`` (default-deny), ``direct`` (NATed
    WAN egress with a per-silo resolver + silo-isolation forward-filter), and
    ``wg:<name>`` (the silo's own tunnel) — are selectable. ``direct`` was
    de-scoped from the first task-3 landing and is now completed (Opt 3-A
    follow-up): IPv4 forwarding, the dnsmasq-backed per-silo resolver, and the
    nft forward-isolation rule all ship, so the dormant gate is removed."""
    if value is None:
        return None
    return EgressPolicy.parse(value).spec


@dataclass(frozen=True)
class TunnelConfig:
    """Non-secret per-tunnel config (the private key is fetched separately via
    ``keyfn`` so it never lands in this object or on disk in a silo home)."""

    name: str
    peer_public_key: str
    endpoint: str                  # host:port
    address: str                   # silo-side tunnel address, e.g. "10.7.0.2/32"
    dns: str | None = None         # tunnel-side resolver IP
    allowed_ips: str = "0.0.0.0/0, ::/0"
    keepalive: int | None = 25


@dataclass(frozen=True)
class EgressResult:
    """Outcome of an ``apply``. ``dark`` means no egress device is up (``none``,
    or wg with an unavailable key); ``pending`` is a short reason for a
    retriable dark state so the session-manager can record a condition."""

    mode: str
    dark: bool
    pending: str | None = None


# ---- naming (uid-derived, bounded for IFNAMSIZ=16 -> 15 usable chars) -------

def netns_name(silo: str) -> str:
    return f"qd-{silo}"


def wg_ifname(uid: int) -> str:
    return f"wg-{int(uid)}"


def veth_host_ifname(uid: int) -> str:
    return f"veth-{int(uid)}"


def veth_silo_ifname(uid: int) -> str:
    return f"vpeer-{int(uid)}"


def link_up_event(line: str, ifname: str) -> bool:
    """Pure predicate over one ``ip monitor link`` output line: True iff it
    reports ``ifname`` transitioning to admin-UP. Drives the reattach watcher
    (Probe 2 finding 1: a wg link bounce flushes the default route but keeps the
    address, so re-add the route on link-up). Reattach is idempotent + cheap, so
    a borderline match is harmless; the cost of a *missed* up event is only that
    a manual flap won't auto-heal (still fails closed, never open).

    A monitor line looks like ``"<idx>: <ifname>[@peer]: <FLAGS> mtu ..."``; a
    removal is prefixed ``Deleted``. We match the device exactly and require the
    ``UP`` admin flag (not ``LOWER_UP``) to be set."""
    s = line.strip()
    if not s or s.startswith("Deleted"):
        return False
    parts = s.split(":", 2)
    if len(parts) < 3:
        return False
    dev = parts[1].strip().split("@", 1)[0]
    if dev != ifname:
        return False
    flags = parts[2].strip()
    head = flags.split(">", 1)[0].lstrip("<")
    return "UP" in head.split(",")


def direct_addrs(uid: int) -> tuple[str, str, str]:
    """Per-silo routed /30 derived from the uid: (host_ip, silo_ip, subnet/30).

    No shared bridge — each silo gets its own point-to-point /30, so two
    ``direct`` silos cannot reach each other at L2 (preserves Probe 2's
    silo<->silo isolation property). Carved from 10.128.0.0/9 (~8.4M addrs,
    ample for the 2000..60000 uid range at 4 addrs/silo)."""
    idx = int(uid) - _SILO_UID_MIN
    if idx < 0:
        raise EgressError(f"uid {uid} below silo minimum {_SILO_UID_MIN}")
    base = (10 << 24) | (128 << 16)
    net = base + idx * 4
    if net + 3 > (10 << 24) | (255 << 16) | (255 << 8) | 255:
        raise EgressError(f"uid {uid} exhausts the direct-egress /9 pool")

    def _dotted(n: int) -> str:
        return f"{(n >> 24) & 0xff}.{(n >> 16) & 0xff}.{(n >> 8) & 0xff}.{n & 0xff}"

    host_ip = _dotted(net + 1)
    silo_ip = _dotted(net + 2)
    subnet = f"{_dotted(net)}/30"
    return host_ip, silo_ip, subnet


class EgressBackend:
    """Applies/tears down a silo netns's egress per its policy. Stateless: all
    state lives in the kernel (netns, links, nft) and the caller's silo record.
    ``ns`` arguments to ops use ``None`` (or "") to mean the **init** netns."""

    def apply(self, ns: str, uid: int, policy: EgressPolicy, ops,
              *, tunnel: "TunnelConfig | None" = None,
              keyfn=None) -> EgressResult:
        """Bring the silo netns to the requested egress state. Idempotent:
        teardown-stale-first so a re-apply over a half-configured or restart-
        surviving netns can't trip on a leftover device (S4)."""
        self._teardown_devices(ns, uid, ops)
        ops.link_up(ns, "lo")
        if policy.mode == EGRESS_NONE:
            # Dark: no egress device + an EMPTY resolv (not absent — absent
            # would make `ip netns exec` skip the bind-mount and the silo would
            # read the HOST's /etc/resolv.conf, leaking the host's resolver IPs
            # into a default-deny silo).
            ops.write_netns_resolv(ns, [])
            ops.nft_skuid_drop(uid, True)        # backstop for stray processes
            return EgressResult(mode="none", dark=True)
        if policy.mode == EGRESS_DIRECT:
            return self._apply_direct(ns, uid, ops)
        if policy.mode == "wg":
            return self._apply_wg(ns, uid, ops, tunnel, keyfn)
        raise EgressError(f"unknown egress mode {policy.mode!r}")

    def reattach(self, ns: str, uid: int, policy: EgressPolicy, ops,
                 *, tunnel: "TunnelConfig | None" = None) -> None:
        """Reinstall the address + default route on a link-up (Probe 2 finding
        1: a wg link bounce flushes the route, not the address). Idempotent and
        cheap; safe to call on every start/reconfigure.

        Wired (Opt 3-C) to a per-silo ``ip netns exec <ns> ip monitor link``
        watcher (``_SystemOps.start_link_watcher``): the store starts one for
        each non-dark wg silo and calls this on every admin-UP event for the
        ``wg-<uid>`` device (see ``link_up_event``). Inside the netns only root
        holds CAP_NET_ADMIN, so a silo process still cannot bounce the link and
        any flap fails dark (closed); the watcher only auto-heals a route flush
        from a legitimate carrier/admin bounce. The watcher callback re-attaches
        directly via this method (no store lock), so a teardown that stops +
        joins the watcher can never deadlock against an in-flight reattach."""
        if policy.mode == "wg" and tunnel is not None:
            self._bring_up_wg(ns, wg_ifname(uid), tunnel, ops)

    def teardown(self, ns: str, uid: int, policy: "EgressPolicy | None",
                 ops) -> None:
        """Remove all egress devices + the backstop + resolver for this silo.
        Idempotent (safe on a never-applied or partially-applied netns)."""
        self._teardown_devices(ns, uid, ops)       # also stops a stale dnsmasq
        ops.nft_skuid_drop(uid, False)
        if policy is not None and policy.mode == EGRESS_DIRECT:
            # Drop the NAT/forward-isolation enrolment (the @nat_subnets element
            # drives both the masquerade and the isolation drops). The host
            # ip_forward sysctl is left on — harmless and possibly shared.
            ops.nat_masquerade(direct_addrs(uid)[2], False)
        ops.remove_netns_resolv(ns)

    # ---- wg ----------------------------------------------------------------

    def _apply_wg(self, ns, uid, ops, tunnel, keyfn) -> EgressResult:
        if tunnel is None:
            raise EgressError("wg egress requires a tunnel config")
        ifn = wg_ifname(uid)
        # Fetch the private key. A locked/pre-auth vault (autostart before admin
        # unlock, or idle relock) or a missing key must NOT fail the start: come
        # up dark, retriable, with the backstop still in place (B3).
        try:
            key = keyfn(tunnel.name) if keyfn is not None else None
        except KeyUnavailable as e:
            return self._dark_wg(ns, uid, ops, str(e) or "vault-locked")
        if not key:
            return self._dark_wg(ns, uid, ops, "no-key")
        # Born in the INIT netns (None) so WireGuard binds its encrypted socket
        # there; configure BEFORE moving (avoids needing `ip netns exec wg`).
        ops.wg_add_dev(ifn)
        ops.wg_configure(
            ifn, private_key=key, peer_public_key=tunnel.peer_public_key,
            endpoint=tunnel.endpoint, allowed_ips=tunnel.allowed_ips,
            keepalive=tunnel.keepalive)
        ops.link_set_netns(ifn, ns)               # move into the silo netns
        self._bring_up_wg(ns, ifn, tunnel, ops)   # addr + up + default route
        ops.write_netns_resolv(ns, [tunnel.dns] if tunnel.dns else [])
        ops.nft_skuid_drop(uid, True)
        return EgressResult(mode="wg", dark=False)

    def _dark_wg(self, ns, uid, ops, pending) -> EgressResult:
        # A wg silo that can't bring its tunnel up (locked/pre-auth vault, no
        # key) comes up dark + retriable: no egress device, the backstop on,
        # and an EMPTY resolv so it can't fall back to the host resolver or a
        # stale previous-tunnel resolver.
        ops.write_netns_resolv(ns, [])
        ops.nft_skuid_drop(uid, True)
        return EgressResult(mode="wg", dark=True, pending=pending)

    def _bring_up_wg(self, ns, ifn, tunnel, ops) -> None:
        ops.addr_add(ns, ifn, tunnel.address)
        ops.link_up(ns, ifn)
        ops.route_add_default_dev(ns, ifn)

    # ---- direct ------------------------------------------------------------

    def _apply_direct(self, ns, uid, ops) -> EgressResult:
        host_if = veth_host_ifname(uid)
        peer_if = veth_silo_ifname(uid)
        host_ip, silo_ip, subnet = direct_addrs(uid)
        # Host IPv4 forwarding so the routed veth can reach the WAN. The
        # silo<->silo / silo<->LAN exposure this would otherwise open is closed
        # by the nft `fwd` chain (keyed on @nat_subnets), installed by the ops
        # layer's nft scaffold; the silo's enrolment in that set happens via
        # nat_masquerade below, so the forward-isolation drop and the NAT rule
        # turn on together.
        ops.enable_ip_forward()
        ops.veth_create(host_if, peer_if)
        ops.addr_add(None, host_if, f"{host_ip}/30")   # init-netns host end
        ops.link_up(None, host_if)
        ops.link_set_netns(peer_if, ns)
        ops.ipv6_disable(ns, peer_if)                  # no SLAAC around the NAT
        ops.addr_add(ns, peer_if, f"{silo_ip}/30")
        ops.link_up(ns, peer_if)
        ops.route_add_default_via(ns, host_ip)
        ops.nat_masquerade(subnet, True)
        # Per-silo resolver that actually answers: a dnsmasq pinned to the veth
        # host address (the host stub 127.0.0.53 is unreachable from the silo
        # netns's own lo, and DNS to it would otherwise be the one thing the
        # silo could NOT do). Started AFTER the host end has its address so the
        # bind succeeds; the silo's resolv.conf then points at host_ip.
        ops.dns_start(ns, host_if, host_ip)
        ops.write_netns_resolv(ns, [host_ip])
        ops.nft_skuid_drop(uid, True)
        return EgressResult(mode="direct", dark=False)

    # ---- teardown ----------------------------------------------------------

    def _teardown_devices(self, ns, uid, ops) -> None:
        # link_del is idempotent in the ops layer (ignores a missing device),
        # so this is safe over a never-applied or half-applied netns. We must
        # delete the wg device in BOTH the init netns and the silo netns: a
        # `wg set` failure in _apply_wg (e.g. a boot-time endpoint-DNS race)
        # leaves the device orphaned in the INIT netns *before* the move, and a
        # successful apply leaves it in the silo netns. Missing either one wedges
        # the next start on `ip link add wg-<uid>` -> EEXIST.
        ops.link_del(None, wg_ifname(uid))     # init netns (pre-move / failed move)
        ops.link_del(ns, wg_ifname(uid))       # silo netns (post-move)
        ops.link_del(ns, veth_silo_ifname(uid))
        ops.link_del(None, veth_host_ifname(uid))
        # Stop any per-silo `direct` dnsmasq here too — _teardown_devices runs
        # both on teardown AND as apply()'s teardown-stale-first, so an
        # apply(none) over a crashed `direct` silo (policy already flipped) can't
        # leave the old resolver bound to a now-deleted veth address. Idempotent
        # (no pidfile -> no-op).
        ops.dns_stop(ns)
