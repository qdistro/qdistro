# Permissions, policy, workflows

## Authorization stack

```
 user app requests something sensitive
 |
 v
 qdistro_app SDK / PyQt agent in the user's session
 |
 v
 (connects to qbus-admin daemon)
 |
 v
 qbus-admin broker: policy pipeline
 1. Declarative rules (YAML / TOML) -> allow / deny / prompt / transform
 2. Python hooks (if rules inconclusive) -> same actions
 3. polkit agent (interactive admin prompt)
 |
 v
 response: allow / deny / ...
 |
 v
 caller acts on response
```

Every qdistro action is polkit-namespaced: `org.qdistro.device.camera.claim`,
`org.qdistro.clipboard.send`, `org.qdistro.window.handoff`,
`org.qdistro.network.join_interactive`, etc. polkit rules route them all to
the admin PyQt agent by default.

Actions operate on [resources](resources.md) and resource verbs. The action
string remains the polkit namespace, but `details` should carry manifest-shaped
resource identity, labels, typed security fields, lock state, workflow/run
identity, and requested attachment semantics rather than a flat tag bag.

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
exe)` — one per deleted row. Subscribers that granted resources on the
strength of the row listen for this and tear down immediately.

qdshell is the first consumer: on a matching `(caller_uid, action)`, it calls
`qdwin_view_stream_v1.destroy()` on affected open streams so remote peers
lose access at the same instant the cache row disappears.

Without this signal, revocation would be lazy.

## Admin PyQt polkit agent

Runs in admin's desktop session. The UI is **not a modal dialog stack** but
a persistent queue-based app (see [admin-approval](admin-approval.md)).
Briefly:

- Permission requests land in a queue; admin triages at their own pace.
- Left pane: list of pending items. Right pane: detail + action buttons
 (Approve / Deny / Rule-from-this / Defer).
- Non-modal — admin's other work is never blocked.
- Keyboard-first triage; notifications don't steal focus.
- Scope picker (once / 1h / 24h / forever / forever-this-argv) in the detail
 pane.

## Declarative rules

Admin authors rules in YAML or TOML, loaded by `qdistro-admin-broker` at
startup and on SIGHUP.

Rule shape:

```yaml
- match:
    action: org.qdistro.clipboard.send
    source_user: work-user
    target_user: dev-user
    mime: text/plain
  decision: allow

- match:
    action: org.qdistro.device.camera.claim
    user: work-user
    app: /usr/bin/notebook
  decision: allow_session

- match:
    action: org.qdistro.device.microphone.claim
  decision: prompt
```

Rules are matched top-to-bottom; first match wins. Unmatched requests fall
through to the polkit prompt.

String selectors (`action`, `exe`, `app_id`, `mime_type`, `sandbox_engine`)
accept fnmatch-style globs when the value contains `*`; exact match
otherwise.

The policy model is ABAC-shaped: subject identity, action, resource metadata,
typed security fields, environment, lock state, and workflow context are policy
inputs. Policy returns allow, deny, prompt, warn, transform, contaminate, or
declassify. Labels are the fast selector path; annotations are not routine
selectors.

## Python hooks

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

Hooks run when rules are inconclusive. Hooks run inside (or next to)
`qdistro-admin-broker`, which is privileged, so admin-authored Python in
that context is effectively privileged code. The deployed model is a
**sandboxed hook executor**: hooks run in a dedicated unprivileged uid with
seccomp, with an API surface restricted to a well-defined hook protocol;
the broker IPCs to the executor.

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

The `qbus-admin` broker's rule + hook system extends into a **universal
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

New grants and new cross-silo approvals require admin unlock. A previously
approved activity may continue while locked only when the grant explicitly has
lock-continuation semantics and the relevant indicators remain visible.

## xdg-desktop-portal

qdistro implements a **custom portal backend** on top of this framework.
Upstream Flatpak / GTK / Qt apps already use portals for clipboard, camera,
screenshot, and file-picker. The qdistro portal backend routes those
requests through `qbus-admin` instead of the usual same-user approval. It
is a PyQt service registered as `org.freedesktop.impl.portal.qdistro`.

> Status (2026-05-16): doc-only. No portal backend ships yet; SDK
> callers use direct D-Bus today.

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
  per row), `RulesReloaded`.
- Scope vocabulary: `once`, `1h`, `24h`, `forever`, `forever_exe`,
  `forever_argv`, `forever_basename`, `forever_prefix`.
- Cross-silo clipboard policy (`CheckClipboardTransfer`): same-silo
  short-circuit allow only after qdshell identity verification (and launch
  record verification when `LINEAGE_ENFORCE` is on), cross-silo default-deny,
  opt-in via rule.
- Per-uid + per-action rate limiting (`.RateLimited` D-Bus error).
- Audit log with `source ∈ {prompt, cache, rule, revoke, hook,
  clipboard_same_silo, clipboard_rule, clipboard_default_deny}`.
- **Python hooks executor** — sandboxed hook executor
  (`qdistro_hook_executor.py`) runs as a dedicated uid, listens on
  AF_UNIX socket, loads `.py` hooks from `/etc/qdistro/hooks/`,
  hot-reloads on file change, returns `allow/deny/transform/null`
  verdicts.  The broker consults hooks after rules+cache are
  inconclusive and before the admin prompt.  Systemd service unit
  provides `ProtectSystem=strict`, `PrivateNetwork=true`,
  `NoNewPrivileges=true` sandboxing.

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

- **xdg-desktop-portal backend** (`org.freedesktop.impl.portal.qdistro`).
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
