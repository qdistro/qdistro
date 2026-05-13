# Isolation tiers

qdistro's isolation ladder is a graduated set of containment mechanisms,
selectable per app or per user's default. Higher tier = stronger
isolation = less seamless.

The outer-compositor windowing contract is **the same at every tier**:
every toplevel arrives via `qdwin_nested_manager_v1.advertise_toplevel`,
identity is carried by `wp_security_context_v1`, and policy is enforced
by the broker's clipboard / handoff gates. The tier choice affects what
carries pixels and window-create events to the outer compositor, not how
the outer renders them.

| Tier | Mechanism | Seamless? |
|-------------------|-------------------------------------------------------------|-----------|
| 0. none | Direct Wayland client of admin compositor | Yes |
| 1. SELinux | LSM restrictions, same Wayland connection | Yes |
| 2. podman | User namespace; container has a nested compositor | Yes |
| 3. Different user | Separate uid; waypipe bridges `wl_display` | Yes |
| 4. VM whole-window| KVM + libvirt + `virt-viewer` (SPICE) | Yes |
| 5. VM per-app | KVM + libvirt + waypipe over `AF_VSOCK` | Yes |
| 6. Remote machine | Separate physical machine; remote-output | No |

**Default tier for new user-silo apps:** tier 1.

## Tier 0 — none

Direct Wayland client of admin's compositor. No containment at all.
Reserved for fully-trusted first-party apps.

## Tier 1 — SELinux sandbox

Sits between tier 0 (no containment) and tier 2 (rootless podman with own
user namespace + nested compositor). The "lightest containment that's
still enforced."

### Threats tier 1 blocks

- An app *in the same uid* reading another app's clipboard via Wayland
 selection without going through the broker gate.
- The same app reading screen contents via a synthetic XWayland client or
 `/proc/<pid>/maps`-style introspection.
- The same app calling `setuid` / `ptrace` to escalate within the uid.
- Random `/dev` / `/sys` access beyond the narrow allowed list.

### Threats tier 1 does not block

- Kernel-level escapes — mitigated by reduced syscall surface but tier 1
 is not seccomp-bpf.
- Side-channel attacks (cache, frequency, etc.).
- Hardware-bus access (USB, GPU shaders) — tier 2 podman is the next
 layer that adds device-cgroup whitelist.

### Implementation

A custom SELinux module `qdistro_tier1` cloned from Fedora's `sandbox.te`
(the non-X variant — qdistro is Wayland-only) with Wayland, PipeWire,
DRI, and broker rules layered on. A wrapper binary `qdistro-tier1-exec`
wraps `qdistro-secctx-exec` with a `setexeccon()` call on the exec edge —
two independent attestations of the same identity: SELinux type for
enforcement, `wp_security_context_v1` tag for routing.

A spawn helper `qdistro-tier1-spawn` takes `(silo_user, app...)` and
calls the wrapper.

### Filesystem labelling strategy

**Type, not mount.** `$HOME` stays labelled `user_home_t`; a narrow
interface allows reading a per-app whitelist (e.g., `~/.config/<app>`
relabelled to `qdistro_tier1_config_t`). Per-app private state lives
under `~/.local/share/qdistro/tier1/<app>/` and is pre-relabelled by the
launcher.

The seunshare-style alternative (tmpfs over `$HOME` and `/tmp`, relabelled
`sandbox_file_t`) needs setuid root or `CAP_SYS_ADMIN` and inherits
seunshare's setuid-race lineage. Type-not-mount avoids setuid root and
integrates with SELinux's existing labelling story.

### Audit integration

AVC denials land in `/var/log/audit/audit.log`. The broker consumes them
via an audispd plugin and writes one audit row per denial with subject
context (which silo it came from). The audit log gains a
`selinux_subj_type` column.

### Compatibility with `wp_security_context_v1`

Tier 1 sets **both** the SELinux type and the secctx tag — they answer
different questions:

- SELinux: "is this syscall / file access allowed?"
- secctx: "which silo's clipboard does this paste come from?"

The broker independently verifies via `/proc/<pid>/attr/current` that the
process is actually in `qdistro_tier1_t`. If the two disagree, broker's
`CheckPermission` denies (defence in depth).

## Tier 2 — podman / container

Default for most user-owned apps. Rootless podman + `--userns=keep-id` +
bind-mount `/run/user/<uid>` so the container's nested compositor (with
qdwin-shell.so) connects to the outer admin compositor's Wayland socket
and advertises each inner toplevel via `qdwin_nested_manager_v1`.

## Tier 3 — different user (waypipe over UNIX)

Primary mechanism for data-silo separation. `qdistro-tier3-spawn
<silo_user> -- <app...>` runs `waypipe client` as admin uid 1000
(creating a bridge socket against the local `wayland-1`), and
`waypipe server -- <app>` as the silo uid (which connects to the same
socket and exposes a synthetic `wayland-tier3-<silo>-<pid>` display for
the app).

Cross-uid socket access is gated by the `qdistro-tier3` group — the silo
can't reach `wayland-1` directly (qdwin's `QDWIN_ALLOWED_UID` rejects via
`SO_PEERCRED`), but waypipe-client on the admin side passes the gate and
re-marshals the protocol. Default `--no-gpu` (SHM-only, VM-safe; flip on
accel3d clones); `--oneshot` per bridge so each silo lifecycle is
independent.

