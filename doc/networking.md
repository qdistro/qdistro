# Networking (net VM + per-silo VPN)

Status: design (target state). Three states, documented separately:

1. **Current** — admin's NetworkManager owns the network
   ([architecture.md](architecture.md)); per-user network namespaces
   provide silo separation.
2. **Interim** — the per-silo netns contract below, on the host.
   Ships per-silo VPN before any VM exists.
3. **Target** — the OpenWrt net VM described here.

> **v1 ships state 2 (interim host-netns backend).** The OpenWrt net VM
> (state 3) is post-v1. What the interim backend does **not** protect against
> is stated honestly under [The silo-facing contract](#the-silo-facing-contract):
> Wi-Fi (802.11) parsing, DHCP/DNS handling, and netfilter correctness all
> remain on the host kernel and root daemons until the net VM lands. v1's
> network promise is the silo-facing contract (one route, one resolver,
> per-silo policy) and the per-silo-VPN kill-switch — not host-stack isolation.

Implementation is planned in `todo/fable-networking` (umbrella
tracker), starting with research probes into Qubes sys-net and
OpenWrt under KVM. Claims below marked *(probe)* are design
assumptions to be verified there, not established facts.

## Motivation

- **802.11 and the netstack edge are hostile-input surfaces** parsed
  today by the host kernel and root daemons. A device silo
  ([device-silos.md](device-silos.md)) relocates that parsing into a
  disposable guest.
- **Firewall correctness bugs are a class of their own.** Netfilter
  bugs like the iptables `!` rule mismatch are not memory corruption
  — the rule silently does not say what its author thinks. Host
  hardening cannot fix that class; a dedicated, declarative,
  testable firewall image can at least make it inspectable.
- **Per-silo VPN is a product feature**, not only a security one:
  each silo can have its own tunnel, resolver, and egress policy.

## Architecture

```
 host                                      net VM (OpenWrt x86_64)
 +-------------------------------+         +------------------------+
 | silo A ── vif/VLAN A ─────────┼────────▶| fw4 (default-deny)     |
 | silo B ── vif/VLAN B ─────────┼────────▶|  ├─ PBR: A → wg-A      |
 | admin  ── vif/VLAN adm ───────┼────────▶|  ├─ PBR: B → wg-B      |
 |                               |         |  └─ dnsmasq per VLAN   |
 | admin app ◀── ubus JSON-RPC ──┼─ vsock ─| rpcd/ubus              |
 |               (control plane) |         | hostapd/wpa_supplicant |
 +-------------------------------+         +───────────┬────────────+
                                                       │ USB redirect /
                                                       │ PCI passthrough
                                                  Wi-Fi NIC / ethernet
```

The host sees only virtio-net interfaces. All 802.11 handling,
DHCP, DNS forwarding, firewalling, and tunnel termination live in
the VM. The NIC enters the VM via PCI passthrough (the proven path
for internal NICs — what Qubes sys-net uses) or USB redirection for
dongles, which is the riskier, less-proven path *(probe)*.

### Topology: one VM, a deliberate deviation from Qubes

Qubes splits this into two qubes — sys-net (hardware-facing,
considered contaminated) and sys-firewall (policy enforcement) —
and warns against attaching clients directly to the hardware-facing
qube. The design here combines both roles in one OpenWrt VM: on a
single-tenant workstation the firewall protects silos from each
other and the WAN, not from a hostile co-tenant, so the simpler
topology is acceptable. The Qubes research probe
(`todo/fable-networking/01-RESULTS.md`) settled this: **keep one
VM.** None of Qubes' documented reasons for the split (it warns
against running a VPN/DNS/IPS service in the same qube as the
firewall) defends against a hostile co-tenant, which we do not
have. The one surviving risk is firewall-policy integrity against a
compromised *in-VM* network service: if the WireGuard stack is
compromised it could in principle rewrite per-silo policy. We accept
this because the fw4 ruleset is regenerated from host-side
declarative sources on every boot (so in-VM tampering does not
persist) and the VPN stack is ours, built, not user-supplied.
**Trip-wire to split later:** if we ever run a user-supplied/third-
party VPN stack, or compile *silo-supplied* firewall rules inside
the VM, move the network service into its own VM so a compromise
there cannot rewrite policy. The load-bearing Qubes lesson is not
the VM count but the enforcement *location*: per-silo rules are
enforced in the net VM, never in the silo, fail closed, and validate
untrusted rule input (one bad rule once DoS'd every qube behind a
Qubes net VM).

## Why OpenWrt, not a hand-rolled routing VM

- **Purpose-built integration.** hostapd/wpa_supplicant, fw4
  (nftables), dnsmasq, WireGuard, VLANs — assembled and shipped
  together for two decades. A generic distro VM with NetworkManager
  plus nftables plus WireGuard is the same pile, maintained by us
  alone.
- **UCI is agent-legible.** The whole network config is declarative
  plain text under `/etc/config/` — squarely inside the
  modifiable-source doctrine ([overview.md](overview.md)). An LLM
  can read and edit a UCI file confidently; reverse-engineering
  NetworkManager D-Bus state, it cannot.
