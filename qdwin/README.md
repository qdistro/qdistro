# qdwin

A libweston shell plugin that hosts the qdistro desktop. Single-seat,
single-user, designed for one trusted shell client (typically
[qdshell](https://github.com/qdistro/qdshell)) plus arbitrary
sandboxed application clients placed by the shell.

qdwin is the compositor half of [qdistro](https://github.com/qdistro/qdistro)
but is published as a separate project: nothing in qdwin assumes the
qdistro broker, the qdshell QML stack, or the qdistro userland is
present. A different shell can adopt it.

## Role in qdistro

In qdistro, qdwin is the trusted Wayland compositor. It owns window placement,
trusted chrome boundaries, lock-layer enforcement, private qdwin protocols, and
the metadata that lets qdshell and the broker reason about which silo owns a
surface. It deliberately stays small and C/libweston-based while the modifiable
product code lives in Python/QML sibling repos.

**Mechanism, not policy.** qdwin does not make policy decisions by itself:
privileged operations surface as `*_pending` events and complete on
`*_decision` replies from the trusted shell. Policy lives in qdshell, qdlocker,
the qdistro daemons, and the broker, which use these mechanisms to implement
the "one owner, many silos, dynamic sessions" model. This contract — and the
deliberate choice to keep the plugin one C translation unit — is documented in
[doc/AGENTS.md](doc/AGENTS.md).

If you ARE building qdistro: the umbrella repo expects qdwin checked
out as a sibling directory (`../qdwin/`) so its daemons can compile
against qdwin's protocol XML. See the qdistro umbrella README for
the canonical 3-repo checkout layout.

## What's in here

- `qdwin/` — the shell plugin: `qdwin.c` (the single-TU compositor core),
  `qdwin-logic.c/.h` (pure decision kernels extracted for compiled unit
  testing), nested-compositor client glue, the protocol XML, the
  `backend/cffi` Python subpackage, and the `test_*.py` source-invariant
  checks that grep `qdwin.c` for policy call-site shape.
- `libweston-vendored/` — libweston 16 with a single local patch
  (`0001-allow-null-parent-xdg-popup.patch`) that lifts the assertion
  blocking root-level popups. Vendored because the patch hasn't
  landed upstream.
- `test-client/` — minimal Wayland clients used by the host and VM
  tests: `qdwin-probe`, `qdwin-bystander`, plus xdg-toplevel and
  clipboard helpers.
- `tests/unit/`, `tests/protocol/` — the meson-wired test suites (see
  [Testing](#testing)); `tests/lib/` holds shared test helpers.
- `tests/host/` — host-side scenarios (markdown playbooks + shell
  helpers + screenshot/ctrl helpers), headless backend only.
- `tests/gui/` — VM scenarios driving qdwin via virsh send-key and
  the qemu guest agent.
- `tests/apps/` — VM scenarios validating real toolkits (GTK, Qt,
  Electron, FLTK, Tk, Java Swing, wxWidgets, imlib2).
- `meson.build` — top-level build, including the registered test suites.

## What's NOT in here

The qdistro userland — broker, polkit agent, SDK, vault, admin app,
etc. — lives in the [qdistro umbrella repo](https://github.com/qdistro/qdistro).
The qdshell QML lives in [qdshell](https://github.com/qdistro/qdshell).

Several daemons that consume qdwin protocols (`qdistro-cursor-sprites`,
`qdistro-nested-pixelfeed`, `qdistro-secctx-exec`, `qdistro-tier1-exec`,
`qdistro-forward`) ship in the umbrella repo. They consume qdwin's XML
via the system `wayland-protocols` directory once qdwin is installed.

## Build

```sh
meson setup build
meson compile -C build
meson install -C build      # installs qdwin-shell.so under libdir/weston/
```

Then launch weston with the qdwin shell:

```sh
weston --shell=qdwin-shell.so
```

For the vendored libweston (only needed if the distro libweston isn't
patched), see `libweston-vendored/README.md`.

### Build variants (the `role` option)

`meson.options` exposes `role={host|guest}`:

```sh
meson setup build                     # role=host (default)
meson setup build-guest -Drole=guest  # tier-4-guest VM image
```

- `role=host` (default) — full host-side compositor. `qdwin_locker_v1`,
  `qdwin_nested_manager_v1`, and `qdwin_shell_v1` are all registered.
- `role=guest` — slimmed variant for the tier-4-guest VM image (the
  inner compositor that runs inside the guest qcow2). Compiles out
  `qdwin_locker_v1` and `qdwin_nested_manager_v1` (the guest is not a
  locker target and does not itself nest compositors). `qdwin_shell_v1`
  stays registered so the in-guest `qdwin-bystander --inner-display …
  --forward-session` mode can enumerate inner toplevels and ferry one
  outer xdg_toplevel out over waypipe-server's vsock transport to the
  host compositor.

When changing code near role-conditional sections, compile-check **both**
roles — a bare host build can miss errors the guest build hits, and vice
versa.

## Testing

Three complementary suites run against the same C source, all wired into
meson:

```sh
meson test -C build                    # everything
meson test -C build --suite logic      # compiled C unit test of qdwin-logic.c kernels
meson test -C build --suite protocol   # live pywayland clients against headless weston+qdwin
                                       # (skips cleanly if weston/pywayland are absent)
# the remaining tests are the source_invariant suite: Python scripts that
# grep qdwin.c to pin policy invariants (lock fail-secure, identity gates,
# nested-identity, popup-grab hardening, ...)
```

This mirrors what qdistro's CI (`qci host`) runs: `meson setup`,
`meson compile`, `meson test`.

Beyond the meson suites:

- `tests/host/` scenarios run only against `weston --backend=headless` —
  never a real seat.
- `tests/gui/` and `tests/apps/` scenarios must run **inside a libvirt VM**
  (they inject input via virsh/QMP and would hijack a real session if run
  on the host). The qdistro umbrella's `ci/bin/qci gui` gate provisions
  disposable VMs and drives these.

## Protocol

qdwin exposes a private protocol (`qdwin_shell_v1` — see
`qdwin/qdwin-shell.xml` for the current version) plus a nested-compositor
protocol (`qdwin_nested_v1`) for proxying second-tier compositors. Public
protocols supported: xdg-shell, xdg-decoration, xdg-activation,
ext-idle-notify, idle-inhibit-unstable-v1, cursor-shape-v1,
fractional-scale-v1, primary-selection-unstable-v1, security-context-v1,
zwlr-layer-shell-v1, wlr-output-management-unstable-v1, ext-workspace-v1.
(libweston-16 also contributes the core globals and, ungated,
relative-pointer, pointer-constraints, tablet-v2, tearing-control,
single-pixel-buffer and dmabuf — see doc/protocol.md.)

See [doc/protocol.md](doc/protocol.md), [doc/architecture.md](doc/architecture.md),
and [doc/nested.md](doc/nested.md).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

`libweston-vendored/` carries upstream's MIT license (see
`libweston-vendored/COPYING`). The `0001-allow-null-parent-xdg-popup.patch`
modifies libweston source and is offered under MIT to match upstream.
