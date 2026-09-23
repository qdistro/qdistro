# Agent instructions for the qdistro monorepo

qdistro is a single-tenant Linux distribution with Qubes-inspired app
isolation, built on libweston + Wayland + Python/Qt/QML. This repository holds
all of it: qdistro's own root content (broker, daemons, SDK, installers, image,
CI) and ten components as top-level directories. Start with
[README.md](README.md) and [doc/overview.md](doc/overview.md).

## Component map

| Dir | What it is | Read first |
| --- | --- | --- |
| `qdwin/` | libweston shell plugin: the compositor (C, meson) | [qdwin/README.md](qdwin/README.md), `qdwin/doc/AGENTS.md` |
| `qdshell/` | desktop shell (Quickshell/QML fork of Noctalia) + its QML plugin | [qdshell/README.md](qdshell/README.md), `qdshell/tests/AGENTS.md` |
| `qdlocker/` | screen locker (PyQt/QML) over the `qdwin_locker_v1` protocol | [qdlocker/README.md](qdlocker/README.md), `qdlocker/tests/gui/AGENTS.md` |
| `qdgreeter/` | boot greeter for greetd (PyQt/QML) | [qdgreeter/README.md](qdgreeter/README.md) |
| `qdbrowser/` | first-party browser (PyQt6 + QtWebEngine) | [qdbrowser/README.md](qdbrowser/README.md), `qdbrowser/AGENTS.md` |
| `qdchrome-extension/` | Chromium MV3 extension for the browser bridge | [qdchrome-extension/README.md](qdchrome-extension/README.md) |
| `qdfirefox-extension/` | Firefox MV3 extension for the browser bridge | [qdfirefox-extension/README.md](qdfirefox-extension/README.md), `qdfirefox-extension/AGENTS.md` |
| `qdterm/` | terminal (Python package `qterminator`) | [qdterm/README.md](qdterm/README.md), `qdterm/AGENTS.md` |
| `qdfileman/` | file manager (Python package `qfileman`) | [qdfileman/README.md](qdfileman/README.md), `qdfileman/AGENTS.md` |
| `qnotebook/` | notes/wiki app (PyQt6) | [qnotebook/README.md](qnotebook/README.md), `qnotebook/AGENTS.md` |

