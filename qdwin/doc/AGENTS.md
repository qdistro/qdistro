# Rules for contributors (humans and LLM agents)

This file is read by LLM coding agents at the start of any session
that touches qdwin. Humans should read it too. The rules below are
not style preferences — each one cost real time to learn.

## Project shape

qdwin is a libweston shell plugin in **one C translation unit**
(`qdwin/qdwin.c`, ~11k lines) plus the protocol XML and a
nested-compositor client glue file. Do not refactor it into multiple
files "for cleanliness" — the file is organized by protocol surface
and that is the structure that matters.

The rest of qdistro (broker, SDK, polkit-agent, etc.) lives in the
qdistro umbrella repo and the qdshell repo. qdwin must not depend on
either. A different shell client can implement `qdwin_shell_v1` and
work against qdwin without ever touching qdshell.

## Language policy

C is acceptable here, and only here in the qdistro ecosystem,
because qdwin is the compositor TCB and bare-metal protocol glue.
Everything else (broker, SDK, shell logic, admin app) is Python or
QML so that LLMs can modify it without a build-debug-rebuild cycle.

When adding code:

- **Bare wayland-protocol handling**: C is right. Put it in qdwin.c
  next to the relevant interface region.
- **Policy logic that decides "should X be allowed"**: do NOT put it
  in qdwin. The shell client owns policy. qdwin fires a
  `*_pending` event and accepts a `*_decision` reply. Adding policy
  to qdwin permanently couples it to a specific shell.
- **Helpers and tools**: write them in Python and put them in the
  umbrella repo as a daemon, not in qdwin.

## Protocol versioning

`qdwin_shell_v1` is versioned. When adding a request or event:

1. Bump `version="N+1"` on the `<interface>` in
   `qdwin/qdwin-shell-v1.xml` and add `since="N+1"` on the new
   request/event.
2. In `qdwin/qdwin.c`, update the version argument passed to
   `wl_global_create()` for the qdwin_shell_v1 global.
3. In the bind handler in `qdwin.c`, check the client-requested
   version before sending the new event, and reject `since`-violating
   request invocations with `wl_resource_post_error`.

All three edits are required. Missing the third pin (the
`wl_global_create` argument) is silent — the global keeps advertising
the old version even though XML and handlers say otherwise. Clients
bind the old version and the new requests are unreachable.

## Cross-client gates

When a new event-decision pair is added:

- Default to deny on shell-client timeout. Never default to allow.
- The decision request must include enough identity context to be
  auditable: source security-context tag, destination tag, the
  specific resource being granted (e.g. a MIME type, an
  activation-token serial). qdwin's audit log entry comes from this
  data, not from a separate channel.
- Document the gate in `doc/protocol.md`. If the gate is invisible
  to anyone reading the protocol doc, future shells will not
  implement it.

## Testing

- **Host tests** (`tests/host/`): markdown playbooks driven by the
  shell scripts in the same directory (`start.sh` / `ctrl.sh` /
  `screenshot.sh`). qdwin runs **only with `--backend=headless`**
  on the host — no real seat, no real display, no input-injection.
  These tests exercise protocol behaviours that don't need a seat;
  anything that needs a real seat goes in `tests/gui/` to run in a
  VM. Never run qdwin against the developer's actual display server.
- **GUI tests** (`tests/gui/`, `tests/apps/`): VM tests. They run
  inside a virt-manager VM driven over `virsh send-key` + the
  qemu-guest-agent ctrl-socket + screenshot extraction. GUI tests
  must NOT run on the host — bad input injection on the host can
  kill the developer's session.
- Adding a new test scenario: create a numbered markdown file in
  the appropriate directory. The playbook is the test; the helper
  scripts in the same directory execute it. There is no separate
  test framework.

## Vendored libweston

`libweston-vendored/` exists because of one local patch
(`0001-allow-null-parent-xdg-popup.patch`). When the system
libweston has the patch, qdwin builds against system and the
vendored tree is unused.

Do not add a second local patch without first attempting to upstream
it. Vendoring is a debt.

## Commit conventions

- Each commit should be reviewable on its own.
- The commit message body should explain *why*, not what. The diff
  shows what.
- Reference the protocol version bump in the subject line when you
  bump one (e.g. `protocol: v21 -> v22 — chrome_button gains
  popup_kind`).
- No "WIP", no "fix typo", no merge-commit clutter.

## What not to do

- Do not add scene-graph / animation / blur / shadow code to qdwin.
  qdwin renders flat. Effects are the shell client's job (see
  [architecture.md](architecture.md)).
- Do not add a "trusted-but-not-shell" client tier. Either implement
  `qdwin_shell_v1` (one client only, peer-uid filtered) or be a
  sandboxed Wayland client behind a security-context tag.
- Do not introduce a Python or Lua scripting hook inside qdwin.
  qdwin is the TCB. Scripts run in the shell client.
- Do not depend on systemd, dbus, polkit, or any qdistro service
  from inside qdwin. The shell client orchestrates those.
