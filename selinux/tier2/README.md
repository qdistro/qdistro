# Tier-2 — SELinux container confinement

> **Status: stock `container_t` desktop transport validated under enforcing.**
> Version 0.2.0 adds narrow host socket connection rules
> and is installed by bootstrap and the native-stage VM path. The optional
> `qdistro_tier2_t` narrowing remains disengaged. See
> [`doc/selinux.md`](../../doc/selinux.md).

## What this constrains (and why it's not just container_t)

Tier-2 already gets its core isolation from the user namespace +
podman's default `container_t` + the launcher's runtime hardening
(`--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`
rootfs, custom seccomp, `--network=none`, private ipc/pid,
per-container `/run/user`; see `tier2/spawn-tier2.sh`).

What `container_t` does *not* give is a qdistro-specific **narrowing**:
every podman container on the host shares one `container_t` allow-set,
including its full unconfined-network and kernel-state-reader surface
that a Tier-2 workload (default `--network=none`, no kernel
introspection) never uses.

### Domain construction (the key design point)

`qdistro_tier2_t` is built as a **member of the `container_domain` and
`svirt_sandbox_domain` attributes**, then capped:

```
typeattribute qdistro_tier2_t container_domain;
typeattribute qdistro_tier2_t svirt_sandbox_domain;
typebounds container_t qdistro_tier2_t;
```

This matters because **`typebounds` does NOT inherit `container_t`'s
allows** — a bounded type only gets its own rules, capped at the parent.
A from-scratch bounded type with a handful of hand-written `allow`s
could not even exec the image entrypoint or load libraries; the
container would fail to start. Joining the two attributes is what makes
`qdistro_tier2_t` a *working* container domain (entrypoint, exec image
rootfs, read libs, `container_file_t`, `tmpfs` — the same baseline the
working `container_t` path uses). `typebounds container_t` then
guarantees it can never *exceed* the default container surface.

### The actual narrowing

`container_t` carries extra attributes that `qdistro_tier2_t`
deliberately does **not** join:

| Omitted attribute(s) | Surface dropped |
|---|---|
| `corenet_unconfined_type`, `corenet_unlabeled_type`, `container_net_domain`, `sandbox_net_domain` | network sockets — matches `--network=none` |
| `can_dump_kernel`, `can_receive_kernel_messages`, `kernel_system_state_reader` | kernel state read / core-dump-to-kernel surface |

It **does** still join `mcs_constrained_type` (which `container_t` also
carries) — that attribute enforces per-container MCS-category isolation,
so dropping it would *broaden* cross-container access rather than narrow
it. Joining it keeps a Tier-2 container from reaching a sibling
container's `container_file_t` or signalling its process across
categories.

Because the network attributes are not joined, the domain has no network
sockets. The module then **pins the practical network surface off** with
`neverallow` assertions so a future `allow` (here or in a child module)
can't quietly add it back — `neverallow` is enforced at policy
build/expand time, failing the load loudly:

```
neverallow qdistro_tier2_t self:tcp_socket { create listen };
neverallow qdistro_tier2_t self:{ udp rawip sctp dccp icmp } socket create;
neverallow qdistro_tier2_t self:{ netlink_route netlink_tcpdiag packet } socket create;
```

This covers the transport + raw + route/diag socket classes
`container_t` can create; it is the practical net surface, not a claim
of total coverage of every `netlink_*` subclass. The real guarantee is
structural — the network *attributes* are simply not joined — and the
`neverallow`s are the belt-and-braces pin on the classes most likely to
be re-added by a careless future `allow`.

A workload that genuinely needs outbound (`TIER2_NETWORK=pasta`)
must run as stock `container_t`, not `qdistro_tier2_t` — an explicit,
auditable downgrade rather than a silent allowance.

The follow-up's "image-fs writes" and "pipewire socket access" points
are handled by the launcher's existing posture rather than by widening
this domain: the image rootfs is `--read-only` (writes hit ENOSPC; the
domain reads/execs it via the `container_domain` attribute exactly as
`container_t` does), and only the specific `pipewire-N` sockets that
exist at spawn time are bound in (no dbus/pulse/gpg/ssh-agent — they
are simply not bound, so they stay unreachable).

## Shipped transport and deferred domain engagement

The launcher still runs stock `container_t`. It uses `:Z` only on the private
runtime tree and binding-resolved silo home, preserving per-container MCS
separation. The runtime tree contains regular stub files before podman mounts
the shared socket inodes over them; relabelling the tree does not relabel the
host sockets. PipeWire lock files are not sockets and are not bound.

