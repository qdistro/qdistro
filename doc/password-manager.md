# Password manager (pwd)

Secure, shared password and secret manager for qdistro. Multi-vault,
session-independent vault lifecycle, strong app-identity verification so a
request for "firefox's gmail password" has actually come from firefox
running in the expected context.

## Why not reuse GNOME Keyring / KDE Wallet

- **Weak identity granularity.** They gate by user session, not by app.
 Any app in the user's session can read any secret. Firefox reads
 Thunderbird's entries.
- **Tied to user-session lifecycle.** Secrets unlock and lock with the
 session. qdistro wants vaults whose unlock state is independent of which
 users are currently active.
- **No cross-user shared vaults.** qdistro has one fingerprint owner with
 multiple user silos; they should be able to share a vault (e.g., work
 and dev both need the same SSH keys).

qdistro's pwd daemon replaces them for first-party use. It also implements
`org.freedesktop.portal.Secret` so unmodified Flatpak / upstream apps see a
compliant secret service.

## Threat model

**In scope:**

- **Misidentification** — a malicious app impersonating firefox to steal
 its saved passwords.
- **Cross-silo leaks** — dev-user's browser reading work-user's vault
 items.
- **Background polling** — a compromised app repeatedly asking "give me X"
 hoping for an accidental approval.
- **Plaintext leakage** via clipboard, logs, stdout, environment.

**Out of scope:**

- Attacker with root on admin's machine (whole system compromised; vault
 master keys accessible regardless).
- Hardware-level attacks.

## Vaults

A **vault** is an encrypted keystore, persistent, independent of any user
session's lifecycle.

- **Master key** — typical setup: admin fingerprint + TPM-sealed key
 (hardware-backed). Alternatives: password, YubiKey.
- **Unlock state** — `locked` or `unlocked`. Independent of user-session
 state. A vault can be unlocked while the machine is otherwise locked
 (autotype flows), or locked while users are active.
- **Access policy** — which users, which apps, which specific items can be
 read.
- **Auto-lock** — on idle, qdwin/admin screen lock, logind
 `PrepareForSleep(true)`, logind `Session.Lock`, or explicit lock.

Typical vault arrangement:

- **admin vault** — admin's personal passwords.
- **per-user vault** — one per data-silo user; accessible only to that
 user's apps by default.
- **shared vaults** — e.g., `work-shared` accessible to work-user and
 dev-user.
- **app-specific vaults** — optional; for sensitive apps (finance, crypto
 wallets) with tight policy.

Items carry tags: target URL, username, app-identity selector, arbitrary
metadata. Policies match on these.

## Vault format

Two formats coexist on the same daemon; `UnlockVault` auto-routes by
on-disk version:

- **v1** — scrypt KEK + AES-GCM, password-encrypted.
- **v2** — TPM-sealed master key with admin PIN as the TPM auth-value
 (anti-DA-lockout enforced by hardware). PCR-bound seal optional: PCR 7
 (secure boot state) + PCR 11 (UKI/initrd digest) is the default
 selection, so a tampered initrd, firmware, or secure-boot keyset fails
 to unseal even with the right PIN.

## Daemon architecture

`qdistro-pwd.service` — systemd unit.

- Runs as a dedicated uid `qdistro-pwd` with narrow capabilities (TPM
 access, vault-file read/write).
- SELinux type `qdistro_pwd_t`. On the supported Tumbleweed hardened
 bootstrap path, SELinux runs Enforcing and the `qdistro_pwd` policy
 carries `neverallow` ratchets so only this type may read vault files or
 ptrace the daemon.
- Exposes a socket at `/run/qdistro/pwd.sock`. Accessible from any user
 session subject to policy.
- No network (its own netns with no interfaces).

Vault files live in `/var/lib/qdistro/vaults/<name>.vault`, sealed with
keys that require the daemon's environment (TPM + admin-enrolled print)
to unwrap.

## App identity verification

The core feature. Layered — all layers must agree for a request to be
honoured.

When an app's D-Bus connection arrives at the pwd daemon socket, the daemon
gathers:

