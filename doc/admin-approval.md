# Admin approval app

The admin-side UI for reviewing and responding to permission requests.
**Not a modal-dialog stack** — a persistent queue with a master-detail panel
that stays open and accepts requests asynchronously.

Authority is not granted to every process running as uid 1000. The broker
accepts approval and rule/cache control methods only from trusted admin
control-plane peers: the installed Qt admin app, the installed admin TUI,
qdshell where applicable, or the root maintenance helper. The system-bus
policy makes these methods reachable to the admin account, but the server-side
peer identity check is the authority boundary.

## Approving without the graphical app

The image ships two non-graphical approval surfaces next to the Qt app. All
three are installed by the bootstrap chain's `admin-app` step
(`scripts/install/install-admin-app-for-vm.sh`, which runs
`install-admin-cli-for-vm.sh`), so a machine install and the image get the
same files.

| Command | Run as | What it is |
|---|---|---|
| `qdistro-approvals` (`/usr/local/sbin`) | root | CLI: `pending`, `approve`, `deny`, plus the cache and audit commands (`list`, `revoke`, `audit`, `gc`, `audit-gc`) |
| `qdistro-admin-tui` (`/usr/local/bin`) | the admin user (uid 1000) | Textual approval queue for any terminal (a VT, SSH, a serial console); same scopes and keys as the Qt app, see `tui/SHORTCUTS.md` |

As root, for example over SSH while the admin's session is down:

```sh
qdistro-approvals pending                  # id, uid, pid, action, exe, details
qdistro-approvals pending --json           # the broker's GetPending rows
qdistro-approvals approve 12               # scope once (the default)
qdistro-approvals approve 12 --scope 24h   # cached; warns, revoke with `revoke`
qdistro-approvals deny 12
qdistro-approvals audit --limit 5          # the decision's audit row
```

`approve` and `deny` call the broker's `DecideRequest` with the same
semantics as the Qt app and the TUI: `--scope` takes the broker's scopes
(`once`, `1h`, `24h`, `forever`, `forever_exe`, `forever_argv`,
`forever_basename`, `forever_prefix`), and the broker refuses the ones a
request cannot take (delegated and one-shot requests, argv scopes without a
captured argv) with an error the CLI prints. A deny is never cached, so it is
always sent with scope `once`. The CLI checks `GetPending` first and exits
1 with `no pending request with id=N` for an id that is not pending (see
below for how the broker reports the outcome). Requester-
supplied text (action, exe, details) is printed with control characters
escaped. The audit row records `approver_uid` 0 for a CLI decision.

How the broker recognises them, and what that is worth: it reads
`/proc/<pid>/exe`, which for a Python script is the interpreter
(`/usr/bin/python3.13`), so for a Python peer it also requires the installed
script path somewhere in the process argv. When the kernel runs a script
through its shebang it puts the path the script was executed by into argv,
so the installed `qdistro-approvals` and `qdistro-admin-tui` are admitted,
while `python3 cli/qdistro_approvals.py` from a source tree or a copy
elsewhere is refused with `AccessDenied`. This identifies the genuine tool
for honest callers; it is **not** a security boundary against root (or
against other code running as the admin uid). Any root Python process that
merely names `/usr/local/sbin/qdistro-approvals` in its argv is admitted
without running the script (`tests/unit/test_cli_pending_decide.py` pins
that), and root can reach the broker through `busctl` anyway. Root is fully
trusted here; the boundary the broker enforces is against other uids. The TUI
refuses to start as root and points at the CLI.

Run the CLI by its absolute path or from a root login shell: `sudo`'s
default `secure_path` on openSUSE does not include `/usr/local/sbin`, so
`sudo qdistro-approvals` may not find it; use
`sudo /usr/local/sbin/qdistro-approvals ...` or `sudo -i`.

