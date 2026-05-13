# Tier-1 — SELinux sandbox (spike skeleton)

> **Status: design + spike, no implementation yet.** See
> [`doc/selinux.md`](../../../doc/selinux.md)
> for the full design. This directory carries the skeletons the
> implementation pass will fill in once the four blocking spikes
> resolve on a Tumbleweed VM.

## Files

- `qdistro_tier1.te` — type, transition, allow rules. Skeleton.
- `qdistro_tier1.if` — exported interfaces. Skeleton.
- `qdistro_tier1.fc` — file-context regexes. Skeleton.
- `Makefile` — wraps `/usr/share/selinux/devel/Makefile` to build
 `qdistro_tier1.pp`.
- `install-policy.sh` — `make` + `semodule -i` driver, to be
 invoked from `fresh-vm-bootstrap.sh` once the policy module is
 ready.
- `spawn-tier1.sh` — bash wrapper skeleton in the
 `qdistro-tier3-spawn` shape.
- `spike-checklist.md` — the four blocking spikes with verification
 recipes.

## Why "skeleton"

Six prior sessions deferred Tier-1. The deferral pattern means the
work is genuinely multi-week and the next session should start from
a researched baseline, not blank paper. The skeletons here capture:

- The exact type / interface / file-context shape we picked.
- The wrapper script flow with TODO markers around the four
 uncertain bits.
- The build + load story so the next session can compile the policy
 module on day one and iterate from there.

What's deliberately not done yet:

- Compiling the `.te` file. We don't yet know the precise interface
 names every Tumbleweed install ships (they vary across selinux-
 policy versions); compiling against current Tumbleweed is part of
 Spike 1 in the checklist.
- The C wrapper `qdistro-tier1-exec`. Trivial once the policy
 module loads — `setexeccon()` + `execvp()` — but pointless before
 the policy is alive.
- Hooking spawn into the broker as `qdistro.tier1.spawn:<app>`.
 Same reason.

## Reading order

1. `doc/selinux.md` — design, threat model, alternatives.
2. `spike-checklist.md` — the four spikes that must resolve first.
3. `qdistro_tier1.te` — concrete starting policy.
4. `spawn-tier1.sh` — runtime entry point.
