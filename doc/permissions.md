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
 action: allow

- match:
 action: org.qdistro.device.camera.claim
 user: work-user
 app: /usr/bin/notebook
 action: allow_session

- match:
 action: org.qdistro.device.microphone.claim
 action: prompt
```

Rules are matched top-to-bottom; first match wins. Unmatched requests fall
through to the polkit prompt.

String selectors (`action`, `exe`, `app_id`, `mime_type`, `sandbox_engine`)
accept fnmatch-style globs when the value contains `*`; exact match
otherwise.

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

### Workflow shape

A workflow is a declarative + scripted definition with:

- **Triggers** — events that start the workflow: user action, clipboard
 event, app lifecycle, scheduled time, incoming request, file change.
- **Conditions** — policy matches on identity, context, time, content.
- **Steps** — actions: deliver secret, copy file, transfer clipboard,
 initiate window handoff, spawn VM / container, call an external API,
 run a command via `qsu`.
- **Roles** — which users and services participate and what each is allowed
 to do within the workflow's scope.
- **Secrets-needed** — declared dependencies on vault items.

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
4. Engine delivers via a narrow mechanism: ephemeral SSH agent socket,
 env var on exec, fd pass, or tmpfs file mounted into the task's namespace.
5. Task runs, consumes the secret.
6. Engine scrubs: socket closed, tmpfs unmounted, env overwritten, kernel
 memory where possible.

Identity verification: the secret is released only if the expected process
(e.g., `git` in expected cgroup via `qsu`) is asking, not any process with
the right uid.

### Principles

- **Workflow language is text** — YAML declarative + Python escape hatch.
 Both inspectable, both editable, both under git.
- **One policy brain** — the workflow engine extends the broker; no new
 daemon.
- **Every workflow run is audited** — who ran it, when, what secrets were
 touched, what steps executed, outcome.
- **Human-in-the-loop remains default.** AI agents may draft workflows;
 admin approves. Auto-run workflows are opt-in per workflow per admin
 decision.

## xdg-desktop-portal

qdistro implements a **custom portal backend** on top of this framework.
Upstream Flatpak / GTK / Qt apps already use portals for clipboard, camera,
screenshot, and file-picker. The qdistro portal backend routes those
requests through `qbus-admin` instead of the usual same-user approval. It
is a PyQt service registered as `org.freedesktop.impl.portal.qdistro`.

> Status (2026-05-16): doc-only. No portal backend ships yet; SDK
> callers use direct D-Bus today. Tracked in
> `todo/qdistro-portal-backend.md`.

## What's implemented vs planned

Implemented and exercised by tests today:

- `RequestPermission` + `WaitForDecision` (sync wait), `CheckPermission`
  fast-path with `allow|deny|unknown` semantics, fire-and-forget
  `RequestPermission` (no waiter).
- `DecideRequest` from admin TUI / Qt admin app, `ListCache`, `ListHistory`,
  `RevokeApproval`, `RevokeAllForUid`, `RunCacheGc`, `RunAuditGc`.
- Declarative rules in `/etc/qdistro/rules.d/*.yaml`: `allow`/`deny`
  decisions, fnmatch globs on string selectors, first-match-wins
  ordering, hot-reload via inotify and SIGHUP, `SaveRule` validation,
  `ReloadRules`, `ListRules`.
- Signals: `RequestPending`, `RequestDecided`, `ApprovalRevoked` (one
  per row), `RulesReloaded`.
- Scope vocabulary: `once`, `1h`, `24h`, `forever`, `forever_exe`,
  `forever_argv`, `forever_basename`, `forever_prefix`.
- Cross-silo clipboard policy (`CheckClipboardTransfer`): same-silo
  short-circuit allow, cross-silo default-deny, opt-in via rule.
- Per-uid + per-action rate limiting (`.RateLimited` D-Bus error).
- Audit log with `source ∈ {prompt, cache, rule, revoke,
  clipboard_same_silo, clipboard_rule, clipboard_default_deny}`.

Doc-only / not yet wired:

- **Python hooks executor** — the *sandboxed hook executor* described
  in §Python hooks is not implemented; the broker has no forward-trigger
  hook surface. Tracked in `todo/qdistro-hook-executor.md`.
- **xdg-desktop-portal backend** (`org.freedesktop.impl.portal.qdistro`).
  Tracked in `todo/qdistro-portal-backend.md`.
- **Workflow engine** (triggers / steps / roles / secrets-needed) — the
  rule engine is the seed; the full orchestration framework is future.
  Tracked in `todo/qdistro-workflow-engine.md`.
- **Admin-app Rules tab + "Rule from this" button** — `SaveRule` /
  `ListRules` are exposed on D-Bus, no UI surface yet. Tracked in
  `todo/qdistro-admin-rules-tab.md`.
- **Notification surface / tray-counter / mobile admin** — current Qt
  admin app is an always-on window with no badge. Tracked in
  `todo/qdistro-admin-notifications.md`.

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