`DecideRequest` returns, atomically with the decision, what that call did:
`applied`; `applied-uncached` (the request is decided, but storing a cached
scope's row failed, so later identical requests prompt again; caching is
best-effort); `already-allow`/`already-deny` (someone decided first; nothing
changed); `deciding` (another call's decision is still being audited and
may yet be downgraded to deny); or `unknown` (no such request in this
broker instance). The CLI exits 0 on `applied`, and on `applied-uncached`
with a warning. On `already-*` it exits 1 and names the decision that
holds; this includes a second root CLI that decides the same request
identically, so exactly one concurrent decider reports success. Anything
else exits 3 with "outcome unconfirmed". The CLI never infers success from
the request leaving the pending list or from audit rows (request ids
restart with the broker and audit timestamps follow the wall clock). Its
D-Bus proxy is bound to the broker's unique bus name, so a broker restart
between `pending`-snapshot and decision fails the call rather than deciding
a reused id. Exit codes: 0 applied; 1 refused, already decided by another
approver, an id not pending at the `pending` check, or a broker error; 2
usage or a missing database; 3 outcome unconfirmed (including a
broker-side `unknown` or `deciding`), or (`list`/`audit`) a database path
that is not a regular file; 4 dbus-python missing; 5 broker unreachable.
The Qt app and the TUI ignore the return value.

## Never block admin's work

Traditional polkit agents pop modal dialogs that steal focus and block
admin until dismissed. qdistro explicitly rejects this:

- A permission request **never forces admin's attention**. It appears as a
 new item in the queue.
- Admin decides when to triage.
- Work in other apps continues uninterrupted.

Consequences:

- Callers must tolerate delayed responses (seconds to minutes).
 `qbus-admin` holds the open polkit call until admin answers; callers
 may have their own timeouts (e.g., `qsu` waits 2 minutes, then surfaces
 a deny/retry prompt to the requesting user).
- Urgent items surface via escalating notification, not by stealing focus.

## Layout — master-detail

```
+---------------------------------------------------------------+
| qdistro — admin approvals [-][o][x] |
+---------------------------------------------------------------+
| +---- Queue -----+---- Details (click an item) ------------+ |
| | * new | | |
| | work-user | User: work-user (blue) | |
| | fill gmail | App: /usr/bin/firefox | |
| | 3s ago | Action: org.qdistro.pwd.fill | |
| | ------ | Detail: gmail.com login form | |
| | dev-user | Reason: "login flow" | |
| | sudo apt | | |
| | 34s ago | Scope: | |
| | ------ | (*) Just this once (default) | |
| | v dev-user | ( ) 1 hour | |
| | (approved) | ( ) 24 hours | |
| | systemctl | ( ) Forever, this exact action | |
| | 2m ago | | |
| | ... | +----------+--------+----------------+ | |
| | | |[Approve] | [Deny] |[Rule from this]| | |
| | | +----------+--------+----------------+ | |
| +----------------+------------------------------------------+ |
| |
| [Filter: user v app v action v] [History] [Rules] |
+---------------------------------------------------------------+
```

Left: scrolling list of queue items. Right: detail + actions for the
selected item.

## Queue item

Fields shown in the list:

- **Status indicator** — new, in-review, approved, denied, expired.
- **Source user** (colored chip).
- **One-line summary** (app + action).
- **Relative timestamp**.

Sort: newest first by default; sortable by priority or user.

## Detail pane

Right pane shows for the selected item:

- Requesting user (colour chip).
- App / process: binary path, pid, exe hash, SELinux label, cgroup
 (layered identity).
- Action name (polkit-namespaced).
- Full details (argv, cwd for qsu; tab URL + form ID for pwd; MIME +
 target for clipboard; etc.).
- Reason (free text from requester, optional).
- Rules that partially matched but fell through (why it's being
 prompted).
- **Scope picker** (radios): once / 1h / 24h / forever (any argv with
 this exe) / forever-argv (this exact argv tuple) / forever-basename
 (this argv[0] basename anywhere) / forever-prefix (this argv[0], any
 trailing args). The argv-aware radios appear only when the request
 carries argv (a qsu invocation).
- **Buttons**: Approve, Deny, Rule from this..., Defer.

### "Rule from this..."

Opens a secondary inline panel with a draft YAML rule that would
pre-approve future requests like this one. Admin edits, confirms, and
saves to `/etc/qdistro/rules/`. The path from ad-hoc approvals to
declarative policy.

### Defer

Keeps the item in the queue but marks it read. Useful for "I'll think
about this one."

## Notification behaviour

- **New item** → a system notification (bottom-right). Not focus-stealing.
- **Tray icon** with a count of pending items.
- **Panel badge** near the clock with queue depth.
- **Click notification** → opens the approval app with that item selected.

## Keyboard navigation

