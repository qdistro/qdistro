# qdistro local CI

`qdistro/ci` is the handoff folder for local continuous integration across this
umbrella checkout. It is meant to be usable by a developer or by an agent with
shell access to the same workspace.

The preferred entrypoint is `just` (run from `qdistro/ci/`):

```bash
just full            # or: just bats --file <f.bats>, just triage --latest
just --list          # all recipes
```

Every recipe delegates to the stable CLI, which is also usable directly (and is
what scripts/agents/run-artifacts invoke):

```bash
qdistro/ci/bin/qci <gate>
```

`just` is only the task surface; the CI engine (run lifecycle, VM/golden
management, parallel pools, report finalization, exit-class accumulation) lives
in bash modules under `qdistro/ci/lib/` that `bin/qci` sources into one process.
See [`AGENTS.md`](AGENTS.md) for the module map.

Every invocation writes artifacts under:

```text
qdistro/ci/runs/<gate>-<utc>-<pid>/
  manifest.txt
  repo-state.tsv
  results.tsv
  report.md
  report.html
  host/
  vm/
  bats/
  gui/
  screenshots/
  journals/
  agent-notes/
```

`report.md` and `report.html` are the primary CI result. They include failing
commands, links to logs/artifacts, excerpts, and first-pass fix recommendations.
For machine parsing, use `results.tsv`, `repo-state.tsv`, and `manifest.txt`.

`results.tsv` carries a 9th `category` column placing each row in the shared
**confidence taxonomy** (`unit`/`integration`/`gui`/`vm`/`source_invariant`/
`fake_backend`/`real_backend`/`slow`). The report's *Test categories* section
tallies results per category and calls out dependency-missing skips. This is
reporting only — it gates nothing. See [`TAXONOMY.md`](TAXONOMY.md) for the
canonical vocabulary, per-layer tagging (pytest markers / meson suites /
vitest tags), and the per-suite relabel action items.

## Gates

