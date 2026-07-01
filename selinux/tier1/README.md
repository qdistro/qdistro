# Tier-1 — SELinux sandbox

> **Status: shipped source with enforcing VM coverage.** See
> [`doc/selinux.md`](../../../doc/selinux.md) for the v1 posture.
> The standard Tumbleweed bootstrap installs this policy and hardened
> profiles require SELinux Enforcing unless the operator sets the
> documented `QDISTRO_ALLOW_PERMISSIVE=1` override.

## Files

- `qdistro_tier1.te` — type, transition, allow rules, and v1 ratchets.
- `qdistro_tier1.if` — exported interfaces.
- `qdistro_tier1.fc` — file-context regexes.
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
- `spike-checklist.md` — historical spike notes and verification
 recipes.

## v1 ratchets

The v1 policy intentionally does **not** grant:

- `domain_read_all_domains_state(qdistro_tier1_t)`. Tier-1 must not
  read sibling domains' `/proc/<pid>` state.
- broad `user_home_t:file` read access. Tier-1 may traverse home
  directories and manage its relabelled per-app state, not read all
  files in `$HOME`.

The enforcing VM probe (`tests/integration/vm/s55-tier1-enforcing.sh`)
is the acceptance lane for discovering any narrower self-only proc or
app-state allows needed by representative workloads.

Every expected Tier-1 app still needs an admin-authored allow rule such
as:

```yaml
- name: allow-tier1-firefox
  decision: allow
  match:
    action: qdistro.tier1.spawn:/usr/bin/firefox
  rationale: expected Tier-1 browser launcher
```

## Reading order

1. `doc/selinux.md` — design, threat model, alternatives.
2. `spike-checklist.md` — historical spike notes.
3. `qdistro_tier1.te` — concrete starting policy.
4. `spawn-tier1.sh` — runtime entry point.
