# Vendored Quickshell (qs runtime)

`src/` is upstream [Quickshell](https://quickshell.org) **0.3.0**
(quickshell-mirror/quickshell tag `v0.3.0`, tarball sha256 in
[VERSION](VERSION)), unpatched.

## Why vendored

qdshell forked Noctalia at upstream v4.5.0 — before Noctalia v4.6.0 moved its
own shell onto `noctalia-qs`, a Noctalia-maintained Quickshell fork. Tumbleweed
packages only `noctalia-qs` (binary `/usr/bin/qs`); upstream `quickshell` is
not in the distribution repos at all. The noctalia-qs repository was archived
in July 2026 (Noctalia v5 dropped Quickshell for a native C++ rewrite), so the
fork receives no further fixes. qdistro therefore builds the maintained
upstream Quickshell itself.

## What the build produces

`cmake --install` lays down `${prefix}/bin/quickshell` plus a `qs` symlink —
the same pair every qdistro call site already uses (`/usr/bin/qs` in the
qdshell unit and `qs ipc` in the CI waiters; `quickshell` in the host-side
gate tests). Quickshell's QML modules are compiled into the binary; there are
no separate qmldir payloads to stage.

## Build-time-only wrinkle: CLI11

Upstream does `find_package(CLI11 CONFIG REQUIRED)` for its launch/CLI code,
but Tumbleweed ships no cli11 package. CLI11 is header-only, so
`third-party/cli11/include/CLI/` carries the official single-header release
(`CLI11.hpp`) behind thin forwarding headers for the three names quickshell
includes (`CLI/CLI.hpp`, `CLI/App.hpp`, `CLI/Validators.hpp`), and
`cmake/CLI11Config.cmake` supplies the imported `CLI11::CLI11` target.
`build-quickshell.sh` prepends `cmake/` to `CMAKE_PREFIX_PATH`.

## Building

```sh
./build-quickshell.sh                          # configure + build into src/build
DESTDIR=/stage ./build-quickshell.sh           # also install under /stage/usr
```

Requires the snapshot-pinned toolchain (see the `quickshell` block in
`scripts/vm/container-native-deps.sh` for the Tumbleweed package set). The
qdistro native builder and host container build it this way; do not build it
on a developer host by hand unless the same dep set is installed.

## Upgrading

1. Replace `src/` with the new release tarball contents.
2. Record the new version and tarball sha256 in [VERSION](VERSION).
3. Check `BUILD.md`'s dependency section upstream for newly-required
   packages and add them to `container-native-deps.sh`,
   `ci/containers/host-packages.txt`, and `image/config.xml` together.
