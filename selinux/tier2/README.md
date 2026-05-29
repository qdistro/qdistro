# Tier-2 — SELinux container confinement

> **Status: compiling, not yet engaged or enforced.** The module builds
> cleanly on the qdistro host (see "Build contract") and is a functional
> container domain narrowed below podman's default `container_t`, but it
> is **not** wired into `spawn-tier2.sh` and has **not** been exercised
> in an enforcing-mode VM. See [`doc/selinux.md`](../../../doc/selinux.md)
> for the design and threat-model context.

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

A workload that genuinely needs outbound (`TIER2_NETWORK=slirp4netns`)
must run as stock `container_t`, not `qdistro_tier2_t` — an explicit,
auditable downgrade rather than a silent allowance.

The follow-up's "image-fs writes" and "pipewire socket access" points
are handled by the launcher's existing posture rather than by widening
this domain: the image rootfs is `--read-only` (writes hit ENOSPC; the
domain reads/execs it via the `container_domain` attribute exactly as
`container_t` does), and only the specific `pipewire-N` sockets that
exist at spawn time are bound in (no dbus/pulse/gpg/ssh-agent — they
are simply not bound, so they stay unreachable).

## Engaging the domain

The module is **inert** after `semodule -i`. Two things must happen to
engage it, neither yet done — both deferred until an enforcing-VM pass:

1. **Process label.** `spawn-tier2.sh` must pass
   `--security-opt label=type:qdistro_tier2_t` to podman. Today it sets
   no process label, so the container runs as stock `container_t`.

2. **Bind reachability.** The launcher mounts the per-container
   `/run/user` dir, the outer wayland socket, the `pipewire-N` sockets,
   and `qdwin-shell.so` with plain `-v ...:rw`/`:ro` — **no `:z`/`:Z`**,
   so podman does not relabel them; they keep their host labels
   (`user_tmp_t` for the runtime/sockets, `lib_t`/`usr_t` for the
   shared object). For `qdistro_tier2_t` to reach the sockets the
   socket/dir binds need `:z` (shared) or `:Z` (private) so they become
   `container_file_t`. `qdwin-shell.so` needs a deliberate strategy — a
   `:z` would mutate a host library label, so prefer a copy-in or a
   dedicated read interface. **This is a launcher change, not a policy
   change**, which is why the policy ships no allow rules against the
   raw host labels (that would cargo-cult against the wrong types).

The wiring should be capability-gated (module loaded *and* a clean
enforcing dry-run) — mirroring the persist-only + capability-gate
posture used elsewhere in qdshell. Tracked in `doc/selinux.md` and in
the TODO block at the bottom of `qdistro_tier2.te`.

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

**NOT yet validated — requires an enforcing-mode VM pass:**

- **Bind relabel + label wiring (launcher change).** Until the binds
  carry `:z`/`:Z` and `qdwin-shell.so` has a label strategy, engaging
  the domain would break socket/plugin access. See "Engaging the
  domain".
- **Zero-new-AVC workload run.** That the nested weston +
  qdwin-shell.so + guest app run cleanly under `qdistro_tier2_t`.
  Expected (same file/exec surface as the working `container_t` path via
  the shared attributes), but *asserted, not proven*, until
  `tests/integration/vm/s32-tier2-podman.sh` +
  `s40-tier2-hardening.sh` run enforcing with the domain engaged.
- **Load-time `typebounds`/`neverallow` resolution** against the active
  policy (needs `semodule -i` on a policycoreutils host).

The iteration loop (capture AVC → confirm `container_t` allows it →
add the narrowest allow to `qdistro_tier2_t` directly, never by joining
another broad attribute) is in the TODO block at the bottom of
`qdistro_tier2.te`.
