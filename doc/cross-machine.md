# Cross-machine

## Goal

Seamless window migration between physical machines that share a logical
desktop. Drag a window to the next screen, and if that screen is on a
different machine, it works — as if both displays were connected to one
computer.

## Model — single home machine, secondary as remote display

- All apps run on machine A (the "home").
- Machine B is a **thin client** whose monitor is a remote output of A's
 compositor.
- Dragging across the screen edge is just "dragging to another output" —
 same protocol path as a second physical monitor. A's compositor renders
 the window to that output; the output happens to be a network stream.
- B's role is rendering received framebuffers and forwarding input back.

This is what "like both displays connected to the same machine via RDP"
describes.

## Implementation — libweston backend for remote output

libweston supports backend plugins that present remote outputs as if they
were local. `backend-rdp` is already in-tree; it advertises a virtual
output whose framebuffer the transport encodes and ships over the network.

- The admin compositor loads `backend-rdp`; each connected remote machine
 maps to one output.
- That output's framebuffer is encoded and streamed (FreeRDP) to the remote
 machine.
- Input from the remote machine arrives via libei and is injected into the
 local seat.
- The output participates in all standard Wayland geometry (monitor layout,
 drag-across-edge, cursor movement, multi-monitor shortcuts).

qdistro uses the in-tree backends as-is and adds a policy layer that
authorizes per-remote connections via the broker.

## Transport choice

| Transport | Codec | Notes |
|------------------------------------------|----------------------|--------------------------------------------------------------------------------------------------------------------|
| **RDP** (FreeRDP) | H.264 / RemoteFX | Best codec; mature; matches Mutter / KDE screen-share and WSL2's `wslg`. **Primary.** |
| Custom PipeWire + waypipe-over-TCP | — | Reinventing codecs. Don't. |

RDP is the primary transport across the board (cross-machine, per-view
forwarding, remote display). Control plane (auth, session setup,
output geometry negotiation) is PyQt;
transport is FreeRDP — don't write codecs.

## Thin client on the secondary machine

A minimal PyQt app that:

- Presents the received framebuffer as a monitor (fullscreen or windowed).
- Captures local input (keyboard, mouse) and forwards it.
- Handles session setup: pair with the home machine, auth (cert /
 fingerprint), negotiate resolution.
- Optionally routes local audio back to home's PipeWire.

Ships as a standalone qdistro thin-client package.

## Drag-across-screen-boundary

No special protocol needed. From the admin compositor's perspective, B's
monitor is just another output. Standard Wayland multi-monitor handles
cursor movement across output edges, window dragging onto other outputs,
per-output cursor scaling and DPI, and the "move window to next output"
shortcut.

The user experience is identical to a physically-attached second monitor.
The only differences are latency and a pairing setup.

## Latency and cost

- **LAN wired**: under 20ms round-trip, near-native feel for desktop work.
 H.264 hardware encoding on the home machine is cheap.
- **Wi-Fi**: 30-60ms typical; cursor feels rubbery under drag.
- **WAN / tethered**: gets ugly fast; 100ms+ is disruptive.
- **Prototype on the target network early.** Users blame the compositor
 when the network is actually the problem.
- **GPU cost**: encoding a second 4K@60 stream uses real CPU / GPU. Fine
 on a workstation host; painful on a thin laptop host.

## Trust model

The thin client:

- Sees every pixel rendered to that output.
- Injects keyboard / mouse events into admin's session.
- Is effectively equivalent to someone with physical access to an unlocked
 laptop.

Implications:

- Only pair with machines you own.
- Pairing auth: certificate exchange with a fingerprint-unlock on first
 pairing (admin approves pairing).
- Ongoing session auth: mTLS or SSH-tunnelled RDP.
- Don't use over untrusted Wi-Fi without a VPN / SSH tunnel.

## Audio, clipboard, devices on remote outputs

From the broker's perspective, the remote session is "another agent in the
system." The same policy framework applies:

- **Audio** — RDP routes audio natively; policy decides which audio
 streams follow the window.
- **Clipboard** — the remote session has its own clipboard state; treated
 as a peer in the cross-compositor transfer model.
- **Device claims** — the remote client can be granted or denied access to
 its *local* devices (mic, camera on machine B) through admin policy,
 surfaced to apps on A as virtual devices.

## Host vs remote-display — mutually exclusive

A given qdistro machine at a given time is **either** a full host (running
the admin compositor on its own hardware) **or** a remote display (thin
client for another qdistro host). Not both concurrently.

Cleanest model. Switching modes requires admin approval and session
teardown. Avoids confusing "who owns my display right now" cases.
