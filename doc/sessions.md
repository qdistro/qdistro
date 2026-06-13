# Authentication and sessions

## There is no user login

qdistro has **no user login screen**. This is a deliberate departure from
typical Linux multi-user.

The only authentication boundary is **admin**. Admin authenticates once
(fingerprint or password); once authed, all user sessions admin has marked
"available" are accessible without further credentials.

Users are not humans in qdistro; they are implementation identities for silos
and sessions spawned by admin's session manager. There is only ever one human.
Fingerprint = "the owner is present."

This does not collapse work into one context. A session is a dynamic set of
processes and attached or reserved resources. The owner may keep separate TTY
sessions for strong mental separation, use a mixed desktop where multiple
silos share one compositor, or run a headless session for an automated
workflow.

## Session launch chain

Each TTY starts a greetd instance with a role-specific config:

| TTY | greetd config | Runs |
|-------|------------------------------------------|---------------------------------------------------------------------|
| tty1 | (none — agetty only) | Raw text login, emergency only. |
| tty2 | (none — no greetd config) | No qdistro VT login is wired here. Text-mode recovery when Wayland is down is via GRUB rescue/emergency or a read-only snapshot boot (`doc/recovery.md`). |
| tty3 | `default_session.command = /usr/bin/qdgreeter` (deploy/greetd-config.toml) | Graphical qdgreeter → admin auth via greetd JSON-IPC → `qdwin-session-launcher` → `qdwin-session.target` → qdwin compositor + qdshell. |
| tty4 | `default_session.command = qdistro-startlxqtwayland` (deploy/greetd-config-fallback.toml, run by greetd-fallback.service) | **Escape hatch — dev/test bakes only.** A *passwordless* `admin` LXQt+labwc autologin for recovering from a broken qdwin commit, reachable via Ctrl+Alt+F4. Enabled only under the `dev` profile where the LXQt stack is installed; on daily-driver/release the unit is installed-but-disabled (the passwordless graphical admin VT would bypass the locked tty3 greeter). Production recovery is via GRUB (`doc/recovery.md`). Documented in `deploy/AGENTS.md`. |
| tty5+ | Dynamic; session manager writes ephemeral configs | TTY work sessions, fullscreen user sessions, special-role sessions, or VM viewers. |

`systemd.default_vt=3` boots to admin.

Admin-configured sessions may autostart or autologin before the owner performs
the first admin login after boot. They may run background jobs and use network
if their policy allows it, but they must not be visible or interactable until
admin authenticates and the machine lock is cleared.

> **History:** before P01 (closed 2026-05), tty3 ran
> `qdistro-startlxqtwayland` (LXQt+labwc) with qdshell as a
> parity-test overlay. P01 made qdgreeter functional and made qdwin
> the actual compositor greetd boots; LXQt+labwc demoted to tty4.

### Greeter keyboard grab and `_greeter` input access

On eglfs (the tty3 boot path has no compositor of its own), qdgreeter reads
the keyboard by opening a raw evdev device and taking an **exclusive grab**
(`EVIOCGRAB`) before Qt starts — so every pre-auth keystroke is routed through
the greeter, not leaked to a background VT. Properties of this surface, which is
**accepted and bounded**, not a hole to close:

- The grab is held for the greeter's whole lifetime and the greeter **exits on
  successful auth** (`controller.succeeded → app.quit`), at which point greetd
  starts the user session. The grab is **released explicitly** — `EVIOCGRAB 0`
  plus an fd close on `succeeded`/`aboutToQuit` and in a `finally` around the
  event loop (`qdgreeter/app.py`, `_RawKeyboardBridge.release`) — rather than
  relying only on implicit process-exit cleanup. It grabs **one** device (the
  first candidate that succeeds), not every input node.
- `_greeter` is an unprivileged system user (`useradd --system`, `nologin`,
  `/nonexistent` home), so the blast radius of holding the grab is small.
- `_greeter` gets read access to `/dev/input/*` via static **`input`-group**
  membership (`enable-qdgreeter.sh` / `qdistro-bootstrap.sh`:
  `usermod -aG video,render,input,tty _greeter`). Seat-scoped logind `uaccess`
  device ACLs do **not** apply here: those grant the *active logind session's*
  user on a seat, but qdgreeter runs as a **seatless greetd system service**
  with no logind session of its own. The `input` group is therefore the minimum
  mechanism the platform actually offers for a raw-evdev greeter, and is
  documented here as an accepted, bounded surface.
- Narrower device scoping (a udev rule granting only `_greeter` the keyboard
  event nodes, or a systemd `DeviceAllow=`/`SupplementaryGroups=` on the greetd
  unit) may be considered **per-appliance, only after hardware-specific
  testing**: a mis-scoped rule across keyboards/USB-hubs/initramfs timing can
  brick keyboard login on tty3 (the only graphical login), so it is not folded
  into the base image.

## PyQt locker

The locker is a **subsystem of the admin compositor**, not a separate
application or overlay. Properties:

- The compositor owns lock state directly. When `locked == true`:
 - No user-session surfaces are rendered.
 - No input is dispatched to user sessions.
 - Only the lock UI and admin background render.
