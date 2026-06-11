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

The core nouns are defined in [glossary.md](glossary.md). Short version:
qdistro has one owner, many resources, many data/state silos, and one or more
sessions that can attach resources for a task. A Linux uid is a useful
isolation primitive, not the user-facing definition of a session.

Design shorthand: **one owner, many silos, dynamic sessions**. The owner is the
single human and policy authority. Silos are isolated desktop workloads with
program state, health checks, actions, rollback policy, and guarded
capabilities. Sessions are runtime contexts that reserve resources and attach
silos while work is happening.

## Single tenant

"Single-tenant" means more than isolated data silos. Because there is only ever
one physical person at the machine, configuration and authentication are
unified rather than replicated per user.

Single-tenant does **not** mean single-context. qdistro intentionally keeps many
data and program contexts separate: work, home, dev, client projects, browser
profiles, credentials, and task-specific state. The simplification is on the
human-authentication and machine-policy axis, not on the data-separation axis.

**One configuration.** Settings live in a single, system-wide place rather than
being duplicated per component or per uid. Appearance — UI theme and colours,
fonts, icons, cursors — together with monitor arrangement is defined once and
shared by *every* component: qdgreeter, the lock screen, qdshell, first-party
apps, and the desktops inside embedded VMs. There is no per-app theming to keep
in sync; change the font once and the greeter, locker, panel, and VM windows
all follow when their sessions next start. The canonical store is admin-owned —
changing the system theme is an admin action — though a small set of values may
be overridden per uid where it makes sense ([ui.md](ui.md)). The per-user accent
colour is the deliberate exception: it is the visual cue that tells silos apart,
so it is *meant* to differ per user.

**Many sessions.** qdistro supports both coarse session separation and
Qubes-style mixed desktops. A TTY session has its own compositor, shell, panel,
clipboard surface, and notifications. A mixed session shows windows from
multiple silos on one compositor, with silo identity carried by trusted chrome
and cross-silo actions mediated by the broker. Both modes are valid; the owner
chooses based on task, performance, and desired mental separation.

Sessions are not the same as silos. A development session may attach a
source-code silo without commit authority. A commit session may attach the same
source-code silo plus a signing-key or GitHub-authority silo. A browser silo
logged into Google may be temporarily attached, or used by a workflow in a
headless compositor, to authenticate another tool. These are policy decisions,
not hardcoded product flows.

**One unlock.** One human means one lock. A single screen lock covers the whole
machine, and one unlock — password or fingerprint — releases everything at once.
You never return to the machine and have to dismiss a separate lock screen per
silo or per app. (Password-vault unlock state is a separate, deliberate
exception — see [password-manager.md](password-manager.md).) The lock mechanics
are in [sessions.md](sessions.md).

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
 pure-Python logic on top. If a future Qt 6 feature has no Python binding
 (e.g. a custom QML type), the same shape applies: thin C++ glue, no
 product behaviour in C++.
- Where C infrastructure offers an embedded extension language, extend it
 there instead of in C — e.g. Weston 15's lua-shell scripts window
 management in Lua (demo tiling shell included).

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
