# Podman-to-cloud-VM experiment (2026-09-24)

This is a continuation of the monorepo migration work, on the
`experiment/github-qdistro-image` branch. It does not change the KIWI release
path. The final workflow is `.github/workflows/qdistro-test-vm.yml`. Earlier
Podman and in-guest-build experiment workflows were removed after validation.

## Verified result

- [Runtime-only run 35993860510](https://github.com/qdistro/qdistro/actions/runs/35993860510): successful; 15m08s job; [QCOW2 artifact](https://github.com/qdistro/qdistro/actions/runs/35993860510/artifacts/10805489190), 676 MB compressed.
- [Developer-image run 35994485920](https://github.com/qdistro/qdistro/actions/runs/35994485920): successful; 20m12s job; [QCOW2 artifact](https://github.com/qdistro/qdistro/actions/runs/35994485920/artifacts/10806601494), 1.11 GB compressed. It retains the native compiler, Meson, Ninja, Git, pkg-config and development headers, per user preference. The 8 GiB virtual disk's 7.5 GiB root had 2.3 GiB used and 5.3 GiB free; the runner's final disk-use delta was 3.78 GB, below the 15 GiB target.
- Both builds compiled qdwin, daemons, qdshell's native plugin, and the patched production libweston 16 in an openSUSE Tumbleweed Podman pod. The staged tree was about 12 MB. The signed Minimal-VM QCOW2 was verified, resized, and populated offline with libguestfs. No host privilege or KVM was required.
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

The artifact is a native-components *test VM*, not a complete qdistro desktop or a KIWI replacement. `qdshell/meson.build` installs only its native QML plugin, not the QML shell/session wiring. The VM has no baked builder SSH key; SSH remains enabled so cloud-init can provision the user's key on first boot. Artifact retention is 30 days.

RPM distribution was considered and rejected for this test-image workflow.
Keep more extensive VM testing on the user's own hardware as requested, with
the GitHub TCG boot/probe serving as a portable smoke test.

Before calling this reproducible release packaging: pin or consistently snapshot the container, cloud image and RPM repositories; record their digests; inspect peak (not just final) runner disk use; and verify upgrades/rebuilds against the pinned Weston ABI. The current workflow compares four key container/guest RPM versions and performs full staged-ELF closure checks, but it does not prove snapshot identity for the entire dependency graph.