`qdterm/` and `qdfileman/` take their GitHub repository names; their Python
packages, binaries, desktop IDs and D-Bus names are still `qterminator` and
`qfileman`. Everything else at the root is qdistro's own content; the root
[README.md](README.md#repository-layout) maps it.

Subdirectory agent docs (read the nearest one before editing there):
[ci/AGENTS.md](ci/AGENTS.md), [doc/AGENTS.md](doc/AGENTS.md),
[image/AGENTS.md](image/AGENTS.md), [tests/AGENTS.md](tests/AGENTS.md),
[deploy/AGENTS.md](deploy/AGENTS.md), and the `AGENTS.md` files under
`tests/integration/*/`.

## Building

Build order: `qdwin` first (the root daemons and the qdshell QML plugin compile
against its protocol XML), then the root daemons, then qdshell. From the repo
root:

```sh
(cd qdwin && meson setup build && meson compile -C build)
# the daemons and qdshell find qdwin's protocol XML via its uninstalled .pc
export PKG_CONFIG_PATH="$PWD/qdwin/build/meson-uninstalled${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
(cd daemons && meson setup build && meson compile -C build)
(cd qdshell && meson setup build && meson compile -C build)
python3 -m pytest            # root unit tests (tests/unit)
```

`qdistro-forward` and `qdistro-nested-pixelfeed` are optional: meson skips them
(with a `message`) when the FreeRDP 3 / PipeWire development packages are
missing, and the build still exits 0. See [doc/dev.md](doc/dev.md).

Before `ci/bin/qci host` in a fresh clone, install the two WebExtensions' npm
dependencies (the `host` gate runs their tests but does not install them):

```sh
(cd qdchrome-extension && npm ci)
(cd qdfirefox-extension && npm ci)
```

Host prerequisites and the VM path: [doc/dev.md](doc/dev.md).

## Testing: qci

`ci/bin/qci` (or `just` in `ci/`) is the monorepo's CI. There is no hosted CI.

```sh
ci/bin/qci preflight      # host tools, libvirt session, in-tree components
ci/bin/qci affected --changed-from main   # which gates a change needs
ci/bin/qci host           # host tests/builds for root + every component
ci/bin/qci full           # everything, VM gates included (hours)
```

The VM gates need a libvirt session with nested KVM. The GUI gate needs the
sanctioned visual driver pinned explicitly in `QCI_AGENT_CMD`; see
[ci/README.md](ci/README.md) and [ci/AGENTS.md](ci/AGENTS.md) (including its
"Operational lessons" section) before running or grading a GUI run.

## Working workflow

- **One worktree per task:** `git worktree add .worktrees/<topic> -b <branch>`
  (`.worktrees/` is gitignored). A worktree is a complete tree; no sibling
  checkouts or symlinks are needed.
- **Pick gates with `qci affected`**, then run the gates it selects from the
  worktree. It selects whole **gates**, not component-precise tests: a change
  inside one component usually avoids some gates (a qdterm change selects only
  `host`), but every gate it does select runs in full. `host` builds and
  tests every component, and `gui` runs every GUI scenario, so a qdshell or
  qdlocker change (`host gui`) costs most of the 2–3 h suite. For quicker
  feedback while developing, run single scenarios
  (`ci/bin/qci gui --scenario <abs path>`); the selected gates are the
  acceptance bar.
- **Review before merge.** Get the change reviewed (diff + gate evidence)
  before it lands on `main`.
- **The live checkout on `main` is merge-only.** Agents never edit it
  directly; work happens in worktrees and is merged in.
- **Commit by explicit path** (`git add <paths>`, never `git add -A`) when
  several sessions may share a checkout.
- Edits to root `tests/`, `ci/prompts/` or `selinux/` are guarded; qci fails
  them unless `QCI_ALLOW_TEST_EDITS=1` is set for a genuine test/CI change.
  Say so in the commit message.

## Shared-host qci rule

Run **one `qci full` / GUI run at a time per host**: the HTTP staging port
(8765) and VM/golden names are host-wide singletons. Before starting one, check
what is running:

```sh
systemctl --user list-units 'qci-*'          # runs launched under systemd-run
pgrep -af '[c]i/bin/qci'                     # any qci process ([c] keeps pgrep from matching this command)
virsh -c qemu:///session list --all | grep qci-
```

Launch long runs under `systemd-run --user --unit=qci-<name> ...` and never
edit a script while a run that sources it is going. Each run's
`ci/runs/<run>/repo-state.tsv` records the tree (worktree path + full SHA +
dirty count) that produced it.

## History and bisecting

Each component's pre-monorepo history is kept, unrewritten, on
`legacy/multirepo/<dir>` (see [MIGRATION.md](MIGRATION.md)):

```sh
git fetch origin 'refs/heads/legacy/multirepo/*:refs/remotes/origin/legacy/multirepo/*'
git log origin/legacy/multirepo/qdwin -- qdwin/qdwin.c   # path WITHOUT the leading qdwin/
```

`git blame` on `main` stops at a component's import commit. When bisecting,
`git bisect skip` the migration range listed in MIGRATION.md (components
present but the tree still wired for the old sibling layout).

## Licensing

Per directory. The root `LICENSE` (GPL-3.0-or-later) covers qdistro's own root
content. Each component keeps its own license: its `LICENSE` file where it has
one (qdwin, qdshell, qdterm, qdfileman) or its package metadata (qdbrowser
GPL-3.0-only, qnotebook GPL-2.0-or-later, qdgreeter and qdlocker MIT; the two
extensions declare none). Do not relicense across directories.

## Not in this repo

The project tracker and the websites are separate repositories. Private notes
are not required reading: everything needed to build, test and change qdistro
is in this tree.