Admin should triage without touching the mouse.

| Key | Action |
|-----------------------|------------------------------------------------------------------------|
| ↓ / ↑ | Move selection in queue. |
| Enter | Focus detail pane (or toggle between queue ↔ detail). |
| Ctrl+Y or Alt+A | Approve current item. |
| Ctrl+N or Alt+D | Deny current item. |
| Ctrl+R | "Rule from this..." |
| Ctrl+Shift+A | Approve **all** pending — confirmation required, scope forced to `once` in the TUI. |
| Ctrl+Shift+D | Deny **all** pending — confirmation required. |
| Alt+Shift+A | Approve all pending in the currently-selected silo (uid filter on the queue) — confirmation required. |
| Alt+Shift+D | Deny all pending in the currently-selected silo — confirmation required. |
| Ctrl+Shift+1..8 | Set scope (once / 1h / 24h / forever / forever-exe / forever-argv / forever-basename / forever-prefix). |
| Escape | Return focus to queue list. |
| Delete | Defer (mark read, keep in queue). |

The bulk-decide shortcuts (`Ctrl+Shift+A` / `Ctrl+Shift+D`) and
silo-scoped variants (`Alt+Shift+A` / `Alt+Shift+D`) are
intentionally guarded behind a confirmation modal in both the GUI
and the TUI — a single keystroke shouldn't be able to approve or
deny dozens of queued requests. The TUI also forces scope=`once`
for `Ctrl+Shift+A` regardless of the active scope picker, so a
fatigued admin who left scope on "Forever" can't accidentally pin
a long-lived grant on every queued row.

In the GUI these decision shortcuts are scoped to the admin window
(they don't fire while it's unfocused or sitting in the tray) and
are guarded by the Pending tab: pressed while another tab (Rules,
History, …) is showing, the first keypress switches to the Pending
tab so the admin sees the request, and a second keypress acts on
it — a stray `Ctrl+Y` can't approve a request the admin never
looked at. The scope-picker keys (`Ctrl+Shift+1..8`) only tick a
radio button and commit nothing, so they are exempt from this
guard and take effect from any tab.

## Urgency levels

Three levels, admin policy determines per action:

1. **Normal** — queue, regular notification, no escalation. (Most
 requests.)
2. **Important** — queue + more prominent notification + tray badge
 brightens.
3. **Urgent** — queue + a persistent full-width banner at the top of
 admin's compositor that does not disappear until addressed. Still
 non-modal; admin can keep working below. Examples: vault unlock at
 login, fingerprint-absent lock override.

Login-time vault unlock is "urgent"; routine context-menu approvals are
"normal."

## Admin unavailable

- Items accumulate in the queue.
- **Critical items auto-route to the phone.**
- Each item has a timeout (requester-controlled). When the timer expires,
 the item moves to the **Expired** view; it's still inspectable in audit
 but no longer actionable — the caller already gave up.

## Phone integration

When policy routes an approval to the phone:

- The phone app shows the detail pane in a mobile-friendly layout.
- The phone user approves → the signed response is relayed to `qbus-admin`
 → the queue item is marked decided (with approver = phone).
- The queue item shows "decided on phone" in history.

If both tty3 and phone are active: the item appears in both, first
response wins, the other dismisses automatically.

## History view

Separate tab in the same app. Table of past decisions:

- Columns: when, uid, exe, action, decision, scope, source (rule / cache
 / prompt — encodes whether admin / phone / rule decided it), argv (qsu
 invocations only).
- Searchable, filterable.
- **Revoke recent**: if something was approved that shouldn't have been,
 admin removes the cache entry; future identical requests will re-prompt.

## Rules view

Also a tab. Admin-authored rules list, each row: matches + action +
scope. Edit inline. Create from "Rule from this..." or from scratch.
**Test**: "would this rule match the currently selected queue item?" —
useful for debugging rule edges.

## Relationship to the bigger admin panel

This app is the **approval-queue view** of what is the broader admin
panel. The full panel also has tabs for:

- Users (create / edit / suspend silos).
- Devices (device grants, active streams, hardware config).
- Vaults (pwd management, recovery codes).
- Recall-user management.
- Phone pairings.
- System (update, snapshots, backups).

All progressively integrate as their underlying features land. One PyQt
app, more tabs over time.
