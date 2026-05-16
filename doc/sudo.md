# sudo replacement (qsu)

A qdistro-native replacement for `sudo` that defaults to **no caching** and
routes every privilege-escalation through admin approval with explicit scope
selection.

## Why replace sudo

- sudo's default `timestamp_timeout = 15min` means one approval opens a
 15-minute window where *any* sudoable command runs without further
 prompting. Convenient, but a background malicious action at minute 14 is
 indistinguishable from the user's intent.
- sudo's approval model is "type your password"; there is no admin-in-the-
 loop for a single-tenant-with-admin model like qdistro's.
- The scope of an approval (once / 1h / forever) cannot be picked at
 approval time.

qdistro replaces sudo with **`qsu`**, which:

- Defaults to **single-command approval, zero caching.**
- Lets admin pick scope at approval time: once / 1h / 24h / forever /
 forever-this-exact-command.
- Routes prompts through the existing `qbus-admin` broker (which can
 surface on tty3 or phone).
- Audits every escalation.
- Can escalate to any user admin approves, not just root.

A **sudo compat shim** remains so scripts that hard-code `sudo` keep
working (`/usr/bin/sudo` is a wrapper that invokes `qsu` with converted
args).

## Architecture

```
 +-----------------+ +----------------------+ +-------------------+
 | user | | qsu client | | qdistro-root- |
 | (some silo uid) |---> | (runs as caller) |---> | exec.service |
 | | | - parses args | | (runs as root) |
 +-----------------+ | - streams tty | | - policy check |
 | - returns exit code | | - awaits approval|
 +----------^-----------+ | - execs command |
 | | - streams output |
 | pty + stdio +--------+----------+
 +-----------------+ |
 |
 +-----------v----------+
 | qbus-admin broker |
 | (policy engine) |
 | - apply rules |
 | - consult approvals |
 | - polkit prompt if |
 | needed |
 +-----------+----------+
 |
 +-----------v----------+
 | admin polkit agent |
 | (tty3 OR phone) |
 | - approval dialog |
 | with scope picker |
 +----------------------+
```

### `qsu` client

Not setuid. A normal user-space tool.

- Parses `qsu [-u target_user] <command> [args...]`.
- Opens a pty pair, connects to `qdistro-root-exec.service` over a
 per-invocation socket.
- Sends: target_user, argv, cwd, env (filtered per policy), optional
 `--reason="..."` text.
- Streams the pty through.
- Exits with the remote command's exit code.

### `qdistro-root-exec.service`

Runs as root (or with the necessary capabilities).

- systemd unit, started on demand.
- Accepts connections on a socket.
- For each request:
 1. Peer credentials + `/proc/<pid>/exe` + SELinux label identify the
 caller — layered identity.
 2. Constructs a polkit action `com.qdistro.sudo.exec` with detail
 annotations (target_user, argv, caller details).
 3. Polkit consults `qbus-admin` rules and the approval cache.
 4. If cache miss and no auto-allow rule: polkit agent prompts (tty3 or
 phone).
 5. On approval, admin records the scope (stored in the approval cache).
 6. The service `execve()`s the command as `target_user` with sanitized
 env.
 7. stdio / pty forwarded to the caller.
 8. Command exits; the service logs an audit record.

### Approval cache

Persistent storage in `/var/lib/qdistro/approvals/`:

```
approvals.sqlite
 id | caller_user | target_user | match_kind | match_value
 | expires_at | created_at | approver
```

- `match_kind`: `argv_exact` | `argv_basename` | `argv_prefix` |
 `exe_only` | `always`.
- `match_value`: the pattern.
- `expires_at`: timestamp, or NULL for persistent ("forever").

On each request the service queries this table. Match → no prompt, just
execute. No match → polkit prompt.

Storage is protected: file has strict mode (0600 by root), on a subvolume
snapshotted per filesystem policy.

## Approval scopes

When the polkit agent prompts, admin sees:

```
+--------------------------------------------------------------+
| Privilege escalation request |
| |
| User: work-user (blue) |
| Target: root |
| Command: /usr/bin/systemctl restart nginx |
| Cwd: /home/work-user/projects/web |
| Reason: "restart after config change" |
| |
| How long? |
| ( ) Just this once <default> |
| ( ) 1 hour |
| ( ) 24 hours |
| ( ) Forever |
| ( ) Forever, but only this exact command |
| ( ) Forever, any `systemctl restart` on work-user |
| |
| [ Deny ] [ Approve ] |
+--------------------------------------------------------------+
```

