# Filesystem, snapshots, backups

## Base distribution

**openSUSE Tumbleweed.** Rolling release, btrfs default, Snapper preinstalled,
`zypper` transactional snapshots out of the box, openQA-tested upstream. The
btrfs + Snapper story on openSUSE is production-proven; qdistro leans on it
rather than rebuilding.

Alternatives considered: Fedora (also btrfs-default but with less polished
Snapper integration), Arch (bleeding edge, manual btrfs setup, no Snapper-
equivalent package-transaction integration), Debian (conservative, btrfs not
default). Tumbleweed wins on default-correct-behaviour and upstream
maintenance. Overriding this should require an explicit design decision.

### Hard storage constraints

- **btrfs RAID5/RAID6 is NOT supported** by qdistro. The upstream write-hole
 bug remains unresolved, and `btrfs-progs` itself emits a warning at
 filesystem-create time. qdistro storage layouts are restricted to single
 disk, RAID0, RAID1, or RAID10 (the latter three for `data` and `metadata`
 profiles). Operators wanting RAID5/6-style parity should layer btrfs on top
 of mdraid or LVM RAID.
- **ZFS is not used.** Out-of-tree kernel module, CDDL/GPL friction, no
 Tumbleweed-default integration with snapshots or zypper transactions.
- **Daily-driver/release installs require encrypted root.** qdistro's silo
 isolation is a runtime boundary; without LUKS/dm-crypt, a stolen disk or
 offline boot can read every silo subvolume. `qdistro-bootstrap.sh` refuses
 daily-driver/release installs when it cannot detect an encrypted root layer.
 Operators who intentionally accept a runtime-only posture must set
 `QDISTRO_ALLOW_PLAINTEXT_ROOT=1` for that bootstrap run.

## Subvolume layout

```
/ (root) snapshotted pre/post package transaction
/home separate subvolume, not auto-snapshotted
/home/<user> per-user subvolume — enables per-user snapshots
/var/lib/qdistro/vaults subvolume, snapshotted with extra care
/var/lib/qdistro/recall/<user> subvolume per recall-user, TTL-aware policy
/var/lib/libvirt/images VM disk images subvolume; snapshotted lazily
/var/lib/containers podman / container storage subvolume
/var/log not snapshotted (rotating data, noise)
/.snapshots Snapper's snapshot store
```

Per-user-home subvolumes are the key qdistro-specific decision: they let
admin roll back *just one silo's data* (e.g., recover work-user after
accidental deletion) without touching other users.

**When this layout is created:** at user-creation time, never retrofit at
runtime. Tumbleweed's installer creates `/home` as a single subvolume by
default and doesn't offer per-user-home subvolumes. Converting an existing
`/home/<user>` directory into a subvolume after the fact is non-atomic on
btrfs (no in-place "promote directory to subvolume" primitive — the sequence
is `subvolume create new`, `cp --reflink=always -a old/. new/`, swap, fix
ownership) and the user must be logged out for the duration. qdistro's
user-creation flow performs the subvolume create + ACL set + Snapper config
in one step; the legacy admin home stays as a directory under the single
`/home` subvolume unless promoted manually via `qdistro-home-promote --user
<name>`.

## Snapper integration

Snapper's default behaviour is kept:

- **Pre/post snapshots** on every `zypper` transaction.
- **Timeline snapshots** (hourly / daily / weekly) on `/` and configured
 subvolumes.
- **Retention** per Snapper's defaults; overridable per subvolume.

qdistro-specific Snapper configs:

- Per-user-home subvolumes have their own Snapper config with user-scoped
 retention.
- Vault and recall subvolumes have separate policies (see caveats below).
- Container storage subvolume snapshotted only before major container-
 runtime upgrades.

## Admin panel over Snapper

A PyQt app wraps Snapper's CLI / D-Bus:

- **Timeline view per subvolume** — scrollable list of snapshots with
 timestamps, pre/post correlation, Snapper description.
- **Compare snapshots** — diff view (added / changed / removed files) via
 Snapper's D-Bus `GetFiles(config, num1, num2)`. Cached comparisons via
 `CreateComparison` / `DeleteComparison`.
- **Rollback** — one click, with safety: rollback creates a new snapshot of
 the current state first (so the rollback itself is reversible). Rollback
 semantics differ between `/` and non-root subvolumes — see below.
- **Export for backup** — triggers `btrfs send` of the snapshot.
- **Delete** — reclaim space.

The broker drives Snapper ops over D-Bus rather than shelling out: faster
(~10ms vs ~150ms shell-out), no fork-exec, and avoids the polkit prompt
that Snapper's CLI triggers under non-root callers. Per-user processes
never touch Snapper directly — they go through the broker, which logs the
caller identity.

