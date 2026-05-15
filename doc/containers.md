# Containers (tier-2 podman) — first-class windowing & launcher

Landing page for the tier-2 podman story. Tier 2 is the first
isolation tier with **first-class qdshell launcher integration**: per
container, the apps installed inside it appear as badged entries in
the host launcher, and clicking one auto-starts the container (if
needed) and exec's the app into a nested compositor whose individual
xdg_toplevels surface as native peer toplevels on the outer qdwin.

For the underlying isolation story, see [isolation-tiers.md](isolation-tiers.md);
for the chrome / focus / clipboard contract every tier shares, see
[window-hierarchy.md](window-hierarchy.md) and [clipboard.md](clipboard.md);
for the secctx tagging that drives broker policy, see [permissions.md](permissions.md).

## Why tier-2 first

Tier 2 lands before tier 4 and tier 5 because:

- The wayland substrate (`qdwin_nested_manager_v1`,
  `wp_security_context_v1`, qdshell's chrome/proxy machinery) was
  already in place. The missing piece was a host-side
  spawn helper, a container image with an inner weston + the qdwin
  shell plugin, and the three bats drivers (s32/s33/s34) referenced
  from `tests/integration/vm/tiered-isolation.bats`.
- Podman is cheap to start (sub-second), no virtualization, no qga,
  no vsock, no SPICE — the entire isolation pipeline fits in two
  bash scripts plus a Containerfile.
- The qdshell launcher integration ("PodApps") it unlocks is
  structurally identical to the eventual "VMApps" service for tier-5;
  picking podman first establishes the contract that tier-5 will reuse.

## Image-per-workload model

Each workload ships its own image:

| Image                            | Workload      |
|----------------------------------|---------------|
| `qdistro/tier2-weston-terminal`  | weston-terminal (bats minimum) |
| `qdistro/tier2-firefox`          | firefox       |
| `qdistro/tier2-libreoffice`      | libreoffice   |
| `qdistro/tier2-dev`              | dev toolchain (gcc, gdb, make, ...) |

Rationale:

- **Smaller images, faster start.** A weston-terminal image is ~250
  MiB; a fat "everything" image would be multi-GiB and slow to pull
  or rebuild.
- **No shared mutable state between workloads.** A compromise in
  firefox's image can't leak into libreoffice's; tier-2 isolation
  becomes per-app, not per-container-family.
- **Smaller attack surface per image.** Each Containerfile installs
  only what its workload needs.
- **Simpler upgrade.** Bumping firefox in `qdistro/tier2-firefox` does
  not touch the libreoffice image.

The qdshell launcher merges every container's apps into one badged
list (see "Launcher UX" below); the user does not see the image
boundary.

Trade-off: each `.desktop` entry binds to a single workload image. If
a workload wants two related entries (`firefox`, `firefox-private`),
they ride in the same image — fine, no inefficiency.

## Architecture (one click → one window)

```
admin compositor (qdwin) ← outer
 │
 ▼ wayland-1 (UNIX socket, /run/user/1000/wayland-1)
 │
[qdistro-secctx-exec]    ← wraps the spawn with wp_security_context_v1
 │                         (sandbox_engine="qdistro.tier2",
 │                          app_id="<container>/<app>",
 │                          instance_id=<launch-token>)
 ▼
[podman run --userns=keep-id -v /run/user/1000:/run/user/1000 ...]
 │
 ▼ inside the container (uid 1000 via keep-id)
 │
[weston --shell=qdwin-shell.so]
 │       env QDWIN_NESTED_MODE=1
 │           QDWIN_OUTER_DISPLAY=wayland-secctx-NN
 │
 │  inner weston binds qdwin_nested_manager_v1 on the outer; for each
 │  inner xdg_toplevel it captures the surface to a PipeWire output
 │  and calls advertise_toplevel(pw_node, input_sink, app_id, title,
 │  origin_uid). Outer qdwin creates a proxy surface that qdshell
 │  decorates exactly like any other toplevel.
 │
 ▼
[guest app: weston-terminal, firefox, ...]
```

The chain is fully Linux-only, no VM, no remote protocol — just
container userns + a nested compositor.

## Secctx contract

Every tier-2 spawn emits a `wp_security_context_v1` tag carrying:

| Field            | Value                                  |
|------------------|----------------------------------------|
| `sandbox_engine` | `qdistro.tier2`                        |
| `app_id`         | `<container-name>/<app-binary-name>`   |
| `instance_id`    | A per-launch 32-hex token (LAUNCH_TOKEN) |

The broker's rules engine (`broker/qdistro_admin_rules.py`) matches on
all three. `instance_id` is the load-bearing field for the qdshell
cold-start UX: qdshell reads LAUNCH_TOKEN from `spawn-tier2.sh`'s
stdout and waits for a `toplevel_added` carrying the matching
`instance_id` to swap its placeholder taskbar entry for the real one.

`instance_id` is purely correlation, not auth. The auth boundaries are
peer-uid filtering on `qdwin_nested_manager_v1`, the secctx listener,
and the broker rules.

## Launcher UX (qdshell)

### Discovery (Phase B)

A host helper `qdistro-podapps-scan <container>` shells `podman exec`
into the container, enumerates `.desktop` files under
`/usr/share/applications` and `~/.local/share/applications`, and
writes them to a host-side cache:

```
/var/lib/qdistro/podapps/<container>/
    apps.json    ← parsed entries with appId, name, iconName, exec, comment
```

Icons are **not** fetched from the guest — the host's icon theme is
assumed to contain whatever the guest's `Icon=` value names. If the
host theme misses the name, qdshell falls back to a generic
placeholder glyph. This is the explicit simplification (memory note):
the cross-silo icon theme story is out of scope; rely on the host
having the same icon set installed.

The scan is triggered on container start, on explicit refresh from
qdshell, and once per day.

### Badging

In the qdshell launcher (Modules/Bar/Widgets/Launcher.qml) and dock
taskbar (Modules/Bar/Widgets/Taskbar.qml), every PodApps entry renders
with a small silo badge on `NIcon`:

- A subtle ring tint keyed off a hash of the silo name (so
  `firefox-on-tier2-private` and `firefox-on-tier2-work` are
  distinguishable at a glance).
- A bottom-right glyph indicating the silo class ("podman" — small
  container box icon).

The badge convention is defined in [ui.md](ui.md#silo-badges) so that
qdbrowser, qfileman, and future VMApps badges share the same vocabulary.

### Cold-start contract

The visible UX while a container starts up:

1. User clicks a badged app icon. qdshell calls `spawn-tier2.sh` and
   reads LAUNCH_TOKEN from its stdout.
2. qdshell inserts a **placeholder taskbar entry** into the Taskbar
   model immediately:
   - resolved icon (from host theme, or placeholder)
   - app name with silo badge
   - `NBusyIndicator` overlay
   - opacity reduced (e.g. 0.6) to differentiate from real toplevels
3. qdshell subscribes to `qdwin_shell_v1.toplevel_added` and watches
   for a toplevel whose `wp_security_context_v1.instance_id` equals
   the LAUNCH_TOKEN.
4. On match: remove placeholder, real toplevel takes its slot.
5. On 15s timeout with no match: remove placeholder, post a toast
   "Failed to start <app> in <container>".

No separate placeholder *window* is created — the taskbar entry alone
is the visible feedback. The pointer's standard cursor-feedback
(busy/wait) is left to qdwin / the desktop environment per the
existing conventions.

If the container is already running (warm start), the placeholder
phase is usually under 500ms.

## VM-side test infra

`tests/integration/vm/tiered-isolation.bats` covers tier-2 end-to-end
via three driver scripts:

| Driver                       | Asserts                                         |
|------------------------------|-------------------------------------------------|
| `s32-tier2-podman.sh`        | one container's inner toplevel becomes a peer  |
| `s33-tier2-input.sh`         | QDNI input wire alive, pointer button reaches inner |
| `s34-tier2-lifecycle.sh`     | two containers concurrent, stop A leaves B running |

The drivers self-stage from the bats host over the established
port-8765 http-staging convention (same as `s90-phase5-broker-e2e.sh`).
`s32` builds `qdistro/tier2-weston-terminal:latest` on first run if
missing; subsequent runs reuse the cached image.

Load-bearing assertions are **journal lines** (per the repo
convention; see `tests/integration/vm/README.md`). Pixel-level
correctness rides on the existing pixelfeed pipeline tested
independently in phase 6.8.

## Files

| Path                                                  | Purpose                                       |
|-------------------------------------------------------|-----------------------------------------------|
| `tier2/README.md`                                     | Operator-facing intro                         |
| `tier2/Containerfile.weston-terminal`                 | First workload image                          |
| `tier2/make-tier2-image.sh`                      | Image build script (image-per-workload)       |
| `tier2/entrypoint.sh`                                 | In-container PID 1                            |
| `tier2/weston.ini`                                    | Inner-weston config                           |
| `tier2/spawn-tier2.sh`                                | Host-side spawn wrapper                       |
| `tests/integration/vm/s32-tier2-podman.sh`            | s32 driver                                    |
| `tests/integration/vm/s33-tier2-input.sh`             | s33 driver                                    |
| `tests/integration/vm/s34-tier2-lifecycle.sh`         | s34 driver                                    |

## Status (2026-05-15)

- **Phase A — substrate:** ✓ container image build, host
  `spawn-tier2.sh` wrapper, nested-mode weston publisher, secctx
  tagging. End-to-end green in `phase7-tier2-podman` bats.
- **Phase B — `qdistro-podapps-scan`:** ✓ `tier2/podapps-scan.sh`
  shipped, installed as `/usr/bin/qdistro-podapps-scan`. Auto-fires
  when PodApps observes a container go off→running (see PodApps
  state poll), so the cache self-bootstraps.
- **Phase C — qdshell PodApps service:** ✓ `Services/Qdistro/PodApps.qml`.
  Reads the cache, exposes `apps` and `placeholders` ListModels,
  resolves placeholders by `wp_security_context_v1.instance_id` match.
  Forced-instantiated in shell.qml so the state poll runs whether or
  not a panel is open.
- **Phase D — Containers panel:** ✓ `Modules/Panels/Containers/ContainersPanel.qml`,
  wired through MainScreen + IPC (`qs ipc call containers togglePanel`).
- **Phase E — cold-start placeholder UX:** ✓ `Modules/Bar/Widgets/Taskbar.qml`
  consumes `PodApps.placeholders` (dimmed icon + busy spinner; replaced
  by the real toplevel on secctx match).
- **Phase F — pixel pipeline:** ✓ `qdwin_shell_v1.nested_proxy_pixel_source`
  wired in `qml-plugin/qdwin-binding.{h,cpp}`; Qdwin.qml spawns
  `qdistro-nested-pixelfeed` on each event.

## Future work

- **Delegate-side silo badge** ([ui.md](ui.md#silo-badges) — ring tint
  + bottom-right glyph). PodAppsProvider currently prefixes each
  entry's description with `[tier2/<container>]` as a stand-in;
  the launcher / taskbar delegate badge is the real fix.
- **Tier-5 VMApps service**: same shape as PodApps, badge ring is
  different colour. Lands once tier-5 `--vm` mode is finished
  (see [isolation-tiers.md](isolation-tiers.md#tier-5--per-app-vm-windowed-linux-guest)
  and `todo/qdwin-vm/tier5-vm-bringup.md`).
- **End-to-end click validation** is gated on synthetic input in the
  test VM — see `todo/qdwin-vm/ydotool-install-uinput-missing.md`
  and `todo/qdwin-vm/visual-gui-tests-pending.md`. Wire-level
  correctness is journal-asserted by `phase7-tier2-*` today.
- **Adopt user-created libvirt domains / podman containers**: out of
  scope. Tier 2 surfaces only containers spawned through
  `spawn-tier2.sh`.
