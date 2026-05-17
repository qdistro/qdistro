# tier-5b — per-app VM-windowed (waypipe-over-vsock, `direct` shape)

§Phase-7 tier-5b. Publishes a **single** GUI app from a tier-5b VM to
the host qdwin compositor as a seamless `xdg_toplevel`.

## Shape

Probe verdict (2026-05-17,
`plan2/research/qdwin-nested-over-vsock/12-verdict.md`): publisher
shape is `direct` — no inner Wayland compositor in the guest;
`waypipe-server` runs the target app as its only client. The probe
established that this is the same shape tier-5 already uses (the 41-
LOC `qdistro-tier5-publisher.sh`), so tier-5b is mostly per-app
polish on a shipping foundation.

## Files

| file | role |
|---|---|
| `build-guest-image.sh` | one-shot per-app guest disk builder (Tumbleweed Minimal-VM + waypipe + target app + the publisher) |
| `qdistro-tier5b-publisher.sh` | guest-side publisher: pinned to one binary at image-build time, refuses to launch arbitrary commands; the host invokes it via qga `guest-exec` |
| `spawn-tier5b.sh` | host receiver: spawns the VM, waits for qga, runs `qdistro-secctx-exec --silo <silo> waypipe --vsock client -- ...` so the outer wl_client carries the secctx triple |
| `qdistro-tier5b-cleanup.sh` | idempotent destroy + undefine + overlay-unlink |
| `domain-template.xml` | libvirt domain XML; vsock CID in 100..N range to avoid colliding with tier-5's 3..N |
| `qdistro_integration.py` | App1 launcher entry — claims `org.qdistro.Tier5bVM.uidNNNN` |

## Secctx — why it's stamped host-side

`wp_security_context_manager_v1.create_listener` has signature `nhh`
(two fds: a listen_fd and a close_fd). AF_VSOCK does not implement
`SCM_RIGHTS`, so the protocol literally has no wire form across vsock.
The only architecturally-possible path is: stamp the outer wl_client
host-side, before it connects to qdwin. `qdistro-secctx-exec` does
this; `spawn-tier5b.sh` wraps `waypipe --vsock client` in it.

See `plan2/research/qdwin-nested-over-vsock/02-waypipe-secctx-passthrough.md`
for the full argument and the rejected alternatives.

## First app: Firefox

`MOZ_ENABLE_WAYLAND=1` is set by the publisher so Firefox uses native
Wayland (no XWayland, no inner X server). XWayland-needing apps are
**out of scope for the MVP**; if a future app needs X, add Xwayland
inside the guest as a separate task.

## Tier-5b vs tier-5

| | tier-5 | tier-5b |
|---|---|---|
| grain | one session per VM | one app per VM |
| publisher | session launches arbitrary apps | publisher hard-codes one binary at build time |
| guest RAM default | 512 MiB | 1 GiB |
| CID range | 3..N | 100..N |
| App1 bus name | (no entry) | `org.qdistro.Tier5bVM.uidNNNN` |
| secctx engine | `qdistro.tier5` | `qdistro.tier5b` |

## Build + run

```sh
sudo QDISTRO_VM_PASSWORD=xxx ./build-guest-image.sh --app firefox
sudo ./spawn-tier5b.sh --vm ff-$$ --app firefox
```

The spawned domain destroys + undefines + overlay-unlinks on EXIT,
matching tier-5's lifecycle. Orphans from crash-on-exit are reaped
at startup of the next `spawn-tier5b.sh` invocation.

## Cross-references

- Probe: `plan2/research/qdwin-nested-over-vsock/12-verdict.md`
- Spec: `plan2/tasks/P05b-tier5b-vm-app-windowed.md`
- Adjacent tier-5: `qdistro/tier5-vm/`
- Host-side secctx stamper: `qdistro/daemons/secctx-exec/qdistro-secctx-exec.c`
- App1 SDK: `qdistro/sdk/qdistro_app/`