- The lock UI is Qt / QML, rendered by the admin compositor shell.
- Auth calls `fprintd` over D-Bus (no PAM on the interactive path).
- Password fallback uses PAM (the same admin account).
- Successful auth transitions the compositor to `unlocked`; user-session
 surfaces become reachable again.

### Lock triggers

- Idle timer (configurable, default 5 minutes (300 s)).
- Lid close (via `systemd-logind`).
- Manual "Lock now" from the admin panel or shortcut.
- System suspend.

### Lock scope

A single lock covers the whole machine. No per-user locks. Matches the
single-tenant assumption. Fingerprint unlocks everything at once.

Lock is a visibility and input gate, not a normal process-freeze mechanism.
User sessions can keep running while the machine is locked: downloads,
already-approved network jobs, and other background work may continue. New
privilege grants and new cross-silo approvals require admin to unlock first.

"Already approved" is not sufficient by itself. A grant that may continue
while locked carries an explicit lock-continuation bit. Starting new
mic/camera/screen capture, virtual input, screencopy, new privilege grants,
new resource attachments, and new cross-silo approvals requires admin unlock.

Lock-time defaults:

| Activity | Lock behavior |
| --- | --- |
| Audio output already playing | continue if grant allows continuation |
| Active call media/capture | continue only if pre-approved for lock continuation |
| New mic/camera/screen/system-audio capture | require unlock |
| Virtual input / accessibility control | require unlock unless a specific workflow says otherwise |
| Running games | keep process alive; rendering may pause or lose DRM depending on TTY state |
| VR / immersive session | prefer presence/idle policy, not desktop lock alone |
| Recall viewing (post-v1; cut from v1) | revoke viewer grant and clear decrypted results |

The lock UI must show non-suppressible indicators for live microphone,
camera, screencast/screen capture, system-audio capture, virtual input or
accessibility control, and qdistro-specific network egress.

User sessions do not run independent screenlockers and must not prompt for the
admin/root password. When locked, the only unlock path is the admin locker. For
TTY sessions, the visible seat is forced to the admin lock surface or kept
there, and switching to non-admin TTYs is blocked until admin authenticates.

## Fingerprint handling

- Hardware: the laptop's built-in fingerprint reader.
- Service: `fprintd` running as a system daemon.
- Enrolment: **multiple fingers** (primary + backup in case of a cut or
 bandage), all enrolled under the **admin** account. Regular users'
 fprintd DBs stay empty.
- Locker flow: the locker calls `net.reactivated.Fprint.Device.VerifyStart`
 against admin's enrolled prints; any match unlocks.
- The same `pam_fprintd` admin enrolment is also used by any other PAM
 consumer configured for it (e.g. `sudo`/`su`); there is no separate tty2
 text-login fingerprint path (tty2 has no qdistro login — see the TTY table).

