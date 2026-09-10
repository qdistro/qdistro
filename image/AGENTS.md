# image/ — the qdistro disk image (kiwi OEM raw)

Rules for contributors (humans and LLM agents) touching this
subtree. The umbrella conventions live in [../doc/AGENTS.md](../doc/AGENTS.md);
this file adds the build- and verify-pipeline specifics. The direction and
the decisions behind them are in `todo/iso/13` and `todo/iso/14` (tracker
repo); this file states what is true of the tree today.

## What ships

**One artifact per tester release:** `qdistro-<version>-<snapshot>.raw.xz`
plus its `.sha256`, where `<version>` is `config.xml`'s `<version>` and
`<snapshot>` is the Tumbleweed snapshot the build was pinned to (below).
Call it the "qdistro disk image" or "raw image" in user-facing text; not
"qcow2", "OEM" (kiwi's type name, stays in `config.xml`) or "live USB" (it
is a full, persistent install).

- Written to a USB stick (`xzcat … | dd of=/dev/sdX bs=4M conv=fsync`) it
  boots on real hardware as a qdistro install that lives on the stick.
  Booted under qemu/libvirt it is the VM disk; `verify.sh` puts a qcow2
  overlay on it.
- **Sizes.** The raw is **28 GiB** (`<size unit="M">28672</size>` =
  30,064,771,072 bytes): fits nominal "32 GB" media (~31.0 GB usable) with
  ~0.9 GB spare, and a VM booting the raw directly gets that filesystem
  without depending on first-boot repart. Minimum stick: **32 GB**. There is
  no `oem-systemsize`; `dracut-kiwi-oem-repart` grows the root onto a bigger
  stick on first boot. `oem-resize` is left at kiwi's default, which runs on
  **every** boot (the opt-out is the counter-intuitive
  `<oem-resize-once>false</oem-resize-once>`): harmless when there is nothing
  to grow, and a stick copied to a larger one grows again. 2 GiB swap is
  created by kiwi at build time. The xz collapses the free space, so the
  download is a few GB.
- **UEFI-only.** `firmware="uefi"`. Legacy BIOS/CSM is out of scope and is
  stated as such on the download page. `target_removable="true"`
  so grub2-install `--removable` writes `EFI/BOOT/bootx64.efi` (the firmware
  fallback path) and does not create a machine NVRAM boot entry that would
  not travel with the stick.
- **No install ISO.** `installiso="false"`: a tester who boots an install
  ISO from a stick is offered a wipe of their internal disk. The installable
  ISO is post-v1 and gets a kiwi profile when it returns; `installboot` and
  `install-test.sh` stay for it and are inert until then: the CI image gate
  emits no install-test row when `installiso="false"` (not a skip: a skip
  in `QCI_RELEASE_FATAL_GATES` would fail the release battery). The
  `--idempotency` flag has no effect on the image gate until the ISO
  returns. Not a missing stage.
- **Profile: dev.** The tester image is built with `QDISTRO_PROFILE=dev`
  (baked passwordless `admin` sudoers; `config.sh` prints
  `WARN: dev profile …` in the build log). `QDISTRO_PROFILE` accepts only
  `dev` or `release`; `config.sh` validates it once, up front. root/admin/user share the
  crypt-sha512 password `qdistro`; this is decided (`todo/iso/13`), the
  download page states it. **sshd is installed but never enabled** — that is
  what keeps the shared credential local; `verify-contents.sh` fails an image
  with an sshd wants-link. The hardened install stays the bootstrap's track.

## What the chain installs (the product statement `todo/iso/03` asked for)

**One chain.** `config.sh` carries no installer list. It exports
`QDISTRO_REPO_ROOT`, `QDISTRO_PROFILE`, `QDISTRO_STRICT=1`,
`QDISTRO_STATE_DIR` and `QDISTRO_OFFLINE_INSTALL=1`, sources
`scripts/install/qdistro-bootstrap.sh` and runs its `install_python_modules`,
so the image installs exactly what `qdistro-bootstrap.sh` installs on a
machine, by construction. The list lives once, in `installer_chain_entries`
(`qdistro-bootstrap.sh --list-steps` prints it); the image records every
succeeded step in `/var/lib/qdistro/bootstrap/installer-chain.state`, and
the bootstrap's end-of-run completeness check dies (strict) if the record
is short. `verify-contents.sh` diffs that record against the chain for the
image's profile and `verify.sh` repeats the diff on the booted image.

