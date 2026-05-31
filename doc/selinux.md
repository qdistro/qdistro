# SELinux (Tier-1 sandbox + broker compartment)

Tier-1 is qdistro's "lightest containment that's still enforced" rung on
the isolation ladder, and the default tier for new user-silo apps. This
document covers the SELinux modules that implement tier-1 and the broker
compartment that admin-broker code runs in.

## Threat model recap

Tier-1 sits between tier 0 (no containment, fully-trusted app) and tier 2
(rootless podman with own user namespace + nested compositor).

**Tier-1 blocks:**

- An app *in the same uid* reading another app's clipboard via Wayland
 selection without going through qdshell's broker gate.
- The same app reading screen contents via a synthetic XWayland client or
 `/proc/<pid>/maps`-style introspection.
- The same app calling `setuid` / `ptrace` to escalate within the uid.
- Random `/dev` / `/sys` access beyond the narrow allowed list.

**Tier-1 does not block:**

- Kernel-level escapes — mitigated by reduced syscall surface but tier-1
 isn't seccomp-bpf.
- Side-channel attacks.
- Hardware-bus access (USB, GPU shaders) — tier-2 adds the device-cgroup
 whitelist.

A strong-but-not-perfect SELinux containment is acceptable as the default
floor under the non-adversarial threat model.

## Design — custom policy module + setexeccon wrapper

1. **Custom SELinux module** `qdistro_tier1.{te,if,fc}` cloned from
 Fedora's `sandbox.te` (the non-X variant — qdistro is Wayland-only)
 with Wayland, PipeWire, DRI, and broker rules layered on. Loaded via
 `semodule -i`.
2. **Wrapper binary `qdistro-tier1-exec`** in C that wraps
 `qdistro-secctx-exec` with a `setexeccon()` call on the exec edge.
 Two independent attestations of the same identity: SELinux type for
 enforcement, `wp_security_context_v1` tag for routing.
3. **Spawn helper `qdistro-tier1-spawn`** in bash that takes
 `(silo_user, app...)` arguments and calls the wrapper, mirroring the
 shape of `qdistro-tier3-spawn`.

### Alternatives considered

- **Fedora's `sandbox(1)` / `seunshare`.** Not packaged on Tumbleweed.
 Upstream is on life support — no Wayland support, no PipeWire
 awareness.
- **Flatpak as confinement.** Flatpak apps on a default install run in
 `unconfined_u:unconfined_r:unconfined_t`; `flatpak-selinux` only
 confines the system helper daemon, not the bubblewrap-launched child.
- **`container_t` / podman.** Already taken by tier 2 (which now adds
 `qdistro_tier2_t`, a typebounds subset of `container_t` — see the
 Tier-2 policy module section below). Tier-1 is meant to be lighter than
 tier 2 — no user namespace, no nested compositor. Reusing `container_t`
 here would muddy tier semantics.
- **Qubes-style.** Qubes' isolation primitive is the Xen domain; their
 per-template SELinux work is qrexec-scoped, not a per-app
 type-transition mechanism.

## Wrapper behaviour

`qdistro-tier1-exec`:

1. Reads the calling process context via `getcon()`. If the auto-trans
 (`.fc`-driven) already landed us in `qdistro_tier1_t`, no userspace
 work is needed — proceed straight to `execvp`.
2. Otherwise, computes the target context by inheriting the caller's
 user + role + level and swapping the type to `qdistro_tier1_t`. Avoids
 hard-coding `staff_u:staff_r:` which only works on Fedora-style
 `semanage login` migrations; on stock Tumbleweed the caller is
 `unconfined_u:unconfined_r`.
