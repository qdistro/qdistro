# Podman-to-cloud-VM experiment (2026-09-24)

This is a continuation of the monorepo migration work, on the
`experiment/github-qdistro-image` branch. It does not change the KIWI release
path. The final workflow is `.github/workflows/qdistro-test-vm.yml`. Earlier
Podman and in-guest-build experiment workflows were removed after validation.

## 2026-10-02: snapshot 20260930, QML shell, Python services, greeter

[Run 36991468544](https://github.com/qdistro/qdistro/actions/runs/36991468544)
succeeded in 8m41s on the free 4-vCPU runner, with KVM (the runner exposes
`/dev/kvm` once a udev rule opens it; the earlier runs used TCG).
[QCOW2 artifact](https://github.com/qdistro/qdistro/actions/runs/36991468544/artifacts/11220680256),
956 MiB compressed, 8 GiB virtual, retained until 2026-11-01.

What changed:

- Pinned to `snapshot.conf` 20260930 (cloud image, Podman builder base and
  guest repos all from that one pin; on this branch only, `main` still pins
  20260929).
- Native build: the canonical `scripts/vm/build-native-podman.sh` instead of
  the inline build. Its stage now also carries prebuilt `qsu` and the SELinux
  `.pp` modules, so the guest stays free of compilers, `make` and headers.
- `scripts/vm/test-vm-guest-install.sh` installs `QDISTRO_RUNTIME_PKGS` (62
  requested, 439 with dependencies, 239.5 MiB download / 833.8 MiB installed,
  zero `-devel` packages), pip-installs the presentation SDK, qdgreeter and
  qdlocker, then runs the bootstrap's own `main()` with the installer chain
  filtered. 11 steps ran: presentation, sdk, broker, admin-app,
  session-manager, user-relay, polkit, pwd, qsu, portal-backend, tier3.
  Skipped as optional or needing nested VMs/btrfs: browser-bridge, phone,
  print, snapshots, tier4-host, tier5, tier5b.
- No optional apps: no browser, and qterminator, qfileman and qnotebook are
  not installed (as in the kiwi tester image).

Verified in the run:

- All 45 staged dynamic ELFs resolve; the four core RPM versions match the
  build container.
- Headless Weston loads `qdwin-shell.so`; `qdwin-probe` connects.
- QML shell: `qs -p /usr/share/quickshell/qdshell` under Weston's GL renderer
  (llvmpipe) logs `Configuration Loaded` and runs 30 s without QML type or
  import errors. The remaining errors are environmental (no network, PipeWire,
  BlueZ or UPower).
- After a full poweroff and second boot: greetd, the admin broker, session
  manager, pwd and the root-exec socket are active, no unit failed, and
  `org.qdistro.AdminBroker1`, `org.qdistro.Pwd1` and
  `org.qdistro.SessionManager1` are on the system bus.
- Runner disk delta 5.5 GB (budget 30 GiB).

Time: 2m11s native Podman build, 40s QCOW2 preparation, 4m04s guest boot and
install plus both checks, 1m00s artifact upload. The cloud image came from the
Actions cache.

Accounts: `admin` (uid 1000) and `user`, both with the testing password
`qdistro`; the last boot removes cloud-init state and `authorized_keys` and
disables SSH password login. SELinux is permissive (dev profile) with the
prebuilt modules loaded.

Findings for `main`:

- `qdistro-bootstrap.sh` on a fresh Tumbleweed never creates the `seat` group
  that `install-qdwin-session-for-vm.sh` adds admin to; only
  `fresh-vm-bootstrap.sh` and `image/config.sh` do, and the seatd RPM does not.
  The test VM creates it before calling `main()`.
- Sourcing the bootstrap resets globals such as `ADMIN_PASSWORD`; a caller
  must not keep its own values under those names.
- Without `selinux-policy-devel` and `make`, the bootstrap's policy installs
  only warn under dev; a runtime-only install needs the prebuilt modules.

### Consumer validation (run 36994730839)

[Run 36994730839](https://github.com/qdistro/qdistro/actions/runs/36994730839)
adds `scripts/vm/test-vm-consumer-check.sh` and passed in 14m40s;
[QCOW2 artifact](https://github.com/qdistro/qdistro/actions/runs/36994730839/artifacts/11221194788),
955 MiB, retained until 2026-11-01. After the image is final, the check boots
it through a throwaway overlay with fresh UEFI variables and a key-only
cloud-init seed (65 s), then requires:

- the consumer key logs in, the build key does not, SSH password login is
  off, passwordless sudo works and the QEMU guest agent answers;
- admin's password is usable and machine-id and SSH host keys are new;
- `systemctl is-system-running` is `running` with no failed unit, the
  qdistro services and bus names are up and the chain state has 11 steps;
- the repositories are only `history/20260930` and resolve `bats` and `jq`;
- qdgreeter is on the virtual display (virtio-vga), and typing the password
  with QMP `send-key` starts qdwin-session.target, the compositor, qdshell and
  qdlocker; `consumer-greeter.png` and `consumer-desktop.png` (qdshell bar at
  1920x1080) are in the logs artifact;
- the artifact's sha256 is unchanged afterwards.

It found one image bug: a seed listing admin under `users:` locked admin's
password (cloud-init's `lock_passwd` default), so the greeter refused the
documented password. The image now sets cloud-init's default user to admin
with `lock_passwd: false` and ships an empty `/etc/machine-id`. This run also
exercised main's `seat` group fix (`8b0c51fc5`); the guest script no longer
creates the group itself.

Using the image: give cloud-init a seed whose user-data is only

```yaml
#cloud-config
ssh_authorized_keys:
  - ssh-ed25519 AAAA... you@host
```

then `ssh admin@<vm>`; at the console or greeter, admin and user log in with
`qdistro`. Attach a `virtio-vga` display to see the greeter, and a
`org.qemu.guest_agent.0` virtio-serial port for guest-agent exec.

Known limit: the cloud image URL is the rolling one, verified against the
pinned checksum. Once Tumbleweed publishes a newer image the download fails
until the pin is bumped; the Actions cache only covers a hit.

## Earlier results (2026-09-24)

### Verified result

- [Final manually triggered Docker run 36013692783](https://github.com/qdistro/qdistro/actions/runs/36013692783): successful in 15m49s; [downloadable test VM ZIP](https://github.com/qdistro/qdistro/actions/runs/36013692783/artifacts/10814472674), 677 MB, retained until 2026-10-24. The guest root had 1.4 GiB used / 6.1 GiB free; runner final disk-use delta was 2.68 GB. All 44 staged dynamic ELFs passed dependency checks, and `qdwin-probe` received `hello uid=1000` from the launched shell. No build tools were installed in the guest. This run also verified `workflow_dispatch` on the experiment branch.
- [Runtime-only run 35993860510](https://github.com/qdistro/qdistro/actions/runs/35993860510): successful; 15m08s job; [QCOW2 artifact](https://github.com/qdistro/qdistro/actions/runs/35993860510/artifacts/10805489190), 676 MB compressed.
- [Developer-image run 35994485920](https://github.com/qdistro/qdistro/actions/runs/35994485920): successful; 20m12s job; [QCOW2 artifact](https://github.com/qdistro/qdistro/actions/runs/35994485920/artifacts/10806601494), 1.11 GB compressed. It retains the native compiler, Meson, Ninja, Git, pkg-config and development headers, per user preference. The 8 GiB virtual disk's 7.5 GiB root had 2.3 GiB used and 5.3 GiB free; the runner's final disk-use delta was 3.78 GB, below the 15 GiB target.
- The earlier comparison builds compiled qdwin, daemons, qdshell's native plugin, and the patched production libweston 16 in an openSUSE Tumbleweed Podman pod; the final workflow uses Docker. The staged tree was about 12 MB. The signed Minimal-VM QCOW2 was verified, resized, and populated offline with libguestfs. No privileged container or KVM was required; the runner did use sudo to install host image tools.
- In the guest, all 44 staged dynamic ELF files passed dependency checks with the vendored library path. Weston loaded `qdwin-shell.so` on a headless backend, and `qdwin-probe` received `hello uid=1000`. Offline dependency checks and `qemu-img check` passed. Boot/probe used QEMU TCG, not KVM.

## Time and space profile

The developer-image job spent 1m12s on native Podman compilation and dependency installation, 2m45s preparing/copying the QCOW2, 13m32s on TCG guest boot/package installation/launch, 53s on offline checks, and 1m09s uploading the artifact. The developer guest zypper transaction downloaded 318.3 MiB and estimated 1.23 GiB of installed packages. The container build dependencies downloaded 353.1 MiB and estimated 1.35 GiB installed in that run. These are transaction figures, not additive final filesystem usage.

The first smoke run found a missing runtime `libpango-1.0.so.0`; the workflow now installs `libpango-1_0-0` and checks staged ELF dependencies. This shows that the staging/launch tests have caught a real packaging gap.

The successful runtime-only and developer images are a useful A/B comparison.
Omitting the build dependencies from the guest changed its zypper transaction
from 318.3 MiB downloaded / 1.23 GiB installed to 131.0 MiB downloaded /
462.8 MiB installed. The guest root fell from 2.3 GiB to 1.4 GiB used, and
the GitHub artifact fell from 1.11 GB to 676 MB. Total job time fell from
20m12s to 15m08s. The TCG guest step alone fell from 13m32s to 9m25s; native
Podman compilation was essentially unchanged (1m12s versus 1m09s). Thus
removing build tools from a *test* image saves about 0.9 GiB in the guest and
five minutes per build, while still allowing CI to rebuild the binaries.

## Scope and next steps

Final decision: publish **one runtime-only test VM** as a GitHub Actions ZIP
artifact containing an 8 GiB virtual, sparse QCOW2 and its SHA-256 checksum.
Build qdwin, daemons, qdshell's native plugin, and patched libweston inside an
openSUSE Tumbleweed Docker container. Do not install the compiler, Meson,
Ninja, or development headers in the guest. GitHub's `upload-artifact@v4`
provides native ZIP/zlib compression at level 6; no extra gzip/XZ layer is
used. The 8 GiB disk leaves about 6 GiB free in the successful runtime image
and does not imply an 8 GiB download. The artifact is retained for 30 days.

(2026-09-24; superseded by the 2026-10-02 section above, which adds the QML shell, services and greeter.) The artifact was a native-components *test VM*, not a complete qdistro desktop or a KIWI replacement. `qdshell/meson.build` installs only its native QML plugin, not the QML shell/session wiring. The VM has no baked builder SSH key; SSH remains enabled so cloud-init can provision the user's key on first boot. Artifact retention is 30 days.

RPM distribution was considered and rejected for this test-image workflow.
Keep more extensive VM testing on the user's own hardware as requested, with
the GitHub TCG boot/probe serving as a portable smoke test.

## Astra review of a possible single RPM

One versioned RPM for qdistro-owned runtime files is a reasonable *future*
ownership and upgrade boundary, but it should not replace the ready-to-use
QCOW2 artifact. The current 12 MB staged tree is only native qdwin, daemons,
patched libweston and qdshell's native plugin; `qdshell/meson.build` explicitly
leaves the QML shell tree to another installer. Python apps, services, greeter
and policy setup are likewise not captured. An RPM of this tree should be
called `qdistro-native`, not `qdistro`, until the complete runtime manifest is
defined against the bootstrap installer chain.

If an RPM is introduced, it should own first-party immutable files and declare
openSUSE runtime dependencies, not bundle openSUSE RPMs or invoke zypper/pip
inside `%post`. Keep machine provisioning (users, storage, SSH, profile and
service enablement) in an explicit image assembler/bootstrap. Audit generated
RPM requirements/provides around the *private* vendored libweston tree so its
SONAME cannot falsely satisfy unrelated system packages. An XFS Minimal-VM
image would still lack the btrfs/subvolume/snapshot properties of the full
release path. RPM packaging improves upgrade/removal hygiene, but does not
remove the measured VM boot and dependency-install time; making testers assemble
the image locally would also shift network and libguestfs work to their machines.

Before calling this reproducible release packaging: pin or consistently snapshot the container, cloud image and RPM repositories; record their digests; inspect peak (not just final) runner disk use; and verify upgrades/rebuilds against the pinned Weston ABI. The current workflow compares four key container/guest RPM versions and performs full staged-ELF closure checks, but it does not prove snapshot identity for the entire dependency graph.
