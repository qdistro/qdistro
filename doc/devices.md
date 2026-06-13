# Devices and hardware

The forward direction for hostile-input hardware (network, print/scan,
eventually Bluetooth) is to move its management into device silos —
dedicated VMs publishing narrow protocols; see
[device-silos.md](device-silos.md). The rules below govern hardware that
stays on the host.

## Admin owns all hardware

Regular nested sessions cannot directly open device nodes for sensitive
hardware. Policy-approved fullscreen TTY sessions are the explicit exception:
games, VR, and similar sessions may receive direct GPU/input/hidraw/audio
grants scoped to that session and revoked on teardown. Enforcement is layered:

1. **SELinux** (or AppArmor) policy denies `/dev/snd/*`, `/dev/video*`,
 Bluetooth sockets, NetworkManager D-Bus, etc. to non-admin contexts.
2. **cgroup device whitelist** on user sessions — kernel-level deny for
 device nodes not on the whitelist.
3. **D-Bus policy** — system services (NetworkManager, bluez, fprintd) only
 accept calls from admin's uid.
4. **polkit rules** — qdistro actions namespaced `org.qdistro.*` always
 route to admin's approval agent.

Nested users access devices only via **virtualized endpoints** surfaced by
admin-side services. Fullscreen hardware sessions use direct grants only when
the session policy says so.

## Audio and camera — PipeWire

PipeWire is the model that fits perfectly.

- **Admin's host PipeWire daemon** owns ALSA, V4L2, and libcamera nodes for
 nested-session use. In the default virtualized-device path, user sessions see
 only PipeWire-mediated endpoints; policy-approved fullscreen sessions are the
 explicit direct-grant exception.
- **Per-user PipeWire instances** run inside each user session (via
 `systemd --user`). They link upward to admin's PipeWire as downstream
 clients.
- **Per-user virtual streams** — admin's PipeWire creates virtual
 sinks/sources per user, policy-gated. A user sees a "camera" or
 "speakers" in their session because admin's PipeWire is feeding them one.
- **Grant/revoke flow** — admin approves via the polkit agent. On approval,
 admin's PipeWire creates the virtual stream; on revoke, the stream is
 torn down and disappears from the user.
- **Hardware privacy switches** — laptop camera shutter, mic mute key —
 route through the kernel and override any software grant.

PipeWire supports per-client access policy. A user's notebook app can have
mic access while the same user's browser does not.

### Virtual camera with transformations

Admin's PipeWire pipeline can apply transformations to camera streams before
surfacing them to user apps:

```
real webcam ──► admin PipeWire source
 │
 v
 [ transform pipeline ] ← ML models / OpenGL filters
 │
 v
 named virtual camera node (v4l2loopback)
 │
 v
 user app picks it as a normal camera
```

Transformations available include background blur (CPU ML), background
replacement, colour correction, composition with the phone camera
(see [phone](phone.md)), and privacy masking (face / badge / screen
redaction via detection + blur).

Admin panel manages named virtual camera devices, each with a configurable
pipeline. User apps see each as a distinct `/dev/video*` node (v4l2loopback)
and select it like any camera.

Video conferencing is assumed to happen in the **browser** (Zoom web, Google
Meet, Teams web) in a locked-down sandboxed browser session. Effects are
owned by qdistro's virtual-camera pipeline, not by each web app's code —
centralizes control and avoids trusting per-service pipelines. CPU-only
ML transforms; modern laptop CPU handles 720p/30fps background blur at
~30-40%.

## Network — NetworkManager + per-user netns

- **NetworkManager** runs as admin. Only admin's session has the NM UI.
 Users cannot list or join networks.
- **Per-user network namespace** assigned at user creation. Admin wires a
 veth pair between admin's netns and the user's; routes the user's traffic
 through admin's stack per policy.
- **Admin routing policy** per user:
 - Direct (same as admin's net).
 - Through VPN X.
 - Through HTTP proxy Y.
 - No network (isolated silo).

## Bluetooth

- **bluez** runs as admin. Only admin can pair / unpair.
- Per-user access to paired devices is polkit-gated. Admin approves
 "dev-user may use Bluetooth headphones" → the session manager grants
 permission via bluez's D-Bus interface.

## Display config

- **Admin-only UIs** for resolution, scaling, rotation, and output layout.
- **User compositors / games can set modes within their own context.** In a
 fullscreen TTY session, the user's compositor has DRM master
 and can mode-set freely.
- In nested mode, a user app requesting a different resolution asks the
 nested compositor, which either scales or requests from admin's
 compositor.

## Fingerprint reader

- **fprintd** is admin-scope. Only admin enrols prints.
- The PyQt locker and any other `pam_fprintd` consumer (e.g. `sudo`) both
 read from admin's DB. (There is no tty2 text login — see `doc/sessions.md`.)

## Approval scopes

polkit supports action-scoped authorization decisions. qdistro uses:

- **Per-use** — approve each individual access (e.g., each camera frame
 capture session). Safest, most intrusive.
- **Per-session** — approve for the lifetime of the *user session*; revoked
 on session stop. Good default for mic / camera.
- **Per-app** — approve for a specific app within a user session.
- **Persistent** — remembered across reboots. Fine for audio output and
 Bluetooth headphones.

### Default policy

| Resource | Default scope |
|---------------------------------------|------------------------|
| Camera | per-session + per-app |
| Microphone | per-session + per-app |
| Location | per-use |
| Audio output (speakers / headphones) | persistent + per-user |
| Clipboard paste from peer | per-use or per-session |
| Screen recording | per-use |

Admin can adjust per user.

## Revocation and visibility

- **Admin panel**: a live list of currently-granted device streams. One-click
 revoke tears down the stream immediately (PipeWire disconnects the client,
 the polkit action is marked expired).
- **On-screen indicator** in the admin compositor: a top-right red dot
 whenever a sensitive device is live in *any* session. Hover shows which
 session and which device. Matches the mobile-OS convention.
- **Hardware kill switches** always win over software grants.

## Lock-time capture policy

The lock screen is still the active privacy surface. Existing capture may
continue only when the grant explicitly allows lock continuation. New capture
requires admin unlock.

The lock UI shows non-suppressible indicators for active microphone, camera,
screen capture, system-audio capture, virtual input/accessibility control, and
qdistro-specific network egress. Network egress is a qdistro extension to the
usual mic/camera privacy-indicator model.
