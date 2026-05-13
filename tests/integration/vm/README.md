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
export VM_NAME=qdwin-weston-260422-1208 # or any clone

# Run the whole suite:
bats tests/integration/vm/

# Or a single file:
bats tests/integration/vm/s5c-stream-input.bats
```

### Parallel multi-VM run (one VM per .bats file)

```bash
# Pre-bake deps so every clone skips zypper install-deps:
scripts/vm/build-baked-baseweed.sh # one-time, ~15-30 min

# Run all phase{6.5,6.6,7}.bats in parallel on three fresh clones:
tests/integration/vm/run-parallel.sh # default: 3-way concurrency, --from-baked

# Subset / serialised:
tests/integration/vm/run-parallel.sh --jobs 1 tiered-isolation.bats
tests/integration/vm/run-parallel.sh --keep compositor-shell.bats # keep VMs after pass

# Fall back to plain baseweed (slow zypper on every worker):
tests/integration/vm/run-parallel.sh --no-baked
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
installed. If you changed code on the host, sync+build first:

```bash
# In one terminal:
cd compositor && python3 -m http.server 8765 --bind 127.0.0.1

# Then:
$(git rev-parse --show-toplevel)/scripts/vm/vm-exec "$VM_NAME" \
 'bash /root/s3c-sync-and-build.sh'
```

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

Then run `scripts/vm/fresh-vm-bootstrap.sh` inside the VM
(fetched via `http_server_vm_deploy.md` pattern) to sync the source
tree, build qdwin-shell.so + qdistro-forward, stage /root probe
scripts, and start pipewire.

## Maintenance

When a new probe lands in `scripts/vm/`, add a matching
@test here. The test body should:

1. Assume the VM already has the probe deployed (or deploy it via
 HTTP-server-on-host pattern; see `memory/http_server_vm_deploy.md`).
2. Run the probe via `vm-exec`.
3. Assert on exit code via `[ "$status" -eq 0 ]`.
4. Optionally `run-asserts` specific log lines via grep.
