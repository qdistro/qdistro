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

- `spawn-tier5.sh` — host-side wrapper, run **as admin** (root is
 refused). Owns the whole VM lifecycle in admin's `qemu:///session`:
 overlay + sidecar, domain define/start, the hold locks, the
 qemu-guest-agent trigger of the in-guest waypipe server, the session
 watch and teardown. Its one privileged step is the bridge below.
- `qdistro_tier5_bridge.py` — the root bridge, installed as a root-owned
 copy at `/usr/libexec/qdistro/qdistro-tier5-bridge` and run only via
 `pkexec` (polkit `org.qdistro.tier5.bridge`). See
 [Privilege split](#privilege-split).
- `domain-template.xml` — libvirt domain template with
 `<vsock model='virtio'/>` and assigned CID. Linux-only guest
 per spec/00.
- `qdistro-tier5-cleanup.sh` — destroy + undefine helper, run as admin.
- `vmapps-scan.py` — guest-base .desktop scanner. Reads
  `usr/{local/,}share/applications` inside the base qcow2 via
  libguestfs (read-only), applies XDG precedence/filtering, maps
  desktop IDs onto the stable `tier5/<appId>` keys, and publishes the
  catalogue manifest (`/usr/share/qdistro/vmapps/base.json`) atomically.
  Requires a guest `foot` binary whenever the catalogue is non-empty —
  foot is the launcher fallback and operator escape hatch.
  `build-guest-image.sh` runs it after customizing the base and leaves
  `qdistro-tier5-base.json` beside the qcow2. Guest paths probed via
  `stat` are interpolated into double-quoted guestfish commands, so
  the scanner refuses any path carrying `"`, `\`, or a control
  character — inside the quote they could break out and a `!cmd`
  line would execute on the host as the scanning user (sol r28).
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
 isolation** — purely an exercise of the data path. Needs the
 `vsock_loopback` module, which test setup loads (loading a module is
 privileged; the admin-run wrapper only checks for it).

2. **`--vm <vm_name>`** (production): CID=NNN guest VM. waypipe-
 client listens on vsock:NNN:PORT bound to the host side of the
 guest's vsock device; waypipe-server runs inside the guest via
 ssh / qga and connects from CID=2 (host) ← wait, that's
 backwards: from inside the guest, CID=2 is the host. Connection
 direction is guest→host. waypipe semantics: server connects to
 client. So inside guest, `waypipe server -s 2:PORT --vsock`
 reaches the host's listener.

## Privilege split

```
spawn-tier5.sh                          admin   (VM lifecycle, all files)
└─ pkexec → qdistro-tier5-bridge         root    (polkit auth_admin_keep)
   └─ runuser -u admin                   root    (direct root parent)
      └─ qdistro-secctx-exec             admin   (tags the Wayland connection)
         └─ waypipe … client             admin   (the host-side listener)
```

Nothing in the VM lifecycle needs root: libvirt is admin's
`qemu:///session`, and the overlays, sidecars, locks, logs and lifecycle
config all live in admin-owned directories. The lifecycle therefore runs
entirely as admin and never performs a root file operation on an
admin-writable path.

Two things do need root, and the bridge does exactly those:

- **A uid-0 direct launcher parent.** `qdistro-secctx-exec` accepts its
  trusted-launcher marker, and qdwin admits an admin-uid secctx helper,
  only under a live root direct parent. `runuser` is that parent; the
  bridge supervises it.
- **RegisterLaunch.** The broker accepts launch records only from root
  helpers. The bridge calls the existing accepted path — root
  `/usr/bin/dbus-send` naming `org.qdistro.AdminBroker1.RegisterLaunch` —
  with typed, fixed arguments. Registration failure aborts the launch.

The bridge starts under `python3 -I`, builds the chain's whole
environment itself (`env -i`), executes only fixed absolute paths it has
checked are root-owned, and targets the fixed user `admin`. Its caller
supplies three bounded tokens and nothing else:
`--vm <name>|--loopback loopback-<n>`, `--port 1024..65535`,
`--token <32 hex>`. Everything else is derived: engine `qdistro.tier5`,
app-id `qdistro.tier5.<name>`, broker silo `vm-<name>` (the key qdshell's
`ClipboardSilo.js` derives from that app-id; the broker compares them
exactly), instance-id = the launch token, host listener CID 2 —
CID 1 (`VMADDR_CID_LOCAL`) only in the explicit loopback mode.

The waypipe client's pid is learned over a pipe the bridge creates and
passes through runuser (`QDISTRO_LAUNCH_REPORT_FD`). secctx-exec marks it
close-on-exec at once, writes `<pid>` only after the exec of waypipe
succeeded (an exec-status pipe tells it), and writes nothing if the exec
failed. The bridge then verifies the pid against `/proc` — child of a
`qdistro-secctx-exec` that is the child of *its* runuser, all uids
admin, exe the installed `/usr/bin/waypipe`, nonzero starttime stable
across the checks — pins it with a pidfd, and registers the verified exe
and starttime. Only then does it print `READY <pid>` on stdout.

The bridge's stdin is the caller's liveness pipe. EOF — the wrapper
exiting, being killed, or a cancelled launch — makes it SIGTERM the
chain's process group (runuser leads it, so this also reaches the child
waypipe forks per guest connection, which is not the registered pid) and
the pinned waypipe, escalate to SIGKILL after a grace, and exit; runuser
also carries a parent-death SIGTERM. runuser is reaped only once its
group is empty, so the group id cannot be recycled under the signal.
When the chain exits on its own the bridge ends whatever is left of the
group and exits with its status. The wrapper treats the bridge's exit as
the end of the session.

