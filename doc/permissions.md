# Permissions, policy, workflows

## Authorization stack

```
 user app requests something sensitive
 |
 v
 qdistro_app SDK / caller in the user's session
 |
 v
 (D-Bus call to org.qdistro.AdminBroker1 on the system bus)
 |
 v
 qdistro-admin-broker: policy pipeline
 1. Declarative rules (YAML only) -> allow / deny
 2. Python hooks (if rules inconclusive) -> same actions
    [not reachable on a stock install — see "Python hooks" below]
 3. admin approval queue (the Qt admin app / admin TUI decide)
 |
 v
 response: allow / deny / unknown
 |
 v
 caller acts on response
```

**The relationship to polkit runs the other way round.** The broker contains no
polkit code and never escalates to a polkit prompt; step 3 is the broker's own
approval queue. It is `qdistro-polkit-agent` that faces polkit: it registers as
a polkit `AuthenticationAgent`, receives *upstream* polkit actions (mostly
`org.freedesktop.*`), maps each action ID into a qdistro-namespaced string
(`org.freedesktop.X` → `qdistro.X`; anything else → `qdistro.external.<id>`),
and then calls the broker's `RequestPermission` to get a decision.

A real `org.qdistro.*` polkit namespace does exist, but it is **narrow and
per-subsystem**, and it does not cover the cross-silo verbs this page used to
advertise. Twenty action IDs exist in the tree:

| Source | Actions |
|---|---|
| `pwd/org.qdistro.pwd.policy` | 1 — `org.qdistro.pwd.unlock` |
| `print/org.qdistro.print.policy` | 5 — `print.{access,attach-usb,detach-usb,cancel-job,purge-jobs}` |
| generated inline by `install-tier3-for-vm.sh` | 2 — `tier3.{spawn,cleanup}` |
| generated inline by `install-tier5-for-vm.sh` | 2 — `tier5.{spawn,cleanup}` |
| `qdbrowser/polkit/org.qdistro.qdbrowser.policy` | 10 — tabs/downloads/cookies/history/bookmarks/page-extract. **Present in the repo but installed by nothing**: no script copies it to `/usr/share/polkit-1/actions`, and `qdbrowser`'s `pyproject.toml` packages only the Python package. |

So **ten actions ship and ten do not**. Each is scoped to a single subsystem;
none is a cross-silo policy verb. They are not all the same shape:
only the tier-3 and tier-5 actions carry an
`org.freedesktop.policykit.exec.path` annotation, i.e. are true `pkexec` helper
gates. `pwd.unlock` and the five print actions define an authorization ID with
no exec path, checked by the owning daemon; qdbrowser's ten (uninstalled) are
checked from application code via `pkcheck`.

**These actions do reach the qdistro agent**, and that follows from their
`<defaults>`, not from any `.rules` file. An action invokes whatever
authentication agent is registered whenever its applicable default is
`auth_admin` / `auth_admin_keep`:

| Action | `allow_any` | `allow_active` |
|---|---|---|
| `pwd.unlock` | `auth_admin` | `auth_admin_keep` |
| `print.{attach-usb,detach-usb,cancel-job,purge-jobs}` | `auth_admin` | `auth_admin_keep` |
| `print.access` | `auth_admin` | `yes` |
| `tier3.{spawn,cleanup}` | `auth_admin_keep` | `auth_admin_keep` |
| `tier5.{spawn,cleanup}` | `auth_admin_keep` | `yes` |

So for an active admin session, eight of the ten prompt through the agent and
therefore land in the broker's queue; `print.access` and the two tier5 actions
are allowed outright for the active session and only prompt for a non-active or
other-uid caller.

Three `.rules` files ship. None of them is what wires an action to the agent —
two **grant**, and the third mostly bypasses the agent for admin:

- `pwd/qdistro-pwd.rules` (installed as `50-qdistro-pwd.rules`) short-circuits
  admin/root to `YES` on `org.qdistro.pwd.unlock` so the admin's own CLI does
  not trigger a prompt loop, and returns `AUTH_ADMIN_KEEP` otherwise — which is
  what the action's own default already said. Its net effect is *less* agent
  involvement, not more.
