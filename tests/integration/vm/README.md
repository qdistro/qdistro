# tests/integration/vm/

VM-gated regression tests for qdwin. Unlike `qdwin/tests/host/`
(headless weston on host, no VM), these tests require a running
qdwin-weston VM because they depend on:

- PipeWire + Wireplumber daemon (backend-pipewire consumer).
- libfreerdp-shadow3 (`qdistro-forward` links it).
- A real RDP client runtime (`sdl-freerdp` with
 `SDL_VIDEODRIVER=dummy` for headless).
- weston-terminal for the per-view source.

## Usage

```bash
# Point at the VM:
export VM_NAME=qdistro-template # or any clone

# Run the whole suite:
bats tests/integration/vm/

# Or a single file:
bats tests/integration/vm/s5c-stream-input.bats
```

### Parallel multi-VM run (one VM per .bats file)

The `bats` gate spins one disposable VM per `.bats` file and runs them in
parallel. Concurrency auto-sizes to host resources via three tiers — minimal
(≤32 GiB → 4), medium (~64 GiB + ≥10 cores → 10), high (≥90 GiB + ≥12 cores →
16) — RAM-clamped so VMs never oversubscribe memory. Set `QCI_JOBS=N` to
override. Each VM gets `QDWIN_VM_VCPUS` (default 4) vCPUs; CPU is intentionally
overprovisioned since RAM is the binding constraint.

```bash
# Pre-bake deps so every clone skips zypper install-deps:
scripts/vm/build-baked-baseweed.sh   # one-time, ~15-30 min

# Run the whole bats gate in parallel (auto-sized concurrency):
ci/bin/qci bats

# Run a subset of files (still parallelised across them):
ci/bin/qci bats tests/integration/vm/tiered-isolation.bats

# A single file:
ci/bin/qci bats --file tests/integration/vm/compositor-shell.bats

# Force a specific concurrency:
QCI_JOBS=4 ci/bin/qci bats

# Pin all files onto one pre-existing VM (serial, no disposable clones):
ci/bin/qci bats --vm my-test
```

### Enforcing-mode pass (phase7-tier1-enforcing + phase7-broker-enforcing)

The standard `--from-baked` clone flips `/etc/selinux/config` to
`SELINUX=permissive`, so both enforcing tests in `tiered-isolation.bats` cleanly
SKIP. To run them as hard PASS, build a config-pinned-enforcing baked
overlay and drive the bats subset over SSH (qga is denied under
enforcing because `virt_qemu_ga_t` is too restricted):

```bash
# One-time, ~10 min: bake an enforcing-config overlay on top of
# baseweed-baked. Generates ~/.ssh/qdistro_enforcing_id_ed25519 if
# absent.
scripts/vm/build-enforcing-baseweed.sh

# Per-test-cycle, ~1 min: clone, define with <portForward> for SSH,
# wait for sshd, then `bats --filter enforcing tiered-isolation.bats` over SSH.
tests/integration/vm/run-bats-enforcing.sh
tests/integration/vm/run-bats-enforcing.sh --cleanup # destroy + undefine on exit
```

`helpers.bash:vm_run` routes through `ssh -p $VM_SSH_PORT
root@127.0.0.1` whenever `VM_SSH_PORT` is set, so the same .bats
files work over either transport.

The runner spawns one VM per `.bats` file (file-scope is the natural
seam: tests inside one file share `/run/user/1000/wayland-1` and
qdshell socket fixtures and cannot run multi-threaded). Per-worker
logs land under `/tmp/qdistro-parallel-bats-<ts>/`; the per-file
exit code rolls up into the runner's exit code.

Each @test wraps one of the reproducible probes in
`scripts/vm/`. The tests assume the VM is already booted,
has pipewire up, and has a recent qdwin-shell.so + qdistro-forward
installed. If you changed code on the host, re-run
`scripts/vm/spin-test-vm.sh <prefix>` against a fresh clone — the
bake pipeline tarballs the three sibling repos, pushes them into
the VM, rebuilds qdwin + daemons, and reruns the install scripts.

## Dependencies

### Host
- `bats-core` (`zypper install bats`). The tests run on host and
 `vm-exec` into the VM.
- `scripts/vm/vm-exec` reachable via absolute path or PATH.

### VM (baseweed clone + these packages)

Installed once via `zypper install` after cloning baseweed:

```
weston weston-devel libweston-14 libweston-14-0
freerdp freerdp-sdl freerdp-server freerdp-devel winpr-devel
libpixman-1-0 libpixman-1-0-devel
pipewire wireplumber pipewire-tools pipewire-devel libpipewire-0_3-0
gstreamer gstreamer-plugin-pipewire gstreamer-plugins-good gstreamer-utils
meson ninja gcc gcc-c++ pkgconf-pkg-config
wayland-devel wayland-protocols-devel libxkbcommon-devel libevdev-devel
libinput-devel libgbm-devel libdrm-devel seatd-devel
libXcursor-devel adwaita-icon-theme
python313-pywayland python313-cffi python313-PyQt6
qt6-wayland python313-setuptools
qt6-base-devel qt6-declarative-devel
socat Mesa Mesa-libEGL1 Mesa-libGL1 Mesa-dri
```

`libXcursor-devel` is required by (b) — qdwin-shell links libXcursor
for cursor-shape-v1 theme loading. `adwaita-icon-theme` (or any theme
that provides CSS cursor names) gives the loader a populated theme;
without one, set_shape still accepts but all lookups miss.

`wayland-protocols-devel` and `python313-cffi` are easy to miss —
`python313-pywayland` depends on `_cffi_backend` at runtime but
doesn't pull it in transitively, and the build step runs
`pywayland-scanner` which needs `wayland-protocols` pkgconfig data.
`python313-PyQt6` + `qt6-wayland` are for `qdshell.py` (which binds
`qdwin_shell_v1` as a PyQt app).

`qt6-base-devel` + `qt6-declarative-devel` are required to build the
qdshell QML plugin (`qdshell/qml-plugin`, a Qt6 C++ meson build). Its
`meson.build` resolves `Qt6Core`, `Qt6Network`, `Qt6Qml` and
`Qt6QmlIntegration` (the latter `required: false`) and runs `moc` via
`import('qt6')`: `qt6-base-devel` provides Core/Network + the moc
tooling, and `qt6-declarative-devel` pulls in the Qml/QmlIntegration
development files (via `qt6-qml-devel`). The
plugin's Wayland side uses plain `wayland-client` + `wayland-scanner`
(already covered by `wayland-devel`), not Qt's WaylandClient.

Then run `scripts/vm/fresh-vm-bootstrap.sh` inside the VM
(fetched via `http_server_vm_deploy.md` pattern) to sync the source
tree, build qdwin-shell.so + qdistro-forward, stage /root probe
scripts, and start pipewire.

## Maintenance

When a new probe lands in `scripts/vm/`, add a matching
@test here. The test body should:

1. Assume the VM already has the probe deployed (or deploy it via
HTTP-server-on-host pattern: the host serves the three sibling-repo tarballs over SLIRP NAT on 10.0.2.2:8765.
2. Run the probe via `vm-exec`.
3. Assert on exit code via `[ "$status" -eq 0 ]`.
4. Optionally `run-asserts` specific log lines via grep.