Shared Wayland/PipeWire sockets, the host library and presentation directory
retain their host labels. Two narrow rules let `container_t` stat/connect to
`user_tmp_t` socket inodes and connect to the `unconfined_t` admin servers.
There are no new regular-file writes or directory-management rules against
`user_tmp_t`. The socket permissions apply to stock containers generally;
the mount namespace allowlist is what limits a tier-2 app to the selected
Wayland/PipeWire endpoints. No host runtime directory or bus/agent is mounted.
The library remains `lib_t` (stock read/execute), and presentation retains
`qdistro_presentation_t` (its separate read/watch-only policy).

To engage `qdistro_tier2_t` later, wire the process label explicitly, supply
its bounded transport permissions, and validate the whole workload under
enforcing. Loading the module alone enables stock-domain transport; it does
not activate this optional narrower domain.

## Build contract (reproducible, validated on this host)

This module is **kernel policy language**, not refpolicy m4 (tier1 is
m4). The qdistro host ships `checkpolicy` (`checkmodule`) +
`semodule_package` + `container-selinux` but **not**
`selinux-policy-devel`, so the tier1 `make -f
/usr/share/selinux/devel/Makefile` path is unavailable here. The build
uses the base toolchain instead:

```bash
cd selinux/tier2
make            # checkmodule -M -m -o qdistro_tier2.mod qdistro_tier2.te
                # semodule_package -o qdistro_tier2.pp -m qdistro_tier2.mod
make check      # compile-only pass/fail, no leftover artifacts
make install    # semodule -i qdistro_tier2.pp   (needs container-selinux loaded)
make clean
```

`make check` is the CI/fresh-clone contract — non-zero exit if the
`.te` stops compiling. **Verified green on the dev host.** `make
install` / `semodule -i` was **not** runnable on the dev host
(`semodule` is not installed there — only the compile toolchain
`checkmodule`/`semodule_package`/`seinfo`/`sesearch`), so the load-time
`typebounds`/`neverallow` resolution check has to run on a host with
`policycoreutils` + `container-selinux` loaded.

> The `.if` and `.fc` are written in refpolicy style for symmetry with
> tier1 and for hosts that have the devel toolkit, but the checkmodule
> build path does **not** consume them — the whole policy is in the
> `.te`.

## Files

- `qdistro_tier2.te` — **the whole policy**: the attribute-built
  domain, the `typebounds` cap, and the `neverallow` narrowing. Heavily
  commented.
- `qdistro_tier2.fc` — intentionally near-empty (Tier-2 has no
  host-side labelled exec; the domain is entered via podman's
  `--security-opt label=type:`, not a file-context transition).
- `qdistro_tier2.if` — forward-compat interfaces
  (`qdistro_tier2_setexec`, `qdistro_tier2_read_runtime`); not consumed
  by the checkmodule build.
- `Makefile` — `make` / `make check` / `make install` / `make clean`.
- `install-policy.sh` — build + `semodule -i` driver with a
  container-selinux presence check.

## Validated vs. needs enforcing-VM AVC tuning

**Validated on the dev host (no enforcing VM, no `semodule` here):**

- The `.te` compiles and packages (`make check` green).
- The narrowing holds without conflict: confirmed via `sesearch` that
  neither `container_domain` nor `svirt_sandbox_domain` grants its
  members any of the `neverallow`'d net-socket perms (those come solely
  from the omitted network attributes), so the `neverallow` block can't
  collide with the joined baseline at load time.
- The subset relation behind `typebounds` is structural: the two joined
  attributes are themselves subsets of `container_t`'s attribute set, so
  the bound holds by construction.

**Validated in the live VM at `f30a319ff`:**

`ci/runs/bats-20261005T203006Z-1808035` passed `presentation-enforcing.bats`
6/6, `presentation-live.bats` 2/2, and `tier2-silo-secctx-wiretag.bats` 2/2.
The target policy loaded the module (including its `typebounds`/`neverallow`
checks). Named/disposable Qfileman remained alive with distinct runtime MCS
labels and unchanged shared labels; the binding-resolved silo ran Weston +
weston-terminal and wrote its home. The new lifetime locks need a fresh live
regression run; these results predate them.

The launcher exclusively locks the persistent state directory inode before
`:Z`, independent of container name and path aliases. The supervisor retains
the lock through teardown/detach, including root-launcher mode; a mount
inspection also refuses a home used by a container surviving its launcher.
Errors fail closed. Dead processes leave no stale lock files. Runtime-dir
locks and serialized creation/reaping protect launches before registration.

`:Z` walks the persistent home recursively, so large homes cost more startup
time. It leaves `container_file_t` and private MCS categories on host files
after stop: host processes in domains that cannot read that type/range (for
example a confined backup or file-manager process) can lose access. Never
restore host labels while the container is using the home.

**Still deferred:** a zero-new-AVC workload run under the optional
`qdistro_tier2_t` domain, alongside its launcher wiring.

For a future narrowed-domain AVC, confirm `container_t` already allows the
operation, then add only the required bounded permission; do not join a broad
network/kernel attribute to make a workload pass.
