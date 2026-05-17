# qdistro-pwd — password manager (spec/13 MVP)

Sources for the qdistro password-manager daemon. See
`doc/password-manager.md` for the design and threat model;
this README covers what's actually shipped vs deferred.

## Layout

```
pwd/
├── qdistro_pwd_daemon.py ─ system-bus D-Bus service (org.qdistro.Pwd1)
├── qdistro_pwd_vault.py ─ on-disk vault crypto (v1 scrypt + v2 TPM)
├── qdistro_pwd_tpm.py ─ TPM2 backend abstraction (tpm2tools/mock/none)
├── qdistro_pwd_polkit.py ─ polkit gate for UnlockVault ()
├── qdistro_pwd_portal.py ─ XDG portal Secret backend daemon ()
├── qdistro_pwd_identity.py ─ /proc + SO_PEERCRED snapshot + pin_match
├── qdistro_pwd_audit.py ─ sqlite audit log (payload never persisted)
├── qdistro-pwd-admin.py ─ admin CLI (uid 1000 only)
├── qdistro-pwd-get.py ─ app CLI (any uid; pin-gated)
├── org.qdistro.Pwd1.conf ─ system-bus policy
├── org.qdistro.pwd.policy ─ polkit action (org.qdistro.pwd.unlock)
├── qdistro-pwd.rules ─ polkit rule (admin bypass + auth_admin_keep)
├── org.qdistro.PortalSecret.portal ─ xdg-desktop-portal backend declaration
├── qdistro-portals.conf ─ xdg-desktop-portal preferred-backend config
├── qdistro-pwd.service ─ systemd system unit (sandboxed, root for MVP)
└── qdistro-pwd-portal.service ─ systemd user unit (per-session)
```

## D-Bus method matrix

| Method | Caller | Notes |
|----------------------------------|--------------|--------------------------------------------|
| `CreateVault(name, password)` | admin only | v1 scrypt KEK derived from password |
| `CreateVaultTPM(name, pin)` | admin only | v2 TPM-sealed master key + PIN auth-value |
| `UnlockVault(name, secret)` | any | auto-routes by version: secret = pwd or PIN |
| `VaultVersion(name)` | any | int (1 = scrypt, 2 = tpm-sealed) |
| `VaultInfo(name)` | any | {version, tpm_backend} |
| `LockVault(name)` | any | wipes in-memory key |
| `IsUnlocked(name)` | any | bool |
| `ListVaults()` | any | lists vault names |
| `AddItem(vault, tag, value, …)` | admin only | upsert with optional pin_app_exe/selinux/uid |
| `DeleteItem(vault, tag)` | admin only | bool |
| `ListItems(vault)` | admin only | metadata only (no decryption) |
| `GetItem(vault, tag)` | any | gated by per-item pin against caller |
| `GetItemAdmin(vault, tag)` | admin only | bypasses pin gate |
| `GetPortalKey(app_id)` | non-admin | XDG portal Secret bridge (auto-provisions) |
| `ListAuditLog(limit)` | admin only | newest-first |

## Pin model

Each item stored via `AddItem` may carry up to three pins:

- `pin_app_exe` — kernel-attested `/proc/<pid>/exe` of the caller.
- `pin_selinux` — `/proc/<pid>/attr/current` SELinux process label.
- `pin_uid` — `SO_PEERCRED` uid of the caller.

`pin_match()` requires every non-empty pin field to match the caller's
corresponding kernel-attested attribute exactly. Empty pin fields are
wildcards. An item with **no** pins set is admin-only and only readable
via `GetItemAdmin` (the `GetItem` path refuses with "admin-only
retrieval").

Pins are evaluated against fresh `/proc` reads at each `GetItem` call —
not snapshotted at `AddItem`. This means a re-exec'd binary (different
sha256 than when admin pinned) still satisfies the exe pin if the path
is identical; conversely a process that has exited between request and
verification gets an empty `exe` and fails closed.

## Auto-lock

The daemon ticks every 30 seconds and relocks vaults that haven't seen
a `GetItem` / `IsUnlocked` / list activity in `QDISTRO_PWD_IDLE_S`
seconds (default 600s). Each relock fires a `VaultLocked(name,
"idle-timeout")` signal.

## Vault file format

JSON on disk under `/var/lib/qdistro/vaults/<name>.vault`, mode 0600.
First field is `version`:

- `1` = scrypt-only ( MVP). Master key is 32 random bytes sealed
 by a scrypt-derived KEK (N=32768 r=8 p=1) under AES-GCM with AAD
 `"<vault>\x00__master__"`.
- `2` = TPM-sealed (). Master key sealed directly by TPM2 with
 the admin PIN as the TPM auth-value. Hardware-enforced
 dictionary-attack lockout makes a short PIN (6-12 digits) acceptable.
 v2 vaults carry a `tpm_seal: {backend, blob}` section instead of
 `kdf` + `kek`. Backend is `tpm2tools` in production, `mock` in tests
 / opt-in fallback.

Per-item ciphertext uses its own random 12-byte nonce + AAD
`"<vault>\x00<tag>"` so swapping or renaming entries on disk fails
authenticated decryption — same in v1 and v2.

The daemon's `UnlockVault(name, secret)` auto-routes by reading the
on-disk version first; admin CLI `qdistro-pwd-admin info <vault>`
labels the kind so the prompt asks for "password" vs "PIN" correctly.

### TPM backend selection

The daemon picks a backend at startup via `select_backend()`:

1. `QDISTRO_PWD_TPM_BACKEND` env (explicit: `tpm2tools` | `mock` | `none`).
2. Auto: `tpm2tools` if `/dev/tpmrm0` exists and `tpm2_getrandom` works.
3. Fallback: `none` (CreateVaultTPM raises `PolicyError`).

