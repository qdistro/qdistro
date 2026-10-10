# Tier-5-Linux — per-app VM windowed (waypipe over AF_VSOCK)

Spec ref: `doc/isolation-tiers.md` ; isolation tier 5 in
`doc/architecture.md`. **Linux guests only** per `spec/00`
(memory `qdistro_linux_only.md`).

## Architecture

```
admin compositor (qdwin) ← outer, running on tty3
 │
 ▼ wayland-1 (UNIX socket, /run/user/1000/wayland-1)
 │
[waypipe client, admin uid] ← Wayland client of outer
 │ vsock: -s s<CID>:<port> --vsock
 ▼ AF_VSOCK
 │
[guest VM] ← libvirt-managed, vsock CID assigned
 │
 ▼
[waypipe server -- <app>] ← inside guest, runs the user app
 │
 ▼
[guest user app] ← Wayland client of waypipe-server's
 synthetic display
```

Each tier-5 toplevel arrives at the outer compositor as an ordinary
`xdg_toplevel` from the waypipe-client wl_client; gets the standard
`qdwin_nested_manager_v1` chrome + secctx + broker treatment exactly
like a tier-3 toplevel.

The bridge is **AF_VSOCK** instead of UNIX-domain. Everything else
mirrors tier-3 (`scripts/vm/tier3/`).

## Why waypipe-over-vsock

Picked over weston-rdprail-shell / legacy seamless-VM display / xpra / DIY-RAIL
in `spec/29` . Reasons:

- **Wayland-native both sides**: no XWayland; no remote-protocol re-encoding.
- **Reuses qdistro primitives entirely**: same waypipe wrapper,
 same `qdwin_nested_manager_v1`, same broker gates, same
 `wp_security_context_v1` (apply at vsock-accept time).
- **Distro-packaged**: `waypipe 0.11.0` on Tumbleweed. Confirmed
 vsock support via `--vsock` + `-s [s]CID:port` on 2026-04-27.
- **End-to-end proven** via loopback (CID=1) on
 `qdwin-tier4-fresh-260427-1041`: `wayland-info` enumerated the
 outer compositor's `wl_compositor` through the vsock bridge.

## Files

- `spawn-tier5.sh` — host-side wrapper. Starts a libvirt domain
 (with `<vsock>` device), waits for guest readiness, runs
 waypipe-client on vsock CID/port, triggers waypipe-server inside
 the guest via SSH or qemu-guest-agent, and runs the user app.
 Mirrors `tier3/spawn-tier3.sh` shape.
- `domain-template.xml` — libvirt domain template with
 `<vsock model='virtio'/>` and assigned CID. Linux-only guest
 per spec/00.
- `qdistro-tier5-cleanup.sh` — destroy + undefine helper.
- `vmapps-scan.py` — guest-base .desktop scanner. Reads
  `usr/{local/,}share/applications` inside the base qcow2 via
  libguestfs (read-only), applies XDG precedence/filtering, maps
  desktop IDs onto the stable `tier5/<appId>` keys, and publishes the
  catalogue manifest (`/usr/share/qdistro/vmapps/base.json`) atomically.
  Requires a guest `foot` binary whenever the catalogue is non-empty —
  foot is the launcher fallback and operator escape hatch.
  `build-guest-image.sh` runs it after customizing the base and leaves
  `qdistro-tier5-base.json` beside the qcow2.
- `vmapps-reconcile.sh` — the `qdistro-tier5-vmapps-scan.service`
  ExecStart: identity-samples the base, removes the manifest when the
  base is absent, stale-publishes when admin cannot read it, takes a
  fast path on a fresh valid manifest, and retries scans boundedly on
  mid-scan replacement or failure. Always exits 0.
- `loopback-bridge.sh` — same wrapper but using vsock CID=1
 loopback for in-VM smoke tests (no actual nested guest needed;
 exercises the data path).

## The VM-app catalogue

qdshell's tier-5 launcher catalogue is **derived from the guest base
image**, not hardcoded. The pipeline:

1. `vmapps-scan.py` reads the guest `.desktop` files out of the base
   qcow2 (libguestfs, read-only — or a directory with `--root` for
   tests), applies XDG precedence and filtering, expands Exec field
   codes, and maps desktop IDs onto stable `tier5/<appId>` keys
   (legacy aliases keep `tier5/firefox` etc. pinned across renames).
2. `build-guest-image.sh` runs the scanner after customizing the base
   and publishes `qdistro-tier5-base.json` beside the qcow2. A scan
   failure fails the image build — a base that cannot publish its
   catalogue is not installable.
3. `image/config.sh` stages the sidecar at
   `/usr/share/qdistro/vmapps/base.json` inside the product image and
   fails when the qcow2 was built without one.
