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
- **`container_t` / podman.** Already taken by tier 2. Tier-1 is meant
 to be lighter than tier 2 — no user namespace, no nested compositor.
 Reusing `container_t` here would muddy tier semantics.
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
userdom_stream_connect_user_runtime(qdistro_tier1_t) # /run/user/<uid>/wayland-1
userdom_search_user_runtime_root(qdistro_tier1_t) # /run/user/<uid>
fonts_read_fonts(qdistro_tier1_t) # /usr/share/fonts
miscfiles_read_localization(qdistro_tier1_t) # /usr/share/locale
corecmd_exec_bin(qdistro_tier1_t) # /usr/bin/<app>
corecmd_exec_shell(qdistro_tier1_t) # /usr/bin/sh
dev_rw_dri(qdistro_tier1_t) # /dev/dri/* for GPU
dev_read_urand(qdistro_tier1_t) # /dev/urandom
userdom_manage_user_tmpfs_files(qdistro_tier1_t) # /dev/shm
optional_policy(`qdistro_broker_dbus_chat(qdistro_tier1_t)')
```

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
 `RequestName('com.qdistro.AdminBroker1')`. Without this, dbus-broker
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

## dbus-broker reload requirement

On first install of `/etc/dbus-1/system.d/com.qdistro.AdminBroker1.conf`
on a host where dbus-broker is already running, dbus-broker doesn't
re-read the directory until SIGHUP. A broker restart immediately after
install would fail `RequestName` because the policy still excludes the
new bus name.

Mitigation: `qdistro-dbus-reload.service` is a `Type=oneshot` unit ordered
`After=dbus.service Before=qdistro-admin-broker.service` with
`RemainAfterExit=yes`. ExecStart wraps a single `systemctl reload
dbus-broker.service`. It is pulled in via
`Wants=qdistro-dbus-reload.service` on `qdistro-admin-broker.service`.
Redundant on cold boot (dbus-broker already parsed the .conf at startup),
load-bearing on first install. Costs nothing on every subsequent boot —
keep permanent.

## Audit integration

AVC denials land in `/var/log/audit/audit.log` with `scontext` fields
naming the qdistro subject types. The broker consumes them via an
audispd plugin script that runs as a long-lived child of auditd via
`/etc/audit/plugins.d/qdistro-audisp.conf`. Each line on stdin is parsed
by `qdistro_audisp_parser.parse_avc_line`; AVC records whose subject
context names a `qdistro_*_t` domain are forwarded to the broker via
`com.qdistro.AdminBroker1.RecordSelinuxAvc`. The broker validates the
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

qdshell's `parse_silo_from_secctx` returns `tier1-user1`; the broker
independently verifies via `/proc/<pid>/attr/current` that the process is
actually in `qdistro_tier1_t`. If they disagree, broker's
`CheckPermission` denies — defence in depth against a process that
managed to get the secctx tag without the SELinux transition.