| step | what lands | tester image (dev) | release profile |
|---|---|---|---|
| `sdk` | `qdistro_app` in the system python | yes | yes |
| `broker`, `session-manager`, `user-relay`, `polkit`, `pwd`, `qsu`, `browser-bridge`, `portal-backend`, `print`, `snapshots` | the permission arbiter, silo launcher, relay, credential vault, root-exec helper, browser bridge, portals, print proxy, backups | yes | yes |
| `phone` | phone companion daemon (cut from v1, decision D4) | yes, dev-only guard | **no** (skipped, not a gap) |
| `tier3` | `qdistro-tier3` group, locked silo users `user1`/`user2`, `/usr/local/bin/qdistro-tier3-spawn`, tmpfiles entry, polkit action | yes | yes |
| `tier4-host`, `tier5`, `tier5b` | host launch code only: `tier4_control.py` and siblings, the tier-5/5b spawn wrappers, domain templates and polkit action | yes | yes |

**Tiers.** The stick supports **tiers 0–3** (`doc/isolation-tiers.md`,
decision D3). Tiers 4 and 5 are present but **experimental**: the chain
installs the host side only. The guest base image is not in the image;
build it on the booted system with `qdistro-bootstrap.sh --tier4-base`
(or `qdistro-tier5-build-guest-image`), which needs KVM on the hardware.

**Not installed, and asserted absent** by `verify-contents.sh`: `recall`
(cut from v1, decision D2), `media` and `multimachine` (audit
recommendation DEMOTE, never promoted into the chain), the admin
approval-queue TUI (neither chain has ever installed it). Adding any of
them to the image means adding it to the bootstrap chain, where the
decision is recorded.

**Guest agent.** `/etc/sysconfig/qemu-ga` clears the vendor
`--block-rpcs=guest-exec,guest-exec-status` so `verify.sh` can start sshd
over the hypervisor-only virtio-serial channel; on hardware the agent's
device never appears and it does not run. The same channel is
`verify.sh`'s root channel (`qga_root`): an assertion that needs root
(anything under `/root`, `passwd -S`, sourcing the on-image bootstrap)
reads through it on every profile, not through `sudo -n` over SSH, which
the release profile deliberately breaks. `verify-contents.sh` pins the
unit's `EnvironmentFile`/`ExecStart` lines and that the vendor default
blocks exactly those two RPCs.

**Pip apps ship their QML.** qdgreeter and qdlocker are pip-installed into
`/usr`; each must carry `qml/Main.qml` INSIDE its package (package-data),
or it installs fine and dies at first launch. Run 28 booted to a
crash-looping greeter that way; `config.sh` now fails the build on it and
the checklist has a row per app.

## Tumbleweed snapshot pin and provenance

`config.xml` pins both repositories to
`https://download.opensuse.org/history/<YYYYMMDD>/tumbleweed/repo/{oss,non-oss}/`.
The id is written **only** in that block at the top of `config.xml` (kiwi's
XML parser rejects entities, so it cannot be spelled once); `build.sh
--snapshot-id` reads it and refuses to build if the two paths disagree or a
path is unpinned. `history/` is a rolling window of about four weeks, so a
rebuild is exact inside the window and a bug report can always name its
snapshot after it. Bump the id deliberately per tester release.

What went into an image is in **`/etc/qdistro/release`** on the image:
`VERSION`, `SNAPSHOT`, `PROFILE`, `BUILD_DATE`, `ARTIFACT` and one
`SOURCE <repo> <commit> <clean|DIRTY diff-sha256=… untracked=N>` line per
synced repo (qdistro, qdwin, qdshell, qdgreeter, qdlocker). `build.sh`
strips `.git` during the sync, so `sync_sources` writes that manifest on the
host (`root/root/qdistro-source-manifest`, gitignored) and `config.sh`
installs it via `image/lib/release-stamp.sh` — fatally: an image that cannot
say what went in is not built. The checklist checks the file's content, not
its presence.

## What's here

