# Silos

A silo is qdistro's desktop-workload object. It is the desktop equivalent of a
Kubernetes Pod: a declared program or small program group with persistent
state, expected behavior, health checks, actions, rollback policy, and guarded
capabilities.

A silo is not only an isolation container. It may be backed by a Linux uid,
container, VM, browser profile, credential store, or a mix of those
implementation pieces. The user-facing object is "this workload with this
state and authority", not "this Unix account".

Examples:

- `dev-source`: editor, terminal, source tree, build cache, no commit keys.
- `github-commit`: Git credentials and signing key authority.
- `gmail-work`: browser profile logged into a specific Google account.
- `calendar-work`: web app profile expected to open Google Calendar.
- `libreoffice-work`: office app plus document state and file associations.

## Silo Definition Files

For now, silo definitions are semistructured Markdown. They are intended to be
readable by the owner, the admin UI, and GUI-capable agents. A later registry
can parse the stable headings and simple key-value fields without losing the
paragraph runbooks.

Recommended shape:

```md
# Silo: gmail-work

## Identity

type: browser-webapp
app: qdbrowser
profile: work-google
account_email: alice@example.com
purpose: Work Google account for Gmail and Calendar.

## Desired State

The silo should provide authenticated access to Gmail and Google Calendar for
the configured account.

## Parameters

account_email: alice@example.com
calendar_url: https://calendar.google.com

## Persistent State

- Browser profile directory.
- Cookies and login session.
- Local browser settings.

## States

- `not_created`: no persistent state exists.
- `needs_initialization`: state exists but bootstrap has not completed.
- `bootstrapping`: an agent or user is creating or initializing the silo.
- `starting`: the app or backing service is launching.
- `ready`: required health checks pass.
- `degraded`: non-critical checks fail, but the workload is usable.
- `needs_user_action`: login, MFA, consent, or manual choice is required.
- `recovering`: a recovery action is running.
- `failed`: required checks fail and no automatic action is currently running.
- `rollback_available`: a previous known-good snapshot exists.

## Bootstrap

bootstrap_allowed: true
bootstrap_actor: gui_agent_or_user
requires_user_presence: true
requires_network: true
creates_persistent_state: true
snapshot_after_success: true

### Preconditions

- qdbrowser is installed.
- Network is available.
- The user has access to the Google account.

### Steps

1. Create browser profile `work-google`.
2. Start qdbrowser with that profile.
3. Navigate to `https://calendar.google.com`.
4. Ask the user to complete login for `alice@example.com`.
5. If MFA appears, stop and wait for the user.
6. After login, verify that Calendar is visible.
7. Save a known-good snapshot.

### Success Criteria

- Calendar opens.
- The visible account is `alice@example.com`.
- No login, MFA, consent, or error page is visible.
- The required health checks pass.

### Failure Handling

If the profile was newly created and bootstrap fails before login succeeds,
delete the incomplete profile unless `preserve_failed_state: true`.

If login succeeds but health checks fail, preserve the profile and mark the
silo `failed`.

If MFA or password entry is required, mark the silo `needs_user_action`.

## Health Checks

### Check: browser starts

Open the qdbrowser profile named `work-google`.

Healthy result:
The browser window appears within 10 seconds and is not blank.

Failure:
No window appears, the process exits, or the window remains blank.

### Check: calendar account

Navigate to `https://calendar.google.com`.

Healthy result:
Google Calendar appears and the visible account is `alice@example.com`.

Needs user action:
A Google login page, MFA challenge, consent page, or session-expired message
appears.

Failure:
The page cannot load, crashes, or shows a different account.

## Actions

### Action: relogin

Open Google Calendar. If a login prompt appears, ask the user to authenticate.
After login, rerun the calendar account health check.

Allowed actors:
- user
- gui_agent_with_user_present

### Action: rollback profile

Restore the most recent known-good browser profile snapshot for this silo.
Rerun all required health checks after rollback.

