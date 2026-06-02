# Window handoff

## View handoff, not ownership handoff

qdistro supports **view handoff**: the app continues running under its
original user (process, files, environment). Its display is re-routed to
another user's (or another machine's) compositor. The owner did not change;
the viewer did.

**Ownership handoff** (serialize app state, kill process, relaunch under a
different user) is *not* a platform feature. First-party apps that want it
can implement it themselves using the SDK's state-save hooks, but qdistro
does not promise it.

## View handoff is not data isolation

The handed-off app still runs as its original user. It still reads that
user's files, keys, and env vars. A malicious app that somehow got into A's
session and was handed off to B's compositor can keep exfiltrating A's data
— B's session is not the sandbox. If you want both visual handoff *and*
restricted data access, combine with an isolation tier (container, VM,
different user from the start).

## Primitives by tier

| Isolation tier | Handoff mechanism | Seamlessness |
|-------------------------------------------------|------------------------------|------------------------------------------------------------------------------------|
| 0 none / 1 SELinux / 2 podman / 3 different user| **waypipe** | Fully seamless; native window in target compositor, GPU-accelerated via DMA-BUF. |
| 4 VM whole-guest | **waypipe over AF_VSOCK** | Seamless host toplevels from nested qdwin. |
| 5 VM per-app (Linux guest) | **waypipe over AF_VSOCK** | Fully seamless; per-app Wayland over vsock. |
| 6 remote machine | **RDP** | Not seamless; framed window. |

waypipe is a Wayland-protocol proxy. The app's `wl_display` connection,
normally local to admin's compositor, is bridged through waypipe to the
target user's (or machine's) compositor. All protocol messages — surface,
buffer, input, clipboard — flow over that bridge.

waypipe is a transport, not a trust boundary. It does not by itself enforce
qdistro policy or filter every privileged Wayland protocol. Handoff policy is
enforced by the broker, compositor, nested compositor, SELinux, or VM boundary
around the transport.

## Wayland constraint — one display connection per process

A Wayland client normally has **one** `wl_display` per process. All its
surfaces (top-level, modals, popups) are children of that connection.
Consequences:

- You can hand off a whole app cleanly (the process's one connection
 migrates).
- You **cannot** hand off one window out of many from a multi-window process
 — the connection is process-scoped.

This is why the SDK constrains first-party apps to single top-level windows.
Modals opened *after* handoff naturally appear in the target compositor.

## Multi-window apps (IDEs, browsers, third-party)

Run them inside a **container with a nested compositor** (isolation tier 2):

- The IDE's N windows talk to the nested compositor (separate `wl_display`,
 inside the container).
- The nested compositor is itself one Wayland client of admin's compositor
 (via waypipe or direct connection).
- Handoff = re-home the nested compositor's outer connection. Atomic. The
 IDE's windows never see a disconnect because *their* `wl_display` (to the
 nested compositor) is untouched.
- Each nested top-level becomes a native-feeling top-level in admin's
 compositor via the `qdwin_nested_manager_v1` private protocol.

## Connection migration mechanics (for single-process apps using the SDK)

Wayland does not support connection migration natively. Handoff is
disconnect + reconnect, with the app driving re-creation:

1. The compositor requests handoff (triggered by admin or context menu).
2. The app receives `HandleHandoff(target_user)` over D-Bus.
3. The SDK emits `before_handoff`; the app saves in-memory state.
4. The SDK destroys the top-level QWidget.
5. The SDK closes the current `wl_display`.
6. The SDK opens a new `wl_display` through a bridge pointing at the target
 compositor.
7. The SDK reconnects Qt's platform plugin and recreates the top-level
 widget.
8. The SDK emits `after_handoff`; the app restores state.
9. Visual: the window disappears from the source compositor and reappears
 in the target compositor after a brief flash.

The process stays alive throughout. In-memory state (big data structures,
loaded models) is untouched.

## Visual isolation — per-user colour borders

Each user is assigned a colour at account creation. Handed-off windows
render with the **source** user's colour in the target compositor:

- Dev-user's colour = blue.
- Admin hands dev-user's notebook into work-user's view.
- In work-user's visible area, the notebook has a blue border / titlebar
 tint.

Qubes convention. Critical for the mental model to feel safe.

## Clipboard inside handed-off windows

Wayland clipboard is **compositor-scoped**. Because waypipe bridges the
app's `wl_display` to the target compositor, the app's `wl_data_device`
attaches to the target compositor naturally.

When a viewer does Ctrl+C on a handed-off window, the copy is offered on
the target compositor's clipboard. When they Ctrl+V, paste comes from the
target compositor's clipboard. **Falls out of the protocol** — no broker
needed for this case.

Per-window policy (filtering MIME, logging, redacting) lives in the waypipe
bridge itself.

## Read-only enforcement

Two tiers:

1. **First-party SDK contract.** The app receives `SetReadonly(true)`,
 disables edit UI, shows a lock icon. Clean UX.
2. **Input filter at the nested compositor / waypipe bridge.** Drop key
 and pointer events, keep scroll and focus. Generic, works for any app,
 ugly UX. Belt-and-suspenders for higher-risk handoffs.

Tier 1 for trusted first-party apps; tier 2 for third-party or
defence-in-depth.

UI sharing modes are distinct: read-only mirror, control transfer, and live
shared authority. A single app window has one active controlling session at a
time. New handoff/control grants require admin unlock unless an existing
workflow explicitly has lock-continuation semantics.

## Cross-uid activation gate

The triggering action for cross-uid window handoff is
`xdg_activation_v1.activate(token, target_surface)` — sent by the requesting
silo's app, evaluated by the admin compositor. The compositor stalls the
activate (no raise / focus yet) and waits for qdshell's reply; qdshell
consults the broker via `CheckHandoffActivation(source_silo, dest_silo,
source_app_id, dest_app_id)` which routes through the rules engine with a
default-deny for un-ruled cross-silo activations.