| File | What it does |
| --- | --- |
| `config.xml` | kiwi description: pinned Tumbleweed OSS + non-OSS repos (top of file), OEM raw type (`firmware="uefi"` UEFI-only, `target_removable="true"`, `installiso="false"`, `bundle_format="%N-%v-%I"`, 28 GiB), grub2, btrfs root with subvolumes, admin (uid 1000) + user (uid 1001) baked in. Kiwi XML profiles: `tester` (default, import=true, the published stick) and `ci` (additive: bats/ydotool extras; config.sh masks greetd). Orthogonal to `QDISTRO_PROFILE` (dev/release). |
| `config.sh` | in-chroot post-install script. Branding override, `/etc/qdistro/release`, build qdwin + qdistro daemons + qdshell from `/root/qdistro-src/`, run **the bootstrap's** installer chain (sources `scripts/install/qdistro-bootstrap.sh`, strict, state on the image; every step honours the offline-install contract in `scripts/install/lib/qdistro-offline.sh`), SELinux policy modules and explicit global mode (dev permissive, release enforcing), qdwin session with `QDWIN_SESSION_AUTOSTART=0`, greetd (enabled on tester; **masked** on kiwi profile `ci` so admin's user manager starts the compositor), compositor-VT hardening (`--offline`), qemu-ga RPC filter cleared. A missing or failing installer, or a short chain record, aborts the build. |
| `build.sh` | in-VM kiwi driver (also the host-side sync). `--sync-only` rsyncs the five sibling repos into `root/root/qdistro-src/` and writes the source manifest; `--snapshot-id` prints the pin; the build runs `kiwi-ng system build` then `kiwi-ng result bundle --id <snapshot>` (xz `--threads=0` of the raw + `.sha256`) into `$BUILD_DIR/bundle/`. |
| `build-in-vm.sh` | **the canonical entry point.** Clones `baseweed-baked.qcow2` (`--reuse` keeps an existing builder; always `--from-baked`, never the kiwi tester image — that would be circular), attaches a 120 GiB scratch disk, bakes `image/` into the VM, runs `build.sh` under a liveness-guarded retry loop (`lib/build-guard.sh`), copies the raw and `bundle/` back to `$QDISTRO_BUILD_DIR`, then proves the release artifact on the host: name, `sha256sum -c`, `xz -t`, decompressed size == `<size>` (`logs/in-vm-*/release-artifact.txt`). Forwards `QDISTRO_KIWI_PROFILE` (tester\|ci). |
| `lib/build-guard.sh` | liveness (log mtime / CPU ticks / D-state / uplink bytes), kill-tree and mount/loop cleanup used by the retry loop. |
| `lib/release-stamp.sh` | `qdistro_write_release`: manifest + os-release → `/etc/qdistro/release`, refusing anything but the five expected repos with 40-hex commits, or a version mismatch. |
| `lib/release-proof.sh` | `qdistro_prove_release`: the host-side proof of the copied-out artifact (raw size, checksum file naming and matching, `xz -l` size, `xz -t`). |
| `iterate-kiwi.sh` | pushes local `config.xml`/`config.sh`/`build.sh` into a running builder VM and re-runs kiwi (skips the clone). |
| `extract-root.sh` | guestfish copy-out of the checklist's paths from a `.raw` into `$QDISTRO_BUILD_DIR/extracted` (no boot, no FUSE). |
| `verify-contents.sh` | static checklist over an extracted tree, resolved with the *image's* path semantics (symlinks never followed into the host). |
| `lib/profile-proof.sh` | reads `/etc/qdistro/release` back OUT of the finished raw and fails the build when the baked `PROFILE` is not the `QDISTRO_PROFILE` that was requested. `build-in-vm.sh` runs it beside the release proof. The release proof checks the artifact's name, checksum, integrity and size — everything except *which product it is*; profile is not in the filename, so this is the only place a mis-profiled image can be caught. |
| `lib/select-artifact.sh` | resolve the published artifact (explicit path, 64-hex digest, or unique `bundle/*.raw.xz`); `sha256sum -c` + `xz -t` + decompress to `$BUILD_DIR/published/from-xz-<digest>.raw`; reuse requires a full byte comparison against fresh decompression, with unique temporary files and atomic publication. Sourced by `verify.sh` and the image gate. Never `find \| head -1`. |
| `verify.sh` | boots the resolved disk rootlessly (`qemu:///session`, 64 GiB qcow2 overlay so first-boot repart grows the 28 GiB raw), SSH over a `passt` forward as `admin` plus a root channel through the guest agent (`qga_root`), journal-side assertions, screenshots. Default also: UUID identity, EFI/BOOT, persist marker + btrfs snapshot across a reboot, greeter login (locker session-up). Snapper is packaged but has no root config. `--stick` adds USB / second-disk / hub / Secure Boot / nested-KVM / first-boot power-off / `xzcat \| dd`. Host needs `sshpass` and `jq`. `QDISTRO_IMAGE` is a path or the xz digest. |
| `hardware-run.md` | template for the maintainer's real-stick run (Secure Boot, WPA2/WPA3, silos). Fill in and copy the filled note to `logs/`. |
| `install-test.sh` | drives the *install ISO* (post-v1); inert while `installiso="false"`. |
| `root/` | kiwi overlay tree. `etc/os-release.qdistro` is the branding override (its `VERSION_ID` must equal `config.xml` `<version>`; the build checks). `root/qdistro-src/` and `root/qdistro-source-manifest` are generated (gitignored). |
| `logs/` | (gitignored) per-run build / verify logs and screenshots. |