3. `setexeccon(computed_context)`. On EINVAL (the caller's user/role
 isn't bound to `qdistro_tier1_t`), fall back to
 `unconfined_u:unconfined_r:qdistro_tier1_t:s0`, the canonical
 Tumbleweed admin login context.
4. `execvp(argv[1], &argv[1])`.

## Wayland connectivity policy

The `qdistro_tier1_t` domain needs allow rules for:

```
userdom_stream_connect(qdistro_tier1_t) # /run/user/<uid>/wayland-1
files_search_tmp(qdistro_tier1_t) # /run/user ancestry on Tumbleweed
fonts_read_fonts(qdistro_tier1_t) # /usr/share/fonts
miscfiles_read_localization(qdistro_tier1_t) # /usr/share/locale
corecmd_exec_bin(qdistro_tier1_t) # /usr/bin/<app>
corecmd_exec_shell(qdistro_tier1_t) # /usr/bin/sh
dev_rw_dri(qdistro_tier1_t) # /dev/dri/* for GPU
dev_read_urand(qdistro_tier1_t) # /dev/urandom
userdom_manage_user_tmpfs_files(qdistro_tier1_t) # /dev/shm
optional_policy(`qdistro_broker_dbus_chat(qdistro_tier1_t)')
# optional user_runtime_t discovery allows hedge for refpolicy variants
```

The Tumbleweed spike found `/run/user/<uid>/*` labelled `user_tmp_t`, not
`user_runtime_t`. The live policy therefore uses `userdom_stream_connect(...)`,
which expands to the `user_tmp_t` socket pattern on this policy variant, and
keeps optional `user_runtime_t` discovery allows for systems that relabel
runtime entries after policy load.

Plus a custom interface `qdistro_broker_dbus_chat` defined in the broker's
own policy module so tier-1 apps can call `broker.RequestPermission` for
handoff and clipboard prompts.

## Broker policy module

The broker's SELinux module declares `qdistro_broker_t`,
`qdistro_broker_exec_t`, `qdistro_broker_runtime_t`,
`qdistro_broker_audit_t` (for the audit DB). It exports
`qdistro_broker_dbus_chat()` and `qdistro_broker_read_runtime()`
interfaces.

The broker exec lives at `/usr/libexec/qdistro/` (outside Tumbleweed's
`lib_t` glob, so the `.fc` rule is deterministic under restorecon) and is
labelled `qdistro_broker_exec_t` via the `.fc`. The systemd unit's
`ExecStart` points at the script directly so the kernel's execve hook
reads the script's label, and the broker enters `qdistro_broker_t` via
`init_daemon_domain` transition.

The broker's allow-set, derived from `audit2allow` against representative
workloads:

- `dbus_system_bus_client(qdistro_broker_t)` — connect + sock_file write
 + system-bus message routing.
- `dbus { acquire_svc }` against `system_dbusd_t` — Tumbleweed's
 `dbus_system_bus_client(...)` interface body doesn't bundle
 `acquire_svc`, so the broker needs an explicit allow to
 `RequestName('org.qdistro.AdminBroker1')`. Without this, dbus-broker
 rejects RequestName with a generic policy denial and no AVC is logged.
- `files_search_var_lib(qdistro_broker_t)` + `files_manage_var_lib_*` for
 `/var/lib/qdistro/{audit,approvals,cache}/*.sqlite`.
- `etc_t:dir watch + etc_t:file watch` for `/etc/qdistro/rules.d` inotify
 reload.
- `domain_read_all_domains_state(qdistro_broker_t)` +
 `domain_getattr_all_domains(qdistro_broker_t)` for the broker's
 caller-identity layering (reads `/proc/<pid>/{stat,exe,attr/current,
 cgroup}` for any caller).
- `logging_read_audit_log` + `logging_search_logs` for
 `/var/log/audit/audit.log` follow.
- `corecmd_exec_bin` (Python's subprocess machinery) + `tmpfs_t:file
 { execute read write }` (SQLite `mmap_size` PRAGMA + Python tempfile
 module).
- `self:process { signal sigkill sigchld getsched }` + `self:fifo_file
 rw_fifo_file_perms` + `self:capability sys_resource` for SIGHUP-driven
 reload.
- `optional_policy(auth_use_pam)`, `optional_policy(systemd_dbus_chat_
 logind)`, `optional_policy(fprintd_dbus_chat)` for the admin-approval
 auth path.

## Pwd-daemon policy module

The pwd daemon runs as a dedicated `qdistro-pwd` uid + group; vault dir +
audit dir owned 0700 by that uid; `SupplementaryGroups=tss` for TPM
access. The SELinux module `qdistro_pwd.{te,if,fc}` declares
`qdistro_pwd_t`, `qdistro_pwd_exec_t`, `qdistro_pwd_var_t`,
`qdistro_pwd_audit_t`; labels `/usr/libexec/qdistro/qdistro_pwd_daemon.py`,
the vaults dir, and the audit dir; transitions via `init_daemon_domain`;
allows `dbus_system_bus_client` + `acquire_svc`, `var_lib` management for
the two typed dirs, `/proc` identity layering, optional `dev_rw_tpm` and
`policykit_dbus_chat`.

The audit2allow harvest added `miscfiles_read_generic_certs` (Python ssl
init), `cgroup_t:dir search + :file getattr` (`/proc/<pid>/cgroup`
readers), and `self:capability sys_ptrace` for cross-uid
`/proc/<other-uid>/exe` readlink (the SELinux check happens before the
kernel cap check).

## Tier-2 policy module

`selinux/tier2/qdistro_tier2.{te,if,fc}` now exists (it previously did
not; tier-2 relied solely on podman's default `container_t`). The
follow-up wanted a qdistro-specific policy that constrains the tier-2
podman workload — image-fs writes, pipewire socket access — the way the
tier-1 policy constrains its workloads.

It declares one domain, `qdistro_tier2_t`, built as a **member of the
`container_domain` and `svirt_sandbox_domain` attributes** and then
capped:

```
typeattribute qdistro_tier2_t container_domain;
typeattribute qdistro_tier2_t svirt_sandbox_domain;
typebounds container_t qdistro_tier2_t;
```

The attribute membership is load-bearing: `typebounds` does *not*
inherit `container_t`'s allows — a bounded type gets only its own rules,
capped at the parent — so a from-scratch bounded type with a few
hand-written allows could not even exec the image entrypoint or load
libraries (the container would fail to start). Joining the two
attributes gives `qdistro_tier2_t` the same functional file/exec
baseline the working `container_t` path uses; `typebounds container_t`
then guarantees it can never *exceed* the default container surface.

The **narrowing** is the set of `container_t` attributes
`qdistro_tier2_t` deliberately does NOT join — the network attributes
(`corenet_unconfined_type`, `corenet_unlabeled_type`,
`container_net_domain`, `sandbox_net_domain`) and the kernel-state
attributes (`can_dump_kernel`, `can_receive_kernel_messages`,
`kernel_system_state_reader`). It DOES still join `mcs_constrained_type`
(which `container_t` also carries), because that attribute enforces
per-container MCS-category isolation — dropping it would *broaden*
cross-container access, not narrow it. Omitting the network attributes is
what strips the unconfined-network surface (matching the launcher's
default `--network=none`); the module then pins the practical net socket
surface off with `neverallow` assertions so a later `allow` can't re-add
it:

```
neverallow qdistro_tier2_t self:tcp_socket { create listen };
neverallow qdistro_tier2_t self:{ udp rawip sctp dccp icmp } socket create;
neverallow qdistro_tier2_t self:{ netlink_route netlink_tcpdiag packet } socket create;
```

(The structural guarantee is that the network *attributes* aren't
joined; the `neverallow`s belt-and-braces the transport/raw/diag classes
most likely to be re-added by a careless future `allow`.) A workload
that needs outbound (`TIER2_NETWORK=slirp4netns`) must run as
stock `container_t` — an explicit, auditable downgrade. The follow-up's
"image-fs writes" and "pipewire socket access" points are handled by the
launcher's existing posture (image rootfs `--read-only`; only the
specific `pipewire-N` sockets that exist at spawn time are bound, with
no dbus/pulse/gpg/ssh-agent), not by widening this domain.

Build path differs from tier-1 on purpose. tier-1 is refpolicy m4 built
through `/usr/share/selinux/devel/Makefile`; the dev/host carries
`container-selinux` (which supplies `container_t`, the `container_domain`
/ `svirt_sandbox_domain` attributes, `container_file_t`, the
`container_*` booleans) but not the full `selinux-policy-devel` m4 header
set, so the tier-2 module is written in **kernel policy language** and
built with the base `checkmodule -M -m` + `semodule_package` toolchain
(`make` / `make check` in `selinux/tier2/`). The `.if`/`.fc` are kept in
refpolicy style for symmetry but are not consumed by that build path;
the whole policy is in the `.te`.

**Engagement is deferred** and needs two things, neither landed:
(1) `spawn-tier2.sh` must pass
`--security-opt label=type:qdistro_tier2_t` to podman; and (2) the
launcher's socket/dir binds (today plain `-v ...:rw`, host-labelled
`user_tmp_t`) must gain `:z`/`:Z` so they relabel to `container_file_t`
that the domain can reach — plus a label strategy for the
`qdwin-shell.so` bind (a `:z` would mutate a host library label). Both
are launcher changes, capability-gated behind a clean enforcing-mode AVC
pass on a VM (none was available when the module landed). Until then
tier-2 keeps running as stock `container_t`, so loading the module is a
no-op. What is validated today: the module compiles (`make check`), and
`sesearch` confirms the `neverallow` block can't collide with the joined
attributes at load time (the net-socket perms come solely from the
omitted network attributes). What needs the enforcing VM: the bind
relabel wiring, a zero-new-AVC run of the nested weston under
`qdistro_tier2_t`, and the load-time `typebounds`/`neverallow`
resolution (`semodule -i` is not installed on the dev host). See
`selinux/tier2/README.md` for the full validated-vs-deferred split.

## dbus-broker reload requirement

On first install of `/etc/dbus-1/system.d/org.qdistro.AdminBroker1.conf`
on a host where dbus-broker is already running, dbus-broker doesn't
re-read the directory until it is told to reload its config. A broker
restart immediately after install would fail `RequestName` because the
policy still excludes the new bus name.

Mitigation: `qdistro-dbus-reload.service` is a `Type=oneshot` unit ordered
`After=dbus.service dbus-broker.service Before=qdistro-admin-broker.service`
with `RemainAfterExit=yes`. ExecStart calls the `org.freedesktop.DBus.ReloadConfig`
method via `busctl` first — on Tumbleweed dbus-broker 35.x a `systemctl reload
dbus-broker.service` returns success but does not actually re-read the policy,
whereas dbus-broker implements `ReloadConfig` faithfully. If the `busctl` call
fails for any reason (e.g. `busctl` not installed) it falls back to `systemctl
reload dbus-broker.service`, then `systemctl reload dbus.service`, and finally
`true` so the oneshot always reports success. It is pulled in via
`Wants=qdistro-dbus-reload.service` on `qdistro-admin-broker.service`.
It fires unconditionally before the broker activates, so the mitigation is
deterministic whether or not it's needed: load-bearing on first install and
on first boot of a baked image, a cheap no-op on subsequent boots — keep
permanent.

## Audit integration

AVC denials land in `/var/log/audit/audit.log` with `scontext` fields
naming the qdistro subject types. The broker consumes them via an
audispd plugin script that runs as a long-lived child of auditd via
`/etc/audit/plugins.d/qdistro-audisp.conf`. Each line on stdin is parsed
by `qdistro_audisp_parser.parse_avc_line`; AVC records whose subject
context names a `qdistro_*_t` domain are forwarded to the broker via
`org.qdistro.AdminBroker1.RecordSelinuxAvc`. The broker validates the
caller is uid 0, re-checks the subject prefix, and writes one audit row
with `action='selinux.avc:<tclass>:<perms>'`, `source='selinux_avc
verdict=…'`, and the new `selinux_subj_type` column populated.

audispd uses `format=string` (libauparse dependency is heavy and the
human-readable line is easy to parse). The plugin survives broker
restarts by lazy-reconnecting with exponential backoff; AVC records
during the broker-down window are dropped — they're still in the audit
log for forensic recovery.

## Enforcing-mode harness

A test harness flips SELinux to enforcing, runs representative workloads
through `qdistro-tier1-spawn` (e.g., `id && cat /etc/passwd`,
`dbus-send --session --print-reply ListNames`), and diffs the new AVC
count. **PASS only when delta is 0.** On dirty: dump unique AVC
signatures (`perm + tcontext + tclass`) for `audit2allow` consumption.

Iteration loop: any new AVC the dump surfaces gets added as a named
refpolicy interface (preferred over a raw `allow`) so Tumbleweed's
selinux-policy evolution doesn't silently break the module on a future
`zypper dup`.

Operational note: `virt_qemu_ga_t` (the qga's SELinux domain) cannot
operate in enforcing mode. Drive enforcing-mode tests via SSH where root
lands in `unconfined_u:unconfined_r:unconfined_t`.

## File context labelling

A `qdistro-tier1-spawn` invocation looks like:

```
qdistro-tier1-spawn user1 -- firefox
 └─ qdistro-secctx-exec --sandbox-engine qdistro.tier1 \
 --app-id qdistro.tier1.user1 \
 --instance-id tier1-user1-$$ \
 -- qdistro-tier1-exec firefox
 └─ setexeccon("staff_u:staff_r:qdistro_tier1_t:s0")
 └─ execvp("firefox", ...)
```

qdshell's `parse_silo_from_secctx` returns `tier1-user1`. How the broker
treats the secctx strings on a `CheckPermission` / `RequestPermission`
call depends on the **permission-lineage** posture
(`issues/qdistro/permission-lineage-findings.md`, broker.conf
`lineage_enforce`):

- **`lineage_enforce = true`**: the broker resolves the live caller pid to
  an authoritative subject (`qdistro_resolver`) and uses the
  **launcher-attested** `sandbox_engine` / `app_id` from the broker launch
  record (`RegisterLaunch`, Phase 1) instead of the client-supplied
  strings. A caller with no launch record — or one whose live
  `/proc/<pid>/attr/current` / uid / exe / cgroup diverges from the
  record — resolves to the `unknown` subject (empty `sandbox_engine` /
  `app_id`), so a forged secctx tag can only ever *fail* a rule selector,
  never satisfy one. This is the defence-in-depth against a process that
  obtained the secctx tag without the matching launch record / SELinux
  transition.
- **`lineage_enforce = false`** (current default, "shadow"): the broker
  matches rules on the client-supplied `sandbox_engine` / `app_id` (the
  historical behaviour — see finding P0-1), but the resolver still runs
  and logs to the journal whenever the claimed identity diverges from the
  resolved one, so the gap is observable before enforcement is switched
  on. Until launch-record registration is wired across all tiers, treat
  `app_id` / `sandbox_engine` as advisory on this path; the
  kernel-anchored selectors (`uid`, `exe`, argv) remain trustworthy.

For hosts ready to fail closed, `deploy/etc/qdistro/broker-hardened.conf`
sets `secctx_launcher_gated`, `lineage_enforce`, `identity_strict`, and
`require_silo_active` to true. It is shipped as an explicit profile rather
than replacing the default until all tier launchers publish broker launch
records in the installed image.

Note: the **qdshell-mediated** clipboard / handoff gates already
re-verify the underlying app identity per call via `VerifyClientIdentity`
(Option B, below) regardless of `lineage_enforce`.

Tier-1 spawn authorization is mandatory. Before the wrapper enters
`qdistro-tier1-exec`, it calls broker `CheckPermission` with action
`qdistro.tier1.spawn:<canonical-app-path>` and fails closed unless the
reply is an explicit `allow`. Broker errors, missing D-Bus tooling,
empty replies, `unknown`, and `deny` all block launch. Expected Tier-1
apps therefore need admin-authored allow rules in
`/etc/qdistro/rules.d/`.
For this action namespace, broker `CheckPermission` is rules-only:
approval-cache rows and hook verdicts do not authorize launch.

This independent verification applies to **direct broker authorization**
(the broker resolves the D-Bus caller pid and reads its SELinux context
itself). For **qdshell-mediated decisions** — clipboard set/receive gates
and handoff activation — Option B now re-verifies the underlying
application identity per call: qdwin captures each client's
`(pid, starttime, uid, exe, selinux_label)` at secctx-bind time and
forwards it on `qdwin_shell_v1.toplevel_peer_identity` (protocol v22);
qdshell relays it to broker `VerifyClientIdentity`, which re-resolves the
live process against `/proc` (`/proc/<pid>/stat` field-22 starttime,
`/proc/<pid>/status` uid, `/proc/<pid>/exe`, `/proc/<pid>/attr/current`).
The same-silo allow short-circuit fires only when **both** endpoints
verify; any mismatch or missing endpoint falls through to default-deny.

Caveat specific to the SELinux axis: it is checked only when both the
forwarded label and the live `/proc/<pid>/attr/current` are non-empty, so
on a kernel with SELinux off / unconfined the label axis is *skipped*
(not failed). The uid and exe axes are likewise enforced only when both
sides supply a value; the always-enforced anchor is the `/proc` field-22
starttime (anti-PID-reuse), so the hard verification floor is
`(pid, starttime)`. This is sufficient because `VerifyClientIdentity` and
the three gate methods are denied to non-admin / default-context users by
D-Bus policy (`org.qdistro.AdminBroker1.conf`) — only the admin uid and
root may call them — and starttime is kernel-attested. The
broker trusts qdshell's per-call `identity_verified` flag; it is qdshell
that requires BOTH endpoints to verify before setting it. See
`todo/decisions/secctx-identity-contract.md` (Option B) and
`todo/gpt-review/wider-codex-review.md` finding #2 (resolved).
