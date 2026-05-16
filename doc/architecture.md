# Architecture

## Admin is home

Admin's compositor runs on tty3 and owns all hardware (GPU, KMS, inputs, audio,
network, Bluetooth, camera). Every regular user session is a **nested
compositor** (or container with a nested compositor) whose surfaces appear as
Wayland clients inside admin's compositor.

This is the Qubes Dom0 role, mapped to a single-machine, non-Xen, mainstream-
Linux world.

Seamless cross-user UX (clipboard, view handoff, admin-approval overlays)
*requires* multiple users' windows coexisting on one display — only achievable
in the nested model. The nested model also unifies small apps and large
multi-window apps (IDEs) under one "each isolated thing runs a compositor"
pattern. TTY-switched fullscreen sessions remain as an escape hatch for games,
VR, and GPU-heavy workloads (see [games](games.md)).

## TTY layout

| TTY | Role | Owner | Notes |
|-------|-------------------------------|------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| tty1 | Emergency text console | `agetty` | Raw getty; no greetd; recovery path if everything else breaks. |
| tty2 | Textual admin login | `greetd` + `tuigreet` | Admin login for repairs when Wayland won't start. |
| tty3 | Admin graphical session | `greetd` → `qdgreeter` → `qdwin-session-launcher` → `qdwin-session.target` (qdwin compositor + qdshell) | Pinned; boots here by default; the PyQt locker is active from start. P01 wired this path in 2026-05; before that, greetd ran LXQt+labwc here. |
| tty4 | Escape hatch — legacy LXQt+labwc | `greetd-fallback.service` → `qdistro-startlxqtwayland` | Recovery path when qdwin is broken. Same code that used to run on tty3 pre-P01. |
| tty5+ | Pinned and dynamic mix | `qdistro-session-manager` | Some TTYs may be pinned to special roles (recall-user, future ones); remaining slots are allocated dynamically. |

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
| 4. VM, whole-window | KVM + libvirt + `virt-viewer` (SPICE) | Yes |
| 5. VM, per-app | KVM + libvirt + waypipe over AF_VSOCK (Linux guest) | Yes |
| 6. Remote machine | Separate physical machine; remote-output | No |

Higher tier = stronger isolation = less seamless. Admin policy picks the per-app
default; users can override within admin-allowed bounds.

**Default tier for new user-silo apps:** tier 1 (SELinux sandbox). Tier 0 has no
containment, which is an unacceptable floor even under the non-adversarial threat
model. Tier 2 adds real sandboxing but is heavier; admin opts in per app.

## Hardware ownership

- **GPU / KMS / outputs / inputs** — admin compositor on tty3 (or the TTY-escape
 compositor when active).
- **Audio / camera** — admin's PipeWire daemon (system-wide).
- **Network** — admin's NetworkManager; per-user network namespaces for silo
 separation.
- **Bluetooth** — admin's bluez; device pairing through admin UIs.
- **Fingerprint reader** — admin's fprintd.
- **Disks / filesystem** — root-owned; users have home dirs per uid.

Regular users cannot `open()` device nodes for sensitive hardware. Enforcement
is layered via SELinux policy and cgroup device whitelists on user sessions.
PipeWire and polkit-gated services surface virtual equivalents.

## TTY escape hatch for fullscreen sessions

A user session can be promoted to a **fullscreen TTY session**:

- Launched on tty4+ via greetd.
- Runs the user's compositor directly on that TTY (DRM master, full GPU access).
- Used for games, video editors, GPU-heavy work — anywhere maximum performance
 matters more than seamless multi-user UX.
- No handoff or clipboard-paste-from-other-user in this mode. It's a committed
 fullscreen context; VT-switch back to tty3 when done.

Primary users of this mechanism are games and VR (see [games](games.md)),
multi-monitor SPICE VM viewers, and GPU-heavy creative workloads.

## Components

- **qdwin** — libweston shell plugin (C). Private protocol server, peer-uid
 enforcement, chrome compositing, window placement.
- **qdshell** — per-uid Qt/QML shell client. Panels, menus, notifications,
 system tray, admin overlays, window decorations. Forked from Noctalia.
- **qdistro-admin-broker** — privileged daemon that owns cross-uid permission
 routing, rule evaluation, audit log, and approval cache.
- **qdistro-admin-app** — PyQt admin queue UI for triaging permission requests
 (see [admin-approval](admin-approval.md)).
- **qdistro-session-manager** — user-lifecycle daemon; creates, freezes, and
 resumes user sessions.
- **qdistro_app SDK** — Python library that first-party apps integrate with
 (see [app-sdk](app-sdk.md)).
- **qdistro-pwd** — secret/vault daemon (see [password-manager](password-manager.md)).
- **qdistro-browser-bridge** — native-messaging host that connects browser
 extensions to qdistro services (see [browser](browser.md)).
