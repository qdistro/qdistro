# Tier-4 — whole-VM windowed (libvirt + waypipe/RDP)

```
host: qdwin (outer Wayland compositor)
  │
  ├─ waypipe --vsock client vsock://CID:7879
  │    └─ outer xdg_toplevel per VM (one per VM by default)
  │         ├─ Chrome frame (SSD chrome, close button)
  │         └─ secctx tag: qdistro.tier4.<vm-name>
  │
  ▼ AF_VSOCK (vhost-vsock)

guest: qdwin-stripped (nested Wayland compositor)
  ├─ waypipe server (listens on vsock port 7879)
  ├─ wl_clients (guest apps: Firefox, etc.)
  └─ clipboard / input via Wayland protocol
```

Tier-4 runs a full guest VM (KVM + libvirt) and exposes it as one or more
`xdg_toplevel` windows on the host compositor. The default path uses
**waypipe over AF_VSOCK** to carry the Wayland protocol from a stripped
qdwin running inside the guest. The RDP path can be selected with
`TIER4_STREAMING_METHOD=rdp`; it asks the guest publisher to subscribe to
the qdwin view stream, run `qdistro-forward`, bridge the resulting guest
TCP endpoint over vsock with `socat`, and launch a host FreeRDP client.
Each toplevel is security-context-tagged (`qdistro.tier4.<vm-name>`) so
the host clipboard gate (`ClipboardGate.qml`) and chrome system know
which VM owns which window.

## Why libvirt + waypipe/RDP

Tier-4's isolation boundary is the VM itself. Waypipe carries the
Wayland display protocol over vsock from a guest compositor to the host,
making the guest's application windows appear as native host toplevels.
RDP mode is available for the qdwin view-stream path where a FreeRDP
viewer is the transport endpoint.

- **Strong isolation:** Full KVM VM boundary; guest cannot see host
  processes, memory, or other guests.
- **Wayland-native:** waypipe preserves the Wayland protocol end-to-end;
  clipboard, input, and surfaces ride the same socket.
- **Security context:** Each waypipe-client process is wrapped in
  `qdistro-secctx-exec`, stamping the toplevel with a silo tag.
- **Two display backends:** waypipe is the default Wayland-native path;
  RDP is explicitly selected with `TIER4_STREAMING_METHOD=rdp`.

## How it works

1. `spawn-tier4.sh` creates a per-VM overlay disk (linked clone of the
   P10 baked `qdistro-tier4-guest.qcow2` image).
2. It allocates a unique vsock CID (200–4095) under a flock, then
   defines and starts the libvirt domain.
3. The guest boots a stripped qdwin and publisher. In waypipe mode the
   publisher starts waypipe-server; in RDP mode it starts the qdwin
   bystander RDP subscription and bridges the RDP TCP endpoint to vsock.
   The script probes the selected vsock port until the publisher is ready.
4. Host-side: `qdistro-secctx-exec ... waypipe --vsock client vsock://CID:7879`
   connects to the guest, carrying the guest's Wayland toplevels to the
   outer compositor.
5. `tier4_control.py` runs alongside, claiming a D-Bus bus name for the
   qdshell PodApps integration and exposing a `Close()` RPC.

## Usage

```bash
# As root (uses runuser to drop to admin uid for waypipe-client):
TIER4_VM_NAME=myapp /usr/share/qdistro/tier4-vm/spawn-tier4.sh

# Define-only (for tests that just validate the XML):
TIER4_VM_NAME=myapp TIER4_DOMAIN_DEFINE_ONLY=1 spawn-tier4.sh

# Keep the domain after script exit (for debugging):
TIER4_VM_NAME=myapp TIER4_KEEP_DOMAIN=1 spawn-tier4.sh

# RDP transport instead of waypipe:
TIER4_VM_NAME=myapp TIER4_STREAMING_METHOD=rdp spawn-tier4.sh
```

## Channels

1. **Display:** waypipe over AF_VSOCK by default. With
   `TIER4_STREAMING_METHOD=rdp`, the guest publisher uses qdwin's RDP
   view-stream path and `socat` to expose it over AF_VSOCK; the host
   connects with `wlfreerdp`, `xfreerdp3`, `xfreerdp`, or `sdl-freerdp`.
2. **Clipboard:** Wayland data-device protocol rides the same waypipe
   socket. The host's `ClipboardGate.qml` gates cross-silo transfers
   to `text/plain` + `text/uri-list`.
3. **Audio:** `-audiodev pipewire` + `virtio-snd` in the domain XML;
   guest ALSA → host PipeWire.
4. **Input:** Forwarded through waypipe (keyboard/pointer events from
   the outer toplevel reach guest wl_clients).
5. **Files:** virtiofs share for host↔guest file transfer.
6. **USB:** hostdev passthrough (admin-broker-gated) for trusted devices.

## Environment variables

| Variable | Purpose |
|---|---|
| `TIER4_VM_NAME` | Domain name; validated against `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$` |
| `TIER4_KEEP_DOMAIN=1` | Skip cleanup on exit (leave domain + disk) |
| `TIER4_DOMAIN_DEFINE_ONLY=1` | Define the domain but don't start it |
| `TIER4_NO_VIEWER=1` | Start the domain, probe vsock, don't launch waypipe-client |
| `TIER4_DEBUG=1` | Pass `--debug` to waypipe-client |
| `TIER4_STREAMING_METHOD` | Readable alias for `TIER4_DISPLAY`; set to `rdp` for the RDP transport or `waypipe` for the default |
| `TIER4_DISPLAY` | Display transport (`waypipe` or `rdp`) |
| `TIER4_RDP_SUBSCRIBE` | Guest RDP subscription selector passed to qdwin-bystander (default `last`) |
| `TIER4_RDP_LOCAL_PORT` | Host loopback TCP port used by the local FreeRDP bridge |
| `TIER4_RDP_CLIENT` | Override the FreeRDP client binary |
| `TIER4_CID` | Override vsock CID (must be numeric, 200–4095) |
| `TIER4_PORT` | Override vsock port (must be numeric, 1–65535) |
| `QDISTRO_TIER4_DRY_RUN=1` | Honor `TIER4_*` path/template overrides (dev only) |

## Guest image

Built from `qdistro/tier4-vm-guest/build-guest-image.sh`. Contains:
minimal Tumbleweed + stripped qdwin (guest role) + waypipe server +
publisher script + RDP runtime (`socat`, PipeWire/FreeRDP libraries, and
`qdistro-forward`). The image is a qcow2 baked once and used as the
backing file for per-VM overlay clones. The guest publisher also reads
`/etc/qdistro/tier4-publisher.conf`; see
`tier4-vm-guest/tier4-publisher.conf.example` for the RDP keys.

## Control script

`tier4_control.py` runs alongside the waypipe-client and provides:
- D-Bus App1 receiver claim (`org.qdistro.Tier4VM.uid<NNNN>`)
- Close() RPC (`org.qdistro.Tier4VM.Control`) — ACPI shutdown → destroy
  → orphan reap, then SIGTERMs the waypipe-client
