# image/ — kiwi OEM disk image build

Rules for contributors (humans and LLM agents) touching this
subtree. The umbrella conventions live in [../doc/AGENTS.md](../doc/AGENTS.md);
this file adds the build- and verify-pipeline specifics.

## What's here

| File | What it does |
| --- | --- |
| `config.xml` | kiwi description: Tumbleweed OSS + non-OSS repos, OEM raw image type, UEFI grub2, btrfs root with snapper subvolumes, admin (uid 1000) + user (uid 1001) baked in with crypt-sha512 password `qdistro`. |
| `config.sh` | in-chroot post-install script. Branding override, build qdwin + qdistro daemons + qdshell from `/root/qdistro-src/`, run every `qdistro/scripts/install/install-*.sh`, install SELinux policy modules permissive, install qdwin session with chroot-safe shims for `loginctl` and `runuser`. |
| `build.sh` | host-side raw kiwi-ng driver. `./build.sh --sync-only` rsyncs `../{qdistro,qdwin,qdshell}` into `root/root/qdistro-src/`. The build itself runs inside a VM (see below), not on the host. |
| `build-in-vm.sh` | the canonical entry point. Clones `baseweed-baked.qcow2` via `clone-baseweed.sh --from-baked`, attaches a 60 GiB scratch disk, bakes the whole `image/` description into the VM via `virt-customize`, installs `python3-kiwi` inside, runs kiwi-ng, extracts artifacts back to `$QDISTRO_BUILD_DIR` (default `/tmp/qdistro-build`). |
| `iterate-kiwi.sh` | fast-iteration helper. Pushes the local `config.xml`, `config.sh`, `build.sh` into a running builder VM via `vm-script` (base64-piped to dodge qga JSON quoting), wipes `/build/out`, re-runs kiwi inside. Skips the ~5-min clone + zypper-install cycle. |
| `verify.sh` | boots the built `.raw` rootlessly via `qemu:///session`, SSHes in over a `passt` port forward (host 2299 → guest 22), runs **journal-side assertions** (see below), captures `virsh screenshot` evidence. |
| `install-test.sh` | boots the `.install.iso` against a blank 30 GiB target disk, drives the UEFI + GRUB menus via `virsh send-key`, watches kiwi-oem-dump write the image, then verifies the post-install reboot. |
| `root/` | kiwi overlay tree. `etc/os-release.qdistro` is the branding override; `root/qdistro-src/` is rsynced source (gitignored). |
| `keys/` | (gitignored) future GPG signing keypair location for the RPM repo; the key-generation flow still needs to be created. |
| `logs/` | (gitignored) per-run build / verify / install logs and screenshots. |

## How to run the full pipeline

```sh
cd image/
./build-in-vm.sh          # ~10 min: clone + attach + bake + kiwi build
./verify.sh               # ~5  min: rootless boot + SSH assertions + screenshots
./install-test.sh         # ~30 min: install.iso → wizard → reboot from disk
```

`build-in-vm.sh --teardown <vm>` wipes a builder VM. `./verify.sh
--teardown` and `./install-test.sh --teardown` do the same for their
respective VMs.

## Project conventions this respects

- **Single-tenant.** The image bakes one admin uid (1000, name `admin`)
  and one user uid (1001, name `user`). No multi-user login screen.
- **Wayland-only.** greetd autologins admin to a bash shell on tty1;
  admin's user systemd then auto-starts `noctalia-session.service`
  (weston with `qdwin-shell.so`) + `noctalia-shell.service`
  (quickshell loading `/usr/share/quickshell/qdshell/`).
- **Source-on-disk.** `/root/qdistro-src/{qdistro,qdwin,qdshell}/` stays
  on the installed system — the LLM-modifiability principle in
  [../doc/overview.md](../doc/overview.md) requires that users (and
  agents) can edit the Python services in place.
- **dbus-broker, not dbus-daemon.** The `qdistro-dbus-reload.service`
  oneshot lands via `install-broker-for-qdwin.sh`; the broker D-Bus
  policy in `/etc/dbus-1/system.d/` reloads before the broker starts.
- **SELinux permissive.** The build's three policy modules (broker,
  pwd, tier1) load permissive. Reaching enforcing is a separate
  milestone tracked in [../doc/permissions.md](../doc/permissions.md).
