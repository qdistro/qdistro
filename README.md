# qdistro

A single-tenant Linux distribution with Qubes-inspired seamless app
isolation, built on libweston + Wayland + Python/Qt/QML and designed
to be easy to modify with LLM assistance.

This repository is **the whole of qdistro** (a monorepo): documentation,
permission infrastructure, daemons, SDK, admin app, helper tools,
build scripts, integration tests and the local CI harness at the root, plus
the compositor, shell and first-party apps as top-level component
directories. Agents start at [AGENTS.md](AGENTS.md).

The current design shorthand is **one owner, many silos, dynamic sessions**.
The owner is the single human and policy authority. Silos isolate data and
program state. Sessions attach silos and resources while work is happening.
Start with [doc/overview.md](doc/overview.md) and
[doc/glossary.md](doc/glossary.md) for those terms.

The compositor is [`qdwin/`](qdwin/README.md); the desktop shell (a
Noctalia QML fork) is [`qdshell/`](qdshell/README.md). Until 2026-09 these and
the other components were separate repositories; see
[MIGRATION.md](MIGRATION.md).

## Try qdistro

**Status:** in active development. Tester releases are a **raw disk image**
(`qdistro-<version>-<snapshot>.raw.xz`), not an installable ISO: write it to
a USB stick or boot it as a VM disk. That image is the **dev** profile,
default password `qdistro`, sshd off, **UEFI-only**, 32 GB minimum stick.
How to flash it, how to boot it in a VM, the checksum, and the honest
caveats: the [download page](https://qdistro.org/download/). There is no
signed release yet and **no public download is published yet**;
`sha256sum -c` the sibling `.sha256` is the integrity check once you
have the file.

The from-source path below is for developers iterating on a Tumbleweed
VM. The full-stack integration and GUI test suites still run in disposable
libvirt VMs on a host with nested KVM enabled.

> **Host requirements for the VM path:** a Linux host with libvirt + qemu-kvm,
> nested virtualization enabled, and enough headroom to give the guest
> ≥8 GB RAM and ≥60 GB disk.
>
> **Nested virtualization must be enabled explicitly** (e.g.
> `options kvm_intel nested=1` / `options kvm_amd nested=1` in modprobe.d,
> plus `<cpu mode='host-passthrough'/>` or virt-manager's "Copy host CPU
> configuration" for the guest) — the VM-based isolation tiers run VMs inside
> the VM. Be aware that nested virtualization enlarges the hypervisor attack
> surface and is a potential **security risk** for the host; enable it on a
> development machine, not on a host whose isolation guarantees you rely on.

**1. Create a VM and install Tumbleweed** from
[get.opensuse.org](https://get.opensuse.org/tumbleweed/) — choose Minimal or
Server (no desktop needed). Create the first user as `admin`; qdistro reserves
`admin` at uid 1000.

**2. Install git** (not pre-installed on Tumbleweed Minimal/Server):

```sh
sudo zypper install -y git
```

**3. Clone and bootstrap** (as `admin`, which is in the `wheel` group; `sudo`
is available by default for wheel members on Tumbleweed):

```sh
git clone https://github.com/qdistro/qdistro.git
cd qdistro
sudo bash scripts/install/qdistro-bootstrap.sh --profile=dev
```

The bootstrap installs all dependencies, builds the compositor and daemons,
installs the in-tree apps (qdgreeter, qdlocker, qdbrowser, qterminator,
qnotebook, qfileman) from the same checkout, and
configures greetd. The first run takes a while. Idempotent — re-running is
safe. The explicit `dev` profile is required for this preview path: the default
`daily-driver` profile requires a populated, signed release manifest, which is
not published yet.

**4. Reboot:**

```sh
sudo systemctl reboot
```

On next boot, the qdgreeter login screen appears on tty3. Log in as `admin`.

**5. Try the available isolation paths:**

- **Named user silo:** in the admin app, open **Silos**, create a silo with a
  name and unused uid, then use the lifecycle controls to start and stop it.
- **Tier 2 container (developer preview):** from qterminator, build the minimum
  workload image with
  `cd ~/qdistro && bash tier2/make-tier2-image.sh weston-terminal`, then launch
  it with `QDISTRO_PROFILE=dev /usr/bin/qdistro-tier2-spawn --disposable weston-terminal -- weston-terminal`.
- **Tier 4 VM (experimental):** first rerun the bootstrap with
  `--profile=dev --tier4-base`, then launch the built guest with
  `cd ~/qdistro && sudo bash tier4-vm/spawn-tier4.sh demo-vm`. This path requires
  nested KVM and is outside the v1 security guarantee.

The admin app (tray icon) shows active silos, pending approvals, and audit history.

**6. Report back:**
File issues at [github.com/qdistro/qdistro/issues](https://github.com/qdistro/qdistro/issues).
Most useful: "I ran step X and Y was unclear / broken." See
[doc/support.md](doc/support.md) for what to include, how security issues are
handled privately, and the [known-regressions](doc/known-regressions.md) ledger.

## One clone

Everything is in this repository; one clone is the whole developer layout:

```sh
git clone https://github.com/qdistro/qdistro.git
```

```
qdistro/
├── (root)                  ← qdistro's own content: broker/, daemons/, ci/, doc/, image/, ...
├── qdwin/                  ← compositor (libweston shell plugin)
├── qdshell/                ← desktop shell
├── qdlocker/  qdgreeter/   ← screen locker, boot greeter
├── qdbrowser/  qdterm/  qdfileman/  qnotebook/   ← first-party apps
└── qdchrome-extension/  qdfirefox-extension/     ← browser bridge extensions
```

`qdterm/` and `qdfileman/` are the directories of the `qterminator` and
`qfileman` apps (the Python packages and binaries keep those names). The
component map with one line per directory is in [AGENTS.md](AGENTS.md).

Build order: `qdwin` first (the root daemons compile against qdwin's
protocol XML at `qdwin/qdwin/*.xml`), then the root daemons, then `qdshell`.
See [doc/dev.md](doc/dev.md) for the full developer setup.

## Project principles

1. **LLM-modifiability first.** Userspace (apps, shell, session
   manager, admin tools) is Python + Qt + QML so humans and LLMs can
   modify it directly. The compositor core and transport
   infrastructure are commodity C (libweston, PipeWire, FreeRDP,
   qemu, kernel) — leveraged, not rewritten.
2. **Single-tenant, multi-silo.** One physical person, one fingerprint,
   one admin-owned machine policy. Multiple Linux users, silos, and
   sessions separate *data and work contexts*, not humans.
3. **Admin is the trusted base.** One admin user owns all hardware
   and approves all cross-silo actions. Regular users are
   admin-spawned uid sandboxes.
4. **Mainstream Linux primitives over custom infrastructure.**
   D-Bus, polkit, systemd, PipeWire, libweston, xdg-desktop-portal,
   waypipe, RDP — lean on existing standards.
5. **Seamless UX at lower isolation tiers, framed UX at higher ones.**
   The isolation ladder trades seamlessness for containment;
   per-context choice via admin policy.

## Documentation

Start with [doc/overview.md](doc/overview.md) for the vision, then:

| Topic | Doc |
| --- | --- |
| System model | [glossary.md](doc/glossary.md), [architecture.md](doc/architecture.md), [threat-model.md](doc/threat-model.md), [isolation-tiers.md](doc/isolation-tiers.md) |
| Compositor & shell | [compositor.md](doc/compositor.md), [admin-approval.md](doc/admin-approval.md), [window-handoff.md](doc/window-handoff.md), [window-hierarchy.md](doc/window-hierarchy.md), [clipboard.md](doc/clipboard.md) |
| Sessions & devices | [sessions.md](doc/sessions.md), [devices.md](doc/devices.md), [qbus.md](doc/qbus.md), [filesystem.md](doc/filesystem.md) |
| Permissions | [permissions.md](doc/permissions.md), [sudo.md](doc/sudo.md), [selinux.md](doc/selinux.md) |
| Apps | [app-sdk.md](doc/app-sdk.md), [browser.md](doc/browser.md), [browser-extension-install.md](doc/browser-extension-install.md), [printing.md](doc/printing.md), [password-manager.md](doc/password-manager.md) |
| Special workloads | [games.md](doc/games.md), [recall.md](doc/recall.md), [phone.md](doc/phone.md), [cross-machine.md](doc/cross-machine.md) |
| For contributors | [dev.md](doc/dev.md), [ui.md](doc/ui.md), [vm-dev-tools.md](doc/vm-dev-tools.md), [AGENTS.md](doc/AGENTS.md) |

## Repository layout

Root content (each component directory has its own README):

```
broker/             D-Bus permission broker (the single arbiter of
                    cross-uid actions)
admin_app/          PyQt6 admin approval app (master/detail queue UI)
tui/                Textual approver twin for headless sessions
cli/                qdistro-approvals — CLI queue inspector

sdk/                qdistro_app — the Python SDK first-party apps use
plugins/            send-to plugins for qterminator + qnotebook

polkit/             qdistro polkit AuthenticationAgent
qsu/                sudo replacement (admin-approved, scope-picked)
pwd/                multi-vault password manager + portal Secret backend
print/              host-side print proxy (CUPS lives in a dedicated VM)
phone/              phone integration (Tailscale + ntfy)
recall/             recall-user privilege compartment + activity capture
browser_bridge/     identity-pinned browser native-messaging bridge
snapshots/          btrfs snapshot daemon + Snapper integration
games/              session spawner for full-hardware-access games
user_relay/         per-uid session-bus bridge for cross-user send-to
stubs/              demo apps used to validate the send-to flow

daemons/
  audisp/             SELinux audispd plugin (forwards AVCs to broker)
  cursor-sprites/     cursor-shape-v1 sprite installer
  forward/            per-view RDP proxy (PipeWire → libfreerdp-shadow3)
  nested-pixelfeed/   PipeWire → wl_buffer feeder for nested compositors
  secctx-exec/        wp_security_context_v1 wrapper
  tier1-exec/         SELinux tier-1 exec-context wrapper

selinux/            SELinux policy modules (qdistro_broker, qdistro_pwd,
                    qdistro_tier1)

scripts/
  vm/                 build / clone / launch / bootstrap libvirt test VMs
  install/            in-VM install steps for individual modules
  diag/               diagnostic scripts (event probes, dump utilities)
  noctalia/           Noctalia/qdshell smoke + parity helpers

print-vm/           build scripts + libvirt template for the CUPS VM
tier4-vm/           Tier-4 (Linux-guest VM) image build + spawn
tier5-vm/           Tier-5 (audio-isolated VM) image build + spawn

ci/                 local CI harness — the qci gate runner (see below)

tests/
  unit/               pytest unit tests (headless, no D-Bus, no display)
  integration/
    vm/                 bats integration tests run inside a VM
    permissions-gui/    GUI scenarios for admin app + TUI + cross-user
                        send-to
    qdwin-noctalia/     qdshell-on-qdwin smoke scenarios

deploy/             greetd config, session launchers, dispatcher
                    units installed onto the target machine
doc/                project documentation (read [overview.md](doc/overview.md) first)
pyproject.toml      pytest config
LICENSE             GPL-3.0-or-later (root content; components: see License)

qdwin/ qdshell/ qdlocker/ qdgreeter/ qdbrowser/ qdterm/ qdfileman/
qnotebook/ qdchrome-extension/ qdfirefox-extension/   components (AGENTS.md)
```

## Building and testing

Testing happens at two layers, and the split is strict:

1. **Headless host tests** — pytest unit suites, meson/QML checks, npm test
   runs across the root and every component. No display, no VM.
2. **Full-stack integration** — bats suites and GUI scenarios that run only
   **inside disposable libvirt VMs**. GUI tests are never run on the host:
   they inject real input and would fight your live session.

The day-to-day entry point for both is the local CI runner,
[`ci/bin/qci`](ci/README.md):

```sh
ci/bin/qci preflight    # verify libvirt session, in-tree components, host tools
ci/bin/qci host         # all host-side tests/builds, root + every component
ci/bin/qci bats         # bats integration suites, one disposable VM per file,
                        # run in parallel (QCI_JOBS=N to override)
ci/bin/qci gui          # GUI scenarios in disposable VMs (see below)
ci/bin/qci full         # everything
```

Every run writes a self-contained report (markdown + HTML, with logs,
screenshots, and first-pass fix recommendations) under `ci/runs/`. Failed
disposable VMs are preserved for triage; `ci/bin/qci triage --latest` is the
place to start.

**The GUI gate wants an agent.** The GUI scenarios are markdown playbooks that
need a visual runner — a coding-agent CLI that can look at screenshots and
drive the VM. Point `QCI_AGENT_CMD` at your agent and run the gate in a
foreground terminal (it's the most direct way to watch the product actually
work):

```sh
QCI_AGENT_CMD='your-agent-cli {prompt}' ci/bin/qci gui
```

See [ci/README.md](ci/README.md) for agent-command templating, concurrency
and retry knobs, and the full gate reference.

Lower-level pieces, when you need them directly:

```sh
# Unit tests (headless):
pytest

# A disposable test VM by hand. QDWIN_VM_TEMPLATE is the libvirt domain
# whose XML is cloned for each test VM; spin-test-vm.sh auto-creates it
# on first run.
export QDWIN_VM_TEMPLATE=qdistro-template
scripts/vm/spin-test-vm.sh my-test
```

For host prerequisites (libvirt, qemu-kvm, group membership), see
[doc/dev.md](doc/dev.md#dev-setup).

The unit suite assumes the dependencies installed by
`scripts/vm/install-deps.sh`. The simplest reliable host setup is a
venv with `--system-site-packages` so `dbus-python` resolves from
the distro packages.

## License

Licensing is **per directory**. The root [LICENSE](LICENSE)
(GPL-3.0-or-later) covers qdistro's own root content. Each component keeps
its own license: its `LICENSE` file where it has one (qdwin, qdshell, qdterm,
qdfileman) or the license declared in its package metadata (qdbrowser
GPL-3.0-only, qnotebook GPL-2.0-or-later, qdgreeter and qdlocker MIT; the two
browser extensions declare none yet).

The choice of GPL-3.0-or-later for the root reflects how the codebase weaves
together components under several copyleft licenses (notably
libweston-adjacent code under GPL-3.0+).
