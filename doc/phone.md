# Phone integration

> **Status:** planned post-v1. The phone companion is cut from the v1 release
> (decision D4, `todo/decisions/v1-release-scope-2026-06-12.md`): v1 ships no
> presence attestation, no phone second-factor, no phone approver UI, and no
> window projection / camera input. The bootstrap installer chain still lays
> down the phone components (`scripts/install/install-phone-for-vm.sh`), but it
> does **not** enable them: `qdistro-phone.service` is `ConditionPathExists`-
> gated on `/etc/qdistro/phone/qdistro-phone.conf`, which a v1 install never
> creates, so the daemon stays inert and nothing phone-related runs. No v1
> security promise depends on a paired phone. (Follow-up to fully honor D4:
> gate the `phone` installer step out of the hardened/release profiles so the
> code is not even laid down — tracked as a bootstrap-profile hardening item.)
> Everything below is the design bar for bringing the companion back after v1.

Android phone (iOS best-effort) as a trusted peripheral for qdistro. Primary
uses:

- **Presence attestation** — phone's Bluetooth presence determines whether
 the user is physically near the laptop; absence auto-locks.
- **Second-factor auth** — TOTP codes or push-approval for admin auth.
- **Admin approver UI** — approve qdistro permission requests from the
 phone, not only from admin's tty3 session.
- **Window projection** — stream any qdistro window to the phone app for
 mobile viewing / control.
- **Camera input** — phone's camera virtualized into qdistro as a
 PipeWire-surfaced v4l2 device.

## Transport — Tailscale

Phone and qdistro machines are both on the user's Tailscale tailnet. That
gives us:

- **Authenticated mesh networking** — the phone's Tailscale identity is
 its device identity.
- **Works anywhere** — same addressing whether the phone is on LAN or
 roaming on cellular.
- **End-to-end encrypted** (WireGuard underneath).
- **MagicDNS / stable device naming** for service addressing.
- **Cross-platform clients** — Android (stable), iOS, desktop.

Tailscale handles auth, NAT traversal, and transport crypto. qdistro's
phone features are HTTPS services exposed on tailnet addresses, calling
into `qdistro-phone.service` on admin. The programmatic surface is
`tailscaled`'s LocalAPI (`/run/tailscale/tailscaled.sock`) for `whois` +
peer enumeration, with `tailscale status --json` as a CLI-shaped fallback.

## Companion-app strategy — reuse existing Android stack

qdistro does **not** ship a custom Kotlin Android app. The companion-app
matrix reuses existing components:

| Feature | Path |
|---------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Presence | BlueZ DBus RSSI of the phone advertising a stable BLE service UUID (KDE Connect emits one when its Bluetooth backend is on; or any per-phone known MAC). |
| 2FA | TOTP via Aegis (F-Droid) or any RFC-6238 client. Seed delivered at pairing via QR. |
| Push approval | UnifiedPush via self-hosted **ntfy** (Tumbleweed-packageable; podman-runnable). Phone runs the ntfy app as the UnifiedPush distributor. qdistro-phone publishes push messages with action buttons. |
| Window projection | qdistro emits an RDP target. Phone uses **Microsoft "Windows App" / RD Client** on Android over the tailnet interface. |
| Camera virtualization | DroidCam / Iriun-as-webcam → host-side `v4l2loopback` (Tumbleweed `v4l2loopback-utils`). |
| File send / clipboard / media | KDE Connect (Tumbleweed-default + officially maintained Android app), used **only for convenience features**. qdistro never grants approver authority through KDE Connect. |

**Note:** KDE Connect is reused for general convenience features
(file/clipboard/media). It is **not** used as the approver-authority transport;
qdistro layers its own per-phone key on top of Tailscale's identity for any
approver-grade action. (Earlier design used Tailscale + KDE Connect; the
authoritative transport is **Tailscale** plus a qdistro-specific HTTPS
service.)

## Pairing

Phone and laptop must both already be on the tailnet — that's done once via
Tailscale's own SSO auth, independent of qdistro.

Then qdistro-specific pairing:

1. The admin panel lists tailnet devices visible to this machine.
2. Admin clicks "Add as qdistro phone" next to the phone's entry.
3. The admin panel generates a short-lived pairing token + QR code.
4. The user opens the qdistro companion surface on the phone; scans QR.
5. The phone connects to admin's Tailscale address over HTTPS with the
 pairing token.
6. Admin confirms on the desktop (fingerprint prompt).
7. Exchange of qdistro-specific per-phone key material; the phone is
 registered.

Phone identity is anchored in two layers:

- **Tailscale device identity** — required to be on the tailnet at all.
- **qdistro per-phone key** — required for any qdistro operation.

Revocation: remove from Tailscale, remove from qdistro's approved list, or
both (defence-in-depth).

Per-phone trust level:

- **Limited** — presence detection only; no auth or approval authority.
- **Trusted** — presence + 2FA + can approve admin requests.
- **Full** — trusted + window projection + camera virtualization.

## Presence detection / auto-lock

