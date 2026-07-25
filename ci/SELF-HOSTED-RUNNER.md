# Self-hosted host-gate CI runner (SCAFFOLD)

Status: **SCAFFOLD / NOT PROVISIONED.** This document plus
`.forgejo/workflows/host-gate.yml` are a ready-to-use template. No runner is
registered yet; nothing runs automatically. The "Activation checklist" at the
bottom is what a human must do to turn this on.

> **Forge note (2026-07):** the project moved from Codeberg to GitHub. The
> sibling-repo coordinates in `.forgejo/workflows/host-gate.yml` are updated to
> the GitHub layout, but the *runner registration* steps below still describe
> `forgejo-runner` against a Forgejo instance. Porting the runner itself to a
> GitHub Actions self-hosted runner is a pending follow-up; whoever activates
> this gate does that port first. Nothing here is on the install path.

## Why

The central `qdistro` repo is the only repo in the monorepo with **no hosted
CI**. The siblings each run their own hosted workflow (qnotebook
`tests.yml`, qterminator `ci.yml`, qdshell `test.yml` on
`ubuntu-*`), but the cross-repo *host-tier* gate — `ci/bin/qci host`, which
builds qdwin/qdshell, runs the qdistro unit suite under PyQt6, the blocking
shared-ruff + narrow-mypy lint, the per-project coverage floors, and every
sibling repo's host tests in one pass — only ever runs when a **human types
`qci host`**. A host-tier regression (a broker change that breaks a CLI, a lint
break, a coverage-floor regression) is therefore invisible to CI until someone
remembers to run the gate locally.

A self-hosted runner that wraps `qci host` closes that gap: it runs the same
gate the human runs, on `push` to `main` and on demand, and reports red/green
without a human in the loop. It is the HOST counterpart to qdshell's existing
`qdistro-vm` self-hosted runner (which runs the *VM* bats gate).

This is deliberately a **host gate, not the full `qci full` gate**: `qci host`
needs no libvirt/KVM/VM and so can run on an ordinary (even containerised)
machine. The VM gates (`vm-smoke`, `bats`, `gui`) stay on the libvirt-equipped
`qdistro-vm` runner that qdshell already targets. Keeping the two tiers on
separate runner labels means the host gate is cheap and broadly schedulable
while the heavy VM gate stays pinned to the one box that has KVM.

## What `qci host` actually needs

`qci host` is `gate_host` in `ci/bin/qci`. It does **not** touch libvirt, but it
*does* build and test most of the monorepo. The runner must therefore have BOTH
the full sibling checkout layout (below) AND the toolchain every `run_logged`
step in `gate_host` invokes.

### Workspace / checkout layout (REQUIRED)

`qci` derives its paths from its own location: `WORKSPACE` is the parent of the
`qdistro` checkout, and every sibling repo is expected as a direct child of
`WORKSPACE`. `gate_host` builds/tests these siblings by name:

```
WORKSPACE/                      <- e.g. the runner's $GITHUB_WORKSPACE
├── qdistro/                    <- this repo (ci/bin/qci lives here)
│   └── ci/bin/qci
├── qdwin/                      <- meson build + meson test + vendored libweston
├── qdshell/                    <- meson build + ci-local.sh --no-int (needs qdwin pkgconfig)
├── qdbrowser/                  <- pytest (QtWebEngine)
├── qdgreeter/                  <- pytest (PySide6)
├── qdlocker/                   <- pytest
├── qfileman/                   <- pytest
├── qnotebook/                  <- pytest
├── qterminator/                <- pytest
├── qdchrome-extension/         <- npm test && npm run build
└── qdfirefox-extension/        <- npm test && npm run build
```

(`qdistro-site`, the marketing website, is intentionally not part of the host
gate — it ships through a separate website pipeline.)