4. On the installed system, `qdistro-tier5-vmapps-scan.service` +
   `.path` (installed by `install-tier5-for-vm.sh`) re-run the scanner
   through `vmapps-reconcile.sh` whenever the base image changes — a
   rebuilt or replaced base republishes the catalogue without a
   reboot. Absent base → manifest removed; unreadable base or scan
   exhaustion → a `stale` manifest is published so the degradation is
   explicit in the file itself.
5. `qdshell/Services/Qdistro/VMApps.qml` loads the manifest via a
   watched FileView, so a republish hot-swaps the catalogue in the
   running session. Missing/empty/malformed/stale manifest → the
   catalogue degrades to exactly the Foot entry (the operator escape
   hatch; this is why the scanner requires a guest `foot` binary).

The `appId` is the stable policy key end to end: Settings per-app
lifecycle overrides key on `[app:tier5/<appId>]` sections, and
`VMApps.launch()` passes it to `spawn-tier5.sh --policy-key`, so a
manifest row, its Settings override, and its spawn policy are always
the same identity.

## Modes

The wrapper supports two modes:

1. **`--loopback`** (default for tests): CID=1 loopback. waypipe-client
 listens on vsock:1:PORT; the inner app's waypipe-server is spawned
 on the same machine and connects to vsock:1:PORT. Same kernel,
 same uid space. Used by `phase7-tier5-loopback` bats. **No
 isolation** — purely an exercise of the data path.

2. **`--vm <vm_name>`** (production): CID=NNN guest VM. waypipe-
 client listens on vsock:NNN:PORT bound to the host side of the
 guest's vsock device; waypipe-server runs inside the guest via
 ssh / qga and connects from CID=2 (host) ← wait, that's
 backwards: from inside the guest, CID=2 is the host. Connection
 direction is guest→host. waypipe semantics: server connects to
 client. So inside guest, `waypipe server -s 2:PORT --vsock`
 reaches the host's listener.

## Per-app guests: adoption, not pooling

The domain is the SILO for a catalogue appId: `VMApps.launch()` derives
`<vm_name>` deterministically from the appId
(`t5-<sanitized-stub>-<64-bit hash(appId)>` — the hash covers the FULL
appId and is appended after the truncated stub), so every launch of the
same app names the same guest. Whatever the hash, isolation cannot
silently merge:

- At catalogue load, `_dropDomainCollisions` drops BOTH rows whose
  derived names collide, and `launch()` has no manifest-supplied name
  path — the appId is the only identity.
- The domain records its bound appId in `<metadata>` at define time.
  Adoption verifies bound == `--policy-key`: a LIVE domain bound to
  another (or no) app is refused (the wrapper exits with REAP=0 — it
  never tears down a guest it does not own), and a defined foreign
  domain is refused outright rather than reclaimed — confirmed qemu
  death proves no live guest is killed, but it does not make the
  foreign definition or disk ours; the owner relaunches under its own
  name and an admin removes a genuinely stale squatter deliberately.
  "Live" is decided by `qemu_pids_for_domain` alone —
  a pgrep on libvirt's `-name guest=<vm>` argv token narrowed to real
  qemu via `/proc/<pid>/comm`, with a distinct cannot-determine rc.
  `domstate` is not consulted for liveness at all: nested-virt libvirt
  can lie in both directions — "shut off" for a live qemu and stale
  "running" for a dead one — and no backing qemu process means death,
  since libvirt cannot run a VM without one. The same oracle guards
  the startup orphan-overlay reaper, the adopt/create decision, the
  idle-keep-warm window, the session wait loop, and the standalone
  `qdistro-tier5-cleanup.sh` helper. This covers collisions ACROSS
  catalogue reloads, where the running guest outlives the manifest
  that created it.

  Every destructive step fails closed on that oracle's third state:
  cannot-determine NEVER counts as dead. A pgrep candidate whose
  `/proc/<pid>/comm` is unreadable, a failed `virsh undefine`, and a
  failed defined-state query are all ambiguity, and ambiguity
  preserves the guest, the definition, and the overlay. Defined-state
  is decided by `virsh list --all --name` — an enumeration that
  SUCCEEDS is authoritative (name absent = confirmed absent); any
  query failure is unverifiable. It deliberately does not parse
  `dominfo` stderr: absence phrasing drifts across libvirt versions
  and loose substrings match outage text like "libvirt socket not
  found".
  Concretely: the wait loop consults the oracle every iteration and
  ends only on a confirmed-dead verdict (the app-exit watch remains
  the primary session-end signal); `create_domain` re-verifies the
  domain binding before `virsh start` and refuses on an unverifiable
  scan or a foreign binding; the orphan reaper takes the target's own
  hold before judging, touches only overlays whose domain is
  CONFIRMED ABSENT (it never undefines — a defined-but-dead domain
  has an owner record the reaper cannot adjudicate), and re-verifies
  death after a settle; and every
  overlay `rm` — in teardown, reaper, and
  `qdistro-tier5-cleanup.sh` — is preceded by confirmed-absent
  definition + confirmed-dead qemu, in that order. The cleanup helper
  additionally claims the domain's hold exclusively for its whole
  run, so it cannot destroy a guest a live wrapper owns or race a
  spawn between its checks and the unlink.