Scope granularity:

| Scope | Cache match-kind | Behaviour |
|------------------------------------|------------------|--------------------------------------------------------------------|
| Just this once | n/a | No cache write. |
| 1h / 24h | `exe_only` + ttl | Time-bounded. |
| Forever (any command from exe) | `exe_only` | Broad — any argv from same caller_exe; rare. |
| Forever this exact command | `argv_exact` | Most common forever scope. |
| Forever matching basename | `argv_basename` | Loosens path; tightens command identity. |
| Forever matching prefix | `argv_prefix` | Approves a command + any args. |

Scopes always include the **caller_user + target_user** pair — approvals
are not shared across silos.

### Argv pinning and the delegated path

The broker's `RequestPermissionAs` (used by qsu) originally forbade every
long-lived scope under the "the delegator's peer identity is
unauthenticated for future calls" concern. Argv pinning invalidates that
worry: a `forever_argv` approval of `[apt-get, update]` only matches
future delegated requests with that EXACT argv tuple — a different process
at the same uid asking `[apt-get, install, foo]` still re-prompts. The
argv-pinned subset is therefore permitted on delegated calls and is what
makes qsu's caching worth anything in practice.

## Default — no caching, ever

Unlike sudo's 15-minute timestamp, **qsu's default is no-cache**. If admin
picks "Just this once," the approval is not persisted; the next invocation
prompts again. Admin can opt into longer scopes per approval, but the path
of least resistance is "prompt every time."

## Admin-authored policies (pre-approval)

Admin can author rules that pre-approve certain escalations without a
prompt:

```yaml
- match:
 action: com.qdistro.sudo.exec
 caller_user: admin
 argv_prefix: ["/usr/bin/systemctl"]
 action: allow
 scope: session

- match:
 action: com.qdistro.sudo.exec
 caller_user: dev-user
 target_user: root
 argv_exact: ["/usr/bin/apt-get", "update"]
 action: allow
 scope: persistent
```

Same rule format as broker permissions. Admin's own `sudo` use typically
falls into a broad pre-approval rule (because admin typing fingerprints
every single time is overkill for the TCB-grade user). Regular users
default to always-prompt.

## Phone approval

If admin is not on tty3 (laptop closed, away), polkit routes the prompt to
the phone. The scope picker renders on the phone. The user
biometric-approves; a signed response returns.

Useful for "approve my remote `systemctl restart` without walking to the
laptop."

## Audit log

Every qsu invocation writes a row to the broker's audit DB:

```
timestamp, caller_user, caller_pid, caller_exe, target_user, argv,
cwd, decision (allow|deny), scope_source (prompt|rule|cache),
approver (if prompt: admin or phone id), exit_code, duration
```

The `argv` column is shipped end-to-end: `AuditLog.log()` accepts the
argv list, `broker.ListHistory()` carries it across the wire, and the
admin app's History tab renders it. The History tab also surfaces
`caller_exe` so an admin reviewing yesterday's qsu activity can spot when
the same argv came from a different binary path (e.g., someone exec'd a
renamed copy of `apt-get` to slip through a `forever_basename` approval).

The admin panel has a "Privilege escalations" view: searchable, filterable
by user / time / command.

## Sanitized environment

The service strips / overrides before `execve`:

