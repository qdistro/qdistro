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
processes and attached or reserved resources. The design offers three shapes:
separate TTY sessions for strong mental separation, a mixed desktop where
multiple silos share one compositor, or a headless session for an automated
workflow. **Only the mixed desktop ships.** There is no separate-TTY session
launcher and no headless-session launcher in v1 — see the TTY table below.

## Session launch chain

The design is one greetd instance per TTY with a role-specific config. **One
greetd config is installed**, for tty3 (`deploy/greetd-config.toml`, copied by
`qdistro-bootstrap.sh`); nothing installs per-TTY instances.

| TTY | greetd config | Runs |
|-------|------------------------------------------|---------------------------------------------------------------------|
| tty1 | (none — agetty only) | Raw text login, emergency only. |
| tty2 | (none — no greetd config) | No qdistro VT login is wired here. Text-mode recovery when Wayland is down is via GRUB rescue/emergency or a read-only snapshot boot (`doc/recovery.md`). |
| tty3 | `default_session.command = /usr/bin/qdgreeter` (deploy/greetd-config.toml) | Graphical qdgreeter → admin auth via greetd JSON-IPC → `qdwin-session-launcher` → `qdwin-session.target` → qdwin compositor + qdshell. |
| tty5+ | **Planned — nothing writes these today.** | Intended for TTY work sessions, fullscreen user sessions, special-role sessions, or VM viewers. The session manager writes no greetd config; the only code that approaches it is a self-labelled Phase-8 dry-run probe in `games/` whose docstring says the real launcher is a Phase-9 deliverable, and which no installer ships. A v1 machine has no tty5+ session. |

greetd's `[terminal] vt = 3` boots to admin. (Not a kernel cmdline: the
`systemd.default_vt=3` this page used to cite is not a systemd option.) tty3 is
held exclusively by the compositor — `getty@tty3`/`autovt@tty3` are masked so
logind cannot autospawn a login prompt on it when the VT is free; see
`doc/architecture.md` "TTY layout".

**Planned:** admin-configured sessions may autostart or autologin before the
owner performs the first admin login after boot, running background jobs and
using network if their policy allows, but never visible or interactable until
admin authenticates and the machine lock is cleared. No autostart/autologin
scheduler exists — the session manager has no such path, and the only thing
`StartSilo` starts is the cgroup keep-alive described under "Admin-controlled
silo lifecycle".

> **History:** before P01 (closed 2026-05), tty3 ran
> `qdistro-startlxqtwayland` (LXQt+labwc) with qdshell as a
> parity-test overlay. P01 made qdgreeter functional and made qdwin
> the actual compositor greetd boots. (The legacy LXQt+labwc session
> was later kept only as a tty4 dev fallback, since removed; it now
> survives solely as the GUI test harness — see `deploy/AGENTS.md`.)

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