- `launch()` also requires the row's appId to be in the CURRENT
  catalogue, so a stale provider row cannot adopt a guest it no longer
  owns.
- The key itself is validated, not rewritten: an explicit
  `--policy-key`/`TIER5_POLICY_KEY` outside
  `[A-Za-z0-9][A-Za-z0-9._/-]{0,127}` is a hard error (silently
  falling back to the app basename would let two distinct bad appIds
  share one binding). The scanner drops out-of-charset appIds before
  publication and `_validRow` re-checks at load, so the wrapper only
  ever sees keys it can honor. Unkeyed direct/`--loopback` use derives
  a sanitized key from the app basename.

Ownership is a
flock(2) protocol on `/run/qdistro-tier5/hold-<vm_name>` — no daemon:

- Each wrapper carries a **shared session hold** (fd9) for its whole
  lifetime, so concurrent sessions of the same app share the guest —
  same appId, same trust domain, same silo tag. A spawn that would be
  refused (running foreign domain) exits BEFORE taking the hold, so a
  mismatched launch can never starve the true owner's teardown.
- A launch that finds the domain **running adopts it** under its own
  session hold (no cold start); a missing/stopped domain is (re)created
  under a short **exclusive** decision hold that serializes racing
  creators and teardown claims.
- On app exit the wrapper drops its hold and runs the idle window
  (`IDLE_SHUTDOWN_SECS`, low-mem early end): a relaunch adopting during
  the window cancels teardown. The **last lock holder** wins a
  *bounded* exclusive teardown claim (waits out transient holds, gives
  up on a genuine live session) — then **re-verifies the domain's
  appId binding** before destroying: the claim proves no wrapper holds
  the name, not that the domain is still ours, and a replacement bound
  to another app is left alone. Only then does it run destroy +
  undefine + overlay unlink.
- Locks are fd-bound, so a crashed wrapper releases everything; a
  leftover running/defined domain is adopted or reaped by the next
  spawn. `TIER5_KEEP_DOMAIN=1` still leaves the domain untouched for
  debugging — the next launch then adopts it.

Deliberately out of scope: cross-APP pooling (a shared guest for
different appIds would break the appId==silo identity) and longest-idle
eviction across different VMs. What exists is per-app relaunch reuse
with per-app policy sections (`[app:tier5/<appId>]`), which keeps the
isolation and lifecycle contracts identical to one-VM-per-launch.

## Cold-boot budget and diagnostics

`--vm` waits up to 180 seconds for `qemu-guest-agent` by default. Nested guests
can take substantially longer than ordinary VMs to boot while an eight-worker
CI run is creating other domains. Set `TIER5_QGA_TIMEOUT_SECS` to override the
deadline; values are validated and clamped to 30–600 seconds. Each guest-agent
request is itself bounded to five seconds, so an unresponsive agent cannot make
the overall deadline unbounded.

Set `TIER5_SERIAL_LOG` to an admin-writable path to retain the guest serial
console. The tier-5 VM integration probe uses `/tmp/s45-tier5-serial.log` and
keeps the domain/overlay until its assertions finish, then reaps both in its
exit trap. A failed cold boot therefore reports the spawn log and the last 200
serial lines instead of only reporting a guest-agent timeout.

## Limitations of the MVP

- **Only the loopback mode is fully wired** in this initial
 shipset. The `--vm` mode lays out the libvirt domain template
 + ssh / qga trigger plumbing but the in-guest helper
 (`qdistro-tier5-publisher`) and disk-image build are
 follow-ups.
- **No PipeWire-over-vsock yet.** Audio for tier-5 guests
 needs a separate vhost-user-pipe shim or vsock-tunneled
 pipewire-pulse. Tracked as a follow-up; tier-5-Linux apps
 with audio fall back to silent for now.
- **No clipboard wiring yet.** The broker `CheckClipboardTransfer`
 gate accepts tier-5 secctx tags via the rules-engine
 matchers shipped 2026-04-27 (qdwin v13), but the in-guest
 end of the clipboard plumbing is a follow-up.

## Linux-only

Per `spec/00` qdistro is single-tenant Linux; tier-5 guests are
Linux only. Memory: `qdistro_linux_only.md`.
