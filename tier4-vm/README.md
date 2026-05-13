# Tier-4 — whole-VM windowed (libvirt + virt-viewer)

Spec ref: `doc/isolation-tiers.md` ; isolation tier 4 in
`doc/architecture.md`.

## Architecture

```
admin compositor (qdwin) ← outer, running on tty3
 │
 ▼ wayland-1 (UNIX socket, /run/user/1000/wayland-1)
 │
[virt-viewer | remote-viewer] ← Wayland client of outer
 │
 ▼ SPICE protocol
 │
[QEMU] ← libvirt-managed
 └─ guest VM (e.g. tier-4-secdev) ← Linux only per spec/00
```

One **chromed peer toplevel per VM** — same pattern as Tier-2 podman
(one nested-compositor toplevel per container) and Tier-3 waypipe
(one toplevel per silo app). The outer doesn't care that it's a VM
behind virt-viewer; it sees a regular `xdg_toplevel` and treats it
like any other peer.

## Why libvirt + virt-viewer

Picked over SPICE-seamless / weston-rdprail / Looking-Glass / xpra
in `spec/29` . Reasons:

- All packages on Tumbleweed today (libvirt, virt-viewer, qemu-ui-spice).
- Mirrors tier-2 / tier-3 shape: chrome differentiation, broker
 gates, secctx tag (TODO).
- No fork carry; no upstream patch waiting.
- Fallback path documented: `qemu -display gtk,gl=on` direct
 Wayland-toplevel if SPICE bit-rots upstream (currently in
 maintenance mode, see spec/29 risks).

## Files

- `domain-template.xml` — minimal libvirt domain (256MB, 1 vCPU,
 no disk, spice-app display, vdagent for clipboard, agent channel).
 Substitution markers: `__VM_NAME__`, `__MAC__`, `__SPICE_PORT__`.
- `spawn-tier4.sh` — wrapper that defines + starts the domain,
 launches virt-viewer connected to its spice display, manages
 lifecycle. Mirrors `tier3/spawn-tier3.sh` shape.
- `qdistro-tier4-cleanup.sh` — destroy + undefine domain (for
 tests / shutdown).

## Run

```bash
# As root (uses runuser to drop to admin uid for virt-viewer):
qdistro-tier4-spawn tier4-secdev

# Stop:
qdistro-tier4-cleanup tier4-secdev
```

## Limitations of the MVP

- **No real guest OS in the test VM.** The bats smoke uses an
 empty guest that fails to find a bootable medium; the spice
 console shows the firmware boot-failure screen. Sufficient for
 exercising the wrapper + chrome + outer-toplevel integration.
 Real-guest test needs a small Linux disk image (deferred).

## Clipboard (spec/10)

Closed in two layers:

1. **Wayland selection** path (virt-viewer's spice-gtk ↔ host
 `wl_data_device`) is gated by:
 - **Set side:** `wp_security_context_v1` tag from
 `qdistro-secctx-exec` (task 034) → qdshell resolves silo
 `vm-<vm_name>` → broker `CheckClipboardTransfer`.
 - **Receive side:** `qdwin_shell_v1@v14` focus-aware-clear
 (task 037) drops the seat selection on cross-silo focus
 transitions, so a tier-4 virt-viewer can't paste admin's
 clipboard via `wl_data_offer.receive` unless the VM's own
 toplevel has keyboard focus.
2. **SPICE main-channel** (cliprdr / `spice-vdagent`) is **disabled by
 default** via `<clipboard copypaste='no'/>` in the domain XML
 (task 039). All clipboard traffic between host and guest is
 funneled through layer 1.
 - `TIER4_SPICE_CLIPBOARD=allowed` opts in for legacy guests.
 - `<graphics passwd='...'>` carries a per-launch 16-hex SPICE
 ticket. virt-viewer reads it via `virsh domdisplay
 --include-password`. Re-spawning the VM rotates the ticket so
 a previously-learned value can't re-attach later.

Out of scope today: a host-side `spice-vdagentd` shim that gates per
MIME between two vdagent connections in the SAME domain; covered by
`copypaste='no'` for the typical qdistro deployment shape.

## Live guest validation (task 044)

For end-to-end validation of the SPICE main-channel clipboard gate
on a real running guest, build the SPICE-capable base image once:

```bash
sudo bash tier4-vm/build-guest-image.sh
```

This produces `/var/lib/libvirt/images/qdistro-tier4-base.qcow2`
with `spice-vdagent` + `labwc` + `wl-clipboard` preinstalled and
`qemu-guest-agent` enabled. The bats test
`phase7-tier4-spice-clipboard-live` (s54) then boots default + opt-in
variants via per-VM linked clones, asserts qga reachability and
`spice-vdagentd.service active`, and verifies the running domain XML
still carries the configured `copypaste` value.