**qdlocker is a separate process, in a separate repo, with its own systemd
unit** (`qdlocker/systemd/qdlocker.service`, `Restart=always`, installed by
`qdistro-bootstrap.sh`'s `install_qdlocker_service`). It is a PyQt6 application
that connects to qdwin as an ordinary Wayland client over pywayland and binds
the private `qdwin_locker_v1` protocol; its UI is `LockUI.qml` in qdlocker's own
`QQuickWindow`. It is *not* a subsystem of the compositor and not rendered by
qdshell. (qdshell contains a `Modules/LockScreen/LockScreen.qml` that drives
`WlSessionLock`; that path is dead — qdshell's own code notes qdwin does not
implement `ext-session-lock`.)

What *is* compositor-owned is the **lock state and the input/render gate**, and
that part is genuinely in qdwin's hands rather than the locker's. When
`locked == true`:

- No user-session surfaces are rendered.
- No input is dispatched to user sessions.
- No desktop or shell layer renders. `qdwin_hide_non_lock_layers()` unsets the
  position of the background, normal, panel, notification, launcher, popup, and
  all four layer-shell layers — the admin background goes away too, which is
  blunter than this page previously described. The claim is that no
  content-bearing layer survives, not that nothing else can paint: libweston's
  own compositor-owned cursor and fade layers are separate and remain, which is
  what lets the lock UI still show a pointer.

The security consequence of the split is explicitly handled rather than
accidental: a crashed or killed locker does **not** unlock the machine. qdwin's
`qdwin_locker_resource_destroy` holds `locked`, demotes the lock toplevel, and
repaints the now-empty lock layer to black, logging
`lock held (fail-secure) cause=locker_disconnect`. The residual effect is that
the lock *UI* — and therefore the unlock path — can be absent while the machine
stays locked, which is why the unit carries `Restart=always` with the
start-limit gate disabled so it retries indefinitely.

Other properties:

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
already-approved network jobs, and other background work may continue.

### Lock-conditional authorization — planned, not implemented

> **Status: none of the lock-conditional policy described below ships in v1.**
> The design is that new privilege grants and new cross-silo approvals require
> admin to unlock first, and that "already approved" is not sufficient by itself
> — a grant that may continue while locked would carry an explicit
> **lock-continuation bit**, with anything new (mic / camera / screen capture,
> virtual input, new privilege grants, new resource attachments, new cross-silo
> approvals) requiring admin unlock.
>
> **What exists.** The term `lock-continuation` appears in this and three other
> design docs and in **no code file**. The approval-cache schema has no such
> field, so the bit cannot be expressed. Neither the broker nor the session
> manager reads lock state on any grant path, so it could not be evaluated
> either. On the compositor side the privileged surfaces *are* gated — but on
> identity, never on lock state. Per-view capture
> (`qdwin_handle_subscribe_view_stream`) is gated by
> `qdwin_shell_require_bound()` and nothing else; virtual input
> (`zwp_virtual_keyboard_manager_v1`, and input-method-v2 through the same
> helper) is gated at *bind* time by `qdwin_ime_family_bind_allowed`, which
> rejects any secctx-tagged silo client outright and then requires the caller's
> uid to match `allowed_ime_uid`. Optional executable and SELinux-label pins
> exist and fail closed *when configured*, but **no production installer or
> unit sets them**, so what ships is the secctx deny plus the uid check.
> Neither gate consults `locked`. Every `->locked` site in `qdwin.c` is layer hide/show, curtain,
> grabs, focus, activation, or lock-surface lifecycle. ("screencopy" in the
> earlier wording named a protocol qdwin does not implement; there is no
> wlr-screencopy in the tree at all.)
>
> **The residual risk this leaves.** Locking the screen does not change what a
> running silo or the shell is authorized to do. A capture or virtual-input
> stream started before the lock keeps running, and a client that could start
> one before the lock can still start one after it. The lock is a *rendering and
> local-input* boundary only. What does hold across the lock is the compositor's
> own gate: user-session surfaces are not rendered and local input is not
> dispatched to them.

The table below is the **intended** lock-time default policy, retained as a
design target. No mechanism currently expresses or evaluates any row of it.

| Activity | Intended lock behavior |
| --- | --- |
| Audio output already playing | continue if grant allows continuation |
| Active call media/capture | continue only if pre-approved for lock continuation |
| New mic/camera/screen/system-audio capture | require unlock |
| Virtual input / accessibility control | require unlock unless a specific workflow says otherwise |
| Running games | keep process alive; rendering may pause or lose DRM depending on TTY state |
| VR / immersive session | prefer presence/idle policy, not desktop lock alone |
| Recall viewing (post-v1; cut from v1) | revoke viewer grant and clear decrypted results |

### Lock UI indicators (partial lock-time capture observation)

The lock surface must show non-suppressible state for live microphone,
camera, screencast/screen capture, system-audio capture, virtual input or
accessibility control, and qdistro-specific network egress.

`qdlocker` owns the runtime lock surface, so that is where the indicators
live (`qdlocker/qdlocker/indicators.py` + `qml/LockUI.qml`). qdshell carries
an older, **experimental** derivation of the same idea
(`Services/Qdistro/CaptureStateService.qml`, `SiloEgressService.qml`,
`Modules/LockScreen/`) attached to its `WlSessionLock` lock screen — the
deprecated path qdwin does not implement. That copy is not equivalent (it
differs in polling, timeouts, freshness horizon and process lifecycle) and
is not a lock guarantee; it must be reconciled with qdlocker's before any
consumer instantiates it.

What is observable:

- **Network egress** — `SessionManager1.ListSilos` (row semantics in the
 D-Bus section below). The session manager is authoritative; an
 unreachable one renders as *unverified*, not as "no egress".
- **Microphone, camera, screencast, system-audio capture** — the PipeWire
 graph, read with `pw-dump`. The *kind* is derived from node properties
 (`stream.capture.sink` and a `.monitor` name for system audio,
 `media.role`/`device.api`/name hints for camera, qdwin's own
 `weston.pipewire-N` for screencast). Where those hints are absent the
 classification is a best guess from node metadata, not a link-graph
 conclusion — an open review finding, recorded in
 `todo/fable-release/12-j28-multi-output-lock-indicators.md`'s sibling
 review notes, is that ambiguous cases should render as a generic
 uncertain kind instead. PipeWire is the widest observation point
 available because silos get a bind-mounted view of admin's `pipewire-0`
 socket, per-session daemons link upward into admin's graph, and qdwin's
 view-stream path pins a forwarded toplevel onto a weston
 `backend-pipewire` output whose node is named `weston.pipewire-N`.
- **Virtual input / accessibility control** — **not observable at all.**
 qdwin filters the `zwp_virtual_keyboard` / `zwp_input_method` globals but
 emits no event for a bound client.

**Fail visible, not fail silent.** Selected running PipeWire nodes are
evidence of capture *activity*: where the graph carries a client, the client
is named; where only a source *device* node is running, the activity is real
but the client is not established, and the surface says so rather than
implying attribution. There is no trustworthy *negative* for any kind:

- a policy-approved fullscreen session may hold a direct device grant
 (`devices.md`, `games.md`: `/dev/video*`, or `audio` group + `/dev/snd/*`)
 and never appear in the graph at all;
- a direct `weston_capture_v1` screen grab does not appear either;
- virtual input has no observer.

So **no kind is ever reported as "clear"**. Each kind is either `active`
(positively observed) or `unverified`, and a dead, failed, killed or stale
observer drives every kind to `unverified`. The surface distinguishes three
severities: observed capture, observer-failed, and a standing coverage
disclosure ("capture monitoring: partial — … direct device grants and
virtual input are not monitored"), so a healthy quiet scan never reads the
same as a dead observer, and neither reads as an all-clear.

The lock surface observes only while locked. It re-marks its reading stale
on both the lock *intent* edge and the compositor's authoritative
`locked_changed`, so a scan launched while the machine was still unlocked
cannot survive as locked-machine state. No setting gates any of it.

**Runtime requirements.** The observer shells out to `pw-dump` and
`busctl`. Both ship in the release image (`image/config.xml`
`pipewire-tools`) and in the bootstrap chain
(`scripts/install/install-deps.sh`), and `qdlocker/indicators.py` ships in
the qdlocker wheel that both chains pip-install. If either tool were
absent the indicator would read "unverified" forever — honest, but useless
— so the live gate asserts their presence explicitly.

**Known gaps (not shipped guarantees):**

- **Multi-output.** qdlocker paints one fullscreen window and qdwin
 fullscreens it onto `qdwin_primary_output()`, so the indicators appear on
 the primary output only. The other outputs are covered by qdwin's opaque
 lock curtain, which spans the union bounding box of the outputs present
 when it was installed, and all non-lock layers are unset globally — so a
 secondary output is uniformly black rather than showing stale desktop
 content. An output hot-plugged *while locked* is **not** covered: qdwin
 re-installs the curtain on output removal but not on output creation. Options and costs are in
 `todo/fable-release/12-j28-multi-output-lock-indicators.md`; the same note
 records two pre-existing qdwin defects found alongside (output hotplug
 while locked does not re-install the curtain, and
 `qdwin_locker_surface_v1.configure` is documented but never sent).
- **No live gate has been run.** The derivation is unit-tested and the live
 scenario is written (`qdlocker/tests/gui/09-capture-indicators.md`), but it
 has not been executed against a real qdwin + qdlocker + PipeWire graph.
 It asserts the state of the running observer through an
 introspection-gated `indicators` ctrl verb plus banner pixels. Its
 quiet-lock, mic start/stop-while-locked, observer-timeout and egress
 (including transient `Stopping` and an unreachable session manager) and
 locked-restart steps are unconditional; system audio, camera, screencast
 and the second-output step SKIP with a printed reason when the VM cannot
 provide a default sink, a camera node, a manually driven view stream or a
 second head. Output hotplug while locked is a manual check, not a gate.

Making a kind report "clear" requires a real authoritative feed first: a
qdwin event enumerating `weston_capture_v1` / view-stream clients and bound
virtual-input/IME clients, and a device-grant registry covering direct
`/dev/video*` and `/dev/snd/*` opens. Until those exist, a permanent
"unverified" is the honest reading, not a bug.

User sessions do not run independent screenlockers and must not prompt for the
admin/root password. When locked, the only unlock path is the admin locker.
(The intended rule for separate TTY sessions — that the visible seat is forced
to the admin lock surface or kept there — has nothing to act on in v1, since no
such sessions exist; and the actual VT mechanism is not lock-conditional at
all, as below.)

**VT switching is blocked unconditionally, not conditionally on the lock.**
There is no lock-conditional VT gating anywhere in the tree. What ships is a
static mitigation, applied at install time and never lifted, in two layers:

1. `vt-switching=false` in `weston.ini` (`install-qdwin-session-for-vm.sh`)
   removes weston's own `Ctrl+Alt+F1..F8` bindings.
2. The lower kernel-console layer, which `vt-switching=false` does **not**
   cover, is held by seatd installing `K_OFF` on the compositor's VT, plus
   masking `getty@ttyN` / `autovt@ttyN` so logind cannot autospawn a login
   prompt when that VT is free and reset the keyboard mode
   (`harden-compositor-vt.sh`, invoked from the bootstrap and from
   `image/config.sh`; probed by
   `tests/integration/vm/probes/vt-escape-lockdown.sh`). Without layer 2 a
   chord such as `Super+Left` reaches `Decr_Console` in the kernel keymap and
   an unlock password typed at a locked screen can land in `login(1)` and be
   journalled in cleartext. [threat-model.md](threat-model.md) covers this as
   the VT-escape mitigation.

The practical difference from what this page used to claim: the block is not
conditional on the lock. It holds at the locked screen — the property the page
was reaching for — but it equally holds when the machine is *unlocked*, and
unlocking never restores VT switching for admin either.

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
- **Active** — silo's launcher unit is running; cgroup is populated.
 (Spec's old "running.") The surface behaviour this state is meant to
 carry — surfaces render when admin is unlocked — is **planned**: the
 launcher runs a cgroup keep-alive with no graphical client, so an
 Active silo has no surfaces to render (see "D-Bus surface" below).
- **Frozen** — cgroup-v2 `cgroup.freeze=1`; no CPU; admin can
 `ResumeSilo`. This is `cgroup.freeze`, not POSIX SIGSTOP — syscalls in
 flight unwind cleanly when thawed. (Spec's old "paused / frozen.")
 The "surfaces hidden" half is likewise planned, for the same reason.
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

> **Status: the admin app is written but ships in no production installer.**
> `admin_app/qdistro_admin_app.py` implements the `SilosTab` and every
> operation below that is not separately marked planned, and the operations are
> exercised by the GUI acceptance tests — but `admin_app` appears in neither
> `qdistro-bootstrap.sh` nor `image/config.sh`. The only thing that copies it is
> the labwc GUI test harness, and `deploy/start-admin-app.sh` hardcodes a path
> no production installer creates. On a bootstrapped machine there is no silo
> admin panel; the D-Bus surface below is the only way to drive silo lifecycle,
> via `busctl`/`gdbus`.

A PyQt app in admin's session (see the status note above):

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

The lock-screen network-egress indicator consumes these same `ListSilos`
rows (`qdlocker/qdlocker/indicators.py`, and qdshell's `SiloEgressService`
for its own surfaces). Active tier-3 rows with `egress: null` should be shown as
legacy host egress, `direct` and `wg:NAME` by their policy, and `none` as
intentionally dark. A `ListSilos` call that fails renders as *unverified*
on the lock surface rather than as "no egress".

Error names live under `org.qdistro.SessionManager1.*`:
`UnknownSilo`, `SiloExists`, `SiloBusy`, `BadState`, `BadArgument`,
`NotAuthorized`, plus a `Generic` fallback for unexpected
side-effect failures.

`StartSilo` invokes `systemctl start qdshell-session-<name>@<uid>.service`.
The templated launcher unit **is** shipped and installed
(`install-session-manager.sh` drops `qdshell-session@.service` plus the
per-silo symlinks and the `qdshell-session-launcher` helper).

What it is not, despite the name, is a session. Its job is to keep the silo's
uid alive in `/sys/fs/cgroup/qdistro-silos/<name>/` so the session manager's
`cgroup.events:populated` check reports the silo as live: the helper joins the
cgroup, drops to the silo uid with `setpriv`, and execs `dbus-run-session --
sleep infinity`. **No qdshell, no compositor client, and no shell runs inside a
silo.** The unit's own header says "Real qdshell wiring layers on top of this in
a follow-up task."

It does have one other shipped side effect, conditional on a unit that the
installer chain does not currently install: if
`/etc/systemd/system/qdistro-user-relay@.service` exists, the helper first
enables linger for the silo uid and best-effort starts
`qdistro-user-relay@<uid>.service`. `install-user-relay-for-vm.sh` is not in the
bootstrap chain or `image/config.sh`, so on a stock install that branch is
skipped entirely and the keep-alive is all that happens.

This is worth stating plainly because several containment properties elsewhere
in the docs are, today, vacuously true for that reason: a silo with no shell and
no graphical client cannot exercise the surfaces those properties would gate.
They will need re-verification when a real payload lands.

## What admin "unlock" does

1. fprintd verifies the print.
2. The compositor transitions to `unlocked`.
3. The compositor starts rendering allowed user-session surfaces.
4. Input is dispatched normally.

Unlock does **not** restore VT switching: the VT block is unconditional and is
never lifted (see above). This page previously listed "non-admin TTY switching
becomes available again" as step 3; no such transition exists.

No per-user auth at any point. One fingerprint, everything becomes reachable.
(Recall was designed as the exception — admin unlock would make live sessions
reachable without granting ambient historical Recall browsing, which needed its
own time-boxed viewer grant. Recall is **cut from v1** and ships nothing, so
there is no such exception today.)

## Admin logout / compositor crash

Admin's compositor runs under systemd with **`Restart=on-failure`** — both
`deploy/qdwin-compositor.service` and `deploy/qdshell.service` set that, not
`Restart=always`. The difference is load-bearing: a *crash* (non-zero exit)
restarts, but a **clean exit does not**. A deliberate admin logout that exits 0
leaves no compositor running and no automatic respawn; recovery is via greetd on
tty3 or a manual `systemctl start`.

If the compositor crashes, or once admin has logged out:

1. On a crash, systemd restarts the unit — a fresh admin compositor comes up in
 the locked state. On a clean logout it does not; greetd brings the greeter
 back on tty3 instead.
2. As soon as admin's compositor is gone, the machine is treated as locked.
 Nested user-session surfaces are no longer reachable because the admin
 compositor is their trusted renderer and input gate. TTY user sessions are not
 reachable until admin auth returns. Processes may keep running underneath,
 subject to their normal resource policy.
3. Admin authenticates again → sessions become reachable → the compositor
 renders allowed surfaces.

A deliberate logout reaches the same end state — no desktop, nothing reachable
until admin authenticates again — but by a different route: the compositor is
*not* respawned by systemd (see `Restart=on-failure` above); greetd brings the
greeter back on tty3 instead.

Admin cannot log out in a way that leaves user sessions visibly active —
rendering depends on admin's compositor. The admin session is the host, not
a peer; there is no usable state with "no admin." Reboot-to-locked is
effectively the same as admin log-out-then-back-in.
