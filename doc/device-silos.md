# Device silos (hardware management in VMs)

## Motivation

The host Linux kernel API is not a secure surface (see
[threat-model.md](threat-model.md)). Hardware is where hostile input
enters the machine: 802.11 frames, Bluetooth GATT, USB descriptors,
IPP from the network. On a single-kernel system that input is parsed
by host kernel drivers and root daemons, and no uid or SELinux
boundary changes that — identity separation limits *authority*, it
does not move *parsing*.

A **device silo** moves the parsing: a dedicated VM owns a piece of
hardware (via passthrough or redirection) and publishes a narrow,
high-level protocol to the host. The hostile input is then parsed by
the guest's kernel and daemons, and a compromise is bounded to a
rebuildable image. The CUPS VM ([printing.md](printing.md)) is the
first instance of this pattern; this doc states the general doctrine.

## The three properties

Every device silo has all three:

1. **No durable user state, by construction.** Read-only OS image
   plus tmpfs scratch. Job payloads (a print job, a scan, network
   packets) *transit* the VM, but nothing user-owned persists there
   — no secrets, credentials, or long-lived silo storage. Compromise
   is recovered by rebuilding the image — the same "empty rooms"
   reasoning as template candidate builds
   ([templates.md](templates.md)).

2. **The seam is the highest-level interface available.** In
   preference order:

   | Seam | Example | Why prefer it |
   |------|---------|---------------|
   | Semantic protocol | IPP (print), eSCL/AirScan (scan) | Narrow meaning; mockable in headless tests; agent-legible |
   | Virtual device class | virtio-net (network) | Host keeps virtio/bridge plumbing, but radio, supplicant, DHCP/DNS, firewall parsing all move to the guest |
   | Bus-level redirection | usbredir for a single USB device | Device-scoped; no IOMMU dependency |
   | Raw passthrough | VFIO PCI/USB-controller passthrough | Only when nothing narrower exists |

   A narrow semantic seam is also the cheap-verification answer: the
   host-side proxy is tested against a fake protocol server in
   milliseconds, with no VM in the loop. A virtual device class is
   not semantic the way IPP is — the win there is *what moves off
   the host*, not the narrowness of the seam itself.

3. **Explicit network policy and lifecycle.** Each device silo
   declares what network it gets (vsock-only, dedicated VLAN, or
   none) and whether it is on-demand or always-on. On-demand silos
   (print) cost nothing between uses; only always-on silos (network)
   pay standing RAM.

## Picking the rung

uids separate **authority**, SELinux domains confine **code**, VMs
relocate **kernel attack surface**. Pick the cheapest rung that
defeats the threat:

- A hardware-control *uid* (e.g. a dedicated identity allowed to
  invoke `org.qdistro.network.*`) scopes who may ask. It does not
  protect against the hardware's input.
- A SELinux domain plus systemd sandboxing on a host daemon confines
  what its code can do after compromise. Zero RAM; does not move the
  parsing either.
- A device silo is the only rung that moves driver and daemon parsing
  off the host. It is justified where the input is genuinely hostile,
  not by symmetry.

## Candidates, ranked by hostile-input exposure

| Hardware | Verdict | Notes |
|----------|---------|-------|
| Print (CUPS) | in flight | cups-browsed RCE history; pattern validator; [printing.md](printing.md) |
| Network (Wi-Fi/eth) | next | 802.11 parsing, wpa_supplicant; highest value; [networking.md](networking.md) |
| Scanner (SANE) | with print | eSCL/AirScan seam; SANE backends stay in the VM |
| Bluetooth | later | bluez GATT parsing is hostile-facing; audio profiles drag latency into the seam |
| USB (wholesale) | last / maybe never | Input devices are USB; IOMMU group granularity; interim is usbguard on host + per-device usbredir |
| Audio / camera | stays on host | Latency-sensitive; input is local devices, not radio |

## What this is not

- Not multi-tenancy and not a general app tier — app isolation is the
  [isolation ladder](isolation-tiers.md). A device silo is
  infrastructure with one job.
- Not a reason to weaken host hardening. Host daemons that remain
  (PipeWire, fprintd) still get SELinux confinement and systemd
  sandboxing.