Override TCTI via `QDISTRO_PWD_TPM_TCTI` (default `device:/dev/tpmrm0`).
Override the persistent SRK handle via `QDISTRO_PWD_TPM_PRIMARY_HANDLE`
(default `0x81000010`).

### Polkit unlock gate ()

`UnlockVault` for non-admin callers is gated through the polkit action
`org.qdistro.pwd.unlock` (default `auth_admin_keep`). The daemon calls
`org.freedesktop.PolicyKit1.Authority.CheckAuthorization` with these
details:

| Detail key | Value |
|--------------|------------------------------------|
| `vault` | vault name being unlocked |
| `caller-pid` | caller PID |
| `caller-uid` | caller UID |
| `caller-exe` | best-effort `/proc/<pid>/exe` |

A polkit `AuthenticationAgent` (lxqt-policykit, polkit-gnome, or the
future qdistro admin agent) renders the prompt to the admin. The
admin's PAM password / fingerprint clears the auth, which polkit
caches for the rest of the session (`_keep` tail).

**Admin uid (1000 / admin) bypasses polkit entirely** — the daemon's
own admin-side CLI doesn't trigger a popup loop. Same bypass mirrored
in `qdistro-pwd.rules` so a third-party tool that triggers the action
directly (`pkexec`) also gets the same behaviour.

**Bring-up override:** set `QDISTRO_PWD_POLKIT_REQUIRED=0` (env on
the daemon) to disable the gate entirely. Used by fresh-VM bootstrap
when no polkit agent is yet running, and by tests that drive
`UnlockVault` from a non-admin uid.

### XDG portal Secret backend ()

`qdistro-pwd-portal` is a per-user session-bus daemon registered as
the system-wide backend for `org.freedesktop.impl.portal.Secret`.
Unmodified Flatpak / sandboxed apps that link `libsecret-portal`
already use the portal Secret API; with this backend installed they
get a working secret service through qdistro-pwd.

**Flow:**

```
 Flatpak app
 └── libsecret-portal: org.freedesktop.portal.Secret.RetrieveSecret(fd, app_id)
 └── xdg-desktop-portal (per-user front-end)
 └── org.freedesktop.impl.portal.Secret.RetrieveSecret(handle, app_id, fd, options)
 └── qdistro-pwd-portal (per-user session daemon)
 └── system bus: org.qdistro.Pwd1.GetPortalKey(app_id) -> ay
 └── qdistro-pwd: lookup or auto-provision
 portal/<app_id> in the "portal-keys" vault
```

**Per-app keys are stable across sessions and identical across
silos** of the same flatpak app — otherwise the same app's keyring
storage would re-key from one launch to the next, losing data.

**Admin must unlock the portal-keys vault** before any app can
fetch a key:

```sh
qdistro-pwd-admin unlock portal-keys
```

(Auto-unlock-at-login is deferred — TPM-sealed v2 plus fprintd
integration makes this practical without a popup.)

**Vault name override:** `QDISTRO_PWD_PORTAL_VAULT=othername` on the
daemon picks a different vault (default `portal-keys`).

The portal frontend validates `app_id` against `/proc/<pid>/root/.flatpak-info`,
so we trust the value passed in. Daemon-side: `app_id` is loosely
validated for path-traversal characters; auto-provisioned items
carry `pin_uid = caller_uid` so the audit trail records which user
silo provisioned which app key.

## What's NOT in MVP (deferred)

- Fingerprint unlock via fprintd (`spec/13 §"Unlock flow"`).
- polkit unlock dialog (admin polkit agent integration).
- Multi-vault YAML policy DSL beyond per-item pins.
- Autotype / IME-fill / clipboard delivery (`spec/13 §"Delivery
 mechanism"` — clipboard explicitly forbidden, autotype + IME are
 next).
- `org.freedesktop.portal.Secret` backend so unmodified Flatpak apps
 use this daemon (`spec/13 §"Integration points"`).
- Browser native messaging host (`spec/14`).
- SSH agent backend per vault.
- Recovery codes (`spec/13 §"Recovery paths"`).
- Compositor attestation tokens via qdwin_shell_v1 (`spec/13
 §"Compositor attestation"`).
- Dedicated `qdistro-pwd` system uid + qdistro_pwd.{te,fc} SELinux
 module. MVP daemon runs as root with systemd sandbox lockdown
 (PrivateNetwork, NoNewPrivileges, ProtectSystem strict,
 RestrictAddressFamilies=AF_UNIX, MemoryDenyWriteExecute, system-call
 filter @system-service @file-system @network-io).

## Quick start (manual smoke)

```sh
# As admin (uid 1000):
qdistro-pwd-admin create work
qdistro-pwd-admin unlock work
qdistro-pwd-admin add work gmail.com --pin-exe /usr/bin/firefox
qdistro-pwd-admin items work
qdistro-pwd-admin get work gmail.com # admin bypass
qdistro-pwd-admin lock work

# As an app uid:
/usr/bin/firefox-wrapper qdistro-pwd-get work gmail.com # ALLOWED
bash -c 'qdistro-pwd-get work gmail.com' # DENIED (wrong exe)
```

## Tests

- Unit: `tests/unit/test_pwd_{vault,identity,audit,tpm,polkit,portal,daemon_portal_key,policy_module}.py` — 90 cases.
- Bats: `tests/integration/vm/pwd-print-recall.bats` — `phase8-pwd-e2e` drives
 the full v1 lifecycle including non-admin pin gate.
 `phase8-pwd-tpm-e2e` drives the v2 TPM lifecycle (mock backend
 on hosts without swtpm; real TPM when available).
