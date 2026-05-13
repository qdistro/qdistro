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
| tty3 | `initial_session = qdistro-admin-compositor` | Auto-launches admin compositor; starts locked. |
| tty4+ | Dynamic; session manager writes ephemeral configs | Fullscreen user sessions or VM viewers. |

`systemd.default_vt=3` boots to admin.

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

## Admin-controlled user lifecycle

A separate daemon, `qdistro-session-manager.service`, owns user session
state.

### User states

- **absent** — the account doesn't exist.
- **stopped** — account exists; no processes running, no session.
- **paused / frozen** — cgroup-frozen; no CPU, surfaces hidden; admin can
 resume.
- **running** — processes executing; surfaces render when admin is unlocked.

### Admin panel operations

A PyQt app in admin's session:

- **Create user** — wraps `useradd`, sets qdistro metadata (colour, default
 isolation tier, default device grants, netns policy).
- **Delete user** — teardown session + `userdel`.
- **Start / stop / pause / resume** — session manager transitions state.
- **Edit permissions** — device grants, clipboard policies, netns, per-app
 isolation tier.
- **Schedule** — optional; systemd timers can pause/resume users on time
 windows.

## What admin "unlock" does

1. fprintd verifies the print.
2. The compositor transitions to `unlocked`.
3. The session manager thaws all users in state `running`; their processes
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
 **cgroup-freezes all user sessions**. Processes remain alive; no data is
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
