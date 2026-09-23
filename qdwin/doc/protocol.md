# Protocols

qdwin exposes two private protocols (`qdwin_shell_v1`, currently
at version 28, and `qdwin_locker_v1` v1 — see [locker.md](locker.md)),
one helper protocol for nested compositors (`qdwin_nested_v1`, see
[nested.md](nested.md)), and a curated set of public Wayland
protocols.

## qdwin_shell_v1 (private, single-client)

The shell-management protocol. The global is registered via
`wl_global_create`; the bind handler (`bind_qdwin_shell`) uses
`allowed_uid` only for bootstrap before a shell role exists. Once a
client has claimed the shell role via `bind_as_shell`, later binds from
other clients are rejected even when they share the same uid. Same uid is
not an authorization basis for shell privileges.

### Sub-interfaces

| Interface | Purpose |
| --- | --- |
| `qdwin_shell_v1` | top-level shell handle |
| `qdwin_background_v1` | wallpaper surface bound via `attach_background` (since v18) |
| `qdwin_panel_v1` | panel/bar surface bound via `attach_panel` (since v3); reserves a work-area edge |
| `qdwin_lock_surface_v1` | full-display lock surface bound via `attach_lock_surface` (since v6) |
| `qdwin_launcher_v1` | app-launcher surface bound via `attach_launcher` (since v5) |
| `qdwin_notification_v1` | notification toast attached via `attach_notification` (since v4) |
| `qdwin_popup_v1` | shell-managed popups (right-click menus, chrome) via `show_popup` |
| `qdwin_view_stream_v1` | per-toplevel PipeWire view stream (subscribe + frame events) |
| `qdwin_stream_input_v1` + handle | input forwarding into nested-compositor proxies |

### Versioning policy

Every protocol change bumps the integer version. Three edits are
required for each bump:

1. `qdwin/qdwin-shell-v1.xml` — the `since="N"` annotation on the new
   request/event, and the top-level `version="N"` attribute on the
   interface.
2. `qdwin/qdwin.c` — the `version:` argument to `weston_desktop_create`
   (or wherever the global is registered) AND the version check in
   each `bind` handler that conditionally sends a new event.
3. `qdwin/qdwin.c` again — the `wl_global_create` version argument
   when registering the global on the wl_display.

Missing any one of the three leaves the global pinned at the previous
version even though the XML and handlers claim the new one. The bug
is silent — clients bind the lower version and the new requests are
unreachable. This is documented because it has cost real time in the
past.

### Cross-client policy gates

Several events round-trip a policy decision back to the shell client
before qdwin acts:

- `nested_proxy_pending` / `nested_proxy_decision` (since v8) —
  admit-or-deny for a new nested compositor toplevel.
- `activation_pending` / `activation_decision` (since v12) —
  admit-or-deny for an `xdg-activation` token-driven focus request.
- `data_offer_receive_pending` / `data_offer_receive_decision` (since
  v15) — admit-or-deny each cross-client clipboard receive. The
  event carries the requested MIME type and toplevel handles belonging
  to the actual source and receiving Wayland clients. libweston invokes
  the send shim only for its active `source->offer`, whose resource
  retains the receiving client; keyboard focus never supplies either
  endpoint. Connections without a mapped toplevel are denied, including
  separate proxy/helper connections that only share an app ID. The shell
  resolves the handles to their authenticated identity sidecars.
  Wrapping applies even without a shell; absent/pre-v15 shells deny.
  Shell or endpoint loss cancels pending receives, and allocation or
  timer setup failure closes the pipe. A restarted shell starts with no
  inherited approvals; existing sources remain guarded.

Every gate is fail-safe: timing out the shell's response defaults
to deny.

### Remote display controls (since v34)

Version 34 appends remote identity and display controls after the existing
v31–v33 application-ID and capture interfaces, preserving their opcodes.
`nested_proxy_remote_identity` carries the verified publisher's immutable
remote-origin tuple. `set_remote_output_input` enables a named RDP slot only
through the bound shell; input starts disabled and shell loss disables it.
`drain_remote_output_state` cancels the slot's input and transfer state before
the authenticated carrier is detached. Both requests return explicit result
events and reject callers bound below version 34.

The nested protocol's version 3 `set_remote_identity` request accepts an
immutable origin only from the exact root-installed remote viewer publisher.

### Hotkey channel (since v19)

`register_hotkey(modifiers, keysym, slot)` registers a global hotkey
that fires as `hotkey_pressed(slot)`. qdwin owns the keymap and the
seat keyboard grab; the shell client wires hotkey slots into its UI
intents (open launcher, switch workspace, etc.) but never sees raw
keystrokes.

## Public Wayland protocols supported

Built-in, registered unconditionally:

