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
| tty3 | **Production session.** `greetd` → `qdgreeter` (graphical PAM via greetd JSON-IPC) → `qdwin-session-launcher` → `qdwin-session.target` (qdwin compositor + qdshell). Boots here by default (`systemd.default_vt=3`). | `deploy/greetd-config.toml`, `greetd.service` |
| tty4 | **Escape hatch.** `greetd-fallback.service` → `greetd --config /etc/greetd/config-fallback.toml` → auto-login `admin` into a legacy **LXQt + labwc** Wayland session (`qdistro-startlxqtwayland`). Reachable with **Ctrl+Alt+F4**. | `deploy/greetd-config-fallback.toml`, `deploy/greetd-fallback.service` (enabled at `qdistro-bootstrap.sh:1888`) |
| tty5+ | Dynamic / pinned work sessions written by `qdistro-session-manager`. | session manager |

The tty4 fallback is **graphical** — it is the same LXQt+labwc stack qdwin
replaced (P01, 2026-05). When present it recovers a **broken qdwin/qdshell**
(compositor segfault, qdshell QML failure, `qdwin_shell_v1` binding error)
because the underlying Wayland/KMS stack is still healthy. It does **not**
recover a fully broken graphics stack (KMS/driver fault, missing libweston) —
in that case tty4 fails the same way tty3 does; use the text-mode paths below.

> **v1 limitation — the tty4 escape hatch is not wired on a production
> bootstrap.** `qdistro-bootstrap.sh` *enables* `greetd-fallback.service`
> (`:1888`) but does **not** install the command it execs
> (`/usr/local/bin/qdistro-startlxqtwayland`), its `labwc -S` wrapper
> (`qdistro-lxqt-session-wrap`), or the LXQt+labwc package stack — only the
> VM bake (`scripts/vm/spin-test-vm-gui.sh`) installs the full set;
> `image/config.sh` has the same gap. On a plain bootstrap install tty4
> therefore fails to exec (`203/EXEC`, restarted forever). **Until this is
> reconciled, treat the GRUB paths below — not tty4 — as the dependable
> recovery on a real install.** Tracked in
> `todo/fable-release/04-feature-completion.md` (escape-hatch install gap).

> **v1 limitation — no qdistro text-mode VT login.** `doc/architecture.md`
> and `doc/sessions.md` describe a tty2 `tuigreet` textual admin login "for
> repairs when Wayland won't start." That is **not wired in v1**: no greetd
> config, service, or getty targets tty2 (grep `deploy/`, the units, and the
> install scripts — the only `tuigreet` mention is a stale comment). Until it
> is wired or the docs are reconciled, text-mode recovery when *all* of
> Wayland is broken goes through GRUB (rescue/emergency target or a snapshot
> boot), not a qdistro VT login. (No deploy config, service, or `getty@tty2`
> unit invokes `tuigreet` — only stale docs/comments do.) Tracked as a
> release doc-debt / feature gap.

## Scenario A — the graphical session won't come up (qdwin/qdshell wedged)

Symptom: tty3 shows the greeter but login never reaches a desktop, or the
compositor crashes back to the greeter, while the rest of the machine is
responsive.

If the tty4 escape hatch is installed (VM bakes today — see the limitation
above; on a plain bootstrap install skip to Scenario B/C):

1. Press **Ctrl+Alt+F4** to switch to the tty4 escape hatch. You are
   auto-logged-in as `admin` into LXQt+labwc.
2. Open a terminal. Inspect what failed:
   - `systemctl status qdwin-session.target 'qdwin*' 'qdshell*'`
   - `journalctl -b -u 'qdwin*' -u 'qdshell*' --no-pager | tail -100`
3. Fix forward (rebuild/repair the offending component, see Scenario D) **or**
   roll back the root filesystem to the last-good state (Scenario C).
4. Return to the production session: `systemctl restart greetd` then
   **Ctrl+Alt+F3**.

If tty4 itself does not come up, the graphics stack — not just qdwin — is
broken; go to Scenario B.

## Scenario B — graphics fully broken (tty4 also fails)

Symptom: neither tty3 nor tty4 produces a usable session; black screen,
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
`doc/filesystem.md` for the per-user rollback semantics. (Note: the
`qdistro-snap-swap` CLI is installed by the templates installer step, not by
the minimal snapshots step — see the known gap below if "Roll back this user
(full)" is unavailable.)

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
| Desktop won't start, machine responsive | **Ctrl+Alt+F4** → tty4 LXQt *(VM bakes only today)*, else GRUB → `rescue.target`; diagnose, then Scenario C or D |
| No graphics at all (tty3 *and* tty4 dead) | Reboot → GRUB → `systemd.unit=rescue.target` |
| Last update broke boot/login | Reboot → GRUB → read-only snapshot → `snapper rollback` |
| One user's home corrupted | admin Snapshots panel → "Roll back this user (full)" |
| qdistro components broken/half-installed | `qdistro-bootstrap.sh --resume` (or `--rerun-step NAME`) |
| Return to production session | `systemctl restart greetd` → **Ctrl+Alt+F3** |

## Known gaps (v1)

These are real release blockers/doc-debt for the recovery story, not just
documentation polish. Each needs a decision before v1 sign-off:

- **tty4 escape hatch is enabled but not installed on a production
  bootstrap.** `greetd-fallback.service` is enabled by `qdistro-bootstrap.sh`
  (and `image/config.sh`) but the `qdistro-startlxqtwayland` command, its
  `qdistro-lxqt-session-wrap` (`labwc -S …`) target, and the LXQt+labwc
  package stack are installed only by the VM bake. An enabled-but-missing
  unit fails `203/EXEC` and restarts forever. Fix: either install the full
  fallback stack from the bootstrap/image, or stop enabling the service on
  paths that don't install it. Until then tty4 is not a real install's
  escape hatch — GRUB is.
- **No qdistro text-mode VT login.** The documented tty2 `tuigreet` path is
  not wired anywhere (no deploy config, service, or getty — only stale
  claims in `architecture.md`/`sessions.md`); fully-broken-Wayland text
  recovery is via GRUB rescue/emergency target. Either wire tty2 or reconcile
  those docs before v1 sign-off.
- **`qdistro-snap-swap` not in the minimal install.** The per-user
  "Roll back this user (full)" action depends on a CLI installed only by the
  templates installer step, not the snapshots step. Confirm the install
  chain provides it wherever the admin Snapshots panel is shipped.
- **Recovery drills are human-gated.** The boot-to-snapshot and
  rescue-target paths above are validated by hand in
  `06-human-test-plan.md`; there is no agent harness for a real
  unbootable-root rollback.