| Gate | Purpose |
| --- | --- |
| `preflight` | Verify the in-tree component dirs (and warn about stale pre-monorepo sibling checkouts next to the repo), libvirt session, VM tools, prebaked image, and common host tools. |
| `lint` | Run warn-only shellcheck/scenario-structure metrics plus blocking Bats syntax and maintained-document local-link/anchor validation. `QCI_FLAKE_STRICT=1` also makes scenario flake findings fatal. |
| `selftest` | Self-test the qci runner itself (no VM): run the host-only `tests/integration/qci/*.bats` suite that locks down the gate-runner contract — exit-class table, usage/unknown dispatch, headless gate manifest/results.tsv, and the affected/replay/offline plumbing. Runs first in `host`, after the protected-path edit guard. |
| `feedback qdfileman [paths...]` | Run the shared qdfileman host pytest job for development feedback only; record paths, dependencies, timing and outcome. Refuses release mode. |
| `host` | Run host tests/builds across all in-tree components: Python pytest repos, WebExtension npm tests/builds, and qdwin/qdshell meson/QML checks. (The qdistro-site website is NOT built here — it ships via a separate website pipeline.) |
| `vm-smoke` | Create or reuse a VM and verify the qdwin/qdshell session, Wayland socket, and core user services. |
| `bats` | Run every `tests/integration/vm/*.bats` file (and each component's `<component>/tests/integration/vm/*.bats`). Each file gets a fresh disposable VM, and files run **in parallel**; a file marked `# qci:host-only` runs on the host instead, with no VM, while the golden builds (see [Parallelism & per-run golden](#parallelism--per-run-golden-image)). |
| `gui` | Run executable qdwin GUI smokes, qdshell vision pytest when configured, and markdown scenario assignments for qdwin, qdlocker, qdistro permissions GUI, and qdwin-noctalia. Normal disposable runs use the qdwin profile (`QDISTRO_VM_GUI_SESSION=qdwin`) for product scenarios; the labwc admin profile runs only when explicitly enabled with `QCI_LABWC_ADMIN_LANE=1` or `QCI_XWAYLAND_E2E=1`. The agent scenarios run **in parallel** (one disposable VM each). |
| `gui-admin` | Run the GUI gate in admin/non-qdwin mode: qdwin/qdshell, qdlocker, qdwin-noctalia, tier-4/5 and the qdwin-lane admin-app scenarios are recorded as intentional skips while the labwc admin lane runs (it sets `QCI_LABWC_ADMIN_LANE=1`; that lane is opt-in in `qci gui`/`full`). |
| `full` | Run `preflight`, `host`, `release-manifest`, `bootstrap-release-profile`, `image`, `vm-smoke`, `bats`, and `gui`. |
| `snapshot-daily` | Build a `qdistro-daily-YYYY-MM-DD` VM from current source state. |
| `cleanup` | Remove stale `qci-*` disposable VMs/overlays. Never touches `qdistro-daily*`. |

A default developer `qci full` now needs a built image (`image/build-in-vm.sh`):
with no artifact, or one built from another tree, the image gate exits 20
(`build`) unless `QCI_SKIP_IMAGE=1` is set.
For a developer full run, `QCI_SKIP_IMAGE=1 ci/bin/qci full` omits the image
gate and records an explicit skip row. The report Summary names the selected
published artifact and its `.sha256` sidecar digest (or `none`), without
decompressing or booting it. Such a run does not qualify the image or count as
P8 green full evidence. `QCI_RELEASE=1` rejects this switch before any gate or
VM starts; release full runs always exercise the image gate.

`QCI_RELEASE=1` strengthens `full`: every `skip` or `blocked` row from a
release-relevant gate is fatal. Release evidence is green only when the
required VM, Bats, GUI, manifest, bootstrap-contract, and tester-image
rows actually ran.

## Typical run

```bash
qdistro/ci/bin/qci full
```

A full run takes a long time. A bare `qci` is bound to its terminal, so a closed
terminal or dropped SSH session kills the run mid-step (SIGHUP). Two safeguards:

```bash
# Preferred: run detached in a timestamped tmux session that survives disconnects.
qdistro/ci/bin/qci-tmux full
#   session: qci-full-<UTCstamp>   log: /tmp/qci-full-<UTCstamp>.log
#   attach:  tmux attach -t qci-full-<UTCstamp>
```

`qci` itself also traps `SIGHUP` now, so even a direct run finalizes the report
and releases VMs on a terminal hangup instead of leaving a half-finished run.

Successful disposable `qci-*` VMs are destroyed. Failed disposable VMs are
preserved by default for debugging and recorded in the report. To delete failed
VMs as well:

```bash
QCI_DELETE_FAILED_VM=1 qdistro/ci/bin/qci full
```

Create the daily snapshot VM requested by the CI policy:

```bash
qdistro/ci/bin/qci snapshot-daily
# default VM name: qdistro-daily-$(date -u +%F)
```

## Image release identity

The image gate in `full` checks the image's five clean `SOURCE` commits
against `release-manifest/manifest.snapshot` before boot qualification. It
also checks the version against `image/config.xml`, the Tumbleweed snapshot
against the repo-root `snapshot.conf`,
and the profile against `QDISTRO_PROFILE` (default `release`). A pinned dev
tester remains supported with `QDISTRO_PROFILE=dev`; dirty source stamps do
not qualify as release evidence. Standalone `QCI_RELEASE=1 qci image` captures
the configured release manifest for the same check. The run records the
expected manifest/profile, selected artifact digest and expected/observed
identities in `host/image-release-identity.log`.

Cached raw images are compared in full against fresh decompression before
reuse. Verification therefore needs room for another uncompressed image,
even when a cached raw already exists.

## Parallelism & per-run golden image

The `bats` and `gui` gates run their disposable VMs **concurrently** in a bounded
pool. Each VM is ~4 GiB RAM + `QDWIN_VM_VCPUS` (default 4) vCPUs, so RAM is the
binding constraint; CPU is intentionally overprovisioned.

- **bats concurrency** auto-selects a tier from host RAM + logical CPUs:
  minimal `≤32 GiB → 4`, medium `≥56 GiB & ≥10 cores → 10`, high
  `≥90 GiB & ≥12 cores → 16`. The value is clamped by **current** `MemAvailable`
  (`(avail−6)/5`, ~5 GiB/VM) so a busy host can't be overcommitted.
- **gui concurrency** defaults to **1** (serial). GUI VMs are heavier (nested KVM
  + compositor) and each spawns its own agent process; running many full GUI stacks
  at once has repeatedly produced black screenshots, missed input/focus events, and
  agent timeouts that do not reproduce in isolation. `QCI_GUI_JOBS` is an explicit
  opt-in for throughput experiments and is still RAM-clamped.

**Per-run golden image:** native qdwin, qdshell, daemons and qsu binaries, plus
the SELinux policy modules, are built from current source in rootless Podman and
cached by source, container image and snapshot. The runtime-only cloud VM
receives that verified payload;
`fresh-vm-bootstrap.sh` installs the remaining Python services, QML and units.
When tier-2 workloads are requested, rootless Podman also builds their three
images against the test substrate snapshot and caches a checksum-verified
archive. The golden loads the archive into admin's Podman store and verifies
each image's snapshot label. A cache hit avoids rebuilding these images inside
every golden; the normal guest build remains available with the Kiwi base.
The `bats` gate provisions this **once per run** into a golden qcow2
(`qci-golden-bats-*.qcow2`), then every worker
clones that golden and **skips the build** (per-VM provisioning drops to ~10 s).
The golden is built from *current* source each run (still fresh), cleaned up at
run end, and preserved only if a failed worker that references it is preserved.
The **gui** gate uses the same per-run-golden mechanism (admin + qdwin profiles;
`spin-test-vm-gui.sh`).

### Environment knobs

| Variable | Default | Effect |
| --- | --- | --- |
| `QCI_JOBS` | auto-tier | Override bats pool concurrency (still RAM-clamped). |
| `QCI_GUI_JOBS` | 1 | Override gui pool concurrency (default serial; still RAM-clamped). |
| `QCI_GUI_SKIP_QDWIN` | 0 | `1` runs only the admin/non-qdwin GUI lane for `qci gui`; `qci gui-admin` sets this automatically. |
| `QCI_XWAYLAND_E2E` | 0 | `1` enables the opt-in qterminal/Textual XWayland desktop-integration scenarios; they otherwise skip in normal GUI/full runs. |
| `QCI_LABWC_ADMIN_LANE` | 0 | `1` runs the labwc/XWayland **admin lane**: every permissions-gui/workflow-gui scenario that is not routed to the qdwin lane (`gui_scenario_requires_qdwin`). The image ships none of labwc, LXQt, XWayland or the xcb admin-app launcher, and the lane is the source of the stale/half-drawn frame flakes, so it skips by default; the admin-app scenarios that test shipped behaviour (permissions-gui 03 04 06 08 10 12 13 14 34 43 47, workflow-gui 04) run on the qdwin lane with the shipped native-Wayland launcher instead. `qci gui-admin` sets it to `1`. `QCI_XWAYLAND_E2E=1` also admits its qterminal/TUI scenarios, which only exist on this lane. |
| `QCI_GUI_APPS` | 0 | `1` enables the periodic **gui-apps** lane: the third-party app-compatibility scenarios `qdwin/tests/apps/01` and `05`–`11`, and bakes their ~20 packages into the gui-qdwin golden (`QDWIN_APP_DEPS=1`). Run it on a schedule or when qdwin's XWayland/xdg code changes, e.g. `QCI_GUI_APPS=1 ci/bin/qci gui --scenario <abs path>...`; they never gate the normal run. `QDWIN_APP_DEPS=1` alone only bakes the packages; it no longer turns these scenarios on. The other app scenarios (02, 03, 12, 13) are blocking and use only the core test clients (foot, xterm, xfreerdp) baked into every golden. |
| `QCI_AGENT_TIMEOUT` | 0 | Host-side backstop deadline (s) on each agent scenario, wrapping `QCI_AGENT_CMD` in `timeout -k 15`. `0` = unbounded (the operator command owns the budget). When both are set the smaller wins; on expiry the agent is killed and the scenario fails closed (rc=124, no verdict). |
| `QCI_GUI_RETRY` | 0 | Classified GUI retry. `0`/unset = **report-only**: classify each failure and log to `flake.tsv` what *would* retry, but never re-run. `1`/`classified` = retry **exactly once on a fresh VM**, and only for tight retriable infra/tooling signatures such as `transport-timeout` (qemu-agent/vm-exec wedge), `agent-api-unreachable` (exact external provider connection or selected-model-capacity failure), and `agent-tooling` (agent command-construction failure). `status=FAIL`/`ERROR`, generic `UNKNOWN`, `no-verdict`, and `agent-timeout` (slow agent — possible product hang) are **never** auto-retried. A retried pass always emits a `flake.tsv` row + a note on the result row, so a flake is never silently green. |
| `QCI_NO_GOLDEN` | 0 | `1` disables the per-run golden; every worker runs the full bootstrap. |
| `QDISTRO_VM_BASE` | baked | `baked` uses the pinned cloud-derived baseweed image. `kiwi` explicitly uses the imported Kiwi base; `auto` prefers Kiwi when imported, otherwise baseweed. The product image gate still qualifies Kiwi. |
| `QDISTRO_TEST_SUBSTRATE` | `snapshot.conf` (repo root) | Select an alternate manifest with a cloud URL, SHA256, architecture and Tumbleweed repository snapshot for a test substrate experiment. |
| `QCI_NATIVE_BUILDER` | `podman` for baked cloud, `guest` for Kiwi | Select the native build location. The baked cloud base omits compilers and headers, so its supported mode is `podman`. |
| `QCI_PODMAN_IMAGE` | `registry.opensuse.org/opensuse/tumbleweed:<snapshot>` (the `snapshot.conf` pin) | Change the rootless builder image. The resolved image ID is part of the native cache key; the container aligns its packages to the pinned snapshot before building. |
| `QDWIN_VM_VCPUS` | 4 | vCPUs per disposable VM. |
| `QCI_DELETE_FAILED_VM` | 0 | `1` deletes failed VMs instead of preserving them. |
| `QDISTRO_VM_EXEC_TIMEOUT` | 1800 | Overall deadline (s) for a single `vm-exec` in-guest command. On expiry `vm-exec` attempts an identity-checked TERM, then KILL, of the discovered and pinned guest process tree, and exits 124. It reports its own verification limits: a descendant whose identity it cannot pin is named (`unpinnable-descendants:`) and deliberately **not** signalled, and it cannot guarantee it discovers a reparented process, so reaping is attempted and reported, not guaranteed. The deadline is also checked against elapsed time BETWEEN steps, not enforced as wall clock, so a true wall-clock cap must come from outside — use `timeout -k 30 <n> vm-exec ...`, where `-k` makes the cap an undeniable KILL-at-cap+grace for **vm-exec itself**, which a plain `timeout` does not give you against a TERM-resistant process. It does **not** reach descendants: `timeout` waits only for its direct child, so if vm-exec exits on the TERM the later group KILL is never sent. An outer cap bounds how long you wait; it does not bound cleanup, and a short grace can cut vm-exec's own cleanup verification short. The counter is also clamped across host suspend, so it measures elapsed time as the host saw it, not as the guest experienced it. `0` = unbounded. |
| `QDISTRO_VM_AGENT_RPC_TIMEOUT` | 30 | Host-side cap (s) per `virsh qemu-agent-command` RPC, bounding any ONE wedged agent call. It does not bound the total: the deadline above is checked between steps, so many capped calls can still overrun it. `0` = unbounded. |
| `QDISTRO_VM_EXEC_ORPHAN_REAP` | 1 | `vm-exec` orphan registry. Each call whose guest command is pinned records it (guest pid, start time, guest boot id, domain uuid); the record is removed once the command is known finished. Before launching, `vm-exec` resolves records left by a **SIGKILLed** `vm-exec` (identity- and boot-checked kill in the guest). If an orphan cannot be confirmed gone, or the per-VM lock is not obtained within `QDISTRO_VM_EXEC_REAP_LOCK_WAIT` (default 4 x (`QDISTRO_VM_KILL_VERIFY_TIMEOUT`+grace+1)+10 s), the launch is **refused with exit 75** (retryable, nothing started). Records are also KEPT when a call exits while its command may still run -- a poll error such as `Guest agent not responding`, qga losing its bookkeeping (`PID ... does not exist`), or an unverified signal cleanup -- and the next call on that VM resolves them (normally as `already-gone`) once the agent answers. `0` disables recording and reaping. |
| `QDISTRO_VM_EXEC_STATE_DIR` | `$XDG_RUNTIME_DIR/qdistro-vm-exec-<uid>` | Where the orphan registry lives (one subdirectory per VM name). When `XDG_RUNTIME_DIR` is unset it falls back to `/tmp/qdistro-vm-exec-<uid>`. Records are only seen by `vm-exec` calls that resolve to the SAME directory, so a caller with `XDG_RUNTIME_DIR` set and one without it do not see each other's orphans. Set this explicitly when mixing such environments. Per-VM directories are never removed automatically; they are tiny, and stale ones may be deleted by hand when no `vm-exec` is running. A malformed or unknown-format record refuses launches (exit 75) until reconciled by hand: if no such driver runs in the guest, `rm` the file the error names. |
| `QD_VM_START_MAX_WAIT` | 300 | Backstop cap (s) on guest-agent readiness in `vm-start-and-wait` (raised from 120 for parallel boot contention). |
| `QCI_HOST_STEP_TIMEOUT` | 600 | Per-step wall budget (s) for the `host` gate. It does **not** cover the `qdistro-pytest` step — see the next row. Raising this alone does not give pytest more time. |
| `QCI_QDISTRO_PYTEST_TIMEOUT` | 1800 | Wall budget (s) for the `qdistro-pytest` host step **only**, deliberately independent of `QCI_HOST_STEP_TIMEOUT`. The suite's honest cost is ~550s across ten batches, so the shared 600s step budget left no headroom and one slow test killed the gate; 1800s is ~3.3x the honest cost, which keeps the step a wedge detector without being sensitive to normal variance. Set **both** knobs to slow down every host step. |
| `QCI_EXTRA_BATS_ROOTS` | *(unset)* | Colon-separated extra repo roots to discover `tests/integration/vm/*.bats` under. Discovery otherwise covers only the declared `PROJECTS` checkouts, so an out-of-tree suite is invisible unless opted in here. Non-existent roots are ignored; the file list is de-duplicated. |

A per-task timing breakdown (provision vs work seconds per file/scenario) is
written to `<run-dir>/timings.tsv` for spotting outliers.
The [test split proposal](TEST-SPLITS.md) summarizes a measured full run and
the coverage work needed before automatic selection can omit slow scenarios.
For quick Python app feedback, use an isolated worktree and run
`ci/bin/qci feedback qdfileman qdfileman/qfileman/window.py` (replace the path
with the changed files). It calls the same job as `host`, with the same default
600-second step timeout. A pass is development feedback only and does not
satisfy `host` or `full`; `QCI_RELEASE=1` is refused before a run is created.
The `feedback_*` manifest fields and `host/feedback-paths.txt` record selection,
revision, elapsed time to result, job counts and exit outcome. Dependency content
hashes and Python/pytest/Qt package versions appear in
`host/feedback-dependencies.txt`; `repo-state.tsv` records checkout state.
Compare timings only at matching revision, clean/dirty state and dependency
identity; no speedup against full host has been measured here. Job counts describe
jobs, not pytest test cases; the pytest log has case counts.

The consumer map is deliberately a shadow report: qdfileman changes still need
`host bats gui`, and no paths, shared SDK receive/send, installation changes or
unknown paths require full acceptance coverage. The command executes one explicit
job regardless of this report, and never changes `qci affected` selections.
SDK or installation feedback alone cannot validate their other consumers.

For focused development runs, `ci/bin/qci-lane check` lists the available
group sizes, `ci/bin/qci-lane list bats-fast` prints its files, and
`ci/bin/qci-lane run gui-locker` executes only that scenario group through
the normal qci gate. The group and exact file selection appear in the run
manifest. These explicit lanes do not alter `qci full` or `qci affected`.
`ci/bin/qci-lane audit` lists discovered Bats and GUI cases missing from the
pilot registry before any automatic per-component selection is attempted.

### Cloud test substrate

The default VM base is built from openSUSE's Minimal-VM cloud qcow2. The
checked-in [`snapshot.conf`](../snapshot.conf) at the repo root pins its SHA256
and the OSS/non-OSS history snapshot. It is the **one** Tumbleweed pin: the kiwi
tester image (`image/build.sh` passes the history repositories to kiwi), the
tier-2 workload images, and the rootless Podman native builder (base image
`tumbleweed:<snapshot>`) all read it, so every build downloads from one
snapshot and shares one RPM cache. The download must also
match openSUSE's signed checksum. Changing the manifest or build recipe gives
the next base a new filename; old disks remain available to preserved workers.
`QDISTRO_VM_BASE=kiwi` remains available when image parity is the test target.

To rotate the snapshot, verify the new cloud checksum signature and read the
cloud image's `/etc/os-release` `VERSION_ID`. Select that same history snapshot
(both OSS and non-OSS repositories must be available), edit the manifest's
digest and snapshot together,
then build with `scripts/vm/build-baseweed-from-scratch.sh` followed by
`scripts/vm/build-baked-baseweed.sh`. The history service retains snapshots for
roughly a month; schedule a candidate build about weekly. Cloud base builders
and qci VM test entry points reject a pin more than 14 UTC calendar days old,
including when a matching base is already cached. Refresh the pin and rebuild
both bases (and the tester image) before launching tests. The image build
refuses a pin older than 14 days the same way. An expired snapshot is an error, not a
reason to use rolling repositories. An explicit alternate
manifest via `QDISTRO_TEST_SUBSTRATE` keeps experiments separate.

Downloaded RPMs from the base builders, the tier-5 base, and Bats/qdwin
goldens are exported to
`$QDWIN_CACHE_DIR/rpm/<snapshot>/<arch>/` (default
`~/.cache/qdistro/rpm/`) and seeded on a later rebuild. The repository files
retain `gpgcheck=1`; cached RPMs are only download hints. The cloud qcow2 and
its signed sidecars live in a SHA256-named cache entry. The existing fixed-name
baseweed disks are left untouched when the pinned substrate is first built.
The rootless native builder has a separate RPM cache under
`$QDWIN_CACHE_DIR/podman-rpm/<snapshot>/<arch>/packages/` and stores its staged
archive under `$QDWIN_CACHE_DIR/native-podman/<snapshot>/<arch>/`. A changed
source tree, container image, snapshot or Meson options triggers a rebuild.
Its dependency installation lives in a rootless Podman toolchain image keyed
by the base image ID, snapshot and dependency recipe. A source-only rebuild
reuses that image and skips `zypper dup` and the development-package install; the RPM
cache supplies downloads when the toolchain image itself must be rebuilt.
Golden bootstraps install the local qdbrowser and qdlocker Python packages
without pip build isolation or an index; the snapshot's setuptools RPM supplies
their build backend instead of a fresh PyPI download.
The guest loads the staged SELinux modules with `semodule`; no native compiler,
Meson, Ninja, `make` or policy headers are needed in the cloud test base.
The native builder checks the broker SELinux neverallow negative control
against the pinned snapshot policy store before it caches the payload.
Tier-2 test images use Podman's local image layers and a separate archive cache
under `$QDWIN_CACHE_DIR/tier2-podman/<snapshot>/<arch>/`. Changes to tier-2
source, the base container image or the pinned test snapshot rebuild that
archive. `QCI_OFFLINE=1` requires both the base container and archive to be
cached. There is no separate tier-2 pin: `tier2/make-tier2-image.sh` and the
installed `/usr/lib/qdistro/tier2/SNAPSHOT` both derive from `snapshot.conf`.

## Agent-assisted GUI scenarios

Markdown GUI scenarios need a visual runner. `qci gui` always creates prompt
files under `agent-notes/`. If `QCI_AGENT_CMD` is unset, those scenarios are
marked `blocked` so the report cannot be mistaken for a green run.

`QCI_AGENT_CMD` receives the prompt path as its first argument:

```bash
QCI_AGENT_CMD='my-visual-agent-runner' qdistro/ci/bin/qci gui
```

If your runner needs a template, include `{prompt}` — it is substituted with the
prompt-file path and the result is run via `bash -lc`, so the template can pass
the prompt as a path, on stdin, or inlined with `$(cat {prompt})`, whichever the
runner takes. The supported runner is Codex with `gpt-5.6-luna`, which reads the
prompt on stdin:

```bash
QCI_AGENT_CMD='codex --yolo exec -m gpt-5.6-luna -c model_reasoning_effort=medium --skip-git-repo-check - < {prompt}' \
QCI_AGENT_MODEL=gpt-5.6-luna \
  qdistro/ci/bin/qci gui
```

`-c model_reasoning_effort=medium` pins the effort the visual bar was
validated at; without it the host's codex default applies, and on some hosts
that is `none` (see doc/dev.md). The manifest records it as
`qci_agent_reasoning_effort`.

`--yolo` is required because the agent must run `vm-exec`/`virsh` and write its
`status.txt` without interactive approval. Do NOT add `--ephemeral`: the gate
reads each attempt's codex rollout (`$CODEX_HOME/sessions`, default
`~/.codex/sessions`) to see which attested frames the driver actually opened
(`ci/lib/gui_rollout_views.py`, sidecar `gui/<slug>.views.txt`). A
`qci:visual: required` PASS or FAIL whose driver opened no attested frame is
recorded ERROR (`agent-unviewed-verdict`); with `--ephemeral` there is no
rollout and every attempt is `unobservable:ephemeral` (no verdict changes). If
the template sets its own `CODEX_HOME=...`, also export `QCI_GUI_CODEX_HOME`
with the same value, or the attempts are `unobservable:codex-home`. Retention:
the rollouts are codex's own files and keep every viewed frame as base64, so
`~/.codex/sessions` grows by roughly the encoded size of every frame each
attempt opened (a 1280x800 admin-app frame is about 50 KB of base64; a two-frame
grading session measured 148 KB); prune it as you would any codex history. Each attempt gets its own working
directory regardless of the template: `run_agent_command` creates one with
`mktemp -d` and `cd`s into it before running the agent. Set `QCI_AGENT_MODEL` whenever a wrapper selects the
model outside the visible template, or the manifest records `unknown`.