- `pwd/qdistro-pwd-fprint.rules` **grants** the `qdistro-pwd` uid implicit `YES`
  on three `net.reactivated.fprint.*` actions, so fingerprint-gated unseal does
  not prompt for a password.
- `50-qdistro-locker-idle.rules`, written inline by
  `install-qdwin-session-for-vm.sh`, likewise **grants**: admin gets `YES` on
  `org.freedesktop.login1.lock-sessions` so the locker can lock its own logind
  session without a root-password prompt.

Everything else in the qdistro action vocabulary is broker-internal. Strings
such as `qdistro.clipboard.transfer:<source>:<dest>` are **rule-matching keys,
not registered polkit actions**; writing a polkit policy or `.rules` file
against them has no effect, and `org.qdistro.device.camera.claim`,
`org.qdistro.clipboard.send`, `org.qdistro.window.handoff` and
`org.qdistro.network.join_interactive` — previously listed here as live
examples — are registered nowhere at all. A polkit-visible namespace for
qdistro's own verbs is a design goal that is not implemented; note also that an
unregistered `org.qdistro.*` id fed through the agent would map to
`qdistro.external.org.qdistro.*`, not to itself.

Actions operate on [resources](resources.md) and resource verbs. The action
string is the broker's rule-matching key. `details` is **intended** to carry
manifest-shaped resource identity, labels, typed security fields, lock state,
workflow/run identity, and requested attachment semantics rather than a flat
tag bag; the shipped rule engine reads only the string selectors listed under
"Declarative rules" from it, and lock state and workflow context are not among
them.

## Two broker entry points — synchronous check vs long-term ask

The broker exposes two D-Bus methods. The distinction is about *user
experience*, not different policy engines — both use the same rules and cache
machinery:

- **`CheckPermission(action, details) → "allow" | "deny" | "unknown"`** — a
 synchronous fast-path lookup. Runs rules + cache only; never enqueues an
 admin prompt. Returns within a 2-second D-Bus ceiling (typical hit is
 <50ms). Callers use this on the hot path of a user action. `"unknown"`
 means the policy engine has no pre-decision for this `(uid, action, exe)`
 — the caller typically refuses the immediate attempt because there is no
 authority to invoke admin's attention synchronously.

- **`RequestPermission(action, details) → rid`** — enqueues an admin prompt.
 The caller either waits via `WaitForDecision(rid)` or fires and forgets
 ("please change the policy so next time this works"). On allow, the broker
 writes a cache row; the next `CheckPermission` on the same `(uid, action,
 exe)` returns `"allow"` silently.

Pattern for actions where the caller must not block on human attention:

```
verdict = broker.CheckPermission(action, details)
if verdict == "allow": proceed
elif verdict == "deny": refuse with a policy-deny error
else: # "unknown"
 refuse immediately
 broker.RequestPermission(action, details_with_debug_info)
 # fire-and-forget; admin sees the prompt in their queue,
 # approves when they get to it, caller retries later and
 # the cache row from admin's allow makes it instant.
```

The split lets the same policy engine serve both "never-block-the-user"
callers and "wait-for-admin" callers without surfacing the distinction inside
the broker's decision logic.

## Revocation as a signal

When admin deletes a cache row via `RevokeApproval(id)` or `RevokeAllForUid
(uid)`, the broker emits a D-Bus signal `ApprovalRevoked(caller_uid, action,
exe)` — one per deleted row. The intent is that subscribers which granted
resources on the strength of the row listen for this and tear down immediately.

