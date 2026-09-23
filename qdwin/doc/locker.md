# Locker protocol wiring (`qdwin_locker_v1`)

Adds a second private global so the screen locker (`qdlocker`) is a
peer process to the shell (`qdshell`), not a subsystem of it. The
bind handler always enforces the configured locker uid and, when an
expected executable path and/or SELinux label are configured, also
verifies the peer's `/proc/<pid>/exe` and `/proc/<pid>/attr/current`
(see the trust-model paragraph in `qdwin/qdwin-locker-v1.xml`).
Rationale and shape live in `qdwin/qdwin-locker-v1.xml`; this doc
describes the C-side work in `qdwin/qdwin.c`.

## Model

qdlocker is a peer process to qdshell. It owns `qdwin_locker_v1`
control traffic (`set_locked`, `locked_changed`, `overlay_key`), while
its visible Qt/QML window is a normal xdg_toplevel until the session
locks. On lock, qdwin matches that toplevel to the already-bound
locker process identity and moves the real UI view to `lock_layer`.
The normal desktop/background/panel/layer-shell layers are hidden, so
only the LOCK layer can render.

## Changes

1. **Config**: add `qdwin->allowed_locker_uid`. Source: same admin
   config path that sets `allowed_uid`. Default: equal to
   `allowed_uid` (single-tenant assumption from
   `qdistro/doc/sessions.md:4-14`). Settable separately so future
   sandboxed-locker work can put the locker behind its own uid.

2. **Global registration** (next to qdwin.c:11538):

   ```c
   qdwin->locker_global = wl_global_create(
       ec->wl_display,
       &qdwin_locker_v1_interface,
       1, qdwin, bind_qdwin_locker);
   ```

3. **`bind_qdwin_locker`** — mirror of `bind_qdwin_shell` at
   qdwin.c:5461 but checking `allowed_locker_uid` first. When
   `allowed_locker_exe` / `allowed_locker_label` are set it then
   resolves the peer's `/proc/<pid>/exe` / `/proc/<pid>/attr/current`
   and rejects on mismatch, bracketing the reads with a
   `/proc/<pid>/stat` starttime double-read. That double-read only
   proves the pid named **one stable process across our own read
   window** (mismatched or unreadable starttime ⇒ fail closed); it
   does **not** prove the pid still names the process that connected.
   In particular it does not detect (a) pid reuse that completed
   *before* the first sample, nor (b) a same-process `execve()` — e.g.
   into a different SELinux domain — since `starttime` is the
   fork/clone time and is unchanged by `exec`. That residual
   same-process-exec / pre-read pid-reuse window is closed by relying
   on service confinement (the locker runs under a confined
   unit/SELinux domain), not by these in-process `/proc` reads. The
   exe/label checks are best-effort defence-in-depth on top of the uid
   gate (see the residual-window note in `qdwin-locker-v1.xml`). A
   *second* `bind_as_locker` from a
   new client is accepted by destroying the old locker resource (the
   old process is treated as dead); a second bind on the *same*
   resource is the case rejected via the `already_bound` enum.

4. **Locker resource implementation** — a `struct
   qdwin_locker_v1_interface` vtable:

   - `bind_as_locker`: stash `qdwin->locker_resource = resource`,
     record the peer pid/uid, send `ready(initially_locked =
     qdwin->locked)`.
   - `attach_lock_surface`: legacy compatibility path. Current
     qdlocker does not create a placeholder surface.
   - `set_locked`: hide non-lock layers on lock, promote the matching
     locker Qt toplevel to `lock_layer` when it exists, and fan
     `locked_changed` out to both qdshell and qdlocker.
   - `lock_acknowledged`: log only; used by tests.

5. **Event sources to redirect to the locker**:

   - `lock_requested` — currently fired on `qdwin_shell_v1` from
     the Ctrl+Alt+L hotkey (qdwin.c around the hotkey channel,
     since v7). Add a parallel send on
     `qdwin_locker_v1_send_lock_requested(qdwin->locker_resource, reason)`.
   - `idle_lock_hint` — same shape; folds into
     `lock_requested(reason)` on the locker side. The shell side
     keeps `idle_lock_hint` for source compatibility, but the
     locker only gets the unified event.
   - `overlay_key` with `role=2` (locker) — qdwin.c overlay-key
     dispatch must check whether a locker is bound; if yes, route
     the keystroke to `qdwin_locker_v1_send_overlay_key(locker_resource, sym, utf8)`
     instead of the shell's overlay_key event. **Important**: this
     is the security boundary that means the typed password never
     reaches the shell process.

6. **Shell-side deprecation** of `attach_lock_surface` /
   `set_locked` / `lock_requested` on `qdwin_shell_v1`: not yet.
   Keep them functional during qdlocker rollout. Once
   `qdshell/Modules/LockScreen/*` is deleted (sibling-repo
   migration), bump `qdwin_shell_v1` and remove the shell-side
   handlers in the same commit.

## Lifecycle invariants

- If the locker disconnects while locked, the normal desktop remains
  hidden and the screen stays black until a fresh locker binds and
  maps its Qt toplevel.
- A second `bind_as_locker` from a different client at the locker
  uid is the "old process is dead" path: tear down the old
  resource (it will get a Wayland protocol error on next dispatch
  and exit), accept the new one. Implemented in the
  `already_bound` enum branch.

## Tests to add under `qdwin/tests/`

- A `qdwin-locker-probe` test client (mirror of
  `qdwin-probe`) that binds `qdwin_locker_v1`, attaches a dummy
  surface, calls `set_locked(1)` / `set_locked(0)`, checks the
  `locked_changed` events.
- A negative-uid test: bind from a non-locker uid → expect
  implementation error.
- A handover test: bind locker A → bind locker B → A's
  resource gets the protocol-defined teardown.