Per-subvolume coloured tagging matches the user-colour convention (blue
for work-user's home, green for dev-user's, etc.).

## Per-user silo rollback

If work-user's data gets corrupted (ransomware in the user silo, accidental
`rm -rf`, bad sync), admin can:

1. Open the admin panel → Users → work-user → Snapshots.
2. Pick a snapshot before the corruption.
3. Click Rollback → confirm.
4. Snapper takes a snapshot of the current (corrupted) state, rolls
 work-user's home to the selected snapshot.
5. Other users and system state are untouched.

**Blast radius of user-silo corruption is bounded to that silo's rollback.**

### Rollback semantics — two flavours

`snapper rollback` itself is only defined for `/` (it sets the btrfs
default subvolume). For everything else, qdistro implements rollback in
two flavours, both behind admin-app confirm dialogs:

- **File-granularity restore** ("undochange") — replays a Snapper diff
 against the live subvolume via `snapper -c <cfg> undochange N..0
 [files...]`. Best for "I deleted three files, give them back" or for
 vault-restore-individual-item. Non-destructive to other paths in the
 subvolume.
- **Atomic subvolume swap** (`qdistro-snap-swap`) — the actual "roll back
 the whole silo" action. Takes a fresh RO snapshot of the current
 (corrupted) state, renames the live subvolume out of the way, takes a
 writable clone of the chosen historical snapshot, restores
 ownership/ACLs, and removes the moved-aside subvolume after admin
 confirms a successful boot of the user. qdistro implements this; it is
 not a Snapper feature. The admin app exposes it as "Roll back this user
 (full)" and gates it behind a typed-vault-name confirmation.

## VM and container snapshot integration

- **libvirt** already supports VM snapshots. The admin panel unifies
 libvirt snapshots with btrfs snapshots under one timeline view per VM.
- **podman** layers on btrfs naturally; the container storage subvolume
 gets snapshots at the admin-configured cadence.

## Backup — `btrfs send | rage -e | ssh destination`