- **xdg-shell** (stable) — via libweston-desktop.
- **wl_compositor** (v5), **wl_seat / wl_output / wl_subcompositor /
  wl_shm / wl_data_device_manager**, **wp_viewporter**,
  **wp_presentation**, **zxdg_output_manager_v1** (v2) — libweston
  defaults. Note `wl_compositor` is pinned at v5 by vendored
  libweston-16 (no v6 `preferred_buffer_*` events).
- **zwp_linux_dmabuf_v1** — libweston default; advertised at v5 (with
  dma-buf feedback) when a GL renderer is active, else v3.
- **relative-pointer-v1**, **pointer-constraints-v1** (pointer
  lock/confine), **zwp_input_timestamps_manager_v1**,
  **wp_single_pixel_buffer_v1**, **wp_tearing_control_v1**,
  **tablet-v2** — all registered *ungated* by libweston-16's
  `weston_compositor_create` for every client (i.e. not curated by
  qdwin; games / drawing tablets / VRR work without qdwin involvement).

Built-in, registered by qdwin-shell.so:

- **xdg-decoration** (`zxdg_decoration_manager_v1`) — server-side
  decoration negotiation so toolkits don't self-draw chrome.
- **wlr-output-management-unstable-v1** (`zwlr_output_manager_v1`) —
  output layout/mode enumeration and configuration.
- **ext-workspace-v1** (`ext_workspace_manager_v1`, since shell v24) —
  workspace enumeration/switching surfaced to the shell.

- **zwlr_layer_shell_v1** (v5) — vendored XML in
  `qdwin/wlr-layer-shell-unstable-v1.xml`. The protocol was never
  accepted into wayland-protocols upstream; the XML is the de-facto
  reference. Required so external clients (waybar, fuzzel, mako,
  Quickshell-as-bar, etc.) can place layered surfaces.
- **xdg-activation-v1** — focus tokens, gated by qdwin policy.
- **ext-idle-notify-v1** + **idle-inhibit-unstable-v1** — idle
  detection for screen-locking and inhibit hints.
- **cursor-shape-v1** — CSS-style cursor shape requests; qdwin maps
  to libXcursor theme sprites.
- **wp_fractional_scale_v1** — per-surface fractional scale.
- **primary-selection-unstable-v1** — middle-click clipboard.
- **wp_security_context_v1** — sandbox-engine identity tag carried
  through `wl_socket_create_listener` mode.

Note: `tablet-v2`, `relative-pointer`, `pointer-constraints`,
`tearing-control` and `single-pixel-buffer` are the ungated libweston
defaults listed above — advertised to every client, not curated or
gated by qdwin.

### Development/test output capture

Libweston creates `weston_capture_v1` independently of qdwin's optional
frontends. qdwin's global filter exposes it only to clients holding the
shell credential — normally just the exact client that has successfully
bound `qdwin_shell_v1`; ordinary and security-context clients do not
discover it. (Discovery is broader than capture: an authorized
`qdistro-secctx-exec` trusted launcher is mapped to the shell credential
and can therefore *see* the global, and the explicit `QDWIN_SECCTX_OPEN`
dev mode widens discovery further — but neither yields pixels, because the
late authority below still denies their capture tasks.) A second, late
libweston authority check defaults to deny for every capture task unless a
registered authority explicitly permits that exact client and output.

Two separate opt-in facilities exist:

- `QDWIN_ENABLE_SCREENSHOOTER=1|true|yes` calls Weston's legacy
  `screenshooter_create()`. Its authority permits only the client Weston
  itself launches from the screenshooter binding; an independently executed
  `weston-screenshooter` is not authorized merely because this flag or
  `--debug` is present. This path remains for the nested host development
  harness and is not the VM GUI gate's content source.
- `QDWIN_ENABLE_SHELL_CAPTURE=1|true|yes` registers qdwin's narrow authority.
  It authorizes a task only when the requester is the exact live bound-shell
  `wl_client` and the target is the single designated output
  (`QDWIN_SHELL_CAPTURE_OUTPUT`, `Virtual-1` — an exact-name pin, not a
  deny-list, so additional outputs on a future golden are not silently
  capturable). "Bound shell" is the client currently holding the shell
  role (uid/singleton/secctx-gated), not a pinned qdshell executable
  identity — the same role trust every other shell operation already has.
  qdshell discovers the capture global using a second registry enumeration
  after `bind_as_shell`, selects `Virtual-1` by `wl_output.name`, sends the
  v32 `prepare_output_capture` request to force full damage, and captures one
  framebuffer into SHM. Every other output name — absent, additional heads,
  PipeWire forwards — is refused at both layers.

  v33 adds retained-frame stale-serve on the same gate: renderers retain the
  last composited frame of the designated output, and when a capture task is
  filed while the output cannot repaint (seat taken away, display power off)
  or a scheduled repaint never runs, qdwin completes the task from that
  retained frame and sends `capture_served_stale(output, age_ms, msc)`.
  qdshell surfaces this on the ctrl reply as `live=0 age_ms=… msc=…`, and
  the harness writes a `.meta` sidecar next to the PNG. Stale pixels are
  triage evidence ("the session was showing X before the incident"), never
  post-action proof.

