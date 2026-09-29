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

### Enforcing-mode pass

The standard `--from-baked` clone flips `/etc/selinux/config` to
`SELINUX=permissive`, so enforcing tests in `tiered-isolation.bats` cleanly
SKIP. The supported Tumbleweed `daily-driver`/`release` bootstrap path now
requires SELinux Enforcing; use `QDISTRO_ALLOW_PERMISSIVE=1` only for
documented AVC-harvest/debug runs. To run the VM lane as hard PASS, build a
config-pinned-enforcing baked overlay and drive the bats subset over SSH (qga
is denied under enforcing because `virt_qemu_ga_t` is too restricted):

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
bake pipeline tarballs the qdistro monorepo, pushes it into the VM,
installs the matching Podman-built native payload, and reruns the install
scripts.

## Dependencies

### Host
- `bats-core` (`zypper install bats`). The tests run on host and
 `vm-exec` into the VM.
- `scripts/vm/vm-exec` reachable via absolute path or PATH.

### VM (cloud-derived baked base)

`scripts/vm/install-deps.sh` is the package source of truth. The baked
cloud base installs its runtime/test subset, with no native compiler,
Meson, Ninja, `make`, or development headers. Rootless Podman builds
qdwin, qdshell, daemons, qsu, and SELinux modules against the pinned
Tumbleweed snapshot. `fresh-vm-bootstrap.sh` installs that payload,
the Python services and QML tree, stages `/root` probes, and starts
the session. It also stages Wayland protocol XML and pkg-config metadata
needed by the Python protocol probes. See [ci/README.md](../../../ci/README.md#cloud-test-substrate)
for the snapshot and RPM caches.

## Maintenance

When a new probe lands in `scripts/vm/`, add a matching
@test here. The test body should:

1. Assume the VM already has the probe deployed (or deploy it via the
   HTTP-server-on-host pattern: the host serves the monorepo tarball over
   SLIRP NAT at `10.0.2.2:<staging-port>`; `spin-test-vm.sh` binds a
   kernel-chosen free port per run and passes `http://10.0.2.2:$SPIN_HTTP_PORT`
   to the bootstrap; 8765 remains the manual-bootstrap default and the
   enforcing-base builder's default (`build-enforcing-baseweed.sh --http-port`)).
2. Run the probe via `vm-exec`.
3. Assert on exit code via `[ "$status" -eq 0 ]`.
4. Optionally `run-asserts` specific log lines via grep.
