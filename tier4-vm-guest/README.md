# tier4-vm-guest — nested-qdwin guest image (P10)

This directory ships the **guest-side** half of the nested-qdwin
waypipe migration for tier-4 VMs. `tier4-vm/` now consumes this image
and template by default.

## Files

- `qdistro-tier4-publisher.sh` — guest-side publisher script. Runs
  inside the booted guest under systemd. Binds `qdwin-bystander
  --connect wayland-0 --forward-all-toplevels` and wraps it with
  `waypipe --vsock -s 2:$PORT server` so toplevels from the in-guest
  nested qdwin reach the host's waypipe-client. With
  `QDISTRO_TIER4_STREAMING_METHOD=rdp`, it instead requests a qdwin RDP
  view stream and bridges the guest TCP RDP endpoint to AF_VSOCK with
  `socat`.
- `build-guest-image.sh` — bakes `qdistro-tier4-guest.qcow2`: minimal
  Tumbleweed + libweston + qdwin (compiled with `meson -Drole=guest`)
  + waypipe + weston-terminal + alsa-utils + qemu-guest-agent +
  RDP runtime dependencies (`socat`, PipeWire/FreeRDP libraries, and
  `qdistro-forward`) + systemd units for qdwin-guest-session and
  qdistro-tier4-publisher + /etc/fstab line for the virtiofs `/host`
  mount. The bake verifies that `socat` and `/usr/bin/qdistro-forward`
  are present in the image.
- `tier4-publisher.conf.example` — guest publisher config for the RDP
  mode. Copy the keys into `/etc/qdistro/tier4-publisher.conf` or inject
  equivalent environment when starting the publisher.
- `domain-template.xml` — libvirt domain XML for the waypipe guest:
  vsock, virtio-snd, virtiofs, and no guest display-agent channel.

## Architecture

See `plan2/tasks/P10-tier4-guest-image-nested-qdwin.md` and
`plan2/tasks/P11-tier4-waypipe-migration.md`. tl;dr: a stripped-down
qdwin runs inside the guest VM, presenting a normal
`wayland-0` socket for in-guest clients (weston-terminal in the smoke
test). The publisher wraps the bystander with waypipe-server over
vsock, and the host's waypipe-client receives the toplevel stream and
attaches it to outer xdg_toplevels on the host's full-fat qdwin.

For RDP mode, set `TIER4_STREAMING_METHOD=rdp` on the host launcher. The
launcher starts the guest publisher with `QDISTRO_TIER4_DISPLAY=rdp` and
passes the subscription selector. The guest publisher writes credentials
to `/run/qdistro-tier4-rdp.env`; the host reads that file over qga, opens
a local `socat` bridge, and launches FreeRDP against `127.0.0.1`.

Inside the guest, persistent publisher settings live at
`/etc/qdistro/tier4-publisher.conf`:

```sh
QDISTRO_TIER4_STREAMING_METHOD=rdp
QDISTRO_TIER4_RDP_SUBSCRIBE=last
QDISTRO_TIER4_RDP_PEER_LABEL=tier4-rdp
QDISTRO_TIER4_RDP_CREDS=/run/qdistro-tier4-rdp.env
```

## Troubleshooting: guest window not visible

If `spawn-tier4` brings the VM up but no guest toplevel ever appears on
the host compositor, the failure is almost always in one of the two
in-guest systemd units below. Both are *system* units (baked into
`/etc/systemd/system/` by `build-guest-image.sh`), so query them with
plain `journalctl -u` (no `--user`):

1. **Nested qdwin session.** Confirm the in-guest compositor came up:

   ```sh
   journalctl -u qdwin-guest-session.service
   ```

   This unit runs `weston` with the role=guest qdwin plugin and binds
   the inner `wayland-0`. If it never reaches a steady state, the
   publisher has no inner socket to attach to and nothing reaches the
   host.

2. **Publisher.** Confirm the vsock publisher started and what it did:

   ```sh
   journalctl -u qdistro-tier4-publisher.service
   ```

   ```sh
   cat /var/log/qdistro-tier4-publisher.log
   ```

   The publisher (`/usr/local/bin/qdistro-tier4-publisher.sh`) redirects
   its own stdout/stderr to `/var/log/qdistro-tier4-publisher.log` once
   it has passed its startup checks. In the default waypipe path the log
   gets the startup banner
   (`=== ... tier4-publisher port=... ===`) and then the `waypipe`/qdwin
   output; in RDP mode it also gets the `RDP stream ready ...` line.

   **Caveat — early failures never reach that log.** The publisher
   validates its arguments and environment *before* it redirects output
   to the log file: the `<vsock_port>` argument, `QDISTRO_TIER4_DISPLAY`
   (`waypipe`/`rdp`), the Wayland-socket path, and the RDP-subscribe
   value are all checked first, and any of those failures (`exit 2`)
   print to **stderr** and exit before the redirect happens. So a log
   file that is empty, missing, or stale does **not** mean "nothing ran"
   — it can mean the publisher died during early validation (or the
   service never started). Look at the `qdistro-tier4-publisher.service`
   journal (which captures that pre-redirect stderr), not the log file.

   Note also that if the publisher cannot create
   `/var/log/qdistro-tier4-publisher.log` it falls back to a `mktemp`
   file under `/tmp`, so an empty `/var/log` copy can also mean the runtime
   log went elsewhere — the journal is the reliable source.

### Other first checks

Check these in the guest when the journals point that way:

- **Inner Wayland socket.** The publisher waits up to ~30s for inner
  qdwin's `wayland-0` to appear and exits (`exit 3`,
  `inner Wayland socket ... never appeared`) if it does not. That points
  back at `qdwin-guest-session.service` (step 1) rather than the
  publisher itself.
- **vsock device.** The publisher modprobes `vhost_vsock`/`vsock` and
  briefly polls for `/dev/vsock` (it does not hard-fail if absent — it
  proceeds and `waypipe`/`socat` fail later instead). If the AF_VSOCK
  transport never comes up, confirm the libvirt domain actually has the
  vsock device (see `domain-template.xml`).
- **virtiofs `/host` mount.** The image ships an `/etc/fstab` line
  (`qdistro-host /host virtiofs nofail,_netdev 0 0`). Confirm `/host` is
  mounted if host-shared files are expected; the `nofail` option means a
  missing mount does not block boot, so it can fail silently.
- **RDP mode only.** With `QDISTRO_TIER4_DISPLAY=rdp` the publisher also
  requires `socat` in the guest (`exit 2` if absent), waits for
  `qdwin-bystander` to report an `RDP_PORT`, and waits for
  `qdistro-forward` to accept on `127.0.0.1:<RDP_PORT>` before bridging
  it to vsock (`exit 4` on any of those). Those failures are logged to
  `/var/log/qdistro-tier4-publisher.log` (they happen after the
  redirect), with the captured `qdwin-bystander` output appended.

## Historical scope

P10 originally landed this directory for the waypipe guest image. Later
Tier-4 work wires `spawn-tier4` to this image and adds the explicit RDP
transport selected with `TIER4_STREAMING_METHOD=rdp`.

The older SPICE/`domdisplay` fallback is intentionally retired. Guest
image and integration-test updates should cover the default waypipe path
or the explicit RDP transport, not an implicit libvirt viewer fallback.