Both environment flags also require the compositor euid to equal
`allowed_uid`, and both are disabled by default. The session installer
(`qdistro/scripts/install/install-qdwin-session-for-vm.sh`) emits
`QDWIN_ENABLE_SHELL_CAPTURE=1` on `qdwin-compositor.service` only when the
caller itself exports that variable — the test-VM bake
(`scripts/vm/fresh-vm-bootstrap.sh`) does; the production callers of the
same installer (`image/config.sh`, `qdistro-bootstrap.sh`) do not, so
production units never carry it. The qdshell capture control verb additionally requires a root
Unix peer via `SO_PEERCRED`, while compositor authorization remains the final
gate. Neither `QDWIN_SECCTX_OPEN` nor Weston `--debug` is required or permitted
for this VM capture path.

The VM GUI harness validates DRM ownership of the service compositor
(unit MainPID), asks qdshell to capture only `Virtual-1`, copies the
completed file out through QGA with guest/host size + sha256 agreement,
and fully decodes the PNG, cross-checking decoded dimensions against the
reply. `virsh screenshot` is retained as a diagnostic-only helper
because `video model=none` VMs expose the tty console there, not qdwin's DRM
output.

### Security posture: layer-shell

`zwlr_layer_shell_v1` is registered as an unconditional public global
(advertised to all clients via `wl_registry`), but the bind handler
(`bind_qdwin_layer_shell`) gates access: only the bound shell client
(qdshell, matched by client pointer or pid+uid) or a client running as
`allowed_uid` (before the shell binds) may successfully bind.
Unauthorized bind attempts are rejected with
`wl_client_post_implementation_error`.

Once bound, `EXCLUSIVE` keyboard interactivity is granted directly by
qdwin to any layer surface that requests it (`qdwin_layer_surface_apply`
calls `weston_seat_set_keyboard_focus`). There is no broker decision
path for EXCLUSIVE -- the bind-time gate is the sole access control.

Note: before qdshell binds, any client running as `allowed_uid`
(defaults to compositor real uid via `getuid()`) can bind layer-shell, so the window
between compositor startup and shell bind is a wider-trust period.
After the shell binds, only qdshell (or same pid+uid) may bind.

The global is advertised broadly so that third-party layer-shell clients
(waybar, fuzzel, mako, gtk-layer-shell bars) see it in `wl_registry`.
In practice, these clients will fail to bind unless they run as
`allowed_uid` (pre-shell) or are the shell process; the advertisement
exists for protocol discovery rather than access.

