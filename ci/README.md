# qdistro local CI

`qdistro/ci` is the handoff folder for local continuous integration across this
umbrella checkout. It is meant to be usable by a developer or by an agent with
shell access to the same workspace.

The entrypoint is:

```bash
qdistro/ci/bin/qci <gate>
```

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

## Gates

| Gate | Purpose |
| --- | --- |
| `preflight` | Verify sibling repos, libvirt session, VM tools, prebaked image, and common host tools. |
| `host` | Run host tests/builds across all sibling projects: Python pytest repos, WebExtension npm tests/builds, qdwin/qdshell meson/QML checks, and the Zola site. |
| `vm-smoke` | Create or reuse a VM and verify the qdwin/qdshell session, Wayland socket, and core user services. |
| `bats` | Run every `qdistro/tests/integration/vm/*.bats` file. By default each file gets a fresh disposable VM. |
| `gui` | Run executable qdwin GUI smokes, qdshell vision pytest when configured, and markdown scenario assignments for qdwin, qdlocker, qdistro permissions GUI, and qdwin-noctalia. |
| `full` | Run `preflight`, `host`, `vm-smoke`, `bats`, and `gui`. |
| `snapshot-daily` | Build a `qdistro-daily-YYYY-MM-DD` VM from current source state. |
| `cleanup` | Remove stale `qci-*` disposable VMs/overlays. Never touches `qdistro-daily*`. |

## Typical run

```bash
export QDISTRO_VM_PASSWORD='...'
qdistro/ci/bin/qci full
```

Successful disposable `qci-*` VMs are destroyed. Failed disposable VMs are
preserved by default for debugging and recorded in the report. To delete failed
VMs as well:

```bash
QCI_DELETE_FAILED_VM=1 qdistro/ci/bin/qci full
```

Create the daily snapshot VM requested by the CI policy:

```bash
export QDISTRO_VM_PASSWORD='...'
qdistro/ci/bin/qci snapshot-daily
# default VM name: qdistro-daily-$(date -u +%F)
```

## Agent-assisted GUI scenarios

Markdown GUI scenarios need a visual runner. `qci gui` always creates prompt
files under `agent-notes/`. If `QCI_AGENT_CMD` is unset, those scenarios are
marked `blocked` so the report cannot be mistaken for a green run.

`QCI_AGENT_CMD` receives the prompt path as its first argument:

```bash
QCI_AGENT_CMD='my-visual-agent-runner' qdistro/ci/bin/qci gui
```

If your runner needs a template, include `{prompt}`:

```bash
QCI_AGENT_CMD='claude -p "$(cat {prompt})"' qdistro/ci/bin/qci gui
```

The executable qdwin smokes run before the markdown assignments:

- `qdwin/tests/gui/agent-mvp-session-smoke.sh`
- `qdwin/tests/gui/agent-protocol-audit.sh`
- `qdwin/tests/gui/agent-cursor-clickthrough-smoke.sh`
- `qdwin/tests/gui/agent-click-smoke.sh`

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

The `host` gate runs tests across all sibling projects. Three projects need
dependencies that are not part of the base qdistro install:

**qdbrowser tests** require `jeepney` (D-Bus bridge client, already a
runtime dependency in `qdbrowser/pyproject.toml`):

```bash
# Ubuntu
sudo apt install python3-jeepney

# openSUSE Tumbleweed
sudo zypper install python3-jeepney
```

**qdshell QML tests** require the `QtQml.WorkerScript` QML module:

```bash
# Ubuntu
sudo apt install qml6-module-qtqml-workerscript

# openSUSE Tumbleweed
sudo zypper install qt6-declarative-imports
```

**qterminator tests** require the `QTermWidget` Python binding, which is
built from source as part of the qterminator install (it is not packaged
by any distro). Build it from the `qtermwidget-pyqt/` directory in the
qterminator repo:

```bash
cd qterminator/qtermwidget-pyqt && pip install .
```

See `qterminator/README.md` for full build prerequisites (qtermwidget-devel,
sip, pyqt-builder).

## Host Link Checks

The host gate runs `zola check` for `qdistro-site`, including external links.
If the network is flaky and you want only local site validation:

```bash
QCI_ZOLA_SKIP_EXTERNAL=1 qdistro/ci/bin/qci host
```

## Fast triage

```bash
qdistro/ci/bin/qci list-runs
qdistro/ci/bin/qci triage --latest
qdistro/ci/bin/qci report --latest
```

Start from the report, then inspect the linked logs, journals, screenshots, and
the preserved VM name if the failure kept one alive.
