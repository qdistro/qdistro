# Tier-1 — SELinux sandbox (spike skeleton)

> **Status: spike implementation.** See
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
- `spawn-tier1.sh` — bash wrapper in the `qdistro-tier3-spawn` shape.
  It treats broker launch authorization as mandatory: the wrapper calls
  `CheckPermission("qdistro.tier1.spawn:<canonical-app-path>", {})`
  and only proceeds on an explicit rules-engine `allow`; cache rows and
  hook verdicts are ignored for this action namespace.
- `spike-checklist.md` — the four blocking spikes with verification
 recipes.

## Why "skeleton"

Six prior sessions deferred Tier-1. The deferral pattern means the
work is genuinely multi-week and the next session should start from
a researched baseline, not blank paper. These files capture:

- The exact type / interface / file-context shape we picked.
- The wrapper script flow, including the mandatory broker launch gate.
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
- Broad application allowlist curation. Every expected Tier-1 app needs
  an admin-authored allow rule such as:

  ```yaml
  - name: allow-tier1-firefox
    decision: allow
    match:
      action: qdistro.tier1.spawn:/usr/bin/firefox
    rationale: expected Tier-1 browser launcher
  ```

## Reading order

1. `doc/selinux.md` — design, threat model, alternatives.
2. `spike-checklist.md` — the four spikes that must resolve first.
3. `qdistro_tier1.te` — concrete starting policy.
4. `spawn-tier1.sh` — runtime entry point.