Allowed actors:
- system_agent

## Capabilities

Required:
- network.https.google.com
- ui.browser
- snapshot.profile

Optional:
- files.downloads
- clipboard.read.user_approved

Forbidden:
- secret.agent_visible
- files.home.full_access

## Guardrails

- Do not expose cookies to agents.
- Do not let agents type stored passwords.
- Do not approve account changes without user confirmation.
- If a different Google account is visible, mark `failed`, not `ready`.

## Rollback Policy

snapshot_before_update: true
rollback_on_failed_required_health_check: true
rollback_requires_user_confirmation: false
preserve_failed_snapshot: true

## Update Policy

update_mode: managed
auto_update: disabled
update_sources:
- qdistro-package
- app-native-updater

app_rollback: supported
state_rollback: partial
state_rollback_notes: Cookies and local settings can be restored from a
profile snapshot. Remote Google account state cannot be rolled back by qdistro.

data_rollback: not_supported
data_rollback_notes: Calendar events, mail, and cloud documents are remote
service data. Failed updates must not restore an old local profile in a way
that hides remote changes.

update_health_checks:
- browser starts
- calendar account

auto_update_guardrails:
- If the app has a native auto-updater, qdistro must detect version changes.
- Run required health checks after an observed auto-update.
- If an auto-update fails and app rollback is unavailable, mark `failed` and
  request user action instead of pretending rollback is possible.
```

## Definition Versus Runtime State

The Markdown file describes desired state, procedures, guardrails, and
acceptance criteria. Runtime observations should live in a separate status
record so the definition does not become noisy.

Example status:

```json
{
  "silo": "gmail-work",
  "state": "needs_user_action",
  "reason": "Google login page visible",
  "observedGeneration": 7,
  "lastHealthCheck": "calendar account",
  "lastReadySnapshot": "2026-06-02T10:14:00Z"
}
```

The resource manifest model in [resources.md](resources.md) remains the typed
registry shape. The Markdown definition is the owner-facing source for
bootstrap, health, and recovery intent until qdistro needs a stricter schema.

## Bootstrap Contract

Bootstrap is the process that creates or initializes a silo from missing or
incomplete state. A bootstrap definition must say:

- whether bootstrap is allowed;
- which actor may run it;
- whether the owner must be present;
- which persistent state it creates;
- which credentials or approvals are required;
- how success is verified;
- what cleanup or rollback happens on failure.

Agents may perform mechanical setup, launch apps, navigate screens, inspect
visible state, and request user action. They must stop at user-required or
forbidden steps.

Agent-safe examples:

- install an approved package;
- create an app profile directory;
- launch an app;
- navigate to a configured URL;
- detect a login page;
- detect that a health check passed;
- create a snapshot after success.

User-required examples:

- enter a password;
- approve MFA;
- accept account recovery prompts;
- choose a different account;
- approve a broad new permission.

Forbidden examples:

- read or store passwords;
- export cookies or tokens outside the silo state;
- change the configured account without confirmation;
- bypass a failed health check by weakening the definition.

## Health Checks

Health checks are prose runbooks today, but they should be categorized so the
admin UI and agents can reason about them:

- `process`: the app launches and stays alive.
- `window`: the expected window appears.
- `ui`: expected UI text, role, or control appears.
- `file`: the app can open a known file.
- `account`: the expected account/session is active.
- `network`: required endpoints are reachable from the silo.
- `capability`: required broker permissions work.
- `negative`: forbidden access is blocked.

Each check should state healthy result, user-action result, and failure result.
For GUI checks, prefer accessibility names, roles, window titles, and stable UI
text. Vision and click-capable agents are allowed, but screenshots are evidence,
not the strongest oracle.

## Actions

Actions are declared procedures the system may offer to a user or agent. They
are not arbitrary automation scripts. Each action should state:

- purpose;
- allowed actors;
- preconditions;
- steps;
- stop conditions;
- success criteria;
- failure handling;
- post-action health checks.

Common action classes:

- `bootstrap`: create and initialize the silo.
- `initialize`: complete first-run state after creation.
- `relogin`: refresh an expired account session.
- `repair`: fix known degraded state without replacing everything.
- `rollback`: restore the last known-good persistent state.
- `snapshot`: capture known-good state after successful checks.
- `delete`: remove the silo after finalizers and revocations complete.

## Rollback And Updates

The silo definition must describe the update process. Desktop workloads do not
all update the same way:

- system packages may be updated and rolled back by the host;
- Flatpak/container images may have previous revisions;
- browser profiles and app settings may be snapshot-backed;
- native app auto-updaters may change files outside qdistro's normal update
  transaction;
- cloud-backed or externally synchronized data often cannot be rolled back by
  qdistro at all.

Every silo should declare:

```md
## Update Policy