`spawn-tier4.sh` gained a `TIER4_DISK_BASE=<path>` env knob: when set
+ readable, it creates a per-VM linked clone (qcow2 overlay) under
`TIER4_DISK_DIR` (default `/var/lib/libvirt/images/`) and substitutes
a `<disk>` element into the domain XML. Cleanup deletes the clone on
exit (signal trap or natural).

### In-guest clip-set helper now exercised automatically (task 049)

`s54-tier4-spice-clipboard-live.sh` qga-execs
`/usr/local/bin/qdistro-tier4-clip-set.sh` inside the default-config
guest, then reads its log and asserts `wl-copy exit=0`. This converts
"in-guest endpoint of the clipboard chain works" from manual recipe
to automated proof. The host-side paste observation
(`wl-paste --type text/plain` after a guest copy) still requires
interactive virt-viewer focus and remains manual.

### Automated host-paste cross-check (task 062)

`s54-tier4-spice-clipboard-live.sh` now drives both halves end-to-end
when the spice-glib python binding is present on the host. The Layer-4
addition uses `tier4-vm/qdistro-spice-clipboard-probe.py`
— a headless spice-glib client that connects to the libvirt SPICE
socket directly (no virt-viewer / no host wayland surface required)
and asserts:

- `copypaste='no'` (default): probe sees NO grab within 12 s while
 the in-guest helper runs `wl-copy` for a fresh payload — proves
 spec/10 defense-in-depth is intact at the protocol level.
- `copypaste='yes'` (opt-in): probe receives the grab and the
 payload bytes match what `wl-copy` pushed inside the guest —
 proves the SPICE main-channel actually carries the data when
 admin opts in.

The probe exits 77 (skip) when SpiceClientGLib is missing on the
host, so a host without `typelib-1_0-SpiceClientGLib-2_0` keeps the
existing layers 1-3 green and just tags the layer-4 step as a
runtime skip.

`virsh domdisplay --include-password` is the source of host:port +
ticket; on libvirt builds without `--include-password`, the probe
falls back to passwordless attach (libvirt enforces listen='127.0.0.1'
locally so the listening socket is uid-bounded anyway).

### Manual interactive clipboard cross-check (host wayland-paste)

The protocol-level cross-check above is what the threat model
actually cares about. The host wayland-paste path
(`wl-paste --type text/plain` against the admin compositor's
selection while a virt-viewer surface is focused) remains
observation-grade and stays manual. Recipe to verify by hand:

```bash
# One-time. Without sudo, image lands at
# $HOME/.local/share/libvirt/images/qdistro-tier4-base.qcow2
# (see "Building the SPICE guest image without sudo" below).
sudo bash tier4-vm/build-guest-image.sh

# Default config — guest copies should NOT reach host.
TIER4_DISK_BASE=/var/lib/libvirt/images/qdistro-tier4-base.qcow2 \
 qdistro-tier4-spawn manual-default
# In the virt-viewer window: console-login as root/$QDISTRO_VM_PASSWORD, then
# /usr/local/bin/qdistro-tier4-clip-set.sh "guest-secret"
# On the host:
wl-paste --type text/plain # → should NOT print "guest-secret"

# Opt-in — guest copies SHOULD reach host (when virt-viewer is the
# focused wayland client on the host).
TIER4_DISK_BASE=/var/lib/libvirt/images/qdistro-tier4-base.qcow2 \
TIER4_SPICE_CLIPBOARD=allowed \
 qdistro-tier4-spawn manual-allowed
# Repeat the in-guest set + host wl-paste; this time it should match.
```

Note: the wayland-paste step stays manual because reliable headless
automation of the host-side WAYLAND selection observation (focused
viewer + headless input injection) is its own harness. The
protocol-level coverage from `qdistro-spice-clipboard-probe.py`
already covers the spec/10 contract; a future virt-viewer-focus
harness would only add observation that the wl_seat→cliprdr->wl_seat
round-trip survives the wayland-side bridge.

### Building the SPICE guest image without sudo (task 049)

`build-guest-image.sh` now accepts non-root invocation when `--dest`
points at a directory the calling user can write to. Default dest
remains `/var/lib/libvirt/images/qdistro-tier4-base.qcow2` (sudo
required for that path); for ad-hoc developer builds:

```bash
mkdir -p ~/.local/share/libvirt/images
bash tier4-vm/build-guest-image.sh \
 --dest ~/.local/share/libvirt/images/qdistro-tier4-base.qcow2
```

The bats test reads `$TIER4_DISK_BASE`; pointing it at the user-mode
location lets the test run without root.

## Linux-only

Per `spec/00` qdistro is a single-tenant Linux workstation. Tier-4
guests are **Linux only** — Windows-guest support was explicitly
dropped 2026-04-27 (memory `qdistro_linux_only.md`,
`spec/29` ).
