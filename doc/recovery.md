# Recovery and repair

Operator-facing recovery runbook for a qdistro install. Every mechanism
here is the one actually deployed by `scripts/install/qdistro-bootstrap.sh`
(or its VM sibling `scripts/vm/enable-qdgreeter.sh`); where a recovery path
relies on a stock openSUSE Tumbleweed mechanism rather than qdistro code,
that is called out so an operator knows what guarantees it.

This page is referenced by the human recovery drills in
`todo/fable-release/06-human-test-plan.md`. Keep it honest: if a path is
not wired, say so here rather than implying it works.

## TTY / session map (as deployed)

| TTY | What runs | Wired by |
|-----|-----------|----------|
| tty1 | **Emergency text console** — a raw `agetty`, no greetd. Deliberate: it is the last-resort login if greetd's config is broken. It is a password-authenticated surface, not a qdistro session. | distro default (`getty@tty1`) |
| tty3 | **Production session.** `greetd` → `qdgreeter` (graphical PAM via greetd JSON-IPC) → `qdwin-session-launcher` → `qdwin-session.target` (qdwin compositor + qdshell). Boots here by default because greetd's `[terminal] vt = 3` puts it there. Held exclusively by the compositor: `getty@tty3`/`autovt@tty3` are masked (see below). | `deploy/greetd-config.toml`, `greetd.service`, `scripts/install/harden-compositor-vt.sh` |
| tty5+ | Dynamic / pinned work sessions written by `qdistro-session-manager`. | session manager |

> **No login prompt can appear on tty3.** logind starts `autovt@ttyN` by unit
> name on demand for any VT within `NAutoVTs` (default 6), so a free tty3 would
> otherwise get an `agetty` the moment anything switched to it — precisely in
> Scenario A below, when the compositor is wedged. Install-time masking of
> `getty@tty3`/`autovt@tty3` prevents that. It matters for more than tidiness:
> a getty's start-time TTY reset reverts the `K_OFF` console-keyboard mode
> seatd installs, and `K_OFF` on tty3 is what keeps keystrokes typed at a
> **locked** screen from reaching the kernel console and `login(1)` — where an
> unlock password is recorded in cleartext as a failed-login *username*.
> Only tty3 is masked; the tty1 emergency console above is untouched.

> **By design — there is no graphical escape hatch and no qdistro text-mode VT
> login.** There is no tty4 fallback desktop (the legacy passwordless LXQt+labwc
> hatch was removed — a passwordless graphical admin VT would bypass the locked
> tty3 greeter). tty3 is the only interactive qdistro login (the locked graphical
> greeter); no greetd config, service, or `getty@tty2` wires a textual admin
> login on tty2 (an earlier `tuigreet` design was never implemented and the docs
> have been reconciled). When the compositor or graphics stack is broken — or
> *all* of Wayland is — recover through GRUB (rescue/emergency target or a
> read-only snapshot boot), not a VT login.

## Scenario A — the graphical session won't come up (qdwin/qdshell wedged)

Symptom: tty3 shows the greeter but login never reaches a desktop, or the
compositor crashes back to the greeter, while the rest of the machine is
responsive.

There is no graphical escape hatch — recover from the bootloader in text mode
(Scenario B), then inspect what failed and fix forward or roll back:

1. Reboot into the **GRUB** rescue/emergency target (Scenario B).
2. From the text shell, inspect what failed:
   - `systemctl status qdwin-session.target 'qdwin*' 'qdshell*'`
   - `journalctl -b -u 'qdwin*' -u 'qdshell*' --no-pager | tail -100`
3. Fix forward (rebuild/repair the offending component, see Scenario D) **or**
   roll back the root filesystem to the last-good state (Scenario C).

## Scenario B — graphics fully broken

Symptom: tty3 does not produce a usable session; black screen,
KMS errors, or the compositor cannot open the GPU.

Recover from the bootloader, in text mode:

1. Reboot. At the **GRUB** menu choose the entry, press **`e`**, and append
   to the `linux` line one of:
   - `systemd.unit=rescue.target` — single-user maintenance shell (root
     password required), filesystems mounted.
   - `systemd.unit=emergency.target` — minimal shell, `/` mounted
     read-only; `mount -o remount,rw /` to make changes.
   Press **Ctrl+x** to boot.
2. From the text shell, diagnose the graphics fault
   (`journalctl -b -p err`, check the GPU driver / `libweston` install), or
   jump straight to a snapshot rollback (Scenario C).
3. If the bad state came from a system update, the fastest fix is to boot
   the pre-update snapshot directly — see Scenario C.

## Scenario C — bad update or unbootable root (snapshot rollback)

