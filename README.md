# qdistro

A single-tenant Linux distribution with Qubes-inspired seamless app
isolation, built on libweston + Wayland + Python/Qt/QML and designed
to be easy to modify with LLM assistance.

This repository is the **umbrella** for qdistro: documentation,
permission infrastructure, daemons, SDK, admin app, helper tools,
build scripts, and integration tests.

The current design shorthand is **one owner, many silos, dynamic sessions**.
The owner is the single human and policy authority. Silos isolate data and
program state. Sessions attach silos and resources while work is happening.
Start with [doc/overview.md](doc/overview.md) and
[doc/glossary.md](doc/glossary.md) for those terms.

The compositor lives in a separate repo:
[codeberg.org/qdistro/qdwin](https://codeberg.org/qdistro/qdwin).
The desktop shell (a Noctalia QML fork) lives in
[codeberg.org/qdistro/qdshell](https://codeberg.org/qdistro/qdshell).

## Try qdistro

**Status:** v0.1 — first public test release.

> **Hardware:** spare laptop, ≥16 GB RAM, ≥100 GB free disk, fingerprint reader optional but recommended. Fresh openSUSE Tumbleweed terminal-only install.

**1. Install Tumbleweed** from [get.opensuse.org](https://get.opensuse.org/tumbleweed/) — choose Minimal or Server (no desktop needed). Create the first user as `admin`; qdistro reserves `admin` at uid 1000.

**2. Install git** (not pre-installed on Tumbleweed Minimal/Server):

```sh
sudo zypper install -y git
```

**3. Clone and bootstrap** (as `admin`, which is in the `wheel` group; `sudo`
is available by default for wheel members on Tumbleweed):

```sh
git clone https://codeberg.org/qdistro/qdistro.git
git clone https://codeberg.org/qdistro/qdwin.git
git clone https://codeberg.org/qdistro/qdshell.git
cd qdistro
sudo bash scripts/install/qdistro-bootstrap.sh
```

The bootstrap installs all dependencies, builds the compositor and daemons,
clones the remaining components (qdgreeter, qdlocker, qdbrowser, qterminator,
qnotebook, qfileman), and
configures greetd. Takes 10–20 minutes. Idempotent — re-running is safe.

**4. Reboot:**

```sh
sudo systemctl reboot
```

On next boot, the qdgreeter login screen appears on tty3. Log in as `admin`.

**5. Try the isolation tiers:**

| Tier | How to try |
|------|-----------|
| Tier 1 — native SELinux silo | Open qterminator. Run `id`. Each app launch starts in a silo. |
| Tier 3 — container | Admin app → Silos → New → Container. Launch an app inside it. |
| Tier 4 — VM (waypipe) | Admin app → Silos → New → VM. Open Chrome. |

The admin app (tray icon) shows active silos, pending approvals, and audit history.

**6. Report back:**
File issues at [codeberg.org/qdistro/qdistro/issues](https://codeberg.org/qdistro/qdistro/issues).
Most useful: "I ran step X and Y was unclear / broken." See
[doc/support.md](doc/support.md) for what to include, how security issues are
handled privately, and the [known-regressions](doc/known-regressions.md) ledger.

## Repository layout (sibling checkout required)

The three qdistro repos are designed to live side-by-side under a
common parent directory. Build scripts and tests reference siblings
with relative paths (`../qdwin`, `../qdshell`). Clone them like this:

```sh
mkdir qdistro-org && cd qdistro-org
git clone https://codeberg.org/qdistro/qdistro.git
git clone https://codeberg.org/qdistro/qdwin.git
git clone https://codeberg.org/qdistro/qdshell.git
```

Resulting tree:

```
qdistro-org/
├── qdistro/     ← this repo (umbrella)
├── qdwin/       ← compositor
└── qdshell/     ← desktop shell
```

That three-repo set is the minimum developer layout. The bootstrap and full
desktop image also consume first-party app repos as siblings when present:
`qdgreeter`, `qdlocker`, `qdbrowser`, `qterminator`, `qnotebook`, and
`qfileman`.

Build order: `qdwin` first (the umbrella's daemons compile against qdwin's
protocol XML at `../qdwin/qdwin/*.xml`), then `qdistro`, then `qdshell`. See
[doc/dev.md](doc/dev.md) for the full developer setup.

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
| Apps | [app-sdk.md](doc/app-sdk.md), [browser.md](doc/browser.md), [printing.md](doc/printing.md), [password-manager.md](doc/password-manager.md) |
| Special workloads | [games.md](doc/games.md), [recall.md](doc/recall.md), [phone.md](doc/phone.md), [cross-machine.md](doc/cross-machine.md) |
| For contributors | [dev.md](doc/dev.md), [ui.md](doc/ui.md), [vm-dev-tools.md](doc/vm-dev-tools.md), [AGENTS.md](doc/AGENTS.md) |

## Repository layout

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
LICENSE             GPL-3.0-or-later
```

## Building and testing

```sh
# Unit tests (headless, <1s):
pytest

# Integration tests require a libvirt session + a built test VM.
# The fixed qdistro test VM password is baked into the cloned VM's
# user accounts.
# QDWIN_VM_TEMPLATE is the libvirt domain whose XML is cloned for
# each test VM; spin-test-vm.sh auto-creates it on first run.
export QDWIN_VM_TEMPLATE=qdistro-template
scripts/vm/spin-test-vm.sh my-test

# The `bats` gate spins one disposable VM per test file and runs them in
# parallel, auto-sizing concurrency to host RAM/cores (set QCI_JOBS=N to
# override). Single file: `ci/bin/qci bats --file <path>`.
ci/bin/qci bats
```

For host prerequisites (libvirt, qemu-kvm, group membership), see
[doc/dev.md](doc/dev.md#multi-repo-dev-setup).

The unit suite assumes the dependencies installed by
`scripts/vm/install-deps.sh`. The simplest reliable host setup is a
venv with `--system-site-packages` so `dbus-python` resolves from
the distro packages.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

The choice of GPL-3.0-or-later reflects how the codebase weaves
together components under several copyleft licenses (notably
libweston-adjacent code under GPL-3.0+); aligning the whole project
on GPL-3.0-or-later avoids per-file license accounting.