> **v1 status: shipped, in two pieces that are not yet wired together.**
> 1. **Scheduled export** runs on the `qdistro-backup.timer` →
>    `qdistro-backup.service` units, which execute `qdistro-backup-run`
>    (`snapshots/qdistro_backup_service.py`) over the signed-manifest engine —
>    the same `btrfs send | rage -e | ssh` flow below, but built as argv lists
>    with no local `bash -c` wrapper, and emitting a per-run signed manifest.
>    The unit is
>    `ConditionPathExists`-gated on `/etc/qdistro/backup.conf`, so a vanilla
>    install does not back up until configured.
> 2. **Signed-manifest verify + restore (DR)** is the separate, shipped
>    `snapshots/qdistro_backup_cli.py` (over `qdistro_backup_manifest.py`):
>    per-run, hash-chained, `ssh-keygen -Y`-signed manifests, with restore and
>    verify that **fail closed** — they REFUSE without `--allowed-signers`
>    unless `--insecure-no-verify` is given; restore loads the whole manifest
>    chain, signature-verifies each entry, runs the strictly-increasing chain
>    check, then receives. Covered by `tests/unit/test_backup_manifest.py`,
>    `tests/unit/test_vault_recovery.py`, and the host-runnable
>    `tests/integration/backup-e2e.bats`.
>
> The legacy unsigned `qdistro-snap-export` CLI (which wrapped the export
> pipeline in a `bash -c` string) was **removed** — it interpolated the
> ssh_target/remote_path into a shell command, a command injection
> (opus-security-review HIGH #4). The scheduled timer now drives only the
> signed-manifest `qdistro-backup-run`. Run the DR CLI directly for
> verify/restore; do not run the raw restore idiom below standalone (it skips
> the verify gate).

Scheduled backup of configured subvolumes to a remote target.

- `qdistro-backup.service` (systemd timer) triggers daily.
- For each subvolume flagged for backup: take an incremental **read-only**
 snapshot, `btrfs send` the diff relative to the last backed-up snapshot,
 pipe through encryption to the destination.
- Metadata collector subvolumes exclude known private key names by default
 (`backup-sign-*`, `id_*`, `*.key`, `*.pem`, `identity/`) and scan the staged
 tree for SSH/PEM/age private-key markers. A match aborts the backup before
 any blob is published.
- Destinations: trusted remote btrfs receiver (NAS) or encrypted-blob
 storage (S3-compatible).

### Encryption pipeline

Pinned tool: **`rage-encryption`**, packaged on Tumbleweed. Single
static binary, age-format-compatible. Pinned over the upstream `age`
binary because rage is easier to audit and ships as a single static
binary on Tumbleweed without an extra language runtime.

Idiom for export:

```bash
btrfs send -p <parent-snap> <ro-snap> \
 | rage -e -R /etc/qdistro/backup-recipients.txt \
 | ssh <host> 'cat > /backups/<subvol>/<ts>.btrfs.age'
```

Idiom for restore (the transport `qdistro_backup_cli.py restore` performs
*after* it has signature-verified the manifest chain — do not run it
standalone, which skips the verify gate):

```bash
ssh <host> 'cat /backups/<subvol>/<ts>.btrfs.age' \
 | rage -d -i /etc/qdistro/backup-key.txt \
 | btrfs receive /mnt/restore
```

**Trade-off:** encrypting the stream means the remote side cannot itself
act as a live btrfs receiver — it stores opaque ciphertext blobs, and
restore requires a full decrypt-then-receive to a local btrfs filesystem.
For "trusted target" cases (a self-owned NAS on a private network),
qdistro supports a "no-encrypt" mode that pipes plaintext `btrfs send`
straight to a remote `btrfs receive` — operator opts in per-destination,
never the default.

## Pre-action snapshots

The admin panel can take a snapshot before any risky operation:

- "Before system update" (already handled by Snapper's zypper plugin).
- "Before user deletion."
- "Before importing new data into a silo."

SDK hook for apps that do destructive operations on their own data:

```python
qdistro_app.snapshot_before(description="import large dataset")
# ...risky code...
```

The SDK calls the snapshots daemon, which creates a Snapper snapshot of
the app's owning subvolume.

## Recovery from bad updates

openSUSE's default GRUB snapshot boot path is kept:

- The GRUB menu lets the user boot from any Snapper snapshot.
- Selecting a pre-update snapshot rolls back `/` to that state.
- qdistro addition: rollback triggers a consistency check — if rolled-back
 system state depends on newer user session state (e.g., container images
 built against newer libraries), the admin panel surfaces a reconciliation
 prompt.

## Retention policy

Default per Snapper:

- Hourly: 10 snapshots.
- Daily: 10.
- Weekly: 0.
- Monthly: 0.
- Pre/post package: per-transaction, reaped by `NUMBER_LIMIT`.

qdistro tweaks:

- Per-user home: hourly 24, daily 30, weekly 12, monthly 6.
- Vault: every change triggers a snapshot (small data, cheap); retention
 90 days.
- Recall: coordinated with recall TTL (see caveats).

## Caveats

### Vault snapshots and credential rollback

The vault subvolume is snapshotted on every change. But: **rolling back a
vault snapshot reverts password changes** — if you changed your bank
password yesterday and today roll back, you restore the *old* password in
your vault, diverging from the bank's current state.

Policy: the admin panel shows a loud warning before rolling back any vault
subvolume. The better default is to restore individual items from a
snapshot rather than roll back the whole subvolume; the admin panel has a
"Restore item from snapshot" flow.

### Recall subvolume vs TTL

Recall has a 30-day TTL. Snapshotting recall creates a tension: a snapshot
taken 60 days ago contains data that "should" be expired. qdistro picks:
**snapshot but apply TTL at query time.** Snapper keeps snapshots; recall
queries filter out entries older than the TTL regardless of where they
live. Prevents accidental revival of purged data. Rollback of the recall
subvolume requires an explicit "bypass TTL" admin acknowledgment.

### User-created disk data

If a user installs a browser and it saves a huge cache (500 MB) to
`/home/<user>/.cache`, that's part of the subvolume and consumes snapshot
space rapidly. High-churn subdirs are marked **NOCOW + subvolume-excluded**:
`~/.cache`, `~/.mozilla/firefox/*/storage/default/*/cache`, etc.

## Integration with other features

- **Fullscreen TTY sessions**: snapshot the user's home subvolume
 before launching a game session so save corruption is recoverable.
- **Password vault**: every item add/modify triggers a snapshot of
 `/var/lib/qdistro/vaults`.
- **User creation**: snapshot the new user's home at creation time as a
 "factory reset" baseline.
- **VM-per-app isolation**: the VM disk image is a subvolume or loop
 device; Snapper snapshots capture pre/post state of the VM.

## Transactional updates (alternative path, not default)

openSUSE also offers `transactional-update` (used by MicroOS) for atomic
OS updates. qdistro does not use this by default — regular `zypper` +
Snapper gives the same rollback story with less rigidity. If a user
specifically wants an immutable root, it's available.