| Signal | Source | What it proves |
|-------------------------------------------------|-----------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| **uid / pid** | `SO_PEERCRED` on the socket | Kernel-attested caller identity. |
| **Executable path** | `readlink /proc/<pid>/exe` | What binary is running — kernel-maintained, not forgeable from userspace. |
| **SELinux label** | `getpeercon()` or `/proc/<pid>/attr/current` | Which SELinux type — assigned by the LSM based on exec + policy, can't be forged by the caller. |
| **Cgroup path** | `/proc/<pid>/cgroup` | Which systemd unit / slice / container. |
| **Namespaces** | `/proc/<pid>/ns/*` | User / mount / net namespace membership (identifies containers). |
| **Compositor attestation** (optional, strongest)| `qbus-admin` query to the admin compositor | "This request came from user action on a window owned by pid P" — defends against background polling. |

Any single signal can be spoofable in a corner case; the combination is
robust.

### Policy uses all signals

```yaml
- match:
 vault: work
 item_tag: gmail.com
 app_exe: /usr/bin/firefox
 app_selinux: user_t:firefox_exec_t
 caller_user: work-user
 requires_compositor_attestation: true
 action: allow
 scope: once
```

A process running as uid `work-user` but with the wrong SELinux label, or
an unexpected exe path, or wrong cgroup, fails the match. **All fields
together make the identity claim robust.**

### SELinux policy is a prerequisite

This design assumes per-app SELinux types in policy — `firefox_exec_t`,
`thunderbird_exec_t`, `qdistro_terminal_exec_t`. First-party apps are
straightforward (qdistro authors the labels); third-party apps need either
upstream-shipped policy or admin-authored local rules.

## Compositor attestation

For the highest-assurance requests (autofill into a login form), a second
signal beyond process identity:

1. The user hits a qdistro compositor keybind ("fill password") or clicks
 a compositor-provided fill menu (not an in-page button, which could be
 malicious).
2. The compositor records `{pid, window-id, timestamp}` and forwards a
 token to the daemon.
3. The app then makes its D-Bus request carrying that token.
4. The daemon verifies the token matches a recent user-intent event for
 this pid.

Without compositor attestation, a compromised app could poll for a password
hoping for a user clickthrough. With attestation, the only way to trigger
delivery is a real user action via admin's compositor UI.

## Delivery mechanism

Delivery uses the narrowest mechanism the consumer supports. Path or text
delivery is a fallback, not the normal shape.

1. **Direct D-Bus reply.** The daemon returns the payload on the same
 socket. Stays in app memory; no clipboard. Standard for API-style
 requests (browser extension, app with native secret integration).
2. **Agent socket / fd pass.** For SSH keys, signing keys, tokens consumed by
 qdistro-controlled helpers, and other use-not-read cases, the daemon exposes
 a scoped agent socket or passes an fd over authenticated `AF_UNIX` IPC.
3. **systemd credentials.** For transient systemd-managed helpers, the daemon
 can hand off material as a credential acquired at service activation and
 released on deactivation.
4. **Autotype.** The daemon delivers via simulated keystrokes into the
 currently-focused window, with compositor cooperation ensuring focus
 hasn't changed between trigger and delivery.
5. **IME fill.** The qdistro compositor exposes a special input-method
 backend; the daemon acts as the source. Cleaner than autotype (respects
 IME conventions, handles composition).

**Never via clipboard.** Clipboards leak to other clients of the compositor.
Not an acceptable delivery channel for secrets.

Environment-variable and path delivery are exceptional legacy modes and must
be tied to a controlled exec, mount namespace, SELinux context, and audit
record when used.

## Polkit unlock

`UnlockVault` for non-admin callers routes through the polkit action
`org.qdistro.pwd.unlock`. The shipped qdistro polkit-agent config maps
`org.qdistro.pwd.*` to PAM, so vault unlocks require a fresh admin password by
default. Fingerprint and `auth_admin_keep` caching are policy alternatives, not
the default vault-unlock ceremony.

The daemon calls polkit's `CheckAuthorization` with the vault name + caller
details before any unsealing. The admin uid bypasses polkit entirely.

The actual prompt is rendered by whichever polkit AuthenticationAgent the
admin's session has registered — qdistro ships one (see "polkit agent"
below).

## Admin vs user UI

- **Admin panel** — create / delete vaults, set master-key material, manage
 items, edit per-app access policies, view audit log. Full authority.
- **User panel** — read-only view of "secret requests by my apps,
 recently." No item management.

## Unlock flow

1. The vault is locked.
2. An app requests an item from that vault.
3. The daemon triggers a polkit action `org.qdistro.pwd.unlock.<vault>`.
4. Admin's polkit agent shows a dialog in admin's compositor:
 "dev-user's firefox wants an item from the work vault."
5. Admin password → the daemon unseals the vault master key via TPM. A policy
 override may use fingerprint instead.