- **A scriptable control plane exists**: ubus JSON-RPC via rpcd,
  plus LuCI as a human escape hatch. ubus itself is local IPC —
  remoting it needs a bridge: rpcd's JSON-RPC over HTTP on a
  host-only management vif, or a vsock bridge in the style of the
  print VM's socat *(probe decides the transport)*. Whichever
  lands: the admin app speaks a size-capped structured envelope per
  the TCB parsing rule ([threat-model.md](threat-model.md)), the
  bridge carries auth/ACLs, and no management port is reachable
  from silo networks.
- **Precedent**: qubes-mirage-firewall replaces Qubes' Linux
  firewall VM with a special-purpose unikernel for the same reason —
  a purpose-built network OS over a general-purpose distro.

## Per-silo VPN

- Each silo gets its own virtio vif (or VLAN) into the net VM.
- OpenWrt policy-based routing maps source interface → WireGuard
  tunnel, with fw4 default-deny so silo X can egress **only** via
  tunnel X. The **target invariant** is kill-switch semantics:
  tunnel down ⇒ silo dark, never a silent fallback to raw WAN.
  Stated as a target, not "by construction": it must be
  demonstrated across IPv4, IPv6 (incl. router advertisements),
  DNS, tunnel link loss, service reload, and config error before
  it is trusted *(probe)*.
- Per-silo DNS: one dnsmasq instance per VLAN, forwarding to that
  tunnel's resolver. No cross-silo resolver leaks.
- Direct (no-VPN) egress is just another named route a silo's policy
  may select; the default for a new silo is deny-all until policy
  names an egress.

## The silo-facing contract

Stable across implementations: **each silo sees one default route
and one resolver; policy decides where they go.**

The interim implementation honours the same contract without the
VM: per-silo network namespaces on the host, with WireGuard's
native netns support placing each silo's tunnel inside its
namespace. It is implemented as a per-silo `egress` policy on the
session manager (`SetSiloEgress`, admin-only, audited): `none`
(netns up, dark — default-deny), `direct` (per-silo routed veth +
NAT), or `wg:<name>` (the silo's own tunnel). A silo with no egress
policy keeps today's legacy host networking (no netns) for backward
compatibility. The kill-switch for a `wg:` silo is *by construction*:
the wg device is born in the init netns, moved into the silo netns,
which then holds only `wg` + `lo` — no other egress device exists, so
tunnel-down means no fallback route, and any resolver the silo uses is
reachable only through the tunnel. See `todo/fable-networking/03-…`.

When the net VM lands, the silo-facing shape is preserved — one
default route, one resolver, both reconfigured by the infra owner
across a restart — but it is not a literal byte-identical swap: a
`wg:` silo's inside device is the tunnel itself (`wg-<uid>`), not a
veth, so the net-VM cutover replaces the device kind, the silo's
address, and the resolver IP. A `direct` silo's veth does re-parent
to a vif more directly. Either way the change is on the infra side;
the *contract* the silo sees does not.

Threat delta, stated honestly: the interim path delivers the
*contract* and the per-silo-VPN *feature*, but relocates nothing —
Wi-Fi parsing, DHCP/DNS handling, and netfilter correctness risk
all remain on the host kernel until the net VM lands.

## Escape hatches

The tty1-agetty pattern applied to networking:

- One host-claimable ethernet path stays available for recovery
  when the net VM is wedged or its image is broken. Its ownership
  rule is strict: **normally down, admin-only, never forwarded to
  silo netns**, audited when enabled, and disabled again when
  recovery ends. A recovery path that silos can route through is a
  bypass of the net VM, not an escape hatch.
- Recovery from compromise or misconfiguration is **rebuild from
  image**: the net VM holds no durable state and its config is
  regenerated from host-side declarative sources.

## Caveats (deliberate trade-offs and open risks)

- **OpenWrt is infrastructure, not a roaming laptop client.** Wi-Fi
  picking, captive portals, and WPA-Enterprise prompts are clunkier
  than NetworkManager. Acceptable on a stationary workstation;
  revisit if qdistro ever targets laptops.
- **Client-mode unknowns to retire by probe**: MAC randomization,
  regulatory-domain handling, WPA3/802.11w support per dongle
  chipset, USB dongle firmware packaging in the image, hotplug
  races on re-plug, and NetworkManager coexistence during the
  migration window.
- **OpenWrt's kernel lags upstream.** The image is rebuilt on a
  schedule (same machinery as other device-silo images), not
  patched in place.
- **Suspend/resume with passthrough hardware is fragile.** A
  stationary workstation rarely suspends; the design accepts a
  net-VM restart on resume rather than engineering around it.

## VM definition exception

[vm-definitions.md](vm-definitions.md) prescribes NixOS for new
VM-backed images unless there is a written reason not to. The net
VM is that exception, and this is the written reason: the point of
choosing OpenWrt *is* its integrated UCI/fw4/hostapd/ubus stack —
rebuilding that on NixOS would re-create the hand-rolled routing VM
this design rejects. The exception changes nothing else: the image
still carries the required lineage (package list, config files,
checksums) and a declared runtime policy.
