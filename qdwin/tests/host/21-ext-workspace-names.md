# tests/host/21-ext-workspace-names.md — ext-workspace-v1 NAME parity

Goal: prove the user's custom workspace names (pushed by the shell via
`qdwin_shell_v1.set_workspace_name`, v27) reach EVERY ext-workspace
client through the standard `ext_workspace_handle_v1.name` event — not
just qdshell's local overlay. Closes the "workspace names are positional"
gap in todo/qdwin/other-shells.md.

## Probe

`qdwin-wsname-probe` binds `qdwin_shell_v1` (as the shell) AND
`ext_workspace_manager_v1` (as a third-party bar would), pushes a custom
name, and asserts it is echoed back.

## Cases

1. **echo** — `qdwin-wsname-probe`
   pushes `set_workspace_name(0, "probe-ws-1")`, asserts the workspace at
   index 0 reports `name = "probe-ws-1"` via `ext_workspace_handle_v1.name`.
   Exit 0.

2. **revert** — `qdwin-wsname-probe --expect-revert`
   pushes a name then an empty name, asserts the handle name reverts to the
   positional default `"1"`. Exit 0.

3. **live-VM (PENDING)** — a real third-party ext-workspace bar (waybar)
   shows the user's names. Requires a live VM session; tracked as PENDING.

## Status

`qdwin-wsname-probe` is built by `meson` (target `qdwin-wsname-probe`) and
compiles in CI, but — like `15-ext-workspace-batch.md` — the echo /
`--expect-revert` cases are NOT wired into `meson test` because they need a
live qdwin to bind against (and the probe must run as the shell-gated uid).
Tracked here as a host-headless procedure: run each case against a headless
qdwin (weston-launch or a nested qdwin) and assert exit 0 plus the
`qdwin: workspace 0 name="probe-ws-1"` weston-log line.

The unit-level invariants (handler wiring, shell-bound gate, empty-name
revert, bounded length, name advertised on ext_workspace_handle_v1.name)
ARE covered by `meson test` via `qdwin/test_workspace_name_policy.py`
(test `workspace-name-policy`). The live-VM half — a real third-party
ext-workspace bar (waybar) showing the user's names — is PENDING.