## How to run the full pipeline

```sh
cd image/
QDISTRO_PROFILE=dev ./build-in-vm.sh   # ~30-40 min cold: clone + bake + kiwi (17-26) + xz bundle (~6) + copy-out + host proof
./verify.sh                            # ~10-15 min: 64 GiB overlay + assertions + greeter login + persist reboot
# ./verify.sh --stick                  # + USB / SB / nested / power-off / dd (~45 min); image gate uses this
```

Or through CI: `qci` image gate = resolve `bundle/*.raw.xz` (digest +
`xz -t` + decompress) → `extract-root.sh` → `verify-contents.sh` →
`verify.sh --stick` on the **same** decompressed raw (install-test is
inert without an ISO: no row, not a skip). The full run also compares the
image's five clean source commits with its captured release manifest, and
checks version/snapshot against config.xml and profile against
`QDISTRO_PROFILE` (default release; pass `dev` explicitly for a pinned tester).
Expected/observed identities and the artifact digest are recorded in the run.
Part of `qci full`; a
blocked/skip image row is fatal under `QCI_RELEASE=1` (todo/iso/14 F).

**Do not run `verify.sh` while a builder VM is up** on the same
`qemu:///session` daemon: tearing down the verify VM restarts session
`virtqemud` and crashes the builder (run 30).

**CI base (iso/14 Phase G intermediate).** `scripts/vm/import-kiwi-base.sh`
converts a kiwi `.raw`/`.raw.xz` (tester or `ci` profile) to
`qdistro-kiwi-base.qcow2`. When that stamped qcow2 exists,
`QDISTRO_VM_BASE=auto` (default) clones the golden *and* its workers
from it under OVMF (`--from-kiwi` for the golden build;
`--from-run-golden` still injects OVMF if the backing chain is the kiwi
base — the BIOS template cannot boot a UEFI-only disk).
`fresh-vm-bootstrap.sh` still overlays current source.
`build-in-vm.sh` always clones `baseweed-baked` (using the kiwi image
as the builder backing is circular). A `ci` kiwi profile
(`QDISTRO_KIWI_PROFILE=ci`) bakes bats/ydotool extras and masks greetd;
import that image as the CI base so bootstrap skips the extras zypper.
A tester-as-base still zypper-installs extras (needs guest egress);
`QCI_OFFLINE=1` then fails closed before the DNS wait.

- `QDISTRO_BUILD_DIR` defaults to `/var/tmp/qdistro-build`. **Never `/tmp`**:
  it is a tmpfs on the build hosts and the 28 GiB raw does not fit in RAM.
  Outputs: `qdistro.x86_64-<version>.raw` (what verify/extract use) and
  `bundle/qdistro-<version>-<snapshot>.raw.xz{,.sha256}` (what ships).
- `QDISTRO_PROFILE` is a shell variable read by `config.sh`, not a kiwi
  profile. The default is `release` (no passwordless sudo) so an unqualified
  build never produces the dev image by accident; the tester image passes
  `dev` explicitly. **The default's own failure mode is the quiet one**: a
  tester build that forgets `QDISTRO_PROFILE=dev` gets a release-stamped
  artifact and nothing says so — that is how the 0.1.0-20260902 image shipped
  `PROFILE=release` against this file's own recorded `dev` direction, for the
  whole life of the image. Three things now make that loud rather than silent:
  `config.sh` and `build-in-vm.sh` each print a banner naming the profile *and
  what it decides* (the sudoers rule and the `SELINUX=` line), `build-in-vm.sh`
  says whether the value was **explicitly requested or defaulted**, and
  `lib/profile-proof.sh` asserts the baked stamp against the request and fails
  the build on a mismatch. Since the profile also selects the SELinux runtime
  mode (`dev` permissive / `release` enforcing), it is security-relevant on
  both sides.
- `build-in-vm.sh --teardown <vm>` wipes a builder VM; `./verify.sh
  --teardown` its verify VM.

## Project conventions this respects

- **Single-tenant.** One admin uid (1000, `admin`), one user uid (1001,
  `user`). No multi-user login screen.
- **Wayland-only.** greetd runs `qdgreeter` on tty3; after PAM auth the
  launcher starts admin's `qdwin-session.target` (weston with
  `qdwin-shell.so` + qdshell). The target is deliberately *not* wanted by
  `default.target` (it would race the greeter for `wayland-1`).
- **Source-on-disk.** `/root/qdistro-src/{qdistro,qdwin,qdshell,…}` stays on
  the installed system — the LLM-modifiability principle in
  [../doc/overview.md](../doc/overview.md).
