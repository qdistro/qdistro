# Tier-5-Linux — per-app VM windowed (waypipe over AF_VSOCK)

Spec ref: `doc/isolation-tiers.md` ; isolation tier 5 in
`doc/architecture.md`. **Linux guests only** per `spec/00`
(memory `qdistro_linux_only.md`).

## Architecture

```
admin compositor (qdwin) ← outer, running on tty3
 │
 ▼ wayland-1 (UNIX socket, /run/user/1000/wayland-1)
 │
[waypipe client, admin uid] ← Wayland client of outer
 │ vsock: -s s<CID>:<port> --vsock
 ▼ AF_VSOCK
 │
[guest VM] ← libvirt-managed, vsock CID assigned
 │
 ▼
[waypipe server -- <app>] ← inside guest, runs the user app
 │
 ▼
[guest user app] ← Wayland client of waypipe-server's
 synthetic display
```

Each tier-5 toplevel arrives at the outer compositor as an ordinary
`xdg_toplevel` from the waypipe-client wl_client; gets the standard
`qdwin_nested_manager_v1` chrome + secctx + broker treatment exactly
like a tier-3 toplevel.

The bridge is **AF_VSOCK** instead of UNIX-domain. Everything else
mirrors tier-3 (`scripts/vm/tier3/`).

## Why waypipe-over-vsock

Picked over weston-rdprail-shell / SPICE-seamless / xpra / DIY-RAIL
in `spec/29` . Reasons:

- **Wayland-native both sides**: no XWayland; no remote-protocol re-encoding.
- **Reuses qdistro primitives entirely**: same waypipe wrapper,
 same `qdwin_nested_manager_v1`, same broker gates, same
 `wp_security_context_v1` (apply at vsock-accept time).
- **Distro-packaged**: `waypipe 0.11.0` on Tumbleweed. Confirmed
 vsock support via `--vsock` + `-s [s]CID:port` on 2026-04-27.
- **End-to-end proven** via loopback (CID=1) on
 `qdwin-tier4-fresh-260427-1041`: `wayland-info` enumerated the
 outer compositor's `wl_compositor` through the vsock bridge.

## Files

- `spawn-tier5.sh` — host-side wrapper. Starts a libvirt domain
 (with `<vsock>` device), waits for guest readiness, runs
 waypipe-client on vsock CID/port, triggers waypipe-server inside
 the guest via SSH or qemu-guest-agent, and runs the user app.
 Mirrors `tier3/spawn-tier3.sh` shape.
- `domain-template.xml` — libvirt domain template with
 `<vsock model='virtio'/>` and assigned CID. Linux-only guest
 per spec/00.
- `qdistro-tier5-cleanup.sh` — destroy + undefine helper.
- `loopback-bridge.sh` — same wrapper but using vsock CID=1
 loopback for in-VM smoke tests (no actual nested guest needed;
 exercises the data path).

## Modes

The wrapper supports two modes:

1. **`--loopback`** (default for tests): CID=1 loopback. waypipe-client
 listens on vsock:1:PORT; the inner app's waypipe-server is spawned
 on the same machine and connects to vsock:1:PORT. Same kernel,
 same uid space. Used by `phase7-tier5-loopback` bats. **No
 isolation** — purely an exercise of the data path.

2. **`--vm <vm_name>`** (production): CID=NNN guest VM. waypipe-
 client listens on vsock:NNN:PORT bound to the host side of the
 guest's vsock device; waypipe-server runs inside the guest via
 ssh / qga and connects from CID=2 (host) ← wait, that's
 backwards: from inside the guest, CID=2 is the host. Connection
 direction is guest→host. waypipe semantics: server connects to
 client. So inside guest, `waypipe server -s 2:PORT --vsock`
 reaches the host's listener.

## Limitations of the MVP

- **Only the loopback mode is fully wired** in this initial
 shipset. The `--vm` mode lays out the libvirt domain template
 + ssh / qga trigger plumbing but the in-guest helper
 (`qdistro-tier5-publisher`) and disk-image build are
 follow-ups.
- **No PipeWire-over-vsock yet.** Audio for tier-5 guests
 needs a separate vhost-user-pipe shim or vsock-tunneled
 pipewire-pulse. Tracked as a follow-up; tier-5-Linux apps
 with audio fall back to silent for now.
- **No clipboard wiring yet.** The broker `CheckClipboardTransfer`
 gate accepts tier-5 secctx tags via the rules-engine
 matchers shipped 2026-04-27 (qdwin v13), but the in-guest
 end of the clipboard plumbing is a follow-up.

## Linux-only

Per `spec/00` qdistro is single-tenant Linux; tier-5 guests are
Linux only. Memory: `qdistro_linux_only.md`.
