# Isolation tiers

qdistro's isolation ladder is a graduated set of containment mechanisms,
selectable per app or per user's default. Higher tier = stronger
isolation = less seamless.

The outer-compositor chrome and policy model is **the same at every tier**:
qdwin owns the visible toplevel, qdshell decorates it, identity is carried by
the best available secctx/process metadata, and policy is enforced by the
broker's clipboard / handoff gates. Direct clients in tiers 0 and 1 use normal
`xdg_toplevel`; nested/container/VM paths advertise inner windows through
`qdwin_nested_manager_v1.advertise_toplevel`.

| Tier | Mechanism | Seamless? | v1 status |
|-------------------|-------------------------------------------------------------|-----------|-----------|
| 0. none | Direct Wayland client of admin compositor | Yes | Shipped |
| 1. SELinux | LSM restrictions, same Wayland connection | Yes | Shipped |
| 2. podman | User namespace; container has a nested compositor | Yes | Shipped |
| 3. Different user | Separate uid; waypipe bridges `wl_display` | Yes | Shipped |
| 3s. gVisor | Per-silo uid (`qt3s-*` account) rootless container under the gVisor `runsc` application kernel (systrap) | Yes — via a waypipe byte-stream bridge (no fd passing) | **Experimental, dev profile only** |
| 4. VM whole-window| KVM + libvirt + waypipe (nested qdwin) | Yes | **Experimental** |
| 5. VM per-app | KVM + libvirt + waypipe over `AF_VSOCK` | Yes | **Experimental** |
| 6. Remote machine | Separate physical machine; remote-output | No | Post-v1 |

**Default tier for new user-silo apps:** tier 1 policy is shipped, but direct
Tier-1 user launches are dev/test-only in hardened v1 until a root/broker
launcher is wired. Use Tier 2 or Tier 3 for production launches that need
authenticated compositor provenance today.

> **v1 supported ladder is tiers 0–3** (decision D3,
> `todo/decisions/v1-release-scope-2026-06-12.md`). **Tiers 4 and 5 ship as
> EXPERIMENTAL in v1** — the design is fixed and the plumbing reuses the
> tier-2/3 primitives, but the VM-backed paths are not part of the v1 security
> guarantee, are not exercised by the release test battery, and may fail or
> regress without a supported recovery path. Use them only for evaluation. See
> the per-tier **Experimental status** banners below for prerequisites and the
> unsupported-failure list. Tier 6 (remote machine) is post-v1 and documented
> for posture only.

## Tier 0 — none

Direct Wayland client of admin's compositor. No containment at all.
Reserved for fully-trusted first-party apps.

## Tier 1 — SELinux sandbox

Sits between tier 0 (no containment) and tier 2 (rootless podman with own
user namespace + nested compositor). The "lightest containment that's
still enforced" on the supported Tumbleweed hardened bootstrap path.
`daily-driver` and `release` profiles refuse to finish unless SELinux is
Enforcing; `--profile=dev` and `QDISTRO_ALLOW_PERMISSIVE=1` are explicit
permissive/debug paths and are not release evidence.

### Threats tier 1 blocks

> **v1 provenance scope:** the SELinux domain and broker launch gate ship, but
> the direct `qdistro-tier1-spawn` entry point does not provide the trusted
> root-parent secctx attestation in hardened profiles. It refuses untagged
> direct launch unless `QDISTRO_PROFILE=dev` is set. The bullets below are the
> intended Tier-1 policy boundary once a root/broker launcher path is added; do
> not treat direct Tier-1 as a v1 production clipboard/handoff provenance
> guarantee.

- An app *in the same uid* reading another app's clipboard via Wayland
 selection without going through the broker gate.
- The same app using broad `/proc/<pid>/maps`-style introspection against
 sibling domains.
- The same app calling `setuid` / `ptrace` to escalate within the uid.
- Random `/dev` / `/sys` access beyond the narrow allowed list.

### Threats tier 1 does not block

- Kernel-level escapes — mitigated by reduced syscall surface but tier 1
 is not seccomp-bpf.
- Side-channel attacks (cache, frequency, etc.).
- Hardware-bus access (USB, GPU shaders) — tier 2 podman is the next
 layer that adds device-cgroup whitelist.

### Implementation

