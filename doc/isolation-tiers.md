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
| 4. VM whole-window| KVM + libvirt + waypipe (nested qdwin) | Yes |
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

Scope of "independent verification": this holds for **direct broker
authorization**, where the broker resolves the D-Bus caller's pid and
checks its SELinux context itself. It does **not** currently hold for
**qdshell-mediated decisions** (e.g. clipboard / handoff gates), where
qdshell forwards `(app_id, instance_id, secctx)` strings to the broker
but the broker has no source/destination application pid to re-verify
per call. Treat secctx-only decisions on that path as advisory until
the protocol carries process identity end-to-end. See
`todo/qdistro-qdwin-wider-codex-review.md` finding #2.

## Tier 2 — podman / container

Default for most user-owned apps. Rootless podman + `--userns=keep-id`
plus a per-container runtime dir at
`$XDG_RUNTIME_DIR/qdistro-tier2/<launch-token>/` (mode 0700,
admin-owned, rm-rf'd on spawn-script exit). The host's `/run/user/<uid>`
is **not** exposed; only the resolved outer wayland socket
(`wayland-secctx-NN` when wrapping with `qdistro-secctx-exec`, or
the unwrapped name like `wayland-1` when `TIER2_USE_SECCTX=0`)
and any `pipewire-N`/`pipewire-N-manager` sockets that exist at spawn
time are bind-mounted in as individual files. The container therefore
can't see the dbus session bus, ssh-agent, gnupg-agent, or sibling
tier-2 sockets.

Hardening flags applied by `tier2/spawn-tier2.sh` (overridable via
`TIER2_*` env knobs):

  - `--cap-drop=ALL` (CapEff = 0)
  - `--security-opt=no-new-privileges`
  - `--network=none` (default — relax with `TIER2_NETWORK=slirp4netns`)
  - `--pids-limit=512` (only delegated cgroup controller in the
    typical Tumbleweed user@1000.service setup; memory/cpu need
    `Delegate=memory cpu pids io` drop-in to use)
  - `--ipc=private --pid=private`
  - `--read-only` rootfs + tmpfs at `/tmp`, `/var/cache`,
    `/home/admin/.cache`, `/run` (ENOSPC on any image-rootfs write)

The container's nested compositor (qdwin-shell.so) connects to the
outer admin compositor's Wayland socket and advertises each inner
toplevel via `qdwin_nested_manager_v1`.

Tier-2 is the **first tier with first-class qdshell launcher integration**
(badged app icons, click-to-launch, placeholder taskbar entry on cold
start). The full design — image-per-workload model, secctx contract,
podapps discovery, cold-start UX — is in [containers.md](containers.md).
Host-side bits live under `tier2/`; bats coverage is the
`phase7-tier2-*` block in
`tests/integration/vm/tiered-isolation.bats`.

## Tier 3 — different user (waypipe over UNIX)

**Status: shipped 2026-05-16.** Primary mechanism for data-silo
separation. `qdistro-tier3-spawn <silo_user> -- <app...>` runs
`waypipe client` as admin uid 1000 (creating a bridge socket against
the local `wayland-1`), and `waypipe server -- <app>` as the silo
uid (which connects to the same socket and exposes a synthetic
`wayland-tier3-<silo>-<pid>` display for the app).

Cross-uid socket access is gated by the `qdistro-tier3` group — the
silo can't reach `wayland-1` directly (qdwin's `QDWIN_ALLOWED_UID`
rejects via `SO_PEERCRED`), but waypipe-client on the admin side
passes the gate and re-marshals the protocol. Default `--no-gpu`
(SHM-only, VM-safe; flip on accel3d clones); `--oneshot` per bridge
so each silo lifecycle is independent.

The bridge socket lives at
`/run/qdistro-tier3/qdistro-tier3-<silo>-<token>.sock`. Mode 0660
group `qdistro-tier3`, born under `umask 0117` so the inter-silo
hijack window is closed before waypipe accepts the first connection.
Each spawn writes a sidecar `<sock>.pid` file so the orphan reaper
has a forgery-resistant way to check whether the owner is alive.

Each tier-3 toplevel arrives at qdwin tagged via
`wp_security_context_v1` (engine `qdistro.tier3`, app_id
`qdistro.tier3.<silo>`, instance_id = the spawn's LAUNCH_TOKEN).
qdshell's `Tier3Apps` singleton filters `Qdwin.windows` on the
prefix and exposes per-silo chrome + colour via the standard
qdshell rendering path.

For headless test-driving of cross-silo focus injection (Qubes-style
focus-aware-clear from spec/10 v14), qdshell ships
`Tier3FocusIPC` — a Quickshell IPC handler bound to target
`tier3focus` with `injectFocus(handle)` / `clearSelection(seat)` /
`findSiloHandle(silo)` / `selectionState()` operations. Driven by
`qs -p /usr/share/quickshell/qdshell ipc --any-display call …` from
the bats drivers.

Reference: `qdistro/tier3/README.md` for the operator-facing entry
point; `tests/integration/vm/s{35..41,48}-*.sh` for the 8 bats
drivers; `todo/qdwin-vm/tier3-spawn-design.md` for the original
design + the 2026-05-16 hardening deltas from the two-round
security/correctness/operational review.

## Tier 4 — whole-VM windowed

**libvirt + QEMU + waypipe** (nested qdwin in the guest) as one chromed peer toplevel,
mirroring tier 2's container pattern.

Stack:

- libvirt driving QEMU. Both packaged on Tumbleweed.
- Display: waypipe server in guest → host waypipe-client over AF_VSOCK as a
 Wayland toplevel. The host wraps the waypipe client with
 `qdistro-secctx-exec` so the compositor sees the VM silo tag.
- The outer wraps the viewer toplevel via `qdwin_nested_manager_v1`
 exactly like tier 2 today.
- Clipboard: Wayland data-device over waypipe (same `ClipboardGate.qml` as tier-3),
 `CheckClipboardTransfer` gate.
- Audio: `-audiodev pipewire` + `virtio-snd`; routes to host PipeWire via
 the existing per-uid bridge.

### Why this is the right tier-4 default

- Every package required is already in `openSUSE-Tumbleweed-Oss`.
- One-toplevel-per-VM matches tier 2 / 3's already-shipped pattern:
 chrome differentiation, broker gate, secctx tag — none need rework.
- No external repos / no fork / no downstream patch carry.

### Risks accepted

- **Nested compositor cost.** The guest runs qdwin plus app workloads, so
 default memory is higher than tier 5b.
- **No GPU passthrough by default.** Use `virtio-gpu-gl` with virgl when 3D
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
| **Legacy VM seamless display** | Listed as "future features" since 2010-2011; abandoned; no maintainer. 15-year roadmap stall. |
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
