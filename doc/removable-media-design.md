# Removable media: mount/unmount + autorun policy (security model)

Status: design note for the stream-C removable-media feature. Read
alongside `permissions.md`, `sudo.md`, `containers.md`.

XFCE parity wants: when a USB stick / SD card / external disk is
inserted, notify the user, offer to mount it, and offer to open it in a
file manager. This is also a **privilege + autorun surface**, so the
qdistro posture is *security-model first*.

## The privilege boundary

Mounting a filesystem is a privileged operation (it manipulates the
kernel mount table, runs filesystem drivers on attacker-controlled
on-disk structures, and exposes a new tree to the user's uid). It MUST
NOT be performed by a direct privileged call from qdshell.

We reuse the **qsu / `qdistro-root-exec` model** verbatim rather than
inventing a second privilege channel:

```
  qdshell (uid 1000, unprivileged shell)
      │  newline-delimited JSON over an AF_UNIX socket
      ▼
  qdistro-media-exec  (root, systemd socket-activated)
      │  1. SO_PEERCRED → authoritative (pid, uid) of caller
      │  2. RequestPermission(action, details) on the broker; WAIT
      ▼
  org.qdistro.AdminBroker1  (permission/subject/rule/cache model)
      │  rules → cache → admin prompt   (default: prompt)
      ▼
  on "allow": qdistro-media-exec runs udisksctl with TOKENIZED argv
              (subprocess list form, NEVER sh -c) as the *caller's* uid.
```

The broker is the sole policy authority. qdshell never calls
`udisksctl`, `mount`, or `udisks2` D-Bus directly — it can only ask
`qdistro-media-exec`, which always brokers. There is no new
direct-privilege path: an attacker who opens the media-exec socket
just burns a rate-limit slot and still has to pass the broker.

## Decision: NEW permission protocol/actions — YES

We add new broker **action strings** (not new broker code paths — they
flow through the existing `RequestPermission` / rules / cache machinery,
exactly like `qsu.exec` and `qdistro.tier1.spawn:*`):

| Action                                  | Meaning                          |
|-----------------------------------------|----------------------------------|
| `qdistro.media.mount:<device>`          | mount one block device           |
| `qdistro.media.unmount:<device>`        | unmount one block device         |

`<device>` is the **canonical kernel device path** the helper resolves
and validates (`/dev/disk/by-...` → realpath under `/dev/`), used only
as a stable, admin-readable suffix for rule authoring (e.g.
`action: 'qdistro.media.mount:*'`). It is *never* the human label and is
never shell-interpolated. The details dict carries, for the prompt UI
and for argv-pinned rules:

```
  details = {
    "device":   "/dev/sdb1",         # canonical, validated
    "label":    "<fs label>",        # UNTRUSTED, display-only
    "fstype":   "vfat",              # from udisks/blkid, display-only
    "uuid":     "....",              # display-only
    "argv[00]": "/usr/bin/udisksctl",
    "argv[01]": "mount",
    "argv[02]": "-b",
    "argv[03]": "/dev/sdb1",
  }
```

Because the argv tuple is carried in `argv[NN]` keys, admins can author
**argv-pinned** allow rules (`forever_argv` etc.) so a durable approval
of `mount /dev/sdb1` does not silently approve `mount /dev/anything`.
This is the same argv-leak fix the qsu scopes were built for; we get it
for free by reusing the broker's argv plumbing.

Why a distinct action namespace rather than folding into `qsu.exec`:
clarity of audit + rule authoring, and so an admin can write
`qdistro.media.*` rules (e.g. auto-allow mounting any device) WITHOUT
also granting arbitrary root command execution.

### Why not give qdshell the udisks2 D-Bus name directly?

udisks2's own polkit policy could in principle authorize the mount, but
that would (a) put a second, parallel policy engine beside the qdistro
broker, splitting the audit trail, and (b) let any uid-1000 process
mount without a qdistro prompt. Routing through the broker keeps a
single permission/subject/audit model. udisks2 is used only as the
*mechanism* (invoked by the root helper after the broker says allow);
it is not the policy.

## Autorun policy (hard requirement)

**Autorun NEVER auto-executes anything from removable media.** There is
no code path that runs an executable or a `.desktop`/autorun.inf entry
off the device. The strongest action the shell will ever take
automatically is *opening a file-manager window at the mountpoint*, and
even that is gated by policy.

`autorunPolicy` (qdshell setting, persisted; default **`prompt`**):

| Value    | On insert (after a mount happens)                       |
|----------|---------------------------------------------------------|
| `ignore` | notification only; no prompt, no action                 |
| `prompt` | **default** — show insertion prompt: Mount / Open / Nothing |
| `open`   | open file manager at the mountpoint (NO execution)      |

`mountPolicy` (default **`prompt`**):

| Value    | On insert                                               |
|----------|---------------------------------------------------------|
| `manual` | never auto-mount; user mounts from the prompt           |
| `prompt` | **default** — ask via the broker (which itself prompts) |

The "Open" choice opens the configured file manager **at a directory
path** (the mountpoint) — it launches the file manager binary with the
mountpoint as argv, it does not open/execute any file *on* the device.
There is deliberately no "run autorun" choice anywhere in the UI or
services. `.desktop` files on removable media are treated as inert data.

Defaults are prompt/ignore-flavored: nothing privileged and nothing that
opens the device happens without an explicit user action.

## Untrusted strings

Device labels, fstypes, UUIDs, and paths originate from the on-disk
filesystem and are attacker-controlled:

- **Helper side:** the device argument is validated against a strict
  allow-list regex and resolved to a canonical `/dev/...` path; argv is
  always a Python list passed to `subprocess` (no shell). Labels/uuids
  are passed only in the `details` dict for display, never into argv.
- **qdshell side:** labels/fstypes render as **PlainText** in
  notifications and the prompt; they are never interpolated into a
  shell command or an action string. The action string suffix is the
  canonical device path, slugged the same way other actions are.

## Failure posture

Broker absent / timeout / malformed / `deny` / `unknown` → **deny the
mount** (fail-closed). An unresolvable or non-allow-listed device path →
helper refuses before contacting the broker. Unmount of a device that is
not mounted is a no-op success at the UI layer.