**Optional admin allowlist (security review Finding #4).** On top of the
shell-client/`allowed_uid` gate above, the bind handler supports an
optional, fail-closed peer allowlist, configured via (argv wins over
env):

- `--qdwin-allowed-layershell-uid=` / `QDWIN_ALLOWED_LAYERSHELL_UID`
- `--qdwin-allowed-layershell-exe=` / `QDWIN_ALLOWED_LAYERSHELL_EXE`
- `--qdwin-allowed-layershell-label=` / `QDWIN_ALLOWED_LAYERSHELL_LABEL`

Default posture is **unchanged**: when none of these is set, the block is
skipped entirely and access is exactly the historical broad/test
behaviour (shell-client or `allowed_uid`). When any is set, the bind
handler additionally verifies the peer's uid and/or resolved
`/proc/<pid>/exe` and/or `/proc/<pid>/attr/current` SELinux label and
rejects the bind on mismatch. These checks fail closed: an unreadable,
OOM, or truncated `/proc` read is treated as "unverifiable" and rejects
the bind, and the reads are bracketed by `/proc/<pid>/stat` starttime
samples so a pid that is *recycled to a different process* during the
handler's read window (a different starttime) is rejected.

This mirrors the locker-bind hardening (see `doc/locker.md` /
`qdwin-locker-v1.xml`). It is best-effort defence-in-depth, **not** a
proof of identity. The peer pid is what the kernel pinned at connect
time, but `/proc` is read later when the bind request is serviced. The
starttime bracketing is narrow and does **not** make the exe/label a
stable post-bind identity; specifically it does NOT cover:

- **PID reuse before the first read.** If the connecting process exits
  and its pid is recycled to a different same-uid process *before* the
  first `/proc/<pid>/stat` sample, every read (starttime included)
  observes the impostor consistently and the bind is accepted. The
  bracketing only catches a recycle that races the read window itself.
- **Same-process `execve()`.** `starttime` is the process creation time
  and is **unchanged** by `execve()`, so a process that passes the gate
  and then `exec()`s a different binary keeps the same pid/starttime; the
  exe verified at bind time is not the binary it later runs. The
  `/proc/<pid>/exe` value is thus only a snapshot at read time, not a
  stable identity.
- **SELinux domain transition.** A `setexeccon()`/policy-driven domain
  transition (which can accompany an `execve()`) likewise leaves
  starttime unchanged, so the verified `/proc/<pid>/attr/current` label
  is also only a read-time snapshot and may differ from the domain the
  peer runs under afterwards.

In short, the bracketing closes the *racing-recycle* hole during the
read window only; it does not turn a pid into a tamper-proof identity.
The check remains useful defence-in-depth on top of the
shell-client/`allowed_uid` gate (which it does not weaken — it only
further narrows it), but it must not be relied on as proof of who the
peer is or will be after binding.

**Remaining hardening (deferred to production milestone):**

- Hide the global from non-shell clients via a `wl_global` filter
  (analogous to the secctx global filter) so untrusted clients cannot
  even enumerate it. (The allowlist above restricts who can *bind*, not
  who can *see* the global.)
- Consider per-request policy for `EXCLUSIVE` + high-z layers, gating
  through the shell/broker decision channel rather than granting at bind
  time.
- General cross-silo policy (which clients may interact with which)
  is enforced downstream in qdshell/broker, but layer-shell EXCLUSIVE
  itself is compositor-granted with no broker round-trip.

See `todo/qdwin-codex-review.md` finding #1 for the original review.

### Security posture: wp_security_context_v1

`wp_security_context_v1` tags (`sandbox_engine`, `app_id`,
`instance_id`) are **advisory routing metadata, not trusted identity**.
Any client that can bind the manager can set arbitrary tag values.

Bind-time mitigation: `bind_qdwin_secctx_manager` restricts the manager
global to the bound shell client or the installed `qdistro-secctx-exec`
helper executable. Same uid is deliberately not an authorization basis. A
global filter (`qdwin_secctx_global_filter`) hides the global from
already-sandboxed clients and other unauthorized peers so nesting and
same-session self-minting are impossible. The env var
`QDWIN_SECCTX_OPEN=1` disables the bind gate for developer workflows and
tests.

Registry timing note: a client that becomes the bound shell after its
initial registry roundtrip must re-read the registry before binding the
manager, because the global is hidden until the shell role is claimed.

Compatibility note: qdistro's current tier launchers still use
`qdistro-secctx-exec` to create the listener. qdwin admits only the
installed helper path (overrideable for packaged layouts with
`QDWIN_ALLOWED_SECCTX_HELPER_EXE`) when the helper is either root-owned at
connect time or running as qdwin's configured `allowed_uid` with a direct
root launcher parent (`runuser`, `su`, `sudo`, or `pkexec`). Helpers under
any other uid are refused; deployments should keep tier launchers on the
compositor admin uid or run the helper as root. The helper executable inode
must be owned by root and not writable by group or other users; otherwise a
same-uid process could replace a misinstalled helper before a root launcher
runs it. qdwin also rejects helpers carrying
`QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1` on the admin-uid path unless qdwin
itself is in `QDWIN_SECCTX_OPEN=1` mode. Root-owned helper
clients are already privileged and may not expose a readable `/proc`
environment to qdwin, so they are admitted by uid/executable identity.
For admin-uid helpers, the root launcher must remain the live direct
parent through manager bind time; double-forking launchers fail closed.
The helper's strings are still advisory until the broker resolves the
client pid/starttime against launch records.

**Downstream verification (design intent):** qdwin forwards secctx tags
to the shell, but the authoritative identity check happens in
qdshell/broker via `VerifyClientIdentity`, which checks:

- pid start time (always checked, to detect pid reuse)
- uid match (always checked)
- `/proc/<pid>/exe` (checked when the forwarded exe is non-empty;
  skipped if qdwin could not read it at bind time)
- `/proc/<pid>/attr/current` (SELinux label; checked only when both
  the forwarded label and the live label are non-empty)

Cgroup membership is collected for audit/display but is not currently
used as a verification input. Namespace identity (`/proc/<pid>/ns/*`)
is not collected. The compositor deliberately does not duplicate this
verification; treating secctx as a routing hint rather than an
authentication credential is the intended layering.

Reference: `qdistro/doc/isolation-tiers.md`, `qdistro/doc/permissions.md`.

## Source of truth

`qdwin/qdwin-shell-v1.xml` is the single authoritative description
of the private protocol. Doc files in this directory describe
*policy* — what each interface is for and how the trust model
shapes it. Wire-level details (request/event signatures, since
versions, fixed-point precisions) belong in the XML, not here.