`fprintd` stores enrolled templates per Linux user — there is no native
"shared fingerprint DB accessible to multiple users." For qdistro's
"admin authenticates, all sessions become reachable" model, all fingers enrol
on admin; the locker (which runs in admin's session) auths against admin's DB
directly. A future PAM module backed by a system-wide fingerprint store would
let any context verify against admin's enrolled fingers.

## Admin-controlled silo lifecycle

A separate daemon, `qdistro-session-manager.service`, owns silo lifecycle for
the current uid-backed implementation. A "silo" is a qdistro resource kind: an
isolated program context with state and data. Current code often backs a silo
with a Linux uid plus per-silo state (subvolume, runtime dir, cgroup-v2 scope)
and a registry entry the broker reads when routing send-to / cross-uid actions.
The terms "user" and "silo" are used interchangeably in older spec text; new
code and D-Bus surfaces should use "silo" for the resource kind and "session"
for the dynamic process/UI context.

A silo can be attached to sessions in different ways: UI surfaces, directory
mounts, app state, credentials, or one-shot transfers. Those attachment rules
are not all the same. A source tree may be mounted in more than one session;
a browser profile or signing authority may require stricter brokered use.
See [attachments.md](attachments.md).

A silo also has an owner-facing workload definition: desired state, parameters,
bootstrap steps, health checks, recovery actions, rollback policy, and
capability guardrails. For that higher-level contract, see
[silos.md](silos.md). The state machine below is the current implementation
lifecycle for uid-backed silos, not the full health model.

`Silo` is a resource kind. Future registry-backed implementations should expose
`spec`, `status`, stable `uid`, `generation`, and finalizer-based deletion as
defined in [resources.md](resources.md), even when the current implementation
is still uid-backed.

### Session-manager silo states

The state machine has six states. Two of them (`Stopping`, `Deleting`)
are transient — they're observable on the `SiloChanged` signal for UI
progress badges, but settle to a resting state within a few seconds.

- **Created** — `useradd` happened, per-silo state dir exists, but
 `systemctl start` has never run for the silo. Initial state after
 `CreateSilo`.
- **Active** — silo's launcher unit is running; cgroup is populated;
 surfaces render when admin is unlocked. (Spec's old "running.")
- **Frozen** — cgroup-v2 `cgroup.freeze=1`; no CPU; surfaces hidden;
 admin can `ResumeSilo`. This is `cgroup.freeze`, not POSIX SIGSTOP —
 syscalls in flight unwind cleanly when thawed. (Spec's old
 "paused / frozen.")
- **Stopping** — transient. SIGTERM has been sent; the daemon is
 waiting for the grace window before SIGKILL. `SiloChanged` fires
 once on entry and once on Stopped.
- **Stopped** — account still exists, but no processes are running and
 the cgroup is empty (or has been removed). `DeleteSilo` is only
 legal from this or `Created`.
- **Deleting** — transient. `userdel`, state-dir teardown, cgroup
 removal in progress. On success the silo's row vanishes from
 `ListSilos`; on failure mid-teardown the silo is rolled back to
 `Stopped` with a `SiloChanged` emit.

Silos that don't exist in `silos.yaml` are simply absent from
`ListSilos`; the spec's old "absent" state is now "no row in the
registry."

### Admin panel operations

A PyQt app in admin's session:

- **Create silo** — wraps `useradd -m -u <uid>` and replaces the
 created `/home/<name>` with a btrfs subvolume so each silo has its
 own snapshot / quota boundary. Also seeds qdistro metadata
 (colour, default isolation tier, default device grants, netns
 policy) — planned (post-P02).
- **Delete silo** — teardown silo + `userdel -r`. Only legal from
 `Stopped` or `Created`.
- **Start / Stop / Freeze / Resume** — session manager transitions
 state. Freeze/Resume use cgroup-v2 `cgroup.freeze` rather than
 POSIX-signal pause so SDK hooks and signal handlers behave
 predictably across the pause.
- **Edit permissions** — device grants, clipboard policies, netns,
 per-app isolation tier. **Planned (post-P02).**
- **Schedule** — optional; systemd timers can freeze/resume silos on
 time windows. **Planned (post-P02).**

### D-Bus surface

Bus name `org.qdistro.SessionManager1` on the system bus; object path
`/org/qdistro/SessionManager1`.

```
method  CreateSilo(s name, i uid)        → ()
method  DeleteSilo(s name)               → ()
method  StartSilo(s name)                → ()
method  StopSilo(s name, i grace_s)      → ()
method  FreezeSilo(s name)               → ()
method  ResumeSilo(s name)               → ()
method  SetSiloEgress(s name, s egress)  → ()   # "" | none | direct | wg:NAME
method  ListSilos()                      → (s)   # JSON-encoded array
signal  SiloChanged(s name, s state)
```

`ListSilos` returns a JSON-encoded `s` (not `aa{sv}`) so the same wire
shape is consumable from `gdbus` / `busctl` / Python without an
introspection-driven binding. Subscribers to `SiloChanged` may see the
transient `Stopping` / `Deleting` states followed by the resting
`Stopped` / row-deletion; treat unknown state strings as "transient,
wait."

The qdshell lock screen consumes the same `ListSilos` rows for the
non-suppressible network-egress indicator. Active tier-3 rows with
`egress: null` are shown as legacy host egress, `direct` and `wg:NAME`
are shown by their policy, and `none` is treated as intentionally dark.
The indicator refreshes on `SiloChanged` and by a safety-net poll.

Error names live under `org.qdistro.SessionManager1.*`:
`UnknownSilo`, `SiloExists`, `SiloBusy`, `BadState`, `BadArgument`,
`NotAuthorized`, plus a `Generic` fallback for unexpected
side-effect failures.

`StartSilo` invokes `systemctl start qdshell-session-<name>@<uid>.service`.
The launcher unit is a templated systemd service provided by the
qdshell package (to be added in a follow-up task).

## What admin "unlock" does

1. fprintd verifies the print.
2. The compositor transitions to `unlocked`.
3. Non-admin TTY switching becomes available again.
4. The compositor starts rendering allowed user-session surfaces.
5. Input is dispatched normally.

No per-user auth at any point. One fingerprint, everything becomes reachable.
Recall is the exception: admin unlock makes live sessions reachable, but it
does not grant ambient historical Recall browsing. Recall viewing needs its own
time-boxed viewer grant.

## Admin logout / compositor crash

Admin's compositor runs under systemd with `Restart=always` via greetd. If it
crashes or admin logs out:

1. greetd notices the session ended; systemd restarts it — a fresh admin
 compositor comes up in the locked state.
2. As soon as admin's compositor is gone, the machine is treated as locked.
 Nested user-session surfaces are no longer reachable because the admin
 compositor is their trusted renderer and input gate. TTY user sessions are not
 reachable until admin auth returns. Processes may keep running underneath,
 subject to their normal resource policy.
3. Admin authenticates again → sessions become reachable → the compositor
 renders allowed surfaces.

If admin logs out deliberately, the flow is the same.

Admin cannot log out in a way that leaves user sessions visibly active —
rendering depends on admin's compositor. The admin session is the host, not
a peer; there is no usable state with "no admin." Reboot-to-locked is
effectively the same as admin log-out-then-back-in.
