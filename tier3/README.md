# Tier-3 — different user (waypipe over AF_UNIX)

Spec ref: `doc/isolation-tiers.md` "Tier 3 — different user (waypipe
over UNIX)". Per `spec/02` row 3, the silo runs as a separate Linux
user and bridges to the admin compositor through waypipe over an
AF_UNIX socket gated by group ownership.

## Architecture

```
admin compositor (qdwin) ← outer, /run/user/1000/wayland-1
 │
 ▼ AF_UNIX
 │ /run/user/1000/qdistro-tier3-<silo>-<token>.sock
 │ (created by waypipe-client; chmod 0660 group=qdistro-tier3
 │  so silo uids in the group can connect)
 │
[waypipe client, admin uid, listening]
 │ wrapped by qdistro-secctx-exec so the outer Wayland connection
 │ carries sandbox_engine=qdistro.tier3, app_id=qdistro.tier3.<silo>,
 │ instance_id=<LAUNCH_TOKEN>
 │
 ▼ accepts connection from silo's waypipe-server
 │
[waypipe server, silo uid] ← runs as e.g. user1 via runuser
 │ XDG_RUNTIME_DIR=/tmp/qdistro-tier3-<token>
 │ WAYLAND_DISPLAY=wayland-tier3-<silo>-<pid>
 │
 ▼ exec'd by waypipe-server
 │
[silo app] ← weston-terminal / firefox / etc, runs as silo uid
```

Each tier-3 toplevel arrives at the outer compositor as an ordinary
`xdg_toplevel` from the host-side waypipe-client's `wl_client`,
tagged via `wp_security_context_v1` with `qdistro.tier3.<silo>`.

## Files

- `spawn-tier3.sh` — host-side wrapper. Validates the silo (must be
  in `qdistro-tier3` group), generates a per-spawn LAUNCH_TOKEN,
  starts waypipe-client as admin (secctx-wrapped), chmods the bridge
  socket so the silo can connect, then runs waypipe-server as the
  silo uid wrapping the inner cmd.
- `qdistro-tier3-cleanup.sh` — reaps orphan bridge sockets +
  per-launch silo runtime dirs.

## Install

`scripts/install/install-tier3-for-vm.sh` is the canonical installer.
It creates the `qdistro-tier3` group + `user1`/`user2` silo accounts,
adds the admin user to the group (so admin can chmod the bridge
socket), symlinks the spawn + cleanup helpers into `/usr/local/bin/`,
and installs the polkit policy so qdshell's launcher can pkexec the
spawn helper without re-authenticating.

## Why AF_UNIX (and not vsock / TCP)

- **Local-only by construction**: no network surface.
- **Group-gated**: file mode + posix group ownership is enough to
  cross uid boundaries without further capability negotiation.
- **Cheaper than vsock**: no vsock_loopback module, no kernel CID
  bookkeeping. AF_UNIX is the right transport when both halves are
  on the same kernel.

Tier-5 uses vsock instead because each silo is a separate kernel.
Tier-2 uses no waypipe at all — its container shares the admin uid
and nests a weston instead.

## CLI

```
spawn-tier3.sh <silo> -- <cmd> [args...]
```

The `--` separator is optional. Examples:

```
spawn-tier3.sh user1 -- weston-terminal
spawn-tier3.sh user1 firefox https://example.com
```

Run as root (uses `runuser` to drop to both admin + silo uid). The
polkit policy installed by `install-tier3-for-vm.sh` lets the
admin's active session pkexec the helper with **one** admin-password
prompt per 5-minute session (`auth_admin_keep`).

## Env knobs

See the header comment in `spawn-tier3.sh` for the full table. The
common ones: `TIER3_USE_SECCTX` (default 1), `TIER3_SECCTX_*` for
overriding the secctx triple, `TIER3_DEBUG=1` for waypipe debug.

Note: `TIER3_GROUP` is NOT a runtime knob — the group name is hard-
coded to `qdistro-tier3` because the bats wrappers grep the literal
string in spawn-tier3's log output. The install script has its own
`TIER3_GROUP` knob for choosing what to create; override both in
lockstep if you really need a different name.