Proximity is measured by **Bluetooth LE, not Tailscale.** Tailscale reports
"the phone is on the network" regardless of physical distance — a phone on
cellular in another country is still on the tailnet. We need *local
proximity* specifically, so Bluetooth is the right primitive. Tailscale and
Bluetooth serve different purposes and coexist.

- The phone-side service advertises a qdistro-specific BLE service UUID.
- `qdistro-phone.service` on admin scans continuously; the latency-tolerant
 signal is smoothed with a moving window.
- **Policy:** phone UUID absent for N seconds (default 30) → admin
 compositor locks (same code path as lid close / idle).
- **Graceful return:** the phone reappears → no auto-unlock. The
 fingerprint is still required to unlock.
- **Fallback:** if Bluetooth fails entirely, presence becomes "unknown."
 Admin policy decides whether that means conservative-lock or continue.

## Second-factor auth

On admin unlock (fingerprint), optionally require a second factor:

1. **TOTP.** Companion app has a TOTP generator. The user reads the 6-digit
 code, types into the qdistro locker. Works even when the phone has no
 network.
2. **Push approval.** The admin locker sends an HTTPS request to the
 phone's Tailscale address; the companion surface prompts; the user
 biometric-approves; a signed response returns. Requires a phone
 reachable on the tailnet.
3. **Hardware token** (e.g., YubiKey) — separate from the phone; listed
 for completeness.

Admin policy sets the required factor per context (login / unlock /
sensitive action).

## Admin approver on phone

Polkit approval prompts generated by `qbus-admin` can route to the phone
in addition to (or instead of) admin's polkit agent on tty3.

Flow when policy says "approve from phone":

1. A user action on a silo triggers a permission request.
2. `qbus-admin` generates a polkit action.
3. Admin's polkit agent on tty3 AND the phone both show the prompt (or
 only the phone, depending on policy).
4. First response wins. The user approves on the phone → a signed response
 relays over tailnet HTTPS → polkit records the decision → the action
 proceeds.

Useful for approvals while the laptop is closed (phone stays connected via
LAN or Bluetooth), or for travelling.

**Trust-critical.** The phone is effectively extending admin's authority.
Compromising the phone or its pairing key means compromising admin.

Mitigations:

- Pairing is per phone, revocable from the admin panel.
- Phone biometric required to approve (not just screen-unlocked).
- The feature can be disabled per phone in admin policy.

## Window projection

The user wants to view / interact with a qdistro window on the phone.

Mechanism: reuse remote-output infrastructure (see
[cross-machine](cross-machine.md)). The phone is a lightweight
remote-display client connecting over the tailnet (RDP, via the FreeRDP
backend already shipping for tier-4). Tailscale provides encryption and
auth; no additional tunnelling needed.

- Admin panel: right-click window → "Project to phone."
- qdistro opens an output stream scoped to just that window (or a specific
 user session).
- **Phone-side renderer is Microsoft RD Client for Android**, not a
 qdistro-built decoder. RD Client is free, widely deployed, supports
 windowed connections, and runs over the tailnet natively. qdistro emits
 an `rdp://<tailnet-ip>:<port>` deep-link; RD Client takes the URL via
 Android intent.
- Touch input on the phone translates to pointer events back to qdistro
 through RD Client's RDP path.

Latency is acceptable for desktop work but not for gaming / VR.

## Phone as camera

The phone's camera feed is virtualized into qdistro as a v4l2 source
available through admin's PipeWire.

Mechanism:

- The companion app accesses the phone camera, encodes (H.264 or MJPEG),
 and streams to admin over the tailnet (HTTPS or a WebRTC data channel).
- `qdistro-phone-camera` (admin-side) decodes and feeds into a
 v4l2loopback device.
- Admin's PipeWire picks up the v4l2 node; it's available to user sessions
 like any other camera (subject to polkit approval scopes).

Use cases: better image than the built-in laptop webcam, second camera
angle for recording, document camera (phone suspended over paper), unified
virtual device when the built-in camera is broken or blocked.

## Policy (per-phone feature toggles)

Admin panel per paired phone:

```
Phone: Pixel 9 Pro (trust: Full)
 [x] Presence detection (auto-lock)
 [x] Second factor auth
 Method: push-approval
 [x] Admin approver
 Scope: all polkit prompts
 Biometric required: yes
 [x] Window projection
 Default destination: admin's windows only
 [x] Camera virtualization
 Allowed to users: dev-user (per polkit)
```

Multiple phones can be paired. Typical: primary phone + backup phone.
Features can differ per phone.

## Trust model specifics

The phone is a **separate physical device with its own attack surface.** If
it's compromised or stolen, an attacker gains:

- Ability to approve admin permissions (if "admin approver" was enabled).
- TOTP seeds (if TOTP was used).
- Pairing certificate.

Mitigations:

- Revoke pairing from the admin panel immediately if the phone is lost.
- Require biometric on the phone for any qdistro action; don't rely on
 just screen-unlocked.
- Feature granularity in policy — a backup phone might only have presence
 + TOTP, not approver.
- Short session tokens for push approvals (the phone approves one action,
 not a blanket session).

Phone compromise is approximately as severe as laptop compromise for the
admin's authority. Pair and handle accordingly.
