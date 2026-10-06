# Tier-3s — SELinux confinement for the runsc control plane

> **Status: enforcing-qualified pending.** The module compiles, loads, and
> covers the full launch lifecycle (create/start/state/kill/delete plus the
> host-uds bridge connect) with zero residual AVCs on the permissive dev
> VM. Enforcing-mode qualification (s123–s129) is the phase-D acceptance
> gate — see `todo/paravirt/03-implementation-plan.md`.

## What this constrains

Tier 3s runs the workload under gVisor (`runsc --platform=systrap`), which
needs a handful of real host processes per launch:

- the **Sentry** — gVisor's user-space kernel; ptraces its own sandbox
  tasks, executes stubs from memfd, mounts proc/tmpfs inside its own
  namespaces;
- the **gofer** — the host file server; reads the podman rootfs and, with
  `host-uds=open`, makes the host-side `connect()` to the per-launch
  waypipe `link.sock`;
- the **stubs** — `runsc-fd-parking`, `gvisor-sentry-prewarmer`,
  `checkpointgofer`, `runsc-metric-server`.

All of them consume attacker-influenced input (guest syscalls, LISAFS
requests, the byte stream to waypipe). Before this module they inherited
the caller's domain — effectively unconfined. `qdistro_tier3s_t` bounds
that blast radius.

## How it engages

`qdistro_tier3s.fc` labels the runsc ELF and the gvisor-bin sidecars
`qdistro_tier3s_exec_t` and the state roots `qdistro_tier3s_var_run_t`.
The real launch chain is `unconfined_service_t` (the launch unit) →
podman → base-policy auto-transition to `container_runtime_t` → conmon →
wrapper → runsc ELF, so `container_runtime_t` is the transition source
that actually fires (verified live); `unconfined_service_t` and
`unconfined_t` are kept as sources too so the domain engages even if the
podman auto-transition is absent or bypassed. In-domain re-exec (runsc
subcommands, the sidecars) stays in the domain via `execute_no_trans`.

Runsc is deliberately reachable only through `/usr/libexec/qdistro/
tier3s-runsc` (the bin_t wrapper that pins platform/flags and refuses a
caller-supplied `--root`), but the wrapper itself is not confinement —
the label is on the ELF.

`/run` bases are created by tmpfiles, which applies the file context at
creation; per-uid and per-token children inherit the parent label. If
runsc is provisioned after the module was loaded, `provision-runsc.sh`
restorecons the tree itself.

## Build contract

Same toolchain as tier2 — kernel policy language, no
selinux-policy-devel:

```sh
make            # qdistro_tier3s.pp (packaging the .fc)
make check      # compile-only, leaves no artifacts
make install    # semodule -i + restorecon of the labelled paths
```

## The bridge edge

`allow qdistro_tier3s_t unconfined_service_t:unix_stream_socket connectto`
is the one cross-domain socket permission: the gofer connecting to the
per-launch `link.sock`, whose listener is the waypipe client running under
the launch unit. SELinux has no per-path socket granularity; the path is
pinned separately (the socket file must be `qdistro_tier3s_var_run_t`,
write-reachable only inside `/run/qdistro-tier3s*/`).

## Pinned off (neverallow)

No host network socket `create` (network=none is a launch invariant), no
`sys_module`, and no `ptrace`/`signal` on processes outside the domain —
systrap's ptrace is self-only.