> **Status: the only consumer refreshes UI; nothing tears down, so revocation
> is lazy.** The broker half ships and is exercised by the GUI acceptance tests.
> There is exactly one subscriber — the Qt admin app, which on the signal
> restarts a 250 ms coalescer that refreshes its Cache tab and updates the tray
> icon, so it does not render rows the broker no longer holds. That is display
> consistency, not enforcement, and the admin app itself ships in no production
> installer ([sessions.md](sessions.md#admin-panel-operations)).
>
> **qdshell is not a consumer**, contrary to what this section used to say: it
> has no `ApprovalRevoked` handler, and it never opens `qdwin_view_stream_v1`
> streams in the first place (the only `subscribe_view_stream` callers in the
> tree are a C test client and the VM-gated multimachine components). No
> component anywhere destroys a stream, closes an fd, or releases a resource in
> response to the signal.
>
> **The residual risk this leaves.** Revoking an approval deletes the cache row,
> so the *next* `CheckPermission` for that `(uid, action, exe)` stops returning
> `allow`. It does **not** interrupt anything already running on the strength of
> the old row — an in-flight capture, stream, or held resource survives until the
> holder next re-checks or exits. Read "Revoke" in the admin app as "stop
> granting this from now on", not as "cut off access now". Wiring a first
> consumer (qdshell tearing down affected view streams) is the tracked follow-up.

## Admin PyQt polkit agent

Runs in admin's desktop session. The UI is **not a modal dialog stack** but
a persistent queue-based app (see [admin-approval](admin-approval.md)).
Briefly:

- Permission requests land in a queue; admin triages at their own pace.
- Left pane: list of pending items. Right pane: detail + action buttons
 (Approve / Deny / Rule-from-this / Defer).
- Non-modal — admin's other work is never blocked.
- Keyboard-first triage; notifications don't steal focus.
- Scope picker in the detail pane. The full vocabulary is `once`, `1h`, `24h`,
 `forever`, `forever_exe`, `forever_argv`, `forever_basename`,
 `forever_prefix`. Note there is **no `per-session` scope** — nothing is
 revoked on session stop.

## Declarative rules

Admin authors rules in **YAML** (`.yaml` / `.yml`) under
`/etc/qdistro/rules.d/`, loaded by `qdistro-admin-broker` at startup, on
SIGHUP, and on inotify change. **TOML is not supported** — this page previously
offered it, and a `.toml` file is simply not read.

Rule shape:

```yaml
- name: allow-work-to-dev-plain-text
  match:
    action: "qdistro.clipboard.transfer:work-user:dev-user"
    mime_type: text/plain
  decision: allow

- name: deny-notebook-camera
  match:
    uid: 2000
    exe: /usr/bin/notebook
  decision: deny
```

The loader is **strict and fail-per-entry**: an unrecognised key raises and the
entry is dropped with a message in `load_errors()`, while the rest of the file
loads. So a rule that looks plausible but uses a key the engine does not know
silently does not apply. The exact vocabulary:

- Top-level keys: `name`, `decision`, `match`, `scope`, `rationale`.
- `match` keys: `uid`, `action`, `exe`, `app_id`, `sandbox_engine`,
  `mime_type`, `argv_exact`, `argv_basename`, `argv_prefix`. Earlier examples
  on this page used `source_user`, `target_user`, `mime`, `user` and `app` —
  **none of those exist**, and a rule using them would have been rejected at
  load. Source/destination silo is expressed inside the clipboard `action`
  string, not as separate keys.
- `decision:` accepts exactly `allow` and `deny`
  (`qdistro_admin_rules._VALID_DECISIONS`). `prompt`, `allow_session`,
  `transform`, `warn`, `contaminate` and `declassify` are **not writable as
  rule decisions today** — they are model vocabulary, described below, with no
  rule-engine implementation.

Rules are matched top-to-bottom; first match wins. Unmatched requests do **not**
fall through to a polkit prompt — the broker has no polkit escalation path.
What an unmatched request does depends on the entry point: `CheckPermission`
returns `"unknown"`, `RequestPermission` enqueues an item for admin's approval
queue, and the clipboard gates hit their own default (deny — see
[clipboard.md](clipboard.md)).

String selectors (`action`, `exe`, `app_id`, `mime_type`, `sandbox_engine`)
accept fnmatch-style globs when the value contains `*`; exact match
otherwise.

The intended policy model is ABAC-shaped: subject identity, action, resource
metadata, typed security fields, environment, lock state, and workflow context
as policy inputs, returning allow, deny, prompt, warn, transform, contaminate,
or declassify. That is the target model, not the shipped engine. The v1 rule
engine matches on string selectors only (`action`, `exe`, `app_id`,
`mime_type`, `sandbox_engine`, plus user/silo fields) and returns only
allow/deny; **lock state and workflow context are not policy inputs anywhere in
the broker**. Labels are the fast selector path; annotations are not routine
selectors.

## Python hooks

> **Status: the hook surface is inert on a stock install.** Both halves exist in
> the repo — the executor (`broker/qdistro_hook_executor.py`) and its unit
> (`deploy/systemd/services/qdistro-hook-executor.service`) — and the broker
> does build a `HookClient` with hooks enabled by default and consults it on the
> two permission paths. But **no installer installs either half**:
> `install-broker-for-qdwin.sh` copies `qdistro_hook_client.py` and not
> `qdistro_hook_executor.py`, and nothing installs or enables the unit. On a
> real install the executor socket never exists, the client fails to connect,
> and every hook consultation silently falls through to the next stage.
>
> **The residual risk this leaves.** A hooks directory is not a working policy
> surface in v1. An admin who drops a `00-deny-secrets.py` into
> `/etc/qdistro/hooks/` on a bootstrapped machine gets **no denial** — the file
> is never loaded and the request is decided by rules and the approval queue
> alone, with no error and no log line naming the missing executor. Do not rely
> on hooks for any enforcement until the installer chain ships them. Everything
> in the rest of this section describes the code as written, and is accurate
> only once the executor is installed and running by hand.

For logic rules cannot express (e.g., "if clipboard content matches a git
SHA, auto-route to dev-user's terminal"), admin drops Python files in a
hooks directory.

```python
# /etc/qdistro/hooks/git_sha_router.py

def on_clipboard_send(event):
 if event.mime == 'text/plain' and looks_like_git_sha(event.payload):
 return dict(action='transform', target_user='dev-user',
 new_payload=event.payload)
 return None # fall through
```

Hooks are consulted when rules are inconclusive. Admin-authored Python
evaluated in the broker's own process would be effectively privileged code, so
the design is a **sandboxed hook executor**: hooks run under a dedicated
unprivileged uid (`User=qdistro-hooks`) under a `SystemCallFilter=` seccomp
allow-list, with an API surface restricted to a well-defined hook
protocol, and the broker IPCs to the executor over an AF_UNIX socket rather
than importing hook files itself. That separation is how the executor is
written; per the status note above, neither the executor nor its unit is
installed by any installer, so on a stock install nothing runs at all.

### Execution order

When more than one hook file exports a handler for the same action, the
handlers are invoked in **ascending alphabetical order of the hook
filename** (the `.py` filename stem). The **first** handler to return a
non-`None` verdict wins; later handlers for that action are not consulted.
A handler that returns `None` (or that the file does not define) falls
through to the next hook.

This ordering is stable: it does not depend on the order in which files
were dropped into the hooks directory or hot-reloaded. To make precedence
explicit and leave room to insert hooks later, prefix filenames with a
numeric ordering token:

```
/etc/qdistro/hooks/00-deny-secrets.py    # runs first
/etc/qdistro/hooks/10-route-git-sha.py   # runs next
/etc/qdistro/hooks/20-default-allow.py   # runs last
```

### Concurrency

The executor accepts multiple broker connections concurrently and services
each in its own worker thread, so several events can be evaluated at once.
Hook authors should treat each `on_<action>` call as potentially running
**in parallel with other hook invocations**: do not rely on global mutable
state for per-event data, and guard any shared resource (file, network
handle, in-module cache) you touch with your own locking. Each call
receives its own *shallow* copy of the `event` dict, so replacing or
adding top-level keys in one hook cannot affect another hook — but
nested dicts/lists inside `event` are shared, so do not mutate nested
values in place if you depend on isolation.

## Start declarative, escalate to hooks

Pattern: implement the rule language first and cover the common cases. Only
add Python hooks when rules become awkward. Easier to audit, simpler for
admin, slower drift into ad-hoc code.

## Workflows — universal orchestration engine

The design intent is that `qdistro-admin-broker`'s rule + hook system extends
into a **universal
orchestration engine**, not a clipboard-only policy box. Clipboard policy
is one instance of a framework that coordinates across all qdistro primitives
— clipboard, window handoff, device claims, file access, secret delivery to
privileged tasks, remote service calls, VM / container lifecycle.

The representation contract is in [workflows.md](workflows.md). The short
version: a predictable human/agent-readable Markdown plan is linked to a strict
manifest that the broker can validate, policy-check, execute, and audit.
Material data flows are explicit so guard propagation and lineage are not
hidden inside prose.

### Workflow shape

A workflow is a declarative manifest with:

- **Triggers** — events that start the workflow: user action, clipboard
 event, app lifecycle, scheduled time, incoming request, file change.
- **Conditions** — policy matches on identity, context, time, content.
- **Steps** — actions: deliver secret, copy file, transfer clipboard,
 initiate window handoff, spawn VM / container, call an external API,
 run a command via `qsu`.
- **Data flows** — declared source entities, transformations, destinations,
 effective processing host, generated outputs, and inherited or narrowed
 security fields.
- **Roles** — which users and services participate and what each is allowed
 to do within the workflow's scope.
- **Secrets-needed** — declared dependencies on vault items.
- **Cleanup / compensation** — declared release and repair actions, with
 terminal states for cleanup failure and human review.
- **Lineage** — workflow-run id, input/output resource refs, approval refs,
 and generated artifact refs.

### Example uses

- **Git commit signing** — grants an SSH signing key to a `git` invocation
 for the duration of one commit, then scrubs it.
- **Cross-user paste with transformation** — on copy in work-user's browser,
 run content through a redactor (strip emails / SSH keys), deliver to
 dev-user's clipboard.
- **Nightly backup** — snapshot selected subvolumes, `btrfs send` to remote,
 rotate retention, notify admin on failure.
- **Pair a new phone** — coordinated steps across Tailscale, pairing key
 exchange, policy registration.
- **Fresh sandbox for a suspicious download** — spin up a container with a
 nested compositor, file available read-only, outbound network blocked.

### Secret delivery to privileged tasks

Pattern — a task needs a secret from a vault:

1. Workflow declares `needs: vault/dev/github-ssh-key`.
2. Admin approves (polkit prompt or pre-authored rule).
3. Pwd daemon unseals the item.
4. Engine delivers via the narrowest mechanism the consumer supports.
5. Task runs, consumes the secret.
6. Engine scrubs: handle closed, socket closed, credential released, mount
 namespace torn down, and audit/lineage finalized.

Delivery preference:

1. Authenticated `AF_UNIX` IPC with `SCM_RIGHTS` fd passing or an agent socket
 for consumers qdistro controls. Verify peer credentials and SELinux context;
 use close-on-exec discipline.
2. systemd credentials for systemd-managed tasks. They are acquired at service
 activation, exposed via `$CREDENTIALS_DIRECTORY`, restricted to the service
 user, and released on deactivation.
3. Short-lived path delivery only for legacy apps that require a path, with
 dedicated SELinux type/domain, lifecycle-bound mount namespace, DAC mode, and
 optional MCS range separation.

Environment variables are discouraged for secrets except tightly controlled
exec-only cases because they leak through process inspection, crash dumps,
logs, and child processes.

Identity verification: the secret is released only if the expected process
(e.g., `git` in expected cgroup via `qsu`) is asking, not any process with
the right uid.

### Principles

- **Workflow language is text** — predictable Markdown intent plus a strict
 execution manifest. Side effects live in declared action handlers, not
 arbitrary workflow code.
- **One policy brain** — the workflow engine extends the broker; no new
 daemon.
- **Every workflow run is audited** — who ran it, when, what secrets were
 touched, what resources were attached, what data flowed where, what steps
 executed, cleanup state, lineage refs, and outcome.
- **Human-in-the-loop remains default.** AI agents may draft workflows;
 admin approves. Auto-run workflows are opt-in per workflow per admin
 decision.

**Planned, not implemented:** new grants and new cross-silo approvals should
require admin unlock, and a previously approved activity should continue while
locked only when the grant explicitly carries lock-continuation semantics with
the relevant indicators visible. No part of this ships. Lock state is not a
policy input to the broker, the approval-cache schema has no lock-continuation
field, and neither the broker nor the session manager consults lock state on
any grant path — so today a grant decision is made the same way whether the
machine is locked or unlocked. See [sessions.md](sessions.md) for the same gap
stated from the lock side.

## xdg-desktop-portal

qdistro implements a **custom portal backend** on top of this framework.
Upstream Flatpak / GTK / Qt apps already use portals for file-picker, access
prompts, and notifications. The qdistro portal backend routes those requests
through the broker instead of the usual same-user approval. It is a
`dbus-python` + GLib service (not PyQt, as this page previously said —
`daemons/qdistro_portal_backend.py` imports `dbus`, `dbus.service` and
`gi.repository.GLib`, and no Qt at all) registered as
`org.freedesktop.impl.portal.qdistro`.

> **Status (2026-07-26): ships, with a narrower interface set than portals
> generally imply.** `portal-backend` is an unconditional step in the bootstrap
> installer chain; it installs `daemons/qdistro_portal_backend.py`, the
> `qdistro-portal-backend.service` user unit, the D-Bus activation file, and the
> `qdistro.portal` descriptor.
>
> The descriptor declares exactly three interfaces — `Access`, `FileChooser`,
> and `Notification`. **There is no `ScreenCast` and no `Camera` portal
> interface in the tree**, so no camera or screen-capture request is
> portal-mediated on qdistro, and the `Screenshot` path is deliberately
> unfinished (the backend returns an error rather than a URI until compositor
> capture is wired). Any statement anywhere in the docs that implies
> portal-mediated camera or screencast approval is wrong. The installer also
> does not `systemctl enable` the unit; it relies on D-Bus activation.

## What's implemented vs planned

Implemented and exercised by tests today:

- `RequestPermission` + `WaitForDecision` (sync wait), `CheckPermission`
  fast-path with `allow|deny|unknown` semantics, fire-and-forget
  `RequestPermission` (no waiter).
- `DecideRequest` from trusted admin TUI / Qt admin app identities,
  `ListCache`, `ListHistory`, `RevokeApproval`, `RevokeAllForUid`,
  `RunCacheGc`, `RunAuditGc`.
- **Admin-app Rules tab + "Rule from this" button** — the Qt admin app
  has a `RulesTab` that lists existing rules via `ListRules`, adds/edits
  them via `SaveRule`, reloads via `ReloadRules`, and refreshes live on
  the `RulesReloaded` signal (delete removes the rule file directly, as
  there is no `DeleteRule` RPC yet). A "Rule from this" action opens a
  `RuleEditorDialog` pre-populated from the selected pending request or
  history entry and saves through `SaveRule`, with a client-side
  guardrail refusing empty-match allow-all rules.
- Declarative rules in `/etc/qdistro/rules.d/*.yaml`: `allow`/`deny`
  decisions, fnmatch globs on string selectors, first-match-wins
  ordering, hot-reload via inotify and SIGHUP, `SaveRule` validation,
  `ReloadRules`, `ListRules`.
- Signals: `RequestPending`, `RequestDecided`, `ApprovalRevoked` (one
  per row), `RulesReloaded`. `ApprovalRevoked`'s only subscriber is the
  Qt admin app, which refreshes its Cache tab and tray icon; **no
  component tears anything down on it**, so revocation takes effect at
  the next check, not immediately (see "Revocation as a signal").
- Scope vocabulary: `once`, `1h`, `24h`, `forever`, `forever_exe`,
  `forever_argv`, `forever_basename`, `forever_prefix`.
- Cross-silo clipboard policy (`CheckClipboardTransfer`): same-silo
  short-circuit allow only after qdshell identity verification (and launch
  record verification when `LINEAGE_ENFORCE` is on), cross-silo default-deny,
  opt-in via rule.
- Per-uid + per-action rate limiting (`.RateLimited` D-Bus error).
- Audit log with `source ∈ {prompt, cache, rule, revoke, hook,
  clipboard_same_silo, clipboard_rule, clipboard_default_deny}`.
Written and unit-tested, but **not installed by any installer**, so absent
from a bootstrapped machine:

- **Python hooks executor** — sandboxed hook executor
  (`qdistro_hook_executor.py`) runs as a dedicated uid, listens on an
  AF_UNIX socket, loads `.py` hooks from `/etc/qdistro/hooks/`,
  hot-reloads on file change, returns `allow/deny/transform/null`
  verdicts.  The broker consults hooks after rules+cache are
  inconclusive and before the admin prompt.  The systemd service unit
  provides `ProtectSystem=strict`, `PrivateNetwork=true`,
  `NoNewPrivileges=true` and a `SystemCallFilter=` allow-list.
  Neither `qdistro_hook_executor.py` nor
  `qdistro-hook-executor.service` is copied or enabled by the installer
  chain; the broker's hook client fails to connect and every hook
  consultation falls through. See the status note under "Python hooks".

## Security context (secctx) identity contract

The `wp_security_context_manager_v1` protocol lets a launcher set
`sandbox_engine`, `app_id`, and `instance_id` on behalf of the clients it
spawns. These strings flow through qdwin to qdshell and the broker as the
silo classifier for same-silo clipboard / handoff gates.

**Option A (launcher-gated, active):** qdwin restricts the secctx manager
bind to the trusted launcher:

1. Only the bound shell (qdshell) or the installed `qdistro-secctx-exec`
   helper executable may bind `wp_security_context_manager_v1`. Same uid
   is not an authorization basis; qdwin independently requires an
   admin-uid helper to have a direct root launcher parent. Helpers under
   any non-root uid other than qdwin's configured admin/allowed uid are
   refused. The helper executable inode must be owned by root and not
   writable by group or other users.
2. The broker annotates every clipboard / handoff audit entry with
   `secctx_provenance=launcher_gated` (or `advisory` when the gate is
   off), so admins can filter decisions by trust level.
3. The env var `QDWIN_SECCTX_OPEN=1` disables the bind gate for developer
   workflows. The broker-side config `QDISTRO_SECCTX_LAUNCHER_GATED=0` (or
   `secctx_launcher_gated = false` in `/etc/qdistro/broker.conf`) switches
   the provenance tag to `advisory` and emits warnings when same-silo
   gates fire without identity verification.
4. `qdistro-secctx-exec` is not a generic identity-minting tool. Production
   use must come through a qdistro root launcher, which passes
   `QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1`; the wrapper accepts that marker
   only when its direct parent is a root launcher. Direct
   test/development runs must opt in with
   `QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1` and must run under a qdwin started
   with `QDWIN_SECCTX_OPEN=1`, because production qdwin rejects admin-uid
   helpers that carry the dev-only marker. Root-owned helpers are admitted
   by uid/executable identity because qdwin may not be able to read their
   `/proc` environment. Admin-uid helpers require the root launcher to
   remain the live direct parent until the manager bind; double-forking
   launchers fail closed. Historical Tier-1 and Tier-2 direct-admin
   launch paths cannot satisfy that direct-parent contract yet, so they
   warn and run untagged unless invoked from the root launcher path or an
   explicit dev override is active. The wrapper validates the secctx triple before
   binding the Wayland manager and writes launch-record pid files with
   exclusive, no-symlink creation under `XDG_RUNTIME_DIR`.
   Tier-1 and Tier-2 should move behind that same root launcher/broker path
   before their secctx tags are considered production coverage again.

**Option B (broker-attested, implemented):** qdwin snapshots each tagged
client's `(pid, starttime, uid, exe, selinux_label)` at secctx-bind time
(`SO_PEERCRED` + `/proc`) and forwards it on the
`qdwin_shell_v1.toplevel_peer_identity` event (protocol v22). qdshell
caches the tuple per toplevel handle and, on each clipboard / handoff
decision, calls broker `VerifyClientIdentity`, which re-resolves the live
process against `/proc` and returns true only if the field-22 starttime
(the always-enforced anti-PID-reuse anchor) matches; the uid, exe, and
SELinux-label axes are each additionally enforced only when both the
forwarded and the live value are present (skipped, not failed, when
unreadable / SELinux off), giving a hard floor of `(pid, starttime)`. The
same-silo short-circuit in `CheckClipboardTransfer`,
`CheckClipboardReceive`, and `CheckHandoffActivation` accepts qdshell's
per-call `identity_verified` flag only from a trusted qdshell peer. qdshell
sets it only after verifying **both** the source and destination endpoints,
otherwise the decision falls through to the default-deny cross-silo rule
path. When `LINEAGE_ENFORCE` is enabled, the broker also resolves the
source pid/starttime to a launch record before taking the same-silo
shortcut; a verified endpoint whose launch record is missing, stale, or not
bound to the claimed silo is denied rather than bypassing policy.

`VerifyClientIdentity` and the three gate methods are reachable to the
admin uid by D-Bus policy (`org.qdistro.AdminBroker1.conf`), but reachability
is not authority. The broker accepts them only from the expected qdshell
process identity (installed executable/profile path, plus SELinux type when
the policy supplies one).

Privileged control-plane methods (`DecideRequest`, cache revocation/listing,
and rule save/reload surfaces) likewise do not trust uid 1000 alone. The
broker requires the authenticated peer to match the installed admin GUI,
admin TUI, qdshell, or root maintenance helper identity. Root-only lineage,
launch, portal, and qsu bridge methods require uid 0 plus the expected
broker-owned helper identity.

Doc-only / not yet wired:

- **Portal camera + screencast mediation.** The portal backend itself ships
  (see above), but only for `Access`, `FileChooser`, and `Notification`;
  `ScreenCast` and `Camera` interfaces do not exist, and `Screenshot`
  returns an error pending compositor capture.
- **Lock-conditional policy.** Lock state is not a broker policy input and
  there is no lock-continuation field in the approval schema.
- **Workflow engine** (triggers / steps / roles / secrets-needed) — the
  rule engine is the seed; the full orchestration framework is future.
- **Notification surface / tray-counter / mobile admin** — current Qt
  admin app is an always-on window with no badge.

## Test coverage

End-to-end behaviour is covered by:

- `tests/unit/test_broker_*.py` — pytest, mocked, fast: rule matching,
  cache row shapes, sendto, polkit mapper, scope round-trip, layered
  identity, audit, rate-limit.
- `tests/integration/vm/*.bats` and `s*.sh` — bats / shell on a real
  VM: tier-1 audisp/selinux, tier-2 podman, tier-3/4/5 isolation,
  broker enforcing, qsu argv scopes.
- `tests/integration/permissions-gui/NN-*.md` — agent-driven GUI
  acceptance against a labwc VM: admin TUI + Qt approval app, cross-
  user send-to, signal contracts (`ApprovalRevoked`, `RulesReloaded`),
  rule hot-reload, clipboard policy, scope isolation, fire-and-forget,
  rate-limit, multi-pending navigation, TUI/Qt concurrent subscribers.
  See the index in `tests/integration/permissions-gui/README.md`.
