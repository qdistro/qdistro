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
- **Auto-lock** — on idle, on admin lock, on suspend, or explicit.

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
- SELinux type `qdistro_pwd_t`. Only this type may read vault files.
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

Three modes, per item or per request:

1. **Direct D-Bus reply.** The daemon returns the payload on the same
 socket. Stays in app memory; no clipboard. Standard for API-style
 requests (browser extension, app with native secret integration).
2. **Autotype.** The daemon delivers via simulated keystrokes into the
 currently-focused window, with compositor cooperation ensuring focus
 hasn't changed between trigger and delivery.
3. **IME fill.** The qdistro compositor exposes a special input-method
 backend; the daemon acts as the source. Cleaner than autotype (respects
 IME conventions, handles composition).

**Never via clipboard.** Clipboards leak to other clients of the compositor.
Not an acceptable delivery channel for secrets.

## Polkit unlock

`UnlockVault` for non-admin callers routes through the polkit action
`org.qdistro.pwd.unlock` (default `auth_admin_keep` — admin auth, cached
for the session). The daemon calls polkit's `CheckAuthorization` with the
vault name + caller details before any unsealing. The admin uid bypasses
polkit entirely.

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
5. Admin fingerprint → the daemon unseals the vault master key via TPM.
6. The vault transitions to `unlocked`. The request proceeds through
 normal policy.
7. The vault relocks on idle, suspend, or explicit lock.

## Polkit agent

A per-user session daemon `qdistro-polkit-agent` registers with polkitd and
dispatches `BeginAuthentication` to one of three methods:

- **PAM** — admin types their password, verified via `python-pam`.
- **fprintd** — verify via `net.reactivated.Fprint.Device`.
- **broker** — delegate the yes/no decision to the qdistro admin broker's
 `RequestPermission` flow, surfaced via the admin-approval-app.

The method is picked per polkit action via fnmatch globs in
`/etc/qdistro/polkit-agent.conf`. The default is `broker`. The shipped
config maps `org.qdistro.pwd.*` to `pam` so vault unlocks always require
a fresh admin password.

## Portal Secret integration

A per-user session daemon `qdistro-pwd-portal` registers as
`org.qdistro.PortalSecret` implementing
`org.freedesktop.impl.portal.Secret.RetrieveSecret`. It bridges to a
system-bus method `Pwd1.GetPortalKey(app_id)` that auto-provisions
per-app-id 32-byte random keys in a configurable portal-keys vault.
Per-app keys are stable across sessions and identical across silos of
the same Flatpak app.

A per-user oneshot systemd unit `qdistro-portal-keys-unlock.service` runs
at `graphical-session.target` and calls `Pwd1.AutoUnlockPortalKeys`,
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