- **dbus-broker, not dbus-daemon.** `qdistro-dbus-reload.service` lands via
  `install-broker-for-qdwin.sh`.
- **SELinux mode follows the image profile.** Kiwi explicitly writes global
  permissive for dev and enforcing for release, because sourcing the bootstrap
  installer chain does not run its separate SELinux setup. Static inspection
  checks the config and boot verification requires the matching runtime mode.
  The session-manager policy still declares its domain permissive; global
  enforcing does not remove that separate policy rollout limitation.
- **`admin` (not `jan`).** The installers hardcode `admin` uid 1000; keep
  this image consistent with them.

## Automated testing for an agent

The pipeline is designed for an agent to drive end-to-end without sudo on
the host. Green means:

1. **Build.** `build-in-vm.sh` exits 0 iff kiwi and the bundle step
   succeeded, the raw and `bundle/*.raw.xz` copied out with the sizes seen
   inside the VM, and `release-artifact.txt` reads `RESULT: PASS`. The
   bootstrap chain runs strict and checks its own completeness, so a green
   build is a complete image; the full kiwi log is pulled to
   `logs/in-vm-*/kiwi-build.full.log`.
2. **Static checklist.** `extract-root.sh && verify-contents.sh
   $QDISTRO_BUILD_DIR/extracted` prints one OK/MISS line per row and
   `RESULT: PASS|FAIL`. Rows include every chain installer's artefacts,
   the wants-links, the SELinux module store, `/etc/qdistro/release`
   content, sshd-not-enabled, the sudoers and phone rows matching the
   profile, the absence of media/multimachine/recall, and the chain record
   diffed against `installer_chain_names`.
3. **Boot-verify.** `verify.sh` boots the raw and prints `pass: N / M`.
   The first image to reach that summary was run 28 (Phase D); before it,
   every run died at the sshd-start baseline on the vendor RPC filter.
   Known benign: `RDSEED32 is broken. Disabling the corresponding CPUID
   bit` trips the priority-0/1 journal check under kvm.

Hermetic host tests: `tests/integration/vm/image-release.bats` (config
pins, manifest, release stamp, checklist rows), `build-guard.bats`,
`offline-install.bats` (the chain contract, config.sh's sourcing form),
`bootstrap-installer-resume.bats` (the chain and its completeness check).

## What not to do here

- **Don't run `kiwi-ng` directly on the host.** It needs root for loopback
  + chroot + mount. `build-in-vm.sh` exists so the host needs none of that.
- **Don't `mv` or rename `/root/qdistro-src/` in the image.** The installers
  hardcode that path.
- **Don't turn `installiso` back on for the tester build**, and don't add a
  Calamares-style installer: the `oem` model dumps the whole installed image
  and cannot pick filesystem / users / locale at install time.
- **Don't bake an unencrypted password into `config.xml`.** Use
  `openssl passwd -6 <pw>` and the `pwdformat="encrypted"` form.
- **Don't enable sshd** in `config.sh` or a unit drop-in. Host keys are
  generated so the VM harness can start it on demand over qemu-guest-agent.
- **Don't unpin or half-bump the repositories.** Both paths carry the same
  `history/<id>/`; `build.sh --snapshot-id` is the check.
- **Don't change the `admin` username** without auditing every
  `qdistro/scripts/install/install-*.sh`.
- **Don't add an installer list to `config.sh`.** The chain is the
  bootstrap's `installer_chain_entries`; a step the image needs goes there
  (with its decision), and `offline-install.bats` fails a `config.sh` that
  invokes a chain installer itself.
- **Don't add a chain installer that needs a live system manager.** It must
  source `scripts/install/lib/qdistro-offline.sh` and skip live-only work
  under `is_offline`; the chain is strict in the image.

## Iteration loop for an agent

When a kiwi build fails:

1. Read `logs/in-vm-*/kiwi-build.full.log` (or `vm-exec <builder> 'tail -f
   /root/kiwi-build.log'` while it runs) and find the `FATAL` line.
2. Edit `config.xml` or `config.sh` locally on the host.
3. `./iterate-kiwi.sh` re-syncs, pushes `config.xml`/`build.sh`/`config.sh`,
   the source manifest and `lib/release-stamp.sh` (into the synced tree,
   where `config.sh` reads it) into the builder VM and re-runs kiwi against
   the warm package cache; or `QDISTRO_BUILDER_VM=<vm> ./build-in-vm.sh
   --reuse` for the full path with the copy-out and the artifact proof.
   Other files under `image/lib/` or in the five repos need the full path.
4. When the build lands, run the static checklist, then `verify.sh`.
