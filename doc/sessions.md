# Authentication and sessions

## There is no user login

qdistro has **no user login screen**. This is a deliberate departure from
typical Linux multi-user.

The only authentication boundary is **admin**. Admin authenticates once
(fingerprint or password); once authed, all user sessions admin has marked
"available" are accessible without further credentials.

Users are not humans in qdistro; they are **uid-scoped sandboxes** spawned
by admin's session manager. There is only ever one human. Fingerprint =
"the owner is present."

## Session launch chain

Each TTY starts a greetd instance with a role-specific config:

| TTY | greetd config | Runs |
|-------|------------------------------------------|---------------------------------------------------------------------|
| tty1 | (none — agetty only) | Raw text login, emergency only. |
| tty2 | `initial_session = tuigreet` | Textual admin login via PAM (fingerprint or password) → shell. |
| tty3 | `default_session.command = /usr/bin/qdgreeter` (deploy/greetd-config.toml) | Graphical qdgreeter → admin auth via greetd JSON-IPC → `qdwin-session-launcher` → `qdwin-session.target` → qdwin compositor + qdshell. |
| tty4 | `default_session.command = qdistro-startlxqtwayland` (deploy/greetd-config-fallback.toml, run by greetd-fallback.service) | **Escape hatch.** Legacy LXQt+labwc session for recovering from a broken qdwin commit. Reachable via Ctrl+Alt+F4. Documented in `deploy/AGENTS.md`. |
| tty5+ | Dynamic; session manager writes ephemeral configs | Fullscreen user sessions or VM viewers. |

`systemd.default_vt=3` boots to admin.

> **History:** before P01 (closed 2026-05), tty3 ran
> `qdistro-startlxqtwayland` (LXQt+labwc) with qdshell as a
> parity-test overlay. P01 made qdgreeter functional and made qdwin
> the actual compositor greetd boots; LXQt+labwc demoted to tty4.

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
- Successful auth transitions the compositor to `unlocked`; the session
 manager thaws frozen user sessions.

### Lock triggers

- Idle timer (configurable, default 10 minutes).
- Lid close (via `systemd-logind`).
- Manual "Lock now" from the admin panel or shortcut.
- System suspend.

### Lock scope

A single lock covers the whole machine. No per-user locks. Matches the
single-tenant assumption. Fingerprint unlocks everything at once.

## Fingerprint handling

- Hardware: the laptop's built-in fingerprint reader.
- Service: `fprintd` running as a system daemon.
- Enrolment: **multiple fingers** (primary + backup in case of a cut or
 bandage), all enrolled under the **admin** account. Regular users'
 fprintd DBs stay empty.
- Locker flow: the locker calls `net.reactivated.Fprint.Device.VerifyStart`
 against admin's enrolled prints; any match unlocks.
- tty2 fallback: `pam_fprintd` configured for admin; the same prints work.

`fprintd` stores enrolled templates per Linux user — there is no native
"shared fingerprint DB accessible to multiple users." For qdistro's
"admin authenticates, all nested sessions resume" model, all fingers enrol
on admin; the locker (which runs in admin's session) auths against admin's
DB directly. A future PAM module backed by a system-wide fingerprint store
would let any context verify against admin's enrolled fingers.

## Admin-controlled silo lifecycle

A separate daemon, `qdistro-session-manager.service`, owns silo state.
A "silo" is a Linux uid plus its per-silo state (subvolume, runtime dir,
cgroup-v2 scope) plus a registry entry the broker reads when routing
send-to / cross-uid actions. The terms "user" and "silo" are used
interchangeably in older spec text; new code and the D-Bus surface
both use "silo".

### Silo states

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
method  ListSilos()                      → (s)   # JSON-encoded array
signal  SiloChanged(s name, s state)
```

`ListSilos` returns a JSON-encoded `s` (not `aa{sv}`) so the same wire
shape is consumable from `gdbus` / `busctl` / Python without an
introspection-driven binding. Subscribers to `SiloChanged` may see the
transient `Stopping` / `Deleting` states followed by the resting
`Stopped` / row-deletion; treat unknown state strings as "transient,
wait."

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
3. The session manager thaws all silos in state `Frozen`; their processes
 resume.
4. The compositor starts rendering their surfaces.
5. Input is dispatched normally.

No per-user auth at any point. One fingerprint, everything becomes reachable.

## Admin logout / compositor crash

Admin's compositor runs under systemd with `Restart=always` via greetd. If it
crashes or admin logs out:

1. greetd notices the session ended; systemd restarts it — a fresh admin
 compositor comes up in the locked state.
2. As soon as admin's compositor is gone, `qdistro-session-manager`
 **cgroup-freezes all active silos**. Processes remain alive; no data is
 lost. Surfaces can't render anywhere because admin's compositor is their
 renderer.
3. Admin authenticates again → the session manager thaws sessions → the
 compositor renders their surfaces.

If admin logs out deliberately, the flow is the same plus the SDK's
`before_freeze` hook fires in each active app so apps can persist in-memory
state.

Admin cannot log out in a way that leaves user sessions visibly active —
rendering depends on admin's compositor. The admin session is the host, not
a peer; there is no usable state with "no admin." Reboot-to-locked is
effectively the same as admin log-out-then-back-in.