## Tier 4 — whole-VM windowed

**libvirt + QEMU + `virt-viewer`** as one chromed peer toplevel,
mirroring tier 2's container pattern.

Stack:

- libvirt driving QEMU. Both packaged on Tumbleweed.
- Display: `-display spice-app` → `remote-viewer spice://...` as a
 Wayland toplevel. Or `-display gtk,gl=on` for a direct Wayland toplevel
 without SPICE round-trip. SPICE is the documented default; `-display
 gtk` is the fallback if SPICE bit-rots.
- The outer wraps the viewer toplevel via `qdwin_nested_manager_v1`
 exactly like tier 2 today.
- Clipboard: routes `spice-vdagent` through the broker's
 `CheckClipboardTransfer` gate.
- Audio: SPICE bidirectional audio channel; routes to host PipeWire via
 the existing per-uid bridge.

### Why this is the right tier-4 default

- Every package required is already in `openSUSE-Tumbleweed-Oss`.
- One-toplevel-per-VM matches tier 2 / 3's already-shipped pattern:
 chrome differentiation, broker gate, secctx tag — none need rework.
- No external repos / no fork / no downstream patch carry.

### Risks accepted

- **SPICE upstream is in maintenance mode.** If it bit-rots: switch to
 qemu's `-display gtk,gl=on` direct Wayland-toplevel, which sidesteps
 SPICE entirely.
- **No GPU passthrough** in SPICE; use `virtio-gpu-gl` with virgl when 3D
 matters. VFIO + Looking-Glass is rejected as the tier-4 default because
 it's gaming-niche and requires two GPUs.

## Tier 5 — per-app VM windowed (Linux guest)

**waypipe over `AF_VSOCK`.** A plumbing-only extension of the existing
tier-3 wrapper.

- The VM runs a tiny `qdistro-tier5-publisher` process that listens on
 `AF_VSOCK` (port allocated per VM) and is the inner waypipe endpoint.
 Inside the guest, user apps are ordinary Wayland clients of the
 publisher's synthesized display.
- The host runs `waypipe client --socket vsock://...` per app launch,
 mirroring tier 3's UNIX-socket bridge but across the VM boundary.
- Outer-side: each app shows up as a regular `xdg_toplevel` from the
 waypipe-client `wl_client`; gets the standard
 `qdwin_nested_manager_v1` + secctx + chrome treatment.
- PipeWire pixels and QDNI input ride separate vsock multiplexes.
- Audio: `qemu -audiodev pipewire` with an in-guest virtio/HDA codec. The
 guest's audio backend talks to QEMU on the host; QEMU streams to the
 admin uid's PipeWire daemon via the existing per-uid socket. No vsock
 plumbing needed for audio.

### Why waypipe-vsock

- **Wayland-native both sides.** No XWayland for guest apps.
- **Reuses qdistro's existing primitives entirely:** waypipe wrapper,
 `qdwin_nested_manager_v1`, broker clipboard / handoff gates,
 `wp_security_context_v1` (applied at vsock-accept time exactly like
 uid is applied at UNIX-accept today).
- Plumbing-only; no new compositor protocols, no fork carry.

### Rejected alternatives

| Option | Why rejected |
|----------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **SPICE seamless** | Listed as "future features" since 2010-2011; abandoned; no maintainer. 15-year roadmap stall. |
| **weston-rdprail-shell + VAIL** (WSLg for Linux) | Lives in Microsoft's `Weston-mirror` fork. Upstream rebase MR not merged. Adopting means carrying Microsoft's downstream weston build forever. |
| **DIY FreeRDP RAIL server** | ~10-16k LOC of bespoke compositor↔FreeRDP glue with no upstream landing path. Doesn't reuse `qdwin_nested_manager_v1`. |
| **Qubes `qubes-gui-agent`** | Fixed-header binary stream over Xen's libvchan. Wrong substrate (qdistro is KVM, not Xen). |
| **xpra** | Per-app pixel transport is non-zero-copy by design; xpra is generic remote-app, not VM-aware. Plan B if waypipe-vsock blocks. |
| **Looking-Glass** | Whole-display gaming-targeted; requires VFIO GPU passthrough. Wrong scope. |
| **virtio-gpu native context (drm_native_context)** | Whole-VM display only, not per-app. Tier-4 ingredient at most. |

### North-Star — virtio-gpu cross-domain + sommelier

Architecturally the cleanest answer: guest apps' Wayland surfaces pass
through the virtio-gpu device's cross-domain context, sommelier-in-guest
forwards them to the host's Wayland socket, and the outer compositor sees
**genuine `wl_surface` objects** — no remote protocol, zero-copy buffers.
crosvm proves it works.

Why qdistro is not building on this now: neither `crosvm`, `sommelier`,
nor `qemu-hw-display-virtio-gpu-rutabaga` is in
`openSUSE-Tumbleweed-Oss`. qemu's `-display dbus` + cross-domain CLI
parity is in flight upstream but not in a tagged release. Realistic
Tumbleweed availability is multiple quarters out. The decision is to
revisit when packaging gaps close.

## Tier 6 — remote machine

A separate physical machine acting as a remote-output target. Air-gapped
in spirit; framed, another machine. See [cross-machine](cross-machine.md).
