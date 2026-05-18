# tier4-vm-guest — nested-qdwin guest image (P10)

This directory ships the **guest-side** half of the nested-qdwin
waypipe migration for tier-4 VMs. `tier4-vm/` now consumes this image
and template by default.

## Files

- `qdistro-tier4-publisher.sh` — guest-side publisher script. Runs
  inside the booted guest under systemd. Binds `qdwin-bystander
  --connect wayland-0 --forward-all-toplevels` and wraps it with
  `waypipe --vsock -s 2:$PORT server` so toplevels from the in-guest
  nested qdwin reach the host's waypipe-client. With
  `QDISTRO_TIER4_STREAMING_METHOD=rdp`, it instead requests a qdwin RDP
  view stream and bridges the guest TCP RDP endpoint to AF_VSOCK with
  `socat`.
- `build-guest-image.sh` — bakes `qdistro-tier4-guest.qcow2`: minimal
  Tumbleweed + libweston + qdwin (compiled with `meson -Drole=guest`)
  + waypipe + weston-terminal + alsa-utils + qemu-guest-agent +
  RDP runtime dependencies (`socat`, PipeWire/FreeRDP libraries, and
  `qdistro-forward`) + systemd units for qdwin-guest-session and
  qdistro-tier4-publisher + /etc/fstab line for the virtiofs `/host`
  mount. The bake verifies that `socat` and `/usr/bin/qdistro-forward`
  are present in the image.
- `tier4-publisher.conf.example` — guest publisher config for the RDP
  mode. Copy the keys into `/etc/qdistro/tier4-publisher.conf` or inject
  equivalent environment when starting the publisher.
- `domain-template.xml` — libvirt domain XML for the waypipe guest:
  vsock, virtio-snd, virtiofs, and no guest display-agent channel.

## Architecture

See `plan2/tasks/P10-tier4-guest-image-nested-qdwin.md` and
`plan2/tasks/P11-tier4-waypipe-migration.md`. tl;dr: a stripped-down
qdwin runs inside the guest VM, presenting a normal
`wayland-0` socket for in-guest clients (weston-terminal in the smoke
test). The publisher wraps the bystander with waypipe-server over
vsock, and the host's waypipe-client receives the toplevel stream and
attaches it to outer xdg_toplevels on the host's full-fat qdwin.

For RDP mode, set `TIER4_STREAMING_METHOD=rdp` on the host launcher. The
launcher starts the guest publisher with `QDISTRO_TIER4_DISPLAY=rdp` and
passes the subscription selector. The guest publisher writes credentials
to `/run/qdistro-tier4-rdp.env`; the host reads that file over qga, opens
a local `socat` bridge, and launches FreeRDP against `127.0.0.1`.

Inside the guest, persistent publisher settings live at
`/etc/qdistro/tier4-publisher.conf`:

```sh
QDISTRO_TIER4_STREAMING_METHOD=rdp
QDISTRO_TIER4_RDP_SUBSCRIBE=last
QDISTRO_TIER4_RDP_PEER_LABEL=tier4-rdp
QDISTRO_TIER4_RDP_CREDS=/run/qdistro-tier4-rdp.env
```

## Historical scope

P10 originally landed this directory for the waypipe guest image. Later
Tier-4 work wires `spawn-tier4` to this image and adds the explicit RDP
transport selected with `TIER4_STREAMING_METHOD=rdp`.