`READY` means the client is exec'd and registered, not that it has bound
its vsock port. The wrapper reports `vsock listener ready` only once the
kernel's vsock_diag dump (sock_diag netlink) shows a LISTEN socket on the
port whose inode is one of that pid's own open sockets — ownership by
inode, never by process name, which another admin process could imitate.
A loopback server that connects earlier is refused, so the launch fails
if the client never listens within 10 s.

Authentication: `org.qdistro.tier5.bridge` is `allow_active=auth_admin_keep`,
`allow_inactive=no`, `allow_any=auth_admin`, with `exec.path` pinned to
the installed bridge. The qdistro polkit agent forces the PAM (password)
route for this action ahead of its user config and test overrides — a
broker yes/no vote, which an admin-session process can cast, does not
authorize it. Every launch authenticates: pkexec's polkit subject is the
`spawn-tier5.sh` process that launched it, and each launch is a fresh
process, so polkit's `auth_admin_keep` grant is reused only when that same
process runs pkexec again (`tier5-bridge-enforcing.bats`,
`keep-same-caller`). No long-lived helper exists to carry a grant across
launches; a second app launch prompts again (permissions-gui/63, S3).

Upgrade from the root-run lifecycle: its locks (`/run/qdistro-tier5`),
boot bind mounts (`/run/qdistro-tier5-boot`) and root ownership registry
(`/var/lib/qdistro/tier5-owned`) are retired by `install-tier5-for-vm.sh`
only when that lifecycle is quiescent — no old lock held, no root-run
spawn/cleanup process, no guest booted from the old mount dir
(`scripts/install/lib/tier5-legacy.sh`). Otherwise the migration is
deferred and nothing is deleted: removing the locks under a live old
wrapper would hide its hold from the new admin-run locks, so a new launch
could adopt and then destroy its guest. While either `/run` dir exists,
`spawn-tier5.sh --vm` and `qdistro-tier5-cleanup.sh` refuse to run;
re-running the installer after the old sessions end, or rebooting, clears
it.

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
- `launch()` also requires the row's appId to be in the CURRENT
  catalogue, so a stale provider row cannot adopt a guest it no longer
  owns.
- The domain records its bound appId in `<metadata>` at define time, and
  the overlay carries a `<disk>.appid` sidecar `<appId> <dev> <ino>` that
  binds it to the app and to the very inode created. A running domain is
  adopted only when its binding equals `--policy-key`; a live or defined
  domain bound to another app is refused (and a refusing wrapper never
  tears anything down); an existing overlay is booted only when its
  sidecar binds it, and is otherwise refused and preserved. The sidecar
  is written before the new overlay is linked into place, so a crash
  between the two leaves our own record without a disk, which the next
  create (under the create lock) replaces.