**The runner must be vision-capable.** Visual scenarios are graded by opening
the harvested PNGs and looking at them. A driver that cannot view an image has
no verdict about pixels and must record `ERROR`, never `PASS` or `FAIL` -- see
`doc/dev.md` "Visual evidence: vision, not OCR".

The executable qdwin smokes run before the markdown assignments:

- `qdwin/tests/gui/agent-mvp-session-smoke.sh`
- `qdwin/tests/gui/agent-protocol-audit.sh`
- `qdwin/tests/gui/agent-cursor-clickthrough-smoke.sh`
- `qdwin/tests/gui/agent-click-smoke.sh`
- `qdwin/tests/gui/agent-shell-capture-smoke.sh` (skips with a rebake hint
  when the golden's compositor unit lacks `QDWIN_ENABLE_SHELL_CAPTURE=1`)

The qdshell UI vision pytest is also wired into `gui`; it uses the same local
Codex-backed settings as the rest of the GUI gate.

Prefer a fresh disposable VM for normal GUI CI. Reusing a VM preserved from a
failed smoke run is useful for debugging, but it can carry service restart
loops or stale Wayland socket conflicts into the GUI gate.

## VM policy

- CI-created VMs are named `qci-*`.
- `qdistro-daily` and `qdistro-daily-*` are protected and refused as explicit
  test targets unless `QCI_FORCE_PROTECTED_VM=1` is set.
- Passing CI-created VMs are deleted.
- Failed CI-created VMs are kept by default for triage.
- `qci cleanup` only considers `qci-*` names and has a dry-run mode:

```bash
qdistro/ci/bin/qci cleanup --dry-run --age-hours 24
```

Cleanup asks libvirt for each VM's real disk path and skips domains whose disk
cannot be located. It does not guess paths outside libvirt metadata.

## Host test dependencies

`qci host` builds and tests every component in rootless Podman. Host prerequisites
are Podman, Bash, Git, Python 3 (stdlib), Bats, and the libvirt/VM tooling for VM
gates. `ci/bin/qci-host-deps --check` checks orchestration commands; it never
installs host packages. Preflight requires Podman. The edit guard and runner
selftests remain host orchestration; component builds and tests run only in the
container. There is no public native-host fallback flag.

The image uses `QCI_PODMAN_IMAGE` (default: the `snapshot.conf` Tumbleweed pin),
aligns packages with the native builder's `container-native-deps.sh`, and adds
[`containers/host-packages.txt`](containers/host-packages.txt). This replaces the
old host-dependency probe table as the single host-test dependency list. Qt,
Python test modules, npm, Weston, and development headers live in the image.
The QTermWidget binding is built from the vendored SIP source into the image.
The patched libweston itself is built from current source during the gate,
including the inert-seat regression, before qdwin is compiled against it.

`ci/bin/qci-host-image` prints the resolved image name. Its cache key includes
the dependency recipe and SIP source, snapshot, and resolved base image ID.
Source-only changes reuse the image. RPM downloads share the native builder's
`$QDWIN_CACHE_DIR/podman-rpm/<snapshot>/<arch>/packages/` cache. The default cache
root is `~/.cache/qdistro`. `QCI_OFFLINE=1` refuses a missing base or toolchain
image, without pulling or rebuilding it.

The worktree is mounted at its original absolute path with `--userns=keep-id`,
so generated files retain the invoking user's ownership. Linked-worktree Git
metadata is mounted read-only. No host home, display socket, D-Bus socket or
Python package path enters the test container. Container SELinux labeling is disabled (`--security-opt label=disable`); it
never relabels the worktree, shared Git metadata, or RPM/npm caches. The lab
host currently reports enforcing mode; assigning
`container_t` breaks unit fixtures that use their own PID as a development
peer. This does not change host enforcement. SELinux runtime isolation remains
VM-gated; this toolchain container is for trusted development source.

A private runtime directory and UTF-8 locale support offscreen Qt tools.
An isolated `dbus-run-session` supplies both test session and system-bus
addresses; neither connects to host services. `--init` reaps children, and the
private row entrypoint does not install the outer runner's VM-cleanup traps.
The container preserves the host timezone (`--tz=local`), matching the previous
native gate. A known notebook-test limitation remains on UTC hosts: its PDF
metadata normalizer handles signed offsets but not the UTC `Z` spelling.

A separate preparation container runs `npm ci --prefer-offline` with downloads
cached in `$QDWIN_CACHE_DIR/host-npm/` (default `~/.cache/qdistro/host-npm/`).
Keeping this cache outside the source tree also keeps it out of selftest Git
fixtures. Each extension's `node_modules` receipt includes both
package files, Node version and architecture; matching dependencies are reused.
Offline preparation uses `npm ci --offline` and no network. All build/test rows
run with `--network=none`, Qt offscreen, and the existing printer-test exclusion.
The existing row names, classifications, logs under `ci/runs/<run>/host/`, and
`results.tsv` format are unchanged. Image/preparation diagnostics are saved in
that directory too; infrastructure failures add an explicit failing row.

For short development checks, use the same image without acceptance overhead:

```sh
ci/bin/qci-host-run bash -c 'cd qdfileman && python3 -m pytest -q'
```

This helper runs an explicit command with networking disabled and does not
produce acceptance evidence. Runtime and visual integration testing remains
in VMs. The older `qci feedback qdfileman` developer lane still uses its native
host environment; prefer `qci-host-run` on hosts without Python/Qt dependencies.

## Fast triage

```bash
qdistro/ci/bin/qci list-runs
qdistro/ci/bin/qci triage --latest
qdistro/ci/bin/qci report --latest
```

Start from the report, then inspect the linked logs, journals, screenshots, and
the preserved VM name if the failure kept one alive.

The container init runs as container root (still rootless on the host); `setpriv`
drops to the invoking UID/GID before running any source command. This preserves
normal PID-1 signal permissions while all build outputs retain user ownership.