The v1 tree ships a custom SELinux module `qdistro_tier1` cloned from Fedora's `sandbox.te`
(the non-X variant — qdistro is Wayland-only) with Wayland, PipeWire,
DRI, and broker rules layered on. A wrapper binary `qdistro-tier1-exec`
wraps `qdistro-secctx-exec` with a `setexeccon()` call on the exec edge —
two independent attestations of the same identity: SELinux type for
enforcement, `wp_security_context_v1` tag for routing. In hardened v1, the
secctx half requires a direct root launcher parent; the current direct helper
therefore refuses untagged production launch rather than silently degrading.

A spawn helper `qdistro-tier1-spawn` takes `(silo_user, app...)`, asks
the broker for `qdistro.tier1.spawn:<canonical-app-path>`, and calls
the wrapper only on an explicit `allow`. `unknown`, broker errors, and
missing broker tooling fail closed; Tier-1 launch authorization is part
of the security boundary, not advisory logging. This action namespace is
rules-only; approval-cache rows and hook verdicts do not authorize
launch.

### Filesystem labelling strategy

**Type, not mount.** `$HOME` stays labelled `user_home_t`; a narrow
interface allows managing a per-app relabelled tree
(`qdistro_tier1_config_t`). Per-app private state lives under
`~/.local/share/qdistro/tier1/<app>/` and is pre-relabelled by the
launcher. The v1 policy does not grant broad `user_home_t:file` reads.

The seunshare-style alternative (tmpfs over `$HOME` and `/tmp`, relabelled
`sandbox_file_t`) needs setuid root or `CAP_SYS_ADMIN` and inherits
seunshare's setuid-race lineage. Type-not-mount avoids setuid root and
integrates with SELinux's existing labelling story.

### Audit integration

AVC denials land in `/var/log/audit/audit.log`. The broker consumes them
via an audispd plugin and writes one audit row per denial with subject
context (which silo it came from). The audit log gains a
`selinux_subj_type` column.

### Compatibility with `wp_security_context_v1`

Tier 1 sets **both** the SELinux type and the secctx tag — they answer
different questions:

- SELinux: "is this syscall / file access allowed?"
- secctx: "which silo's clipboard does this paste come from?"

The broker independently verifies via `/proc/<pid>/attr/current` that the
process is actually in `qdistro_tier1_t`. If the two disagree, broker's
`CheckPermission` denies (defence in depth).

Scope of "independent verification" — both broker paths now re-verify
process identity, via two mechanisms:

- **Direct broker authorization**: the broker resolves the D-Bus caller's
  pid and checks its SELinux context (`/proc/<pid>/attr/current`) itself.
- **qdshell-mediated gates** (clipboard set/receive, handoff activation):
  Option B closes the former gap. qdwin captures the source/destination
  client's `(pid, starttime, uid, exe, selinux_label)` at secctx-bind time
  (via `SO_PEERCRED` + `/proc`) and forwards it on the
  `qdwin_shell_v1.toplevel_peer_identity` event (since protocol v22).
  qdshell forwards that tuple to the broker's `VerifyClientIdentity`, which
  re-resolves the *live* process against `/proc` and returns true only when
  the live process still matches — anchored on the field-22 starttime, with
  the uid, exe, and SELinux-label axes additionally enforced when available
  (see the next paragraph for exactly when each axis is skipped vs. failed).
  The clipboard/handoff "same-silo → allow" short-circuit fires **only**
  when qdshell has verified **both** the source and destination endpoints
  this way (`identity_verified=true`); otherwise the decision falls through
  to the cross-silo rule path, which is default-deny.

What is and isn't verified on the qdshell-mediated path: the live process
must exist and its `/proc/<pid>/stat` field-22 starttime must equal the
forwarded value — this is the always-enforced anti-PID-reuse anchor. The
remaining axes are each enforced only when both sides can supply a value,
otherwise that axis is skipped (not failed):

- uid: enforced unless `/proc/<pid>/status` is unreadable (`_read_proc_uid`
  returns `None`);
- exe: enforced unless the forwarded exe OR the live `/proc/<pid>/exe`
  read is empty/`?`;
- SELinux label: enforced only when both the forwarded label and the live
  `/proc/<pid>/attr/current` are non-empty — i.e. skipped when SELinux is
  off / unconfined.

