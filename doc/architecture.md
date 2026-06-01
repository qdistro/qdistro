# Architecture

## Admin is home

Admin's compositor runs on tty3 and owns the trusted control plane for hardware
(GPU, KMS, inputs, audio, network, Bluetooth, camera). Regular user sessions
can run either as **nested compositor** sessions whose surfaces appear as
Wayland clients inside admin's compositor, or as fullscreen TTY sessions with
their own compositor, shell, clipboard surface, and notification surface.

This is the Qubes Dom0 role, mapped to a single-machine, non-Xen, mainstream-
Linux world.

Seamless cross-silo UX (clipboard, view handoff, admin-approval overlays)
requires multiple silos' windows coexisting on one display, which is the nested
or mixed-session model. The nested model also unifies small apps and large
multi-window apps (IDEs) under one "each isolated thing runs a compositor"
pattern.

TTY-switched fullscreen sessions are also first-class. They are useful for
games, VR, GPU-heavy workloads, and for tasks where the owner wants stronger
mental separation than coloured windows on one desktop can provide. The TTY
boundary is not the whole security model, but Linux VT switching gives a
natural UI boundary and useful incidental separation (see [games](games.md)).

A session is not a silo. A session is the dynamic runtime context with
processes, UI, and reserved resources; a silo is an isolated program/data/state
resource that may be attached to one or more sessions. This distinction matters
for workflows such as development versus commit: the same source-code silo can
be visible in different sessions while commit authority remains separate.

## TTY layout

| TTY | Role | Owner | Notes |
|-------|-------------------------------|------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| tty1 | Emergency text console | `agetty` | Raw getty; no greetd; recovery path if everything else breaks. |
| tty2 | Textual admin login | `greetd` + `tuigreet` | Admin login for repairs when Wayland won't start. |
| tty3 | Admin graphical session | `greetd` → `qdgreeter` → `qdwin-session-launcher` → `qdwin-session.target` (qdwin compositor + qdshell) | Pinned; boots here by default; the PyQt locker is active from start. P01 wired this path in 2026-05; before that, greetd ran LXQt+labwc here. |
| tty4 | Escape hatch — legacy LXQt+labwc | `greetd-fallback.service` → `qdistro-startlxqtwayland` | Recovery path when qdwin is broken. Same code that used to run on tty3 pre-P01. |
| tty5+ | Pinned and dynamic sessions | `qdistro-session-manager` | TTY work sessions, fullscreen sessions, VM viewers, and special-role sessions; some slots may be pinned, remaining slots allocated dynamically. |

Kernel cmdline: `systemd.default_vt=3`. Boot lands on admin.

System policy prevents anything but the admin compositor from binding tty3.
`agetty` on tty1 stays free of greetd so a broken greetd config does not take
out the last-resort login.

## Isolation ladder

Isolation tier is selectable per app (or per user's default). All tiers below
"none" run the app's Wayland rendering through something other than admin's
compositor directly. Full detail is in [isolation-tiers](isolation-tiers.md).

| Tier | Mechanism | Seamless? |
|-------------------|--------------------------------------------------|-----------|
| 0. none | Direct Wayland client of admin compositor | Yes |
| 1. SELinux | LSM restrictions, same Wayland connection | Yes |
| 2. podman | User namespace; container has a nested compositor| Yes |
| 3. Different user | Separate uid; waypipe bridges `wl_display` | Yes |
| 4. VM, whole-window | KVM + libvirt + waypipe (nested qdwin) | Yes |
| 5. VM, per-app | KVM + libvirt + waypipe over AF_VSOCK (Linux guest) | Yes |
| 6. Remote machine | Separate physical machine; remote-output | No |

Higher tier = stronger isolation = less seamless. Admin policy picks the per-app
default; users can override within admin-allowed bounds.

**Default tier for new user-silo apps:** tier 1 (SELinux sandbox). Tier 0 has no
containment, which is an unacceptable floor even under the non-adversarial threat
model. Tier 2 adds real sandboxing but is heavier; admin opts in per app.

## Hardware ownership

- **GPU / KMS / outputs / inputs** — admin compositor on tty3 (or a fullscreen
 TTY session compositor when active).
- **Audio / camera** — admin's PipeWire daemon (system-wide).
- **Network** — admin's NetworkManager; per-user network namespaces for silo
 separation.
- **Bluetooth** — admin's bluez; device pairing through admin UIs.
- **Fingerprint reader** — admin's fprintd.
- **Disks / filesystem** — root-owned; users have home dirs per uid.

Regular users cannot `open()` device nodes for sensitive hardware. Enforcement
is layered via SELinux policy and cgroup device whitelists on user sessions.
PipeWire and polkit-gated services surface virtual equivalents.

## TTY sessions

A user session can run as a **TTY session**:

- Launched on tty5+ via greetd. tty4 is reserved as the fallback desktop.
- Runs the user's compositor directly on that TTY (DRM master, full GPU access).
- Owns its own shell, panel, clipboard surface, notification surface, and
 session-local UI state.
- Provides stronger mental separation because the owner switches desktops
 rather than merely seeing differently coloured windows.
- May be used for normal task-focused work, games, video editors, GPU-heavy
 work, VM viewers, or special-role sessions.

Some TTY sessions are fullscreen-only and deliberately give up seamless handoff
or cross-silo clipboard affordances. Other TTY sessions may still use explicit
brokered transfers. Games and VR are examples of the stricter fullscreen mode
(see [games](games.md)).

## Components

- **qdwin** — libweston shell plugin (C). Private protocol server, peer-uid
 enforcement, chrome compositing, window placement.
- **qdshell** — per-uid Qt/QML shell client. Panels, menus, notifications,
 system tray, admin overlays, window decorations. Forked from Noctalia.
- **qdistro-admin-broker** — privileged daemon that owns cross-uid permission
 routing, rule evaluation, audit log, and approval cache.
- **qdistro-admin-app** — PyQt admin queue UI for triaging permission requests
 (see [admin-approval](admin-approval.md)).
- **qdistro-session-manager** — user-lifecycle daemon; creates, starts, stops,
 and attaches silos and sessions.
- **qdistro_app SDK** — Python library that first-party apps integrate with
 (see [app-sdk](app-sdk.md)).
- **qdistro-pwd** — secret/vault daemon (see [password-manager](password-manager.md)).
- **qdistro-browser-bridge** — native-messaging host that connects browser
 extensions to qdistro services (see [browser](browser.md)).
