# Overview

## Vision

qdistro is a single-tenant Linux workstation distribution with Qubes-inspired
seamless app isolation. The target user is one physical person who wants:

- Multiple data silos (work, dev, personal, etc.) isolated from each other at
 the uid / container / VM level.
- Seamless cross-silo UX (clipboard, window viewing, device access) gated by
 admin approval.
- Modern Linux infrastructure (Wayland, PipeWire, systemd, D-Bus) rather than
 Xen-based legacy.
- Everything modifiable with LLM assistance — userspace in Python, Qt, and QML.

## Not Qubes

qdistro is *inspired* by Qubes, not a re-implementation. The major differences:

| Aspect | Qubes | qdistro |
|--------------------|--------------------|-----------------------------------------------|
| Hypervisor | Xen, all VMs | KVM, optional (isolation is tiered) |
| Primary isolation | VMs | uid + container + optional VM |
| GUI | X11 + custom guid | Wayland + nested compositors + waypipe |
| Userspace | GTK / various | Python + Qt + QML |
| Target user | Security-focused | Single tenant with LLM-modifiable userspace |

## Tech stack

### Product code — modifiable Python + Qt + QML

- The admin compositor **shell** (qdshell) — panels, menus, notifications,
 system tray, admin controls. Forked from Noctalia QML; ~99% QML.
- All first-party apps — terminal, notebook, file manager, settings, etc.
- The admin session-manager daemon — user lifecycle, device grants, policy.
- The PyQt polkit AuthenticationAgent.
- The PyQt locker, hosted by the admin compositor.
- The `qdistro_app` SDK — Python library that first-party apps integrate with.
- The remote-output thin client (on secondary machines).

### Infrastructure — commodity C, used as-is

- **libweston** — the Wayland reference compositor as a library; qdistro's
 compositor (qdwin) is a libweston shell plugin (see
 [compositor](compositor.md)).
- **PipeWire** — audio and camera virtualization, per-client streams.
- **NetworkManager** — network configuration (admin-side only).
- **systemd** — service management, user sessions, logind, timers.
- **D-Bus** — IPC, configured per qdistro conventions (see [qbus](qbus.md)).
- **polkit** — authorization layer.
- **greetd** — session launcher.
- **fprintd** — fingerprint reader access.
- **waypipe** — Wayland forwarding for cross-user / container view handoff.
- **waypipe**, **FreeRDP** — remote-output transports.
- **qemu / libvirt** — VMs for the highest isolation tiers.
- **xdg-desktop-portal** — standard permission gating for sandboxed apps.
- **Tailscale** — mesh VPN providing phone ↔ laptop transport
 (see [phone](phone.md)).

## Target hardware

Primary: laptop workstation with a fingerprint reader. Single physical human
user. Optionally paired with a secondary machine (desktop or laptop) acting as
a remote display.

## Base distribution

**openSUSE Tumbleweed** with btrfs + Snapper — rolling release, production-proven
btrfs and snapshot story, strong upstream testing pipeline. The subvolume layout,
Snapper integration, and backup model are covered in [filesystem](filesystem.md).

## Core principle — everything is modifiable source

Userspace in qdistro is **modifiable at the file level at all times**. No
compilation step for product code, no opaque binaries, no atomic root image.
Every Python file, every QML file, every config, every stylesheet is plain
text the user (or an LLM) can edit, reload, and observe the effect of.

This principle drives distribution-level choices:

- **Not MicroOS / transactional-update.** Atomic root filesystems make in-place
 modification impossible. Explicitly rejected.
- **Not Flatpak for first-party apps.** Per-app sandboxes abstract the file
 layout and complicate editing. First-party apps install from git so edits
 take effect immediately.
- **`zypper` + Snapper.** Package updates apply in place; Snapper provides the
 rollback story without immutability.
- **Python stays Python.** No `.pyc`-only distribution, no Cython where pure
 Python suffices, no AOT compilation.
- **QML stays QML.** No C++ codegen step; QML loads at runtime.

Exceptions are infrastructure where product behaviour does not live:

- Compositor core via CFFI to `libweston` (C). Performance-critical, commodity;
 the qdwin plugin is small C, and upstream libweston owns DRM, surfaces,
 and input.
- PipeWire, systemd, kernel modules, libvirt/qemu — commodity C infra.
- SIP-built Python bindings for C++ Qt libraries — thin C++ glue with thick
 pure-Python logic on top.

The rule governs *product behaviour* (apps, shell, session manager, policy,
SDK). Infrastructure uses whatever is best for its job; product code is always
modifiable source.

## Distribution model

qdistro is **not** published as an ISO image. Users:

1. Install openSUSE Tumbleweed from its official ISO, terminal-only.
2. Run the `qdistro-bootstrap` script, which:
 - Installs required packages (Qt6, PipeWire, libvirt, Tailscale, etc.).
 - Sets up SELinux policy.
 - Creates the admin user and initial subvolumes.
 - Installs first-party apps from git.
 - Configures the broker, session manager, and locker.

The script is idempotent; re-runs reconcile toward the documented state. All
installation is inspectable shell or Python.

Bootstrap-on-top preserves Tumbleweed's normal update and rollback path, gives
admin full access to the underlying system for things qdistro does not wrap,
and aligns with the modifiability principle — everything qdistro adds is an
inspectable script or package.