The hard verification floor is therefore `(pid, starttime)`; in practice
uid is also present (the broker can almost always read
`/proc/<pid>/status`). Note the broker trusts qdshell's
`identity_verified` boolean per gate call — it is qdshell that requires
BOTH the source and destination endpoints to verify before passing
`identity_verified=true`. This is acceptable because `VerifyClientIdentity`
and the three gate methods are denied to non-admin / default-context users
by D-Bus policy (`org.qdistro.AdminBroker1.conf`) — only the admin uid and
root may call them — so a non-admin same-uid sandboxed client cannot invoke
them, and starttime is kernel-attested.

## Tier 2 — podman / container

Default for most user-owned apps. Rootless podman + `--userns=keep-id`
plus a per-container runtime dir at
`$XDG_RUNTIME_DIR/qdistro-tier2/<launch-token>/` (mode 0700,
admin-owned, rm-rf'd on spawn-script exit). The host's `/run/user/<uid>`
is **not** exposed; only the resolved outer wayland socket
(`wayland-secctx-NN` when wrapping with `qdistro-secctx-exec`, or
the unwrapped name like `wayland-1` when `TIER2_USE_SECCTX=0`)
and any `pipewire-N`/`pipewire-N-manager` sockets that exist at spawn
time are bind-mounted in as individual files. The container therefore
can't see the dbus session bus, ssh-agent, gnupg-agent, or sibling
tier-2 sockets.

Hardening flags applied by `tier2/spawn-tier2.sh`:

  - `--cap-drop=ALL` (CapEff = 0)
  - `--security-opt=no-new-privileges`
  - `--network=none` by default, or `pasta` when the trusted
    session-manager launch stanza requests egress
  - `--pids-limit=512` (only delegated cgroup controller in the
    typical Tumbleweed user@1000.service setup; memory/cpu need
    `Delegate=memory cpu pids io` drop-in to use)
  - `--ipc=private --pid=private`
  - `--read-only` rootfs + tmpfs at `/tmp`, `/var/cache`,
    `/home/admin/.cache`, `/run` (ENOSPC on any image-rootfs write)
  - `--security-opt=seccomp=tier2/seccomp/<workload>.json`; missing profiles
    fail closed in hardened v1 profiles

The container's nested compositor (qdwin-shell.so) connects to the
outer admin compositor's Wayland socket and advertises each inner
toplevel via `qdwin_nested_manager_v1`.

In hardened v1 (`QDISTRO_PROFILE=daily-driver|release`), Tier-2 launches use
the root-launcher topology:
`qdistro-tier2-silo@<name>.service` (root) →
`qdistro-tier2-silo-launch` (`env -i`) →
`spawn-tier2.sh TIER2_ROOT_LAUNCHER=1` →
`runuser -u admin -- qdistro-secctx-exec` →
rootless admin `podman run`. The root process exists only as the direct trusted
parent for secctx and for bookkeeping; resolver, broker authorization, and
podman run as admin. Direct admin `spawn-tier2.sh` launch is dev/test-only
because it cannot produce qdwin's root-parent secctx attestation.

`spawn-tier2.sh` gates every launch through broker `CheckPermission`
before it creates the per-container runtime dir or runs podman. The
action is `qdistro.tier2.spawn:<workload>/<app-basename>` and only an
explicit rules-file `allow` launches; broker errors, missing D-Bus
tooling, `unknown`, `deny`, empty replies, and malformed replies fail
closed. This spawn namespace is rules-only: approval-cache rows and hook
verdicts do not authorize creating a new tier-2 sandbox.

Tier-2 is the **first tier with first-class qdshell launcher integration**
(badged app icons, click-to-launch, placeholder taskbar entry on cold
start). The full design — image-per-workload model, secctx contract,
podapps discovery, cold-start UX — is in [containers.md](containers.md).
Host-side bits live under `tier2/`; bats coverage is the
`phase7-tier2-*` block in
`tests/integration/vm/tiered-isolation.bats`.

## Tier 3 — different user (waypipe over UNIX)

**Status: shipped 2026-05-16.** Primary mechanism for data-silo
separation. `qdistro-tier3-spawn <silo_user> -- <app...>` runs
`waypipe client` as admin uid 1000 (creating a bridge socket against
the local `wayland-1`), and `waypipe server -- <app>` as the silo
uid (which connects to the same socket and exposes a synthetic
`wayland-tier3-<silo>-<pid>` display for the app).

Cross-uid socket access is gated by the `qdistro-tier3` group — the
silo can't reach `wayland-1` directly because `/run/user/1000`'s file
permissions keep other uids off the socket (`QDWIN_ALLOWED_UID` is a
*connected-client* gate: it limits which uid may bind the privileged
globals — shell, nested manager, layer-shell, IME, secctx manager —
not a connect-time `SO_PEERCRED` filter; every peer qdwin sees from
tiers 0–3 is the admin uid anyway). waypipe-client on the admin side
re-marshals the protocol for the silo. Default `--no-gpu`
(SHM-only, VM-safe; flip on accel3d clones); `--oneshot` per bridge
so each silo lifecycle is independent.

The bridge socket lives at
`/run/qdistro-tier3/qdistro-tier3-<silo>-<token>.sock`. Mode 0660
group `qdistro-tier3`, born under `umask 0117` so the inter-silo
hijack window is closed before waypipe accepts the first connection.
Each spawn writes a sidecar `<sock>.pid` file so the orphan reaper
has a forgery-resistant way to check whether the owner is alive.

Each tier-3 toplevel arrives at qdwin tagged via
`wp_security_context_v1` (engine `qdistro.tier3`, app_id
`qdistro.tier3.<silo>`, instance_id = the spawn's LAUNCH_TOKEN).
qdshell's `Tier3Apps` singleton filters `Qdwin.windows` on the
prefix and exposes per-silo chrome + colour via the standard
qdshell rendering path.

For headless test-driving of cross-silo focus injection (Qubes-style
focus-aware-clear from spec/10 v14), qdshell ships
`Tier3FocusIPC` — a Quickshell IPC handler bound to target
`tier3focus` with `injectFocus(handle)` / `clearSelection(seat)` /
`findSiloHandle(silo)` / `selectionState()` operations. Driven by
`qs -p /usr/share/quickshell/qdshell ipc --any-display call …` from
the bats drivers.

Reference: `qdistro/tier3/README.md` for the operator-facing entry
point; `tests/integration/vm/s{35..41,48}-*.sh` for the 8 bats
drivers.

## Tier 3s — gVisor application kernel (Experimental, dev profile only)

> **Experimental research tier, dev profile only.** Not part of the v1
> security guarantee, not selected automatically, and refused on every other
> profile. Contract: `tier3s/CONTRACT.md`; plan and owner decisions:
> `todo/paravirt/` (D1–D8, O1–O9).

The app runs in a rootless podman container owned by a **dedicated per-silo
host account `qt3s-<silo>`** (provisioned lazily at first launch, validated
on every launch; paravirt D4 model A). `--userns=keep-id` maps the caller to
the same numeric guest uid, so the workload runs as the silo uid on both
sides of the gVisor boundary and silo-owned host state stays silo-owned
inside. The OCI runtime is gVisor `runsc` (systrap platform), so its system
calls are served by the gVisor Sentry rather than the host kernel. Tier 3s
sits beside tier 3 rather than in the integer ladder: the integer tiers stay
stable in every stored contract (paravirt D2). Phase A shipped headless
silos; Phase B added GUI silos through a waypipe byte-stream bridge, since
gVisor blocks `SCM_RIGHTS` to host sockets and direct Wayland fd passing is
impossible; Phase C put the launches under an owning cgroup scope and Phase
C2 moved every podman/runsc call to the silo account (admin keeps only the
host-side bridge client and the broker/resolver calls; root supervises,
creates the scope and tears down — nothing runs podman or runsc as root
except the probe's hash-verified `runsc --version`).

- **Identity.** `qt3s-<silo>` is a real host account in the
  `qdistro-tier3s` group with an automatic `/etc/subuid` + `/etc/subgid`
  range; the launch refuses a pre-existing or foreign account (marker
  GECOS, uid ≥ 1000, not the admin uid). Runtime state keys off the silo
  uid: `/run/qdistro-tier3s-rt/<uid>` (the podman runtime dir — a silo has
  no logind session) and `/run/qdistro-tier3s-runsc/<uid>` (the per-uid
  runsc state root the wrapper enforces). Each silo keeps its **own**
  podman image store — a shared `additionalimagestores` listing works but
  cannot *run* under a foreign uid map, so images are pulled or `podman
  load`ed as the silo account.
- **Launch.** Explicit only (paravirt O6): `SessionManager1.CreateTier3sSilo
  (name, workload, template_silo, network)` then `StartSilo`, which starts
  `qdistro-tier3s-silo@<name>.service`. Its root helper execs
  `spawn-tier3s.sh`, which refuses a non-dev profile, runs the prerequisite
  probe as `qt3s-<silo>`, asks the broker for
  `qdistro.tier3s.spawn:<workload>/<app>` (rules
  only, see [permissions.md](permissions.md)), and runs every podman call as
  the silo account. A refusal or failure at any step fails the launch; there
  is **no fallback** to tier 2 or 3. Tier 3s pod apps are not shipped yet.
- **Network:** `none` only (paravirt O3), both podman's and runsc's.
- **Lifecycle.** Each launch has a root-only control record under
  `/run/qdistro-tier3s-ctl/<token>/`, a root-created owning scope
  `qdistro-tier3s-<token>.scope` bound to the launch unit, and one teardown
  path (`qdistro-tier3s-cleanup`) that never acts by uid or process name.
  `StopSilo` reports Stopped only when the unit, the container and the
  control record are all gone. Freeze/resume is **unsupported** (refused).
  A session-manager restart stops every live tier 3s launch and reaps
  leftovers; wanted silos are relaunched with a fresh token.
- **GUI bridge.** The sandbox runs `waypipe server`; on the host a
  `runuser -> qdistro-secctx-exec -> waypipe client` chain (uid 1000 —
  the admin uid is a trusted parsing surface, see
  [threat-model.md](threat-model.md)) connects one end to the
  compositor's `wayland-secctx` listener (tagged `qdistro.tier3s`,
  `qdistro.tier3s.<silo>`, launch-token instance) and accepts a single
  connection from the sandbox on `$LAUNCHES/<token>/link.sock`. The
  client binds the socket admin-owned, then the root spawn chowns it to
  the silo account `0600` — gVisor's DAC check on `connect()` runs in
  guest uid space, so host groups and ACLs cannot authorize the sandbox —
  under a `0711` per-launch dir (the gofer resolves the path for the
  guest). The launch dir is bind-mounted into the sandbox **read-only**
  at `/run/qdistro/link` — the sandbox only `connect()`s, so it cannot
  exhaust the host's `/run` tmpfs. The attach is **single-shot: there
  is no reconnection** — once the client consumes `link.sock` it is
  unlinked, and a broken bridge means relaunching, not reattaching.
  inotify-style filesystem watching is likewise **in-sandbox only**:
  nothing watches a host path. The bridge client is registered with the
  broker's launch-record store (`RegisterLaunch`, anti-reuse
  starttime anchor) before `podman run`; the store is in-memory with a
  TTL, so a broker restart forgets live registrations — affected
  launches must be restarted to regain attested identity.
- **GUI seccomp** is per-workload (`weston-terminal`, `foot`): the
  launch passes `--security-opt seccomp=<profile>` to runsc's OCI
  converter. Notable decision: `fchmodat2` is forced DENY because the
  runsc converter silently drops that syscall name from an allow list
  — an allow entry would be inert, so `chmod -h`-style calls get EPERM.
- **Resource limits (ΔB7 decision).** The launch unit carries
  `MemoryMax=1G` and `TasksMax=96`, bounding the *trusted* bridge
  client (which lives in the unit's cgroup, not the sandbox scope, and
  mirrors sandbox-controlled shm). The sandbox itself is bounded by its
  owning scope, which is created root-owned with `TasksMax=1024`,
  `MemoryMax=2G`, `MemorySwapMax=0` and `CPUQuota=200%`; only the scope's
  `cgroup.procs`/`subtree_control`/`threads` are delegated to the silo
  uid — every limit file stays root-owned, so the silo can move
  processes into its scope but cannot raise any bound (s130 proves the
  delegation split plus in-scope OOM kill, `pids.max` fork-bomb bound
  and `cpu.stat` throttling). Declined: `-c none` for runsc (it already
  runs without a cgroup manager under `--ignore-cgroups`) and a
  dedicated cgroup scope for the bridge (the unit cgroup is the
  boundary).
- **Packaging exception (paravirt D1).** `runsc` is not in
  openSUSE-Tumbleweed-Oss, unlike the Oss-only precedent of tiers 4 and 5.
  The owner chose a sha512-pinned upstream dated release
  (`tier3s/RUNSC_RELEASE`), provisioned **on demand** by
  `tier3s/provision-runsc.sh`. It is not in the base image and not installed
  by `install-session-manager.sh`, which installs only the launch scripts,
  units and seccomp profiles. Without `runsc` the probe names the missing
  prerequisite and every tier 3s launch is refused.
- **Not claimed:** no KVM comparison (paravirt O5); the gVisor boundary
  is not a VM-equivalent claim — the Sentry and gofer remain trusted
  host processes, side channels are out of scope, and DoS is bounded
  only by the owning scope's limits; no reconnect path; runsc
  confinement is the `qdistro_tier3s_t` SELinux domain
  (`selinux/tier3s/`, enforcing-qualified — Phase D);
  `lineage_enforce` is **opt-in** — the
  clipboard/identity gates run in shadow mode unless it is enabled (the
  s127 scenario enables it for the live path); no tier-3s pod apps yet.

## Tier 4 — whole-VM windowed

> **Experimental in v1 (D3).** Not part of the v1 security guarantee and not
> exercised by the release test battery.
> **Prerequisites:** `libvirt` + `qemu` from `openSUSE-Tumbleweed-Oss`,
> hardware virtualization (KVM) enabled, and a guest image carrying `waypipe`
> + a nested qdwin. **Unsupported failure modes (no recovery path in v1):**
> waypipe-over-vsock disconnect leaving an orphaned viewer toplevel; guest
> qdwin crash; secctx tag not applied if `qdistro-secctx-exec` wrapping is
> skipped; clipboard/gate behaviour under guest compromise; GPU/virgl
> instability. Treat tier-4 silos as evaluation-only.

**libvirt + QEMU + waypipe** (nested qdwin in the guest) as one chromed peer toplevel,
mirroring tier 2's container pattern.

Stack:

- libvirt driving QEMU. Both packaged on Tumbleweed.
- Display: waypipe over AF_VSOCK as a Wayland toplevel. The host runs
 `waypipe --vsock client` (the vsock **listener**); the guest's
 `qdistro-tier4-publisher` runs `waypipe --vsock server`, which
 **connects out** to the host. The host wraps the waypipe client with
 `qdistro-secctx-exec` so the compositor sees the VM silo tag.
- The outer wraps the viewer toplevel via `qdwin_nested_manager_v1`
 exactly like tier 2 today.
- Clipboard: Wayland data-device over waypipe (same `ClipboardGate.qml` as tier-3),
 `CheckClipboardTransfer` gate.
- Audio: `-audiodev pipewire` + `virtio-snd`; routes to host PipeWire via
 the existing per-uid bridge.

### Why this is the right tier-4 default

- Every package required is already in `openSUSE-Tumbleweed-Oss`.
- One-toplevel-per-VM matches tier 2 / 3's already-shipped pattern:
 chrome differentiation, broker gate, secctx tag — none need rework.
- No external repos / no fork / no downstream patch carry.

### Risks accepted

- **Nested compositor cost.** The guest runs qdwin plus app workloads, so
 default memory is higher than tier 5b.
- **No GPU passthrough by default.** Use `virtio-gpu-gl` with virgl when 3D
 matters. VFIO + Looking-Glass is rejected as the tier-4 default because
 it's gaming-niche and requires two GPUs.

New Linux VM images should be defined with the NixOS-language contract in
[vm-definitions.md](vm-definitions.md). Existing Tumbleweed image builders may
continue for current tier-4/tier-5 images, but new VM-backed silos should carry
a declarative guest definition, lock reference, build lineage, and qdistro
runtime policy.

<a id="tier-5-per-app-vm-windowed-linux-guest"></a>

## Tier 5 — per-app VM windowed (Linux guest)

> **Experimental in v1 (D3).** Not part of the v1 security guarantee and not
> exercised by the release test battery.
> **Prerequisites:** the tier-4 stack plus a host `waypipe --vsock client`
> listener per app launch and the in-guest `qdistro-tier5-publisher`, which
> **connects out** to it (`waypipe --vsock -s "2:<port>" server`).
> **Unsupported failure modes (no recovery path in v1):** vsock
> port exhaustion / collision across many per-app launches; publisher crash
> orphaning host waypipe clients; secctx tag missing if not applied at
> vsock-accept; per-app audio/input multiplex stalls. Treat tier-5 silos as
> evaluation-only.

**waypipe over `AF_VSOCK`.** A plumbing-only extension of the existing
tier-3 wrapper.

- The host runs `waypipe --vsock client -s "<host cid>:<port>"` per app
  launch — the **listener** end — mirroring tier 3's UNIX-socket bridge
  but across the VM boundary.
- The VM runs a tiny `qdistro-tier5-publisher` process that **connects
  out** to that listener (`waypipe --vsock -s "2:<port>" server`) and is
  the inner waypipe endpoint. Inside the guest, user apps are ordinary
  Wayland clients of the publisher's synthesized display.
- Outer-side: each app shows up as a regular `xdg_toplevel` from the
  waypipe-client `wl_client`; gets the standard
  `qdwin_nested_manager_v1` + secctx + chrome treatment.
- Pixels, input and clipboard all ride the single waypipe byte stream —
  there are no separate vsock multiplexes (AF_VSOCK does not implement
  `SCM_RIGHTS`, which is also why the secctx tag is stamped host-side on
  the client, not guest-side on the publisher).
- Audio: `qemu -audiodev pipewire` with an in-guest virtio/HDA codec. The
 guest's audio backend talks to QEMU on the host; QEMU streams to the
 admin uid's PipeWire daemon via the existing per-uid socket. No vsock
 plumbing needed for audio.

### Why waypipe-vsock

- **Wayland-native both sides.** No XWayland for guest apps.
- **Reuses qdistro's existing primitives entirely:** waypipe wrapper,
 `qdwin_nested_manager_v1`, broker clipboard / handoff gates,
 `wp_security_context_v1` (applied at vsock-accept time exactly like
 uid is applied at UNIX-accept today).
- Plumbing-only; no new compositor protocols, no fork carry.

### Rejected alternatives

| Option | Why rejected |
|----------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Legacy VM seamless display** | Listed as "future features" since 2010-2011; abandoned; no maintainer. 15-year roadmap stall. |
| **weston-rdprail-shell + VAIL** (WSLg for Linux) | Lives in Microsoft's `Weston-mirror` fork. Upstream rebase MR not merged. Adopting means carrying Microsoft's downstream weston build forever. |
| **DIY FreeRDP RAIL server** | ~10-16k LOC of bespoke compositor↔FreeRDP glue with no upstream landing path. Doesn't reuse `qdwin_nested_manager_v1`. |
| **Qubes `qubes-gui-agent`** | Fixed-header binary stream over Xen's libvchan. Wrong substrate (qdistro is KVM, not Xen). |
| **xpra** | Per-app pixel transport is non-zero-copy by design; xpra is generic remote-app, not VM-aware. Plan B if waypipe-vsock blocks. |
| **Looking-Glass** | Whole-display gaming-targeted; requires VFIO GPU passthrough. Wrong scope. |
| **virtio-gpu native context (drm_native_context)** | Whole-VM display only, not per-app. Tier-4 ingredient at most. |

### North-Star — virtio-gpu cross-domain + sommelier

Architecturally the cleanest answer: guest apps' Wayland surfaces pass
through the virtio-gpu device's cross-domain context, sommelier-in-guest
forwards them to the host's Wayland socket, and the outer compositor sees
**genuine `wl_surface` objects** — no remote protocol, zero-copy buffers.
crosvm proves it works.

Why qdistro is not building on this now: neither `crosvm`, `sommelier`,
nor `qemu-hw-display-virtio-gpu-rutabaga` is in
`openSUSE-Tumbleweed-Oss`. qemu's `-display dbus` + cross-domain CLI
parity is in flight upstream but not in a tagged release. Realistic
Tumbleweed availability is multiple quarters out. The decision is to
revisit when packaging gaps close.

See [vm-definitions.md](vm-definitions.md) for the preferred NixOS module /
flake definition shape for new VM-backed silos.

## Tier 6 — remote machine

A separate physical machine acting as a remote-output target. Air-gapped
in spirit; framed, another machine. See [cross-machine](cross-machine.md).
