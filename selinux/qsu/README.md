# qsu — SELinux confinement for the privileged-exec service

Confines `qdistro-root-exec.service` (the qsu root-side delegator,
`qsu/qdistro_root_exec.py`) into a dedicated domain and gives the
setuid child an explicit type transition. This is the enforcing-mode
complement to the permissive-mode qsu scenarios
(`tests/integration/permissions-gui/43-54`), which all ran against an
`unconfined_service_t` broker (scenario 51 leaked that label).

## Files

- `qdistro_qsu.te` — domains (`qdistro_root_exec_t`, `qsu_child_t`,
  `qdistro_root_exec_runtime_t`), the `init_daemon_domain` transition,
  the setuid-child `domain_auto_trans`, and the analysis-predicted
  allow rules (`/proc/<pid>/{exe,stat}` reads, the
  `/run/dbus/system_bus_socket` connect leg, the broker dbus-chat edge).
- `qdistro_qsu.if` — exported interfaces (`qdistro_qsu_connect`,
  `qdistro_qsu_read_runtime`) for a future confined qsu CLIENT domain.
  No backticks/apostrophes in comments (m4 include-path discipline).
- `qdistro_qsu.fc` — labels `/usr/local/lib/qdistro/qdistro_root_exec.py`
  as `qdistro_root_exec_exec_t` and `/run/qdistro-root-exec/*` as
  `qdistro_root_exec_runtime_t`.
- `Makefile` / `install-policy.sh` — build + `semodule -i` driver,
  same shape as `selinux/tier1` and `selinux/broker`. Installs the
  `qdistro_broker` module first (this module gen_requires
  `qdistro_broker_t` and calls `qdistro_broker_dbus_chat()`).

## What the analysis predicts under enforcing

From `todo/issues/qsu/qsu-selinux-enforcing-untested.md` §"Why it matters":

1. **`/proc/<pid>/exe` + `/proc/<pid>/stat` reads.** The service anchors
   caller identity (SO_PEERCRED + exe + starttime) and re-reads it to
   close the connect→request TOCTOU window. Granted by
   `domain_read_all_domains_state` + `domain_getattr_all_domains` +
   `self:capability sys_ptrace` (cross-uid `/proc` readlink).
2. **`/run/dbus/system_bus_socket` connect.** The service calls
   `RequestPermissionAs` / `WaitForDecision` on the SYSTEM bus. Granted
   by `dbus_system_bus_client` + the `system_dbusd_var_run_t` sock_file
   connect leg + `qdistro_broker_dbus_chat()`.
3. **The setuid `subprocess.Popen(user=uid, ...)` child.** Without a
   type transition the child would silently inherit `qdistro_root_exec_t`
   (sandboxing an arbitrary admin-approved command in the delegator
   domain). `domain_auto_trans(qdistro_root_exec_t, {bin_t,shell_exec_t},
   qsu_child_t)` makes the child run in `qsu_child_t` — explicit and
   auditable. The security gate is the admin APPROVAL, not the child
   domain, so `qsu_child_t` is intentionally a thin
   ordinary-target-user-process domain.

## The ExecStart change this module depends on

`qdistro-root-exec.service` now ExecStart's the script DIRECTLY
(`/usr/local/lib/qdistro/qdistro_root_exec.py`, installed mode 0755 with
a `#!/usr/bin/python3` shebang) instead of `/usr/bin/python3 <script>`.
The kernel takes the domain-transition decision on the FIRST execve
target; with `python3 <script>` that is the interpreter (`bin_t`) and the
script's label never fires the transition. Executing the labelled script
directly makes it the first execve target, so the daemon lands in
`qdistro_root_exec_t`. Mirrors `qdistro-admin-broker.service`.

## Compile-check status

`checkmodule` and `semodule_package` are present on the dev host, but a
FULL refpolicy build needs `selinux-policy-devel` (the `/usr/share/
selinux/devel/Makefile` driver + the support `.spt` macros + the core
`files`/`domain`/`logging` interface layers). On the current dev host
only a PARTIAL `devel/include` tree is present (contrib/services/
distributed `.if` headers, no Makefile, no `.spt`, `semodule` not
installed), so `make qdistro_qsu.pp` cannot run here and is **PENDING a
host/VM with `selinux-policy-devel`** — the same prerequisite the
`tier1`/`broker` Makefiles already document.

Structural validation done locally:
- balanced parens and m4 quote pairs in `.te`/`.if`;
- `.if` comments are quote-free (m4 include-path safe);
- every refpolicy interface called is either already used by the
  known-good `tier1`/`broker` modules or is a canonical refpolicy
  interface (`domain_type`, `domain_entry_file`,
  `application_executable_file`, `logging_send_syslog_msg`,
  `files_read_usr_files`).

To compile + load on a provisioned VM:

```bash
cd selinux/qsu
make                 # builds qdistro_qsu.pp via the devel Makefile
sudo bash install-policy.sh   # installs qdistro_broker first, then this
```