- `PATH` set to a fixed safe value.
- `LD_*`, `PYTHONPATH`, etc. removed to prevent library-injection tricks
 (same as sudo's `env_reset`).
- `TERM`, `LANG` preserved.
- Caller can pass `--keep-env VAR` to preserve specific variables
 (policy-gated).

## Target user

`qsu -u dev-user python3 script.py` runs the command as dev-user's uid.
Useful for admin dropping to a regular silo, or one silo running something
as another silo (admin-approved). Not just for root.

## Interactive commands / pty

The service forwards a pty:

- `qsu bash` opens a shell as the target user with an interactive TTY —
 useful but **explicitly opens a long-lived escalated session**; admin
 approval is for the whole shell, so the "this once" scope covers the
 whole shell session until exit. For shells the scope picker should nudge
 to short-duration, with a warning banner explaining the long-running
 nature.
- `qsu vim /etc/thing` works with full cursor/keys forwarding.
- Output of non-interactive commands streams normally.

## Kernel-level belt-and-suspenders

- User sessions run with `no_new_privs=1` where possible; SUID binaries
 cannot gain privileges in the user session. Escalation must go through
 qsu.
- The capability bounding set is trimmed for user sessions — the kernel
 refuses to grant `CAP_*` to user processes even if someone finds a SUID
 exploit.

## sudo compat wrapper

`/usr/bin/sudo` becomes a small wrapper that translates common sudo
invocations to qsu:

- `sudo cmd args...` → `qsu cmd args...`
- `sudo -u user cmd args...` → `qsu -u user cmd args...`
- `sudo -i` → `qsu -u root bash -l`
- `sudoers` file is ignored; qsu consults its own rules.

Admin can remove the wrapper entirely to make sudo invocations fail
loudly — encourages migrating scripts to `qsu`.

## Relationship to qbus-admin

qsu is **one more caller** on the qbus-admin broker. It reuses:

- The polkit namespace (`com.qdistro.*`).
- The policy rules engine.
- The admin polkit agent UI (extended with the scope picker).
- Phone approval routing.
- Audit log infrastructure.

No new subsystem — qsu is a thin client on top of the broker stack.
Think of it as `pkexec++` with a richer scope model and approval cache.

## Test coverage

The qsu surface is covered by three test layers:

| Layer | Location | What it pins |
|---|---|---|
| Unit | `tests/unit/test_broker_argv.py`, `tests/unit/test_qsu_handler.py`, `tests/unit/test_admin_cache.py` | Scope→match_kind mapping, argv selectors in rules, `_USERNAME_RE`, cache lookup priority |
| Integration (bats) | `tests/integration/vm/s57-qsu-argv-scopes.sh`, `s58-qsu-real-flow.sh` | D-Bus argv-scope round-trips for all four match_kind shapes; real qsu binary end-to-end one allow + one re-prompt |
| Integration (GUI scenarios) | `tests/integration/permissions-gui/43-…` through `54-…` | Admin-UX + audit + security invariants — the surface humans actually touch |

The GUI scenarios (43-54) specifically pin behavior the bats tests
cannot see — admin clicks the argv-aware Forever radios, the audit
`ListHistory` argv field carries a lossless `as` array, the TUI
shows `Argv:` on its own line, and the security-critical guards
(delegated-scope rejection, target_user in action key, username
validation, in-flight cap, env sanitization) fail closed against
realistic abuse.

## What's implemented vs planned

| Area | Status |
|---|---|
| qsu client, qdistro-root-exec service, broker action `qsu.exec:<target>` | LIVE (validated by s57 + s58 + 43-54) |
| Argv-aware approval scopes (`forever_argv` / `forever_basename` / `forever_prefix`) | LIVE on delegated path; admin app + TUI radios wired |
| Delegated guard rejecting `1h` / `24h` / `forever` / `forever_exe` on qsu | LIVE (`_DELEGATED_FORBIDDEN_SCOPES` in broker) |
| Per-uid in-flight cap (MAX_INFLIGHT_PER_UID=4) | LIVE |
| Username validation (`_USERNAME_RE`) | LIVE |
| Env sanitization (PATH reset, LD_* / PYTHONPATH stripped) | LIVE |
| Audit `caller_exe` + lossless argv via `ListHistory` | LIVE; `caller_exe` resolves to `/usr/bin/python3.X` because qsu wrapper exec's into python — see todo `qsu-wrapper-loses-name` |
| pty forwarding (interactive `qsu vim`, `qsu bash`) | PLANNED (qsu v1 is non-pty; spec/21 follow-up) |
| stdin forwarding | PLANNED (v1 uses `/dev/null`) |
| `--keep-env VAR` policy-gated env passthrough | PLANNED |
| Sudo compat wrapper (`/usr/bin/sudo` → `qsu`) | PLANNED (Phase-2 per qsu.py docstring) |
| Phone approval routing for qsu prompts | PLANNED |
| Polkit action namespace (`com.qdistro.sudo.exec`) | PARTIAL — the broker uses `qsu.exec:<target>` natively; polkit-action shim is not wired |
