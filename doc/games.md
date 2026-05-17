# Games and full-hardware-access sessions

Covers workloads that need unmediated hardware access: games (especially
fullscreen-exclusive, VRR / HDR, anti-cheat), VR, GPU-heavy video editing.
The TTY-escape fullscreen mechanism from [architecture](architecture.md) is
the primitive; this document covers how it is used for these workloads.

## Why nested isn't good enough

The admin-compositor-hosts-everyone model costs a frame of compositing
latency and loses direct control of:

- **VRR / FreeSync / G-Sync** — usually needs a direct game-compositor-output
 relationship.
- **HDR tonemapping** — nested often tonemaps incorrectly or not at all.
- **Fullscreen exclusive mode** — nested compositors intermediate.
- **DRM master** — required for modesetting and some anti-cheat sanity
 checks.
- **Frame pacing** — an extra hop through the admin compositor perturbs it.

None of these matter for a terminal. All of them matter for a shooter at
240Hz or a VR headset.

## Mechanism

Admin (or user via a policy-permitted shortcut) requests a fullscreen
session → `qdistro-session-manager` allocates tty4+ → writes an ephemeral
greetd config → greetd launches the user's session with a game-suitable
compositor.

Compositor options for the game TTY:

- **`gamescope`** — Valve's nested compositor, designed for gaming. Frame
 pacing, FSR / NIS upscaling, HDR tonemap. Good default for Steam / Proton.
 Acquires DRM master directly via wlroots when launched on a TTY.
- **`cage`** — minimal kiosk compositor, one fullscreen app. Cleanest path.
 In-tree on Tumbleweed. Lifecycle: launches one fullscreen client, exits
 when the client exits — exactly the kiosk semantics we want. Cage 0.3 +
 wlroots 0.20 add `drm-lease-v1`, the right primitive for VR headsets.
- **No compositor** — XWayland or bare DRM client. Rare; for legacy games
 or benchmarks.

Reuse upstream. Don't write game-specific compositor infrastructure.

## Device grants

Admin policy pre-grants for a game-purpose user (typically a dedicated
`games-user`):

| Device | Default scope | Notes |
|-----------------------|----------------|----------------------------------------------------------------|
| GPU (DRM node) | persistent | Primary need. |
| `/dev/input/*` | persistent | Or per-session if paranoid. |
| `/dev/hidraw` | persistent | Needed for non-evdev peripherals (VR controllers, wheels). |
| Audio | persistent | Direct ALSA or PipeWire socket forward. |
| Network | per policy | Netns routing as with any user. |

Game saves and configs live in `games-user`'s home, isolated from other
silos.

## VT switching and lock interaction

- The admin compositor loses DRM master when the game's TTY is active
 (normal Linux VT behaviour).
- Admin's background services (broker, session manager, fprintd) keep
 running — they don't need DRM.
- Locker triggers (idle, lid close) force a VT-switch to tty3 → the admin
 compositor retakes DRM → the game session loses master and stops
 rendering. Processes keep running; their render loop blocks or errors.
 Some games recover on VT-switch back; some do not. Users should pause
 the game before locking.
- On unlock, admin can VT-switch back to the game's TTY to resume.

## Anti-cheat compatibility

TTY-escape fullscreen provides:

- Native kernel, no hypervisor / VM.
- Direct hardware access, no virtualization trickery.
- Standard Linux environment the anti-cheat vendor has likely tested.

Friendly to kernel-level anti-cheat. Linux anti-cheat support from vendors
is its own saga, separate from qdistro's architecture.

- **EAC (Easy Anti-Cheat) and BattlEye** both have official Proton/Linux
 paths since 2022; opt-in per game.
- **Vanguard (Riot)** is kernel-mode, detects KVM/VFIO, refuses to run
 under Proton or in a VM. Out of scope for qdistro; document and suggest
 dual-boot.

TTY-escape is the most anti-cheat-friendly surface Linux can offer.

## VR

The VR runtime (`monado` for OpenXR, SteamVR for proprietary) runs in the
TTY session. The headset is treated as an output. The admin compositor does
not see the headset at all while the VR session is active.

The standard approach is `monado` using **`drm-lease-v1`** to lease the
HMD output from the session compositor — *not* full DRM master. Both
gamescope (≥ 3.16) and cage (≥ 0.3) implement `drm-lease-v1`. This means
VR can coexist with the TTY-escape compositor running other outputs on
the same GPU; the compositor stays in charge of the rest of the desktop
while monado owns the headset's plane.

## Per-output granularity — not supported

Linux DRM master is per-GPU, not per-output. A game TTY session owns *all*
outputs on the GPU that drives it. You can't split "game gets HDMI, admin
keeps laptop panel" on a single-GPU system. Multi-GPU systems can dedicate
one GPU to the game TTY and keep admin on the other (pin via udev tag +
`qdistro.gpu=pci:<bus:slot.fn>` cmdline).

## Launch flow

The admin panel has a "Launch fullscreen session" action per user:

1. Pick user (e.g., `games-user`).
2. Pick launcher (Steam, VR runtime, specific binary, custom command).
3. Optional: **sandbox mode** — launch in nested gamescope under admin
 instead of TTY-escape, for testing or casual games that don't need
 DRM master.
4. The session manager allocates the TTY, starts the session, and
 VT-switches to it.

A user-facing shortcut ("Super+G → launch Steam as games-user fullscreen")
is permitted if admin policy allows.

## The trade

TTY-escape sessions are fullscreen-only. No seamless handoff, no
cross-user clipboard transfer into the running session, no admin approval
overlays visible. This is the explicit cost of tier choice: maximum
performance and hardware access in exchange for the seamless multi-user
UX. Users VT-switch back to admin (tty3) when done.

## Audio routing for games

Two options, with PipeWire socket-forward as default:

- **(default) PipeWire socket forward** — the games-user gets a
 bind-mounted view of admin's `/run/user/<admin>/pipewire-0` socket
 with an ACL granted via `module-protocol-native` `pipewire.access`
 rules. Same pattern as tier-3 silos. Consistent UX; one PipeWire
 graph; admin's volume mixer sees game streams.
- **(opt-in per game) Direct ALSA** — `audio` group + `/dev/snd/*` ACL.
 Maximum compatibility, lowest latency, but mutually exclusive with
 PipeWire on a single capture device. Use only when a specific game is
 broken under PipeWire.
