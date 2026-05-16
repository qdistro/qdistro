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
admin's active session pkexec the helper without password.

## Env knobs

See the header comment in `spawn-tier3.sh` for the full table. The
common ones: `TIER3_USE_SECCTX` (default 1), `TIER3_SECCTX_*` for
overriding the secctx triple, `TIER3_DEBUG=1` for waypipe debug,
`TIER3_GROUP` to override the gating group name.

## Limitations of the v1

- **Socket mode/group race**: waypipe creates the bridge socket with
  default umask; spawn-tier3.sh chmods + chgrps it after the fact.
  Sub-ms race window; failure mode is silo-side `ECONNREFUSED`.
  Acceptable as a v1 — see `todo/qdwin-vm/tier3-spawn-design.md`
  open question 2 for the upstream-patch alternatives.
- **Persistent silo state**: tier-3 silos are real Linux users with
  potentially long-lived processes outside any spawn invocation.
  Cleanup intentionally does NOT reap silo-uid processes that
  weren't started by the spawn wrapper. Use
  `qdistro-tier3-cleanup.sh --all` to force a sweep.
- **No clipboard wiring beyond what the broker already gates**: the
  tier-3-tagged toplevel reaches the admin compositor with its
  secctx triple set, which the broker's `CheckClipboardTransfer`
  rules-engine matches. The full silo→admin clipboard
  default-deny round trip is `s39-clipboard-gate.sh` (still DEAD
  pending broker + helpers).
