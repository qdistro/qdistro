# Tier-1 SELinux — blocking spikes

> Run on a fresh Tumbleweed clone (per memory `fresh_vm_recipe_phase6.5`)
> before starting Tier-1 implementation. Each spike has a one-line
> verification command + a "next step" depending on the result.

## Spike 1 — `user_runtime_t` socket coverage

What to check: the wayland + pipewire socket labels on a fresh
Tumbleweed clone.

```
ls -Z /run/user/1000/wayland-1
ls -Z /run/user/1000/pipewire-0
```

**Expected:** both show `system_u:object_r:user_runtime_t:s0` (or
similar, key part is `user_runtime_t`).

**If `user_runtime_t` →** `userdom_stream_connect_user_runtime` in
`qdistro_tier1.te` is correct. Move on to Spike 2.

**If different label** (e.g. `user_tmp_t`, `xdm_var_run_t`) →
the policy needs a transition rule on logind/systemd-tmpfiles, OR
we substitute the right `userdom_*_search_*` interface. Note the
actual label, the implementation pass adjusts the .te.

## Spike 2 — `unconfined_t` default for admin

What to check:

```
id -Z
semanage login -l | grep -E '^(admin|__default__)'
```

**Expected `id -Z`:** `unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023`
(Tumbleweed default, admin inherits from `__default__`).

**Implications:**

- The `domain_auto_trans(unconfined_t, qdistro_tier1_exec_t,
 qdistro_tier1_t)` rule in the .te (under `optional_policy`) must
 succeed; verify with `sesearch --type_trans -s unconfined_t -t
 qdistro_tier1_exec_t` after `semodule -i`.
- Alternative: migrate admin to `staff_u`:
 `semanage login -a -s staff_u admin`. Spike must then re-run the
 full qdistro 49-test bats matrix to confirm broker, qdshell,
 admin-app, fprintd unlock all still work in `staff_t`.

**Pick:** stay on `unconfined_u` if the auto_trans works; migrate
to `staff_u` only if it doesn't. The `optional_policy` block in the
.te keeps both paths viable.

## Spike 3 — broker dbus_chat from `qdistro_tier1_t`

What to check:

```
ls -Z /etc/dbus-1/system.d/qdistro*
cat /etc/dbus-1/system.d/qdistro-admin-broker.conf 2>/dev/null
```

**Then load the policy module + run a probe:**

```
cd selinux/tier1 && sudo make install
runcon -t qdistro_tier1_t -- dbus-send --system --dest=qdistro.admin.broker \
 --type=method_call --print-reply / qdistro.admin.broker.Ping
```

**Expected:** dbus-send succeeds OR the AVC denial that bubbles up
points at a specific allow rule we need to add to the policy.

**If denied:** extract `scontext` / `tcontext` from
`tail -1 /var/log/audit/audit.log`, write a `qdistro_broker_dbus_chat`
interface, then re-run.

## Spike 4 — `selinux-policy-sandbox` package usefulness

What to check:

```
zypper info selinux-policy-sandbox
seinfo -t | grep -i sandbox
zypper install -y selinux-policy-sandbox
seinfo -t | grep -i sandbox
diff <(seinfo -t | grep -i sandbox | sort) /tmp/before
```

**Expected:** zypper installs cleanly; `seinfo -t | grep sandbox`
output likely doesn't grow because `sandboxX` is already loaded
from `selinux-policy-targeted`.

**If no new types appear:** the package is dead weight. Don't add
it to fresh-vm-bootstrap.sh.

**If new types appear** (unlikely but possible): add to
fresh-vm-bootstrap.sh's install-deps step and adjust .te to
optionally `require` the new types.

## Spike 5 — broker spawn-action gate (added 2026-04-27 night)

Not strictly blocking, but if we want admin to gate Tier-1 spawns
per-app via a rule like `qdistro.tier1.spawn:/usr/bin/firefox`, the
broker needs to know the action namespace. Verify:

```
~/.local/share/qdistro-test-venv2/bin/pytest \
 broker/test_broker_rules.py -k spawn -v
```

**Expected:** implementation tests cover the mandatory allow path and
fail-closed unknown/error paths under
`qdistro.tier1.spawn:<canonical-app-path>` shape, in line with the
existing `qdistro.clipboard.transfer:` and
`qdistro.handoff.activate:` patterns.

---

## Spike runtime estimate

| Spike | Target |
|---|---|
| 1 | 30 min — single grep |
| 2 | 1-2 hours — id -Z + (optional) staff_u migration smoke |
| 3 | 1 hour — module compile + dbus probe |
| 4 | 30 min — package install + seinfo diff |
| 5 | optional 30 min — pytest run |

Total: one afternoon clone session. The findings live in
 once the spike concludes.