- **`admin` (not `jan`).** The actual qdistro install pipeline
  hardcodes `admin` uid 1000 in `bare-metal-install.sh`,
  `install-broker-for-qdwin.sh`, `install-qdwin-session-for-vm.sh`,
  etc. The `jan` reference in [../doc/AGENTS.md](../doc/AGENTS.md) is
  stale; keep this image consistent with the installers it consumes.

## Automated testing for an agent

The pipeline is designed for an agent to drive end-to-end without
sudo on the host. Three commands answer "did this image actually
work?":

1. **Build.** `./build-in-vm.sh` exits 0 iff a `.raw` + `.install.iso`
   land in `$QDISTRO_BUILD_DIR`. Progress milestones print to stdout
   prefixed `[in-vm]`; the kiwi log inside the builder VM is at
   `/root/kiwi-build.log` and pulled to host on completion.
2. **Boot-verify.** `./verify.sh` boots the `.raw` and prints
   `pass: N / 12` + `fail: M / 12`. The 1 historical false-positive
   to know about: `RDSEED32 is broken. Disabling the corresponding
   CPUID bit` is a benign kernel kvm-cpu notice that trips the
   priority-0/1 journal check. Other failures are real.
3. **Install-verify.** `./install-test.sh` exits 0 iff the
   `.install.iso` writes the image to the target disk AND the
   resulting system reaches SSH on port 2300. Output lands in
   `logs/install-<ts>/` with numbered screenshots and an
   `installed-fingerprint.log` capturing hostname / os-release /
   service states / `lsblk` from inside the installed VM.

### Known fragility: install-test.sh GRUB timing

The install ISO's GRUB defaults to "Boot from Hard Disk" with a
1-second countdown — by design, so a stuck install-image-in-DVD
doesn't reinstall on every reboot. `install-test.sh` works around
this by sending `Enter` (to pick UEFI DVD-ROM in the OVMF picker),
then `Down + Enter` (to select "Install qdistro") within that
1-second window. The race is real and worth fixing properly by
overriding the kiwi GRUB default to install via the `<bootloader>`
description. Until then, expect the occasional install-test.sh run
to bounce back to the OVMF picker and need a manual retry.

## What not to do here

- **Don't run `kiwi-ng` directly on the host.** It needs root for
  loopback + chroot + mount. `build-in-vm.sh` exists so the host
  needs none of that.
- **Don't `mv` or rename `/root/qdistro-src/` in the image.** The
  installers under `qdistro/scripts/install/` hardcode that path
  via `fresh-vm-bootstrap.sh`'s conventions.
- **Don't add a Calamares-style installer** without changing the
  image type from `oem` to a live ISO with a separate installer
  payload. The current `oem` + `installiso=true` flow ships the
  whole installed image embedded as a squashfs and dumps it whole
  to the target; that model is incompatible with picking
  filesystem / users / locale at install time.
- **Don't bake an unencrypted password into `config.xml`.** Use
  `openssl passwd -6 <pw>` and the `pwdformat="encrypted"` form.
- **Don't break the per-disk `<boot order='N'/>`** in `install-test.sh`'s
  domain XML. OVMF on libvirt-rootless ignores the `<boot dev=>`
  shorthand and falls back to the manual picker without it.
- **Don't change the `admin` username** without auditing every
  `qdistro/scripts/install/install-*.sh` — they all hardcode it.

## Iteration loop for an agent

When a kiwi build fails (always at least once on a fresh tree),
the right reflex is:

1. SSH into the builder VM (`vm-exec qdistro-builder-<ts> 'tail -f
   /root/kiwi-build.log'`) and find the failing line.
2. Edit `config.xml` or `config.sh` locally on the host.
3. `./iterate-kiwi.sh` — pushes the new files into the builder VM
   and re-runs `kiwi-ng system build` against the (already warm)
   package cache. Typical loop: 60-90 seconds per attempt vs. ~5
   minutes for a from-scratch `build-in-vm.sh`.
4. When the build finally lands a `.raw`, run `./verify.sh` against
   `$QDISTRO_BUILD_DIR` to confirm it boots.

The whole `image/` was bootstrapped in ~8 iterations following
this loop; it's the expected workflow, not an emergency hack.