qdistro is openSUSE Tumbleweed with btrfs + Snapper (preinstalled; the
zypper plugin takes an automatic **pre/post** snapshot pair around every
`zypper`/system update — `doc/filesystem.md`). The GRUB **"Start bootloader
from a read-only snapshot"** submenu and `snapper rollback` are the
stock-openSUSE root-rollback mechanism qdistro relies on.

1. Reboot to the **GRUB** menu → **Start bootloader from a read-only
   snapshot** → pick the last snapshot that predates the breakage (the
   pre-update snapshot is labelled by the zypper plugin).
2. The system boots that snapshot **read-only**. Verify it is healthy
   (login reaches a desktop, the failing unit is gone).
3. Make it permanent — as root in that booted snapshot:
   ```
   snapper rollback
   reboot
   ```
   `snapper rollback` sets that snapshot as the new default `/` subvolume
   and snapshots the current (broken) state first, so the rollback is
   itself reversible.

Scope: `snapper rollback` is defined for **`/`** only. Per-user home
corruption is bounded to that user and rolled back separately through the
admin Snapshots panel ("Roll back this user (full)", which drives the
`qdistro-snap-swap` atomic subvolume swap) or file-granularity
`snapper undochange` — never by `snapper rollback` of `/`. See
`doc/filesystem.md` for the per-user rollback semantics. (The
`qdistro-snap-swap` CLI ships from the `snapshots` installer chain step, so it
is present wherever the snapshot feature and the admin Snapshots panel are.)

## Scenario D — broken or partial qdistro install (re-run bootstrap)

`qdistro-bootstrap.sh` is idempotent and is the supported repair tool. On a
machine that already has qdistro, re-running it brings the install back to
the manifest's intended state without touching existing accounts'
passwords, silo state, vaults, broker rules, or snapshots.

- **Full re-run** (idempotent): re-run the same bootstrap invocation used to
  install. Re-running the whole chain is safe.
- **Resume after a partial failure:** `qdistro-bootstrap.sh --resume` skips
  every installer-chain step already recorded as complete and runs the
  remaining (unrecorded) steps; per-step state is persisted across runs.
- **Repair one component:** `qdistro-bootstrap.sh --rerun-step NAME` runs
  exactly one installer-chain step. `NAME` is validated against the exact
  step list — run `qdistro-bootstrap.sh --list-steps` for the authoritative
  names (they are the *daemon/component* installers, e.g. `broker`,
  `session-manager`, `pwd`, `snapshots`; an unknown `--rerun-step` name also
  prints the list). Re-deploying the greetd/qdwin **session units** is a separate
  bootstrap phase, **not** a `--rerun-step` target — repair those with a full
  re-run.

Use the **hardened/daily-driver** profile for a real install; the signed
release manifest is verified before any root clone/build runs (see
`doc/release-signing.md` and the public install guide). The dev profile and
the `http://`-staging VM helpers are not for a recovery on a real machine.

## Quick reference

| Situation | First move |
|-----------|-----------|
| Desktop won't start, machine responsive | Reboot → GRUB → `systemd.unit=rescue.target`; diagnose, then Scenario C or D |
| No graphics at all (tty3 dead) | Reboot → GRUB → `systemd.unit=rescue.target` |
| Last update broke boot/login | Reboot → GRUB → read-only snapshot → `snapper rollback` |
| One user's home corrupted | admin Snapshots panel → "Roll back this user (full)" |
| qdistro components broken/half-installed | `qdistro-bootstrap.sh --resume` (or `--rerun-step NAME`) |
| Return to production session | `systemctl restart greetd` (boots tty3) |

## Known gaps (v1)

The first three install/doc gaps below were closed 2026-06-13 (F9a/b/c); the
remaining item is human-gated:

- **tty4 escape hatch — removed.** The fallback was a *passwordless* admin
  LXQt+labwc autologin that (when mis-enabled) caused a `203/EXEC` restart loop
  and a greeter bypass. It has been removed entirely; recovery is via GRUB
  (`qdistro-bootstrap.sh` configure_greetd, `image/config.sh`).
- **No qdistro text-mode VT login — resolved (F9b).** tty3 is by design the
  only interactive qdistro login; the never-implemented tty2 `tuigreet` claims
  in `architecture.md`/`sessions.md`/`devices.md` were reconciled.
  Fully-broken-Wayland text recovery is via GRUB rescue/emergency target.
- **`qdistro-snap-swap` install — resolved (F9c).** The per-user "Roll back
  this user (full)" CLI now ships from the `snapshots` installer chain step
  (`install-snapshots-for-vm.sh`), so it lands wherever the snapshot feature
  (and the admin Snapshots panel) does — not only via the VM-only templates
  installer.
- **Recovery drills are human-gated.** The boot-to-snapshot and
  rescue-target paths above are validated by hand in
  `06-human-test-plan.md`; there is no agent harness for a real
  unbootable-root rollback.
