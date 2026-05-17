# qbus

## What qbus is

`qbus` is **D-Bus configured per qdistro conventions.** It is *not* a new
protocol, not a custom IPC implementation. The name refers to the collective
set of D-Bus instances + their conventions — shorthand, not an invention.

Writing a new IPC bus is not in qdistro's scope. D-Bus handles it.

## Per-concern bus split

qdistro uses multiple D-Bus instances, each for a specific concern. Benefits:

- **Smaller blast radius** — compromise of the clipboard bus doesn't expose
 the permission-approval flow.
- **Simpler policy** — each bus has its own rules file, easy to audit.
- **No centralized broker** — no single daemon sees every call.

| Bus | Instance type | Who connects | Purpose |
|---------------------------|--------------------------------------------------|---------------------------------------------------------|------------------------------------------------------------------------------------|
| `qbus-system` | D-Bus system bus (rare use) | Privileged daemons | Machine-wide: `fprintd`, `NetworkManager`, `bluez`, session-manager supervisor. |
| `qbus-session-$uid` | Standard per-user session bus (`systemd --user`) | That user's processes | Intra-user IPC: app ↔ SDK ↔ nested compositor. |
| `qbus-admin` | Dedicated daemon on its own socket | Any user's PyQt agent; admin's polkit agent; admin app | Cross-user permission requests; all approvals. |
| `qbus-peer-$uidA-$uidB` | Ephemeral unix sockets, direct p2p D-Bus | Exactly two compositors / agents | Clipboard send, window-handoff coordination. |

## `qbus-system`

The standard Linux system bus, used sparingly — only for truly machine-wide
services already designed around it (`NetworkManager`, `fprintd`, etc.).
qdistro daemons usually prefer dedicated sockets for clearer scoping.

## `qbus-session-$uid`

Nothing custom. `systemd --user` gives each user a session bus at
`$XDG_RUNTIME_DIR/bus`. Apps use it to talk to their own SDK, their nested
compositor's local endpoint, the PipeWire session manager, etc.

## `qbus-admin`

The interesting custom piece.

- A privileged daemon (`qdistro-admin-broker.service`) runs with the
 capabilities it needs.
- Exposes a unix socket accessible from any user's agent.
- Users' PyQt agents connect to it and call `RequestPermission(action,
 details)`.
- The broker applies polkit rules (which route qdistro-namespaced actions
 to admin's PyQt polkit agent).
- Admin's polkit agent surfaces an approval dialog in admin's compositor.
- The response flows back: allow, deny, or prompt for details.

A separate daemon (rather than "admin's session bus with ACLs") means admin
can log out of the approval UI or be in a different state without taking down
the approval channel. The daemon also provides a clean place to implement
admin-authored policy independent of admin's desktop session lifecycle.

## `qbus-peer-$uidA-$uidB`

For direct cross-user transfers (clipboard send, view-handoff coordination),
qdistro uses **ephemeral p2p D-Bus sockets** rather than a persistent bus.
D-Bus supports this natively.

Workflow for "user A sends clipboard to user B":

1. A's compositor invokes "Send clipboard to B" (context menu).
2. A's compositor asks `qbus-admin` whether the transfer is allowed.
3. If allowed, `qbus-admin` opens a socket pair, sends one end to A's
 compositor and one to B's via D-Bus fd passing.
4. A's compositor writes the clipboard payload; B's compositor reads.
5. Both ends close. Socket gone.

Ephemeral sockets avoid a persistent bus that both users would need standing
access to. Each transfer is its own scoped channel.

## Polkit vs bus — separation of concerns

Polkit is the **authorization** layer. The bus is the **transport** layer.
They are separate:

- A call arrives on a bus.
- Before dispatching, the receiver asks polkit "is the caller authorized for
 this action?"
- Polkit consults its rules, which may route to the admin PyQt polkit agent.
- Response: allow / deny.

Bus-level access control (who can connect and send) is not action-level
authorization (what they can ask for once connected).
