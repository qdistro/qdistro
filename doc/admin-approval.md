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