6. The vault transitions to `unlocked`. The request proceeds through
 normal policy.
7. The vault relocks on idle, qdwin/admin screen lock, logind
 `PrepareForSleep(true)`, logind `Session.Lock`, or explicit lock. Relock
 wipes resident keys and invalidates outstanding browser fill tokens before
 emitting `VaultLocked`.

## Polkit agent

A per-user session daemon `qdistro-polkit-agent` registers with polkitd and
dispatches `BeginAuthentication` to one of three methods:

- **PAM** — admin types their password, verified via `python-pam`.
- **fprintd** — verify via `net.reactivated.Fprint.Device`.
- **broker** — delegate the yes/no decision to the qdistro admin broker's
 `RequestPolkitAuth` flow, surfaced via the admin-approval-app.

The method is picked per polkit action via fnmatch globs in
`/etc/qdistro/polkit-agent.conf`. The default is `broker`. The shipped
config maps `org.qdistro.pwd.*` to `pam` so vault unlocks require a fresh
admin password unless admin changes that policy.

### Status (2026-10-07) — the broker is the privileged responder

polkitd accepts `AuthenticationAgentResponse2` **only from uid 0**, and
the session agent runs as the admin uid, so the agent never answers
polkitd directly. The privileged broker (uid 0) delivers the response:

- **broker method** — the agent files the authorization as
  `RequestPolkitAuth(action, details, cookie, identities)`, which queues
  like `RequestPermission` but carries polkit's correlation cookie and
  the identity list polkitd offered. On an allow — admin click, rule,
  cache, or hook — the broker calls `AuthenticationAgentResponse2`
  itself. The agent still waits on `WaitForDecision` for the outcome,
  so denial logging and audit correlation stay unchanged.
- **pam / fprint methods** — the verdict is verified locally by the
  agent, then relayed: `RespondPolkitAuth(cookie, identities)` asks the
  broker to make the uid-0 call. Since these paths never file a
  request, the agent first binds the cookie to its connection via
  `AnnouncePolkitAuth(cookie)`.
- **cancellation** — polkitd's `CancelAuthentication` forwards to
  `CancelPolkitAuth(cookie)`, which decides the matching queued request
  deny so a dead prompt does not linger in the admin queue and the
  parked `WaitForDecision` waiter releases.

Two invariants make this safe:

- **The cookie is the correlation secret, and it stays secret.** Only
  polkitd's registered agent ever sees it — the agent does not file it
  in request details (GetPending renders those to admin-control peers),
  and it is never logged. A response naming a cookie polkitd is not
  waiting on is a silent no-op, so a stale, cancelled, or
  attacker-guessed cookie grants nothing.
- **The response identity comes from polkit's own list.** On this image
  `/usr/share/polkit-1/rules.d/50-default.rules` sets
  `polkit._suse_admin_groups = []`, so `identities` is
  `[unix-user uid=0]` — `admin` is uid 1000 and in no `wheel` group.
  Responding with an identity polkitd did not offer is rejected even
  from uid 0, so the broker picks from the supplied list (preferring a
  `unix-user` entry for the requesting uid, else the first `unix-user`,
  else the first entry) rather than asserting one.

The four broker methods are bound server-side to the session agent —
the caller must be python running the agent's installed script inside
its `qdistro-polkit-agent.service` **system**-slice cgroup — and denied
to non-admin callers in the `org.qdistro.AdminBroker1` system-bus
policy. Because exe, argv, and cgroup membership are all forgeable by a
sufficiently motivated same-uid process, the relay additionally binds
each cookie to its declaring connection's **unique D-Bus name**:
`RespondPolkitAuth` and `CancelPolkitAuth` act only for the sender that
announced or filed the cookie, a cancel recorded for one sender cannot
pre-deny another's filing, and the first declaration wins — a foreign
sender cannot rebind a cookie by filing or announcing it later.

The agent is a **system service** (`User=admin`), not a user unit. A
user unit cannot seal its own launch environment: every same-uid
process can push manager variables (`systemctl --user set-environment`
+ restart) or write drop-ins — including an *empty* drop-in
`UnsetEnvironment=` that resets the denylist outright — so injected
loader code could run before `python3 -I` took effect and scrub its own
`/proc/<pid>/environ` entries before the broker ever read them. As a
system unit the unit file, drop-in dirs, manager environment and the
`system.slice` cgroup's `cgroup.procs` are all root-owned: uid 1000 can
neither inject into the agent's environment or argv nor migrate a
foreign process into its cgroup.