update_mode: managed | observed | manual | forbidden
auto_update: disabled | allowed | required | unknown
update_sources:
- qdistro-package
- flatpak
- container-image
- app-native-updater
- browser-extension-store
- manual-installer

app_rollback: supported | partial | not_supported
state_rollback: supported | partial | not_supported
data_rollback: supported | partial | not_supported

update_health_checks:
- <required health check name>

pre_update_actions:
- <action name>

post_update_actions:
- <action name>
```

Meanings:

- `managed`: qdistro applies the update inside a known transaction.
- `observed`: the app may update itself; qdistro detects the new version and
  validates afterward.
- `manual`: the owner or agent follows an update action runbook.
- `forbidden`: the silo must not update except by replacing the definition or
  base image.

Rollback dimensions are separate:

- **App rollback** restores package, image, binary, extension, or launcher
  version.
- **State rollback** restores local profile/config/cache/session state.
- **Data rollback** restores user data created by the app.

The definition must not claim data rollback just because a local snapshot
exists. Cloud mail, calendars, remote documents, GitHub state, synced password
vaults, and remote infrastructure changes are usually not rollbackable by
qdistro. At most, qdistro can stop, warn, preserve evidence, or run an explicit
compensating action.

The normal managed update path is:

1. Snapshot current silo state.
2. Apply the app, package, profile, or definition update.
3. Start the silo.
4. Run required health checks.
5. If checks pass, mark the snapshot as known-good.
6. If checks fail because login or MFA is required, mark
   `needs_user_action`.
7. If checks fail because the workload regressed, rollback to the previous
   known-good snapshot.

Rollback should not hide account-expiration events. A browser profile that
needs login is usually `needs_user_action`, not update failure. A browser
profile that opens the wrong account is `failed`.

If app rollback is supported but state rollback is not, qdistro may restore the
old app version while preserving the current data/state. If state rollback is
supported but data rollback is not, qdistro may restore local profile state but
must clearly warn that remote or synchronized data has not been reverted.

For observed auto-updaters, the lifecycle is:

1. Record the previously observed app/extension/profile version.
2. Detect an app-controlled update.
3. Mark the silo `starting` or `degraded` while validation runs.
4. Run `update_health_checks`.
5. If checks pass, record the new version as healthy.
6. If checks fail and rollback is supported, run the rollback action.
7. If checks fail and rollback is not supported, mark `failed` or
   `needs_user_action` and preserve evidence.

For non-rollbackable data, the definition should include an explicit warning:

```md
data_rollback: not_supported
data_rollback_notes: This silo writes remote service data. qdistro can roll
back local app/profile state but cannot undo remote account changes.
```

## Relationship To Sessions

A silo is the declared workload and persistent authority. A session is the
runtime context that displays or operates on it. The same silo may be attached
to different sessions at different times, subject to attachment policy in
[attachments.md](attachments.md).

The session manager's current D-Bus lifecycle states in [sessions.md](sessions.md)
are implementation states for uid-backed silos. They are compatible with this
model but lower-level than the owner-facing health and bootstrap states above.