When invoked via `pkexec`, security-sensitive env knobs are refused
(`TIER3_SOCKET_DIR`, `TIER3_SILO_RUNTIME`, `TIER3_USE_SECCTX`,
`TIER3_GROUP`, `TIER3_ADMIN_USER`, `TIER3_NO_REAP`, `TIER3_REAP_AGE`,
`TIER3_SECCTX*`, `TIER3_OUTER_DISPLAY`). Cosmetic knobs
(`TIER3_TITLE_PREFIX`, `TIER3_NO_GPU`, `TIER3_DEBUG`) stay allowed.

## Security model

Two-round independent code review (2026-05-16) closed the following:

- **Bridge socket race**: bridge socket born `0660 admin:admin` via
  `umask 0117` before backgrounding waypipe-client. Silos aren't in
  `admin` group, so cannot `connect()` until the chgrp flips group
  to `qdistro-tier3` — eliminates the inter-silo bridge-hijack window
  that the v1 had open via a "sub-ms race, ECONNREFUSED" comment.
- **Orphan reaper forgery**: live-owner check reads a sidecar PID
  file (`<sock>.pid`, written at socket-create time in `$SOCKET_DIR`
  mode 0710 — silos can't write there). Was previously `pgrep -af
  "spawn-tier3.*$token"` substring match, which hostile silos could
  forge via `exec -a "spawn-tier3.sh user1 dummy <token>" sleep`.
- **Silo name validation**: rejects `-` and other non-`[A-Za-z_][A-
  Za-z0-9_]*` chars to keep the trailing-32-hex token regex
  unambiguous.
- **Polkit `auth_admin_keep`** (was: `allow_active=yes`): closes the
  "any compromised admin-session process can pkexec arbitrary
  silo/cmd" hole. Cost: one password prompt per session.
- **Hard-fail on missing `qdistro-secctx-exec`** instead of silent
  `USE_SECCTX=0` downgrade. The wrapper provides the load-bearing
  `wp_security_context_v1` triple; absent, the tier-3 toplevel
  would arrive un-tagged and the clipboard gate would silently treat
  it as admin.
- **install-tier3-for-vm.sh validates pre-existing silo accounts**:
  refuses to co-opt user1/user2 if uid<1000, shell isn't bash/sh, or
  password isn't strictly locked (`L`/`LK`). Defense-in-depth against
  package-collision installs.

## Limitations of the v1

- **Persistent silo state**: tier-3 silos are real Linux users with
  potentially long-lived processes outside any spawn invocation.
  Cleanup intentionally does NOT reap silo-uid processes that
  weren't started by the spawn wrapper. Use
  `qdistro-tier3-cleanup.sh --all` to force a sweep.
- **Per-launch silo runtime dir** lives at
  `$SOCKET_DIR/runtime-<token>` (default `/run/qdistro-tier3/runtime-
  <token>`). Created 0700 owned by the silo uid, inside a 0710
  group-traverse parent — other silos in the group can `cd` into
  the parent but can't list it (no `r`) nor enter sibling subdirs.
  The token is 128-bit; full-path attacks would require knowing the
  token a priori.
- **Clipboard gate end-to-end**: tier-3 toplevels reach the admin
  compositor with their full secctx triple. The broker's
  `CheckClipboardTransfer` rules-engine matches, default-denies, and
  the rules path is exercised by `s39-clipboard-gate.sh` + the v14
  focus-injection path by `s48-focus-aware-clear.sh`. One residual
  PASS line in s39 is journal-soft (the "qdshell cleared the silo→
  admin selection" assertion needs real wl_data_offer.receive flow
  to a focused tier-3 toplevel — best done with the new
  Tier3FocusIPC inject-focus path landed for s48).

## VM validation

All 8 tier-3 bats pass green on `tier2-bats-260515-0917` as of
2026-05-16. See `qdistro/tests/integration/vm/tiered-isolation.bats`
for the @test blocks and `qdistro/tests/integration/vm/s{35,36,37,
38,39,40,41,48}-*.sh` for the drivers.
