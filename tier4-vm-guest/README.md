# tier4-vm-guest — nested-qdwin guest image (P10)

This directory ships the **guest-side** half of the SPICE-retirement
migration for tier-4 VMs. Sibling `tier4-vm/` (the SPICE path) stays
in place until P11 swaps the default; P10 is purely additive.

## Files

- `qdistro-tier4-publisher.sh` — guest-side publisher script. Runs
  inside the booted guest under systemd. Binds `qdwin-bystander
  --connect wayland-0 --forward-all-toplevels` and wraps it with
  `waypipe --vsock -s 2:$PORT server` so toplevels from the in-guest
  nested qdwin reach the host's waypipe-client.
- `build-guest-image.sh` — bakes `qdistro-tier4-guest.qcow2`: minimal
  Tumbleweed + libweston + qdwin (compiled with `meson -Drole=guest`)
  + waypipe + weston-terminal + alsa-utils + qemu-guest-agent +
  systemd units for qdwin-guest-session and qdistro-tier4-publisher
  + /etc/fstab line for the virtiofs `/host` mount.
- `domain-template.xml` — libvirt domain XML forked from
  `tier4-vm/domain-template.xml`. SPICE channels removed; vsock,
  virtio-snd, virtiofs added.

## Architecture

See `plan2/research/spice-retirement/00-overview.md`. tl;dr: a
stripped-down qdwin runs inside the guest VM, presenting a normal
`wayland-0` socket for in-guest clients (weston-terminal in the smoke
test). The publisher wraps the bystander with waypipe-server over
vsock, and the host's waypipe-client receives the toplevel stream and
attaches it to outer xdg_toplevels on the host's full-fat qdwin.

## P10 scope vs P11

P10 lands this directory. **P10 does not touch `tier4-vm/spawn-tier4.sh`,
`tier4-vm/domain-template.xml`, or any other file outside this
directory.** P11 wires `spawn-tier4` to use this image behind a
`TIER4_DISPLAY=waypipe` flag.
