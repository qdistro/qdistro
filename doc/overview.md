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

**One configuration — the goal, not yet the v1 implementation.** The intent is
that settings live in a single, system-wide place rather than being duplicated
per component or per uid: appearance — UI theme and colours, fonts, icons,
cursors — together with monitor arrangement defined once, in an admin-owned
canonical store, and shared by every component, with a small set of values
overridable per uid where it makes sense ([ui.md](ui.md)). The per-user accent
colour is the deliberate exception: it is the visual cue that tells silos apart,
so it is *meant* to differ per user.

> **Status: system-wide theming is not implemented.** What ships is per-component
> configuration. qdshell's settings are per-uid, in `~/.config/qdshell/`.
> qdgreeter and qdlocker do not read a shared store at all — each carries a
> *static, hardcoded copy* of qdshell's default dark palette
> (`qdgreeter/qml/shim/Color.qml`, `qdlocker/qdlocker/qml/shim/Color.qml`, both
> self-labelled "No dynamic theme loading"). Changing the qdshell theme does not
> change the greeter or the locker, and there is no theme-propagation path into
> the desktops inside embedded VMs. Treat every "change it once and everything
> follows" statement on this page as a design target, not shipped behaviour.

**Many sessions.** qdistro's model has both coarse session separation and
Qubes-style mixed desktops. A separate TTY session would have its own
compositor, shell, panel, clipboard surface, and notifications. A mixed session
shows windows from multiple silos on one compositor, with cross-silo actions
mediated by the broker. Both modes are valid in the design; the owner chooses
based on task, performance, and desired mental separation.

> **Status: only the mixed mode exists, and only its brokering half.** v1 boots
> one compositor on tty3; the additional per-TTY sessions are unimplemented
> ([sessions.md](sessions.md)). Within the mixed desktop, the cross-silo
> *brokering* is real (clipboard gates, activation gate), but the trusted
> **chrome** that would make silo identity visible is not: no client attaches
> qdwin's decoration protocol, so silo-coloured window chrome is not painted
> ([compositor.md](compositor.md)). Silo identity is enforced in policy today,
> not shown to the user.

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
- The PyQt locker (qdlocker) — a separate process and repo that connects to the
  admin compositor as an ordinary Wayland client and binds `qdwin_locker_v1`;
  it is not hosted inside the compositor or the shell ([sessions.md](sessions.md)).
- The `qdistro_app` SDK — Python library that first-party apps integrate with.
- The remote-output thin client (on secondary machines) — **VM-gated and
  incomplete**. `install-multimachine-for-vm.sh` is not in the bootstrap chain,
  and even where it runs it installs the broker/session/wrapper subset and
  **not** `multimachine/viewer.py`; live viewer assembly stays under the VM
  harness. There is no thin client to install on a secondary machine today.

### Infrastructure — commodity C/C++

Mostly used as-is, with one significant exception: **libweston is not**. qdistro
carries a full patched weston tree, including security and KMS changes that are
not all recorded as `.patch` files, and qdwin is a custom shell plugin rather
than stock configuration. See [compositor.md](compositor.md) for what is patched
and for the install-time branch that decides whether a given machine runs the
vendored tree or the distro one.

- **libweston** — the Wayland reference compositor as a library; qdistro's
 compositor (qdwin) is a libweston shell plugin (see
 [compositor](compositor.md)).
- **PipeWire** — stock desktop audio (with wireplumber, as the distro ships
 them), plus screen-pixel transport for the compositor's capture outputs via
 libweston's `backend-pipewire`. The *device-mediation* role described in
 [devices.md](devices.md) — per-user virtual sinks/sources, an admin-owned
 daemon owning ALSA/V4L2/libcamera, policy-gated per-client streams — is **not
 implemented**. qdistro ships no PipeWire mediation code, no qdistro
 wireplumber policy, and no libcamera integration; the packages are installed
 with distro defaults.
- **NetworkManager** — network configuration, at **upstream's** defaults.
 qdistro adds no NM-specific restriction: no D-Bus policy override for
 `org.freedesktop.NetworkManager`, no NM polkit rule, and no installer touches
 it. Privileged NM operations are still gated — by NM's own polkit actions, and
 where the qdistro polkit agent is registered (admin's session) those prompts
 route into the broker's approval queue. What is *not* true is a qdistro-added
 "admin-only" boundary; the absence of NM clients in silos today is topology,
 not enforcement (see [networking.md](networking.md)).
- **systemd** — service management, user sessions, logind, timers.
- **D-Bus** — IPC, configured per qdistro conventions (see [qbus](qbus.md)).
- **polkit** — authorization layer.
- **greetd** — session launcher.
- **fprintd** — fingerprint reader access.
- **waypipe** — Wayland forwarding for cross-user / container view handoff.
- **waypipe**, **FreeRDP** — remote-output transports.
- **qemu / libvirt** — VMs for the highest isolation tiers.
- **xdg-desktop-portal** — standard permission gating for sandboxed apps.

Named in the design but **not shipped**, and not installed by
`qdistro-bootstrap`:

- **Tailscale** — planned mesh VPN for phone ↔ laptop transport. Nothing in
 `scripts/` installs or configures it; its only non-doc mention in the tree is
 a comment in the phone daemon, and the phone feature is itself cut from v1
 (see [phone](phone.md)). Do not expect a working phone transport on a v1
 install.

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

- Compositor core in C: qdwin is a libweston shell plugin (`qdwin-shell.so`)
 that libweston dlopens in-process, as described under "Infrastructure" above.
 Performance-critical, commodity; the qdwin plugin is small C, and upstream
 libweston owns DRM, surfaces, and input. (An earlier plan to drive libweston
 from Python over CFFI was abandoned; the only CFFI artefact left in the tree
 is a dead Phase-6.0 spike that nothing builds, installs, or imports.)
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

qdistro is **not** published as an installable ISO. An install ISO that
a tester boots from a stick offers a wipe of the internal disk; that
path is post-v1.

**Tester releases** are a single xz-compressed raw disk image
(`qdistro-<version>-<snapshot>.raw.xz` plus its `.sha256`): write it to
a USB stick or boot it as a VM disk. Dev profile, default password
`qdistro`, sshd off, UEFI-only, 32 GB minimum. The download page states
the flash line, the VM line, and the caveats. Signing is post-v1. No
public download is published yet (GitHub Releases is the intended host).

**From source**, developers:

1. Install openSUSE Tumbleweed from its official ISO, terminal-only.
2. Run the `qdistro-bootstrap` script, which:
 - Installs required packages (Qt6, PipeWire, libvirt, FreeRDP, etc. — not
 Tailscale; see the tech-stack note above).
 - Sets up SELinux policy.
 - Creates the admin user and initial subvolumes.
 - Installs first-party apps from git.
 - Configures the broker, session manager, and locker.

The script is idempotent; re-runs reconcile toward the documented state. All
installation is inspectable shell or Python.

Bootstrap-on-top preserves Tumbleweed's normal update and rollback path, gives
admin full access to the underlying system for things qdistro does not wrap,
and aligns with the modifiability principle — everything qdistro adds is an
inspectable script or package. The tester image runs that same bootstrap
chain in the kiwi chroot (`image/config.sh` sources
`scripts/install/qdistro-bootstrap.sh`).