Missing siblings do not crash the gate (each step fails/reds individually), but
a runner that only checks out `qdistro` will red almost every step. The template
workflow documents how to populate the siblings; the simplest production setup
is to keep a long-lived workspace on the runner with all repos cloned once and
`git pull`ed per run (see the template's "sibling checkout" note), rather than a
fresh full clone of 11 repos on every push.

### Toolchain (derived from what `gate_host` invokes)

| Tool / package | Used by (gate_host step) | Required? |
| --- | --- | --- |
| `python3` (3.13) | every pytest step, report.py | yes |
| `PyQt6` (+ `pytest`, `pytest-qt`) | qdistro/qnotebook/qterminator/qdbrowser/qdlocker/qfileman unit tests; gate pins `QDISTRO_REQUIRE_PYQT6=1` | yes |
| `PySide6` | qdgreeter pytest (the gate pins PyQt6 elsewhere *because* PySide6 is present) | for qdgreeter |
| `pytest-cov` | report-only coverage + the qdistro coverage floor (floor=80, FAILS CLOSED if the JSON is missing) | yes (floor is positive) |
| `ruff` | blocking shared-profile lint (`ci/ruff-shared.toml`) | yes (else SKIP, not green) |
| `mypy` | blocking narrow type check (`ci/mypy.ini`, cli+browser_bridge) | yes (else SKIP) |
| `meson` + `ninja` | qdwin + qdshell builds | yes |
| `pkg-config` | qdshell build (consumes qdwin's `meson-uninstalled` pc files) | yes |
| C/C++ toolchain + Wayland/weston build deps | qdwin meson build + vendored libweston production-symbols test | yes |
| Qt6 declarative tools (`qmltestrunner`/`qmllint`/`qmlformat`) | qdshell `ci-local.sh --no-int` | for qdshell |
| `node` + `npm` | qdshell jstest; qdchrome/qdfirefox extension `npm test && npm run build` | yes |
| QtWebEngine runtime deps | qdbrowser pytest | for qdbrowser |
| `git`, `bash` | everywhere | yes |

A **missing** tool does not always red the gate — `ruff`/`mypy` SKIP (with a
visible SKIP row) when absent rather than passing as green, and several steps
just fail their own row. But a runner intended to *replace the human* should
have the full toolchain so the gate is meaningful. Treat the table as the
provisioning list.

### Recommended base OS

OpenSUSE Tumbleweed with Python 3.13 — the same family the qdistro images and
the qdshell `qdistro-vm` runner target, so the Qt6/meson/weston package names
and versions match what the project builds against. (`qdshell/.github/workflows/
integration.yml` documents the same Tumbleweed + Python 3.13 base for its
runner.) Ubuntu can work for the pure-Python steps but diverges on the
Qt6/weston/meson package layout that the qdwin/qdshell builds assume; if you use
Ubuntu, expect to mirror the qdshell *hosted* workflow's apt list and Qt6 path
shimming for those steps.

## Recommended runner setup

A single Forgejo Actions self-hosted runner labelled **`qdistro-host`**
(mirroring qdshell's `qdistro-vm`, but for the HOST gate — no libvirt needed).

1. Install the toolchain above on the runner box (Tumbleweed):

   ```sh
   zypper in -y python313 python313-pip meson ninja pkgconf gcc-c++ \
       nodejs npm git ruff python313-mypy \
       qt6-declarative-imports qt6-declarative-tools
   pip3.13 install --user PyQt6 PySide6 pytest pytest-qt pytest-cov
   ```

   (Plus the qdwin/qdshell Wayland/weston/Qt6 *-devel* packages the meson
   builds need — track `qdwin`'s and `qdshell`'s build deps; they are the same
   packages the local `qci host` developer already has installed.)

2. Download the Forgejo Actions runner (`forgejo-runner`) and register it
   against the qdistro repo (or the org) with the `qdistro-host` label:

   ```sh
   # On the runner host, as the non-root CI uid (see security note):
   forgejo-runner register \
       --instance https://codeberg.org \
       --token   <REGISTRATION_TOKEN_from_repo_settings_Actions_Runners> \
       --name    qdistro-host-1 \
       --labels  qdistro-host:host \
       --no-interactive
   ```

   The registration token comes from the repo's **Settings → Actions →
   Runners** page on Codeberg (Forgejo). The `:host` suffix declares it a
   `host`-type label (no docker image), so jobs run directly on the box where
   the workspace + toolchain live.

3. Run it as a **systemd unit under a dedicated non-root uid** (NOT root, NOT
   your developer login). Example unit:

   ```ini
   # /etc/systemd/system/forgejo-runner-qdistro-host.service
   [Unit]
   Description=Forgejo Actions runner (qdistro-host)
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=ci-qdistro
   WorkingDirectory=/home/ci-qdistro/runner
   ExecStart=/usr/local/bin/forgejo-runner daemon
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

   ```sh
   systemctl enable --now forgejo-runner-qdistro-host.service
   ```

   (The host gate does NOT need libvirt group membership — that is only the
   `qdistro-vm` runner. Keep this uid OFF the libvirt group; it has no business
   spinning VMs.)

## Security posture (IMPORTANT)

A repo CI runner triggered by `pull_request` executes **untrusted PR code** with
the runner's privileges. `qci host` builds C/C++ (qdwin/weston), runs `npm
test`/`npm run build` (arbitrary lifecycle scripts), and executes the repo's
pytest suites — all of which run attacker-controlled code on a self-hosted box.
Treat this runner as compromised-by-default and contain it:

- **Dedicated, unprivileged uid** (`ci-qdistro` above). No sudo, not in
  `wheel`/`libvirt`/`docker`.
- **Ephemeral / locked-down workspace.** Prefer a runner that resets its
  workspace between jobs (Forgejo `--once`/ephemeral mode, or a per-job
  throwaway container/VM the runner launches), so PR code can't persist between
  runs. At minimum, `git clean -xfd` the workspace before each run.
- **No secrets the host gate doesn't need.** `qci host` needs no deploy keys, no
  registry creds, no signing keys. Do not expose any to this runner.
- **Egress-restricted** if practical (the host gate's only legitimate network
  use is fetching deps; `npm`/`pip` can be pre-seeded). `qci` even has a
  `QCI_OFFLINE=1` posture for the VM tiers; the host gate can largely run
  against a pre-populated dep cache.
- **Restrict the trigger for forks.** Do NOT auto-run the gate on PRs from
  untrusted forks. Use `workflow_dispatch` (manual approval) and/or restrict the
  `push`/`pull_request` trigger to the trusted branches/members only — Forgejo's
  Actions settings can require approval for first-time / outside contributors.

For these reasons the template workflow ships with the **automatic `push`
trigger commented out**: a brand-new runner should be brought up under manual
`workflow_dispatch` first, validated, locked down, and only THEN have the
auto-trigger enabled.

## Activation checklist (what a human must do)

1. Provision a Tumbleweed box with the toolchain table above.
2. Create the `ci-qdistro` uid + the systemd unit; start the daemon.
3. Register the runner against the qdistro repo with label `qdistro-host`
   (`forgejo-runner register`).
4. Populate the sibling-repo workspace layout on the runner (clone all repos
   listed above once; the workflow `git pull`s them per run — or wire the
   workflow's sibling-checkout block).
5. Trigger `.forgejo/workflows/host-gate.yml` manually
   (**Actions → Host gate → Run workflow**) and confirm it goes green.
6. Lock the runner down per the security posture (ephemeral workspace, no
   secrets, egress limits, fork-PR approval).
7. ONLY THEN uncomment the `push:` trigger in `host-gate.yml` so `main`
   regressions are caught automatically.

Until step 5 succeeds, this is a scaffold: the workflow exists but no runner
answers the `[self-hosted, qdistro-host]` label, so a manual dispatch simply
queues with no effect.
