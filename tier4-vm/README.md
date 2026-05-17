# Tier-4 — whole-VM windowed (libvirt + waypipe)

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
`xdg_toplevel` windows on the host compositor. The host uses **waypipe
over AF_VSOCK** to carry the Wayland protocol from a stripped qdwin
running inside the guest. Each toplevel is security-context-tagged
(`qdistro.tier4.<vm-name>`) so the host clipboard gate (`ClipboardGate.qml`)
and chrome system know which VM owns which window.

## Why libvirt + waypipe

Tier-4's isolation boundary is the VM itself. Waypipe carries the
Wayland display protocol over vsock from a guest compositor to the host,
making the guest's application windows appear as native host toplevels.

- **Strong isolation:** Full KVM VM boundary; guest cannot see host
  processes, memory, or other guests.
- **Wayland-native:** waypipe preserves the Wayland protocol end-to-end;
  clipboard, input, and surfaces ride the same socket.
- **Security context:** Each waypipe-client process is wrapped in
  `qdistro-secctx-exec`, stamping the toplevel with a silo tag.
- **No legacy viewer stack:** Just libvirt,
  QEMU, and waypipe (all on Tumbleweed).

## How it works

1. `spawn-tier4.sh` creates a per-VM overlay disk (linked clone of the
   P10 baked `qdistro-tier4-guest.qcow2` image).
2. It allocates a unique vsock CID (200–4095) under a flock, then
   defines and starts the libvirt domain.
3. The guest boots a stripped qdwin + waypipe server. The script probes
   vsock port 7879 until the publisher is ready.
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
```

## Channels

1. **Display:** waypipe over AF_VSOCK. The guest runs a stripped qdwin
   compositor; the host sees its toplevels via `waypipe --vsock client`.
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
| `TIER4_CID` | Override vsock CID (must be numeric, 200–4095) |
| `TIER4_PORT` | Override vsock port (must be numeric, 1–65535) |
| `QDISTRO_TIER4_DRY_RUN=1` | Honor `TIER4_*` path/template overrides (dev only) |

## Guest image

Built from `qdistro/tier4-vm-guest/build-guest-image.sh`. Contains:
minimal Tumbleweed + stripped qdwin (guest role) + waypipe server +
publisher script. The image is a qcow2 baked once and used as the
backing file for per-VM overlay clones.

## Control script

`tier4_control.py` runs alongside the waypipe-client and provides:
- D-Bus App1 receiver claim (`org.qdistro.Tier4VM.uid<NNNN>`)
- Close() RPC (`org.qdistro.Tier4VM.Control`) — ACPI shutdown → destroy
  → orphan reap, then SIGTERMs the waypipe-client