The remaining hardenings keep attacker code off the trusted
connection:

- The unit launches `python3 -I`, ignoring `PYTHONPATH`/
  `PYTHONHOME`/`sitecustomize`/`usercustomize`, and the broker
  *requires* `-I` in the peer argv — without it, even
  `python3 <script>` loads attacker-writable user-site
  `sitecustomize`/`.pth` code with no env var at all.
- The peer's parent must be **init** (`ppid == 1`): only systemd itself
  spawns a system service's main process, and a foreign process keeps
  its own parent. Orphans reparented to PID 1 still fail the exact
  `system.slice/qdistro-polkit-agent.service` cgroup match.
- Defence-in-depth (checked anyway): exe resolves to a python under a
  root-owned system dir (an attacker binary merely *named* `python3`
  ignores argv), only no-argument isolation flags precede the script,
  environ is read untruncated and fail-closed against the injection
  denylist (`LD_*`, `GLIBC_TUNABLES`, `PYTHON*`, `BASH_ENV`,
  `QDISTRO_POLKIT_*` test seams — now honestly attestable because the
  system unit's environment is root-fixed), the SELinux type is not
  hostile, and the cgroup path matches the unit exactly.

Residual: uid 0 remains able to rewrite or restart the system unit —
but root needs no relay (it answers polkitd itself), so tampering
yields at most a denial of service, never a forged approval. ptrace
injection into the live agent is outside this boundary's reach.

History: before this responder existed the agent was verified end-to-end
on a real seat session (registration, dispatch, broker delegation, fail-
closed denies) but could not grant — direct `AuthenticationAgentResponse2`
returned `Only uid 0 may invoke this method`.

## Portal Secret integration

A per-user session daemon `qdistro-pwd-portal` registers as
`org.qdistro.PortalSecret` implementing
`org.freedesktop.impl.portal.Secret.RetrieveSecret`. It bridges to a
system-bus method `Pwd1.GetPortalKey(app_id)` that auto-provisions
per-app-id 32-byte random keys in a configurable portal-keys vault.
Per-app keys are stable across sessions and identical across silos of
the same Flatpak app.

A per-user oneshot systemd unit `qdistro-portal-keys-unlock.service` runs
at `qdwin-session.target` and calls `Pwd1.AutoUnlockPortalKeys`,
which unseals and unlocks the portal-keys vault from a TPM-sealed PIN
stash. Unmodified Flatpak apps then get their per-app portal Secret keys
without a manual unlock step.

## Recovery paths

Recovery is layered from easiest to hardest:

1. **Recovery codes.** At vault setup, admin generates a short set of
 human-enterable recovery codes (6-word phrases or 10-digit codes,
 not long passphrases). Stored offline. One code = one unlock.
 Deliberately easier to type than a long passphrase — the goal is a
 usable fallback.
2. **Password fallback.** Admin may optionally set a long password as a
 secondary unlock path for the vault master key. Not the default
 (TPM is preferred).
3. **Boot another distro.** The vault file on disk is encrypted; unlocking
 from a rescue OS requires the original TPM (unavailable if the machine
 is broken) or a recovery code.
4. **Backup-based restore.** If `btrfs send` backups are current, restore
 to a new machine. After restore, rotate the master key and issue new
 recovery codes.

## Cross-user vault access

Shared vaults are one vault with a policy listing who can read what — not
copies per user. Avoids the divergence problem that per-user copies
create. The access decision happens after identity verification.

## Audit

Every request (allowed or denied) is logged:

- Timestamp, vault, item (hashed, not plaintext), caller identity (uid,
 exe, SELinux label, cgroup), decision, scope.
- Admin panel shows this; optionally forwarded to an external SIEM.

Default: log decisions, not payloads. Admin can enable payload logging for
debugging (dangerous — opt-in and time-limited).

## Integration points

- **`org.freedesktop.portal.Secret`** — qdistro implements the portal
 backend. Upstream Flatpak apps use it unmodified; the portal wraps
 requests with the same identity checks.
- **Browser native messaging** — a browser extension talks to
 `qdistro-browser-bridge` (identity-pinned native-messaging host) which
 forwards to the daemon. See [browser](browser.md).
- **Autotype keybind** — user hits Super+P on a focused password field;
 the admin compositor queries the daemon for a matching item based on
 the focused window's identity.
- **SSH agent** — the daemon can expose an SSH agent socket per vault;
 SSH keys live in vaults.