- The key itself is validated, not rewritten: an explicit
  `--policy-key`/`TIER5_POLICY_KEY` outside
  `[A-Za-z0-9][A-Za-z0-9._/-]{0,127}` is a hard error. Unkeyed
  direct/`--loopback` use derives a sanitized key from the app basename.

These checks protect against confused or racing wrappers and stale
catalogue rows, not against a hostile admin-uid process: everything here
— qemu, virtqemud, the overlays — already belongs to admin. The
privileged boundary is the bridge, above.

**Liveness.** "Live" is decided by `qemu_pids_for_domain` alone: one of
our processes with a `qemu-system-*`/`qemu-kvm` comm whose argv carries
`guest=<name>,` as the argument right after `-name` (split on NUL, so no
crafted argument fakes the pair, and literal, so `t5.a` never matches
`t5xa`). `domstate` lies in both directions under nested virt — "shut
off" for a live qemu, stale "running" for a dead one — so it is never
the liveness authority. The oracle has a third answer, cannot-determine,
and that is never treated as dead: ambiguity keeps the guest, the
definition and the overlay. Defined-state comes from `virsh list --all
--name` (a successful enumeration is authoritative; any failure is
unverifiable). Teardown never signals a qemu by pid: when `virsh
destroy` no-ops, the overlay is kept and the next launch adopts the
still-running guest. The overlay is removed only after the definition
is confirmed gone AND qemu is confirmed dead.

**Orphan reaper.** At startup a wrapper reaps overlays left by crashed
wrappers — only `t5-<stub>-<16-hex>` names backed by our base, whose
hold it can take exclusively, whose qemu is confirmed dead and domain
confirmed absent, whose sidecar binds the file and re-derives to its
name (`tier5_derived_name`, a byte-exact port of `_vmNameForApp` pinned
by a parity test), and whose app a valid, current catalogue has retired.
A published app's orphan is kept for its next launch.

**Cleanup tool.** `qdistro-tier5-cleanup.sh <vm>` (admin) claims the
domain's hold exclusively (refusing while a session holds it), destroys
and undefines a domain whose binding re-derives to its t5 name, and
removes the overlay under the same proofs as teardown; an overlay
without a sidecar needs `TIER5_CLEANUP_ALLOW_OWNERLESS=1`.

### Ownership protocol

flock(2) on files in `$XDG_RUNTIME_DIR/qdistro-tier5/` (admin's runtime
dir) — no daemon:

- **hold-`<vm>`, shared** for a wrapper's whole session, from before its
  adopt/create decision to its exit. Concurrent sessions of the same app
  share the guest. Every exclusive actor — teardown claim, orphan
  reaper, cleanup tool — conflicts with it. A spawn that would be
  refused for a live foreign domain exits before taking it.
- **create-`<vm>`, exclusive** around the dead-guest re-check and the
  create, so racing creators serialize; the loser re-checks and adopts.
- **live-`<vm>`, shared** once a session is ESTABLISHED — it adopted or
  created a guest bound to its app. This is the positive adoption signal.
- **Teardown**: an exiting wrapper releases its locks and claims
  hold-`<vm>` exclusively. Winning means no wrapper holds the domain;
  the winner re-verifies the binding, then destroys, undefines and
  removes the overlay. Losing the claim is not proof of adoption — the
  holder may be a relaunch still deciding, one that may yet refuse — so
  the wrapper keeps retrying until either the claim wins or an
  established session exists (every decision's own waits are bounded).
- **Idle keep-warm** (`IDLE_SHUTDOWN_SECS`, low-mem early end): after
  the app exits the wrapper keeps the guest for a relaunch and hands
  off only to an ESTABLISHED session; hold contention alone keeps it
  waiting, then the teardown rule above applies.
- **Relaunch during teardown**: the tearing-down wrapper holds the hold
  exclusively through its graceful shutdown. A relaunch waits for the
  decision budget (`TIER5_DECISION_WAIT_SECS`, 120 s) plus the worst-case
  teardown budget (maximum grace 300 s + agent fallback + destroy and
  undefine retries), then cold-starts after the teardown instead of
  failing on a fixed timeout.
- Locks are fd-bound, so a crashed wrapper releases everything; a
  leftover running/defined domain is adopted or reaped by the next
  spawn. `TIER5_KEEP_DOMAIN=1` leaves the domain untouched for
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
