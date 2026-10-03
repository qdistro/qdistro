# GitHub-hosted test VM build: experiment record

Branch `experiment/github-qdistro-image` in the product repo; workflow
`.github/workflows/qdistro-test-vm.yml`. Goal: build and smoke the minimal
test VM image on a free hosted runner (no KVM, no libvirt), publish it as an
artifact, then run the suites inside a throwaway overlay of it.

Tradeoffs vs. qci: QEMU runs with TCG (`accel=tcg`), the virsh stand-in
(`scripts/vm/test-vm-suites.sh`) answers `qemu-agent-command`/`domuuid`/
`screenshot` over QEMU's own sockets so `vm-exec` works without libvirt, all
bats files share one guest (qci gives each its own VM), and the test-only
tools go into an overlay so the shipped image stays runtime-only. The suites
step is after the image upload, fails with exit 2 when a suite did not run to
completion (assertion failures are data, in `suites/summary.md`), and sizes
its time budget from what the job has left.

## Runs

| Run | Commit | Result | Note |
|---|---|---|---|
| 36991468544 | | success | build+boot+install green; first on snapshot 20260930 |
| 36994730839 | | success | consumer check green |
| 37032421943 | fed12f028 | failure | suites step added; runner had no `bats` — caught by astra r1 |
| 37034194820 | af42fc2c2 | success | first complete suite run (68 min; suites 59 min) |
| 37035548838 | bd1d89a54 | cancelled | superseded |
| 37036822266 | 2ccb49759 | failure | flake: `vmssh` hit "Connection timed out during banner exchange" on the freshly rebooted guest (ConnectTimeout=5, one attempt). Fixed by `vmsshx` retry + ConnectTimeout=10 in bdc7f18e6 |
| 37037304439 | e41d9952f | failure | suites exit 2: baseline not restored after `shell-modules` (single start + 60 s poll too short on TCG); 12 files unrun |
| 37046652577 | bdc7f18e6 | failure | same stop: the restart loop still did not bring wayland-1 back — likely the greeter reclaimed the seat when admin's session died, so in-place starts can never win it |
| 37052929450 | 35a675c93 | **success** | whole pipeline green: build, boot, checks, consumer validation, artifact upload, all 53 bats files + 8 pytest suites completed (suites step exit 0). `shell-modules` left its usual debris; the restart loop restored it in place (no relogin needed) |
| 37101442488 | 35a675c93 | | workflow_dispatch re-build for local validation |
| 37102833917 | 07ce955d6 | | validates the idle-lock suppression fix |
| 37104111298 | 6cdfe5b8c | | merge of origin/main (72 commits) into the branch; validates the fix on merged code |
| 37109215627 | e13947d3a | | merged main + admin-TUI pkgs + compositor-restart baseline fix |
| 37111198372 | 47f822dc8 | | consumer-check desktop poll + journal dump on blank |
| 37115937113 | 0dd68afe7 | | unconditional compositor-restart escalation + user-journal dump fix |
| 37121328013 | 3471b9816 | **success** | last-file baseline skip + seat-gated compositor restart + keep-id o+x workaround |

## Suite results (runs 37034194820 / 37052929450, both complete; 37037304439 agrees where it ran)

- pytest in-guest: 10,027 tests, 7 failed, 22 skipped — identical set in both runs.
- bats: 600 planned, 517 ok, 44 failed, 39 skipped in the green run
  (507/55 in the first). tiered-isolation improved 19→9 failures: the
  tier-3/4 silo and secctx tests that failed earlier passed on the rerun —
  timing/state sensitivity, not missing capability. The stable failures
  break down as below.

### Failure triage

1. **Rootless podman `--userns=keep-id` vs the 700 home dir** — verified
   by booting the published artifact locally and A/B-testing against a
   baseweed overlay (`/tmp/podman-selinux-verify`; both probe VMs cleaned
   up after). `runc create failed: error preparing rootfs: remount-private
   ... MS_PRIVATE: permission denied`. Explains: disposables-e2e (3),
   disp-export-e2e (2), disp-open-e2e (1), disposable-secctx-wiretag (1),
   wlimg-e2e (2), tier2-hardening-lockin (1), tier2-silo-secctx-wiretag (1),
   qdwin-taskbar-isolation (1), podapp-launch-wiretag (1), templates-browser
   (7), templates-promotion (1), and the tier-2 half of tiered-isolation.

   What was ruled out in the GH guest: XFS itself (baseweed-baked has the
   same root filesystem UUID — the GH image was installed from it; a fresh
   XFS loop-mount and a same-device remount both work), kernel (identical
   7.2.8-1-default), podman/runc versions (identical 6.0.2-1.1 / 1.5.1-2.1),
   storage config (`podman info` identical, rootless overlay graphroot
   `~/.local/share/containers/storage`), stale graphroot state (fresh
   directories fail), SELinux enforcing (**the image is actually
   `SELINUX=permissive`** — the suites serial log's AVC flood was
   permissive-mode logging, not enforcement), `/tmp` (tmpfs in both).

   The mechanism, via strace: runc init inside the container userns does
   `setresuid(0)` (container root = host subuid 100000 via uid_map) and then
   `mount("", ".../merged", NULL, MS_PRIVATE)` on the overlay — that path
   traverse hits `/home/admin` (mode 700, uid 1000), which host-uid 100000
   cannot traverse → `EACCES`. Confirmed by `chmod o+x /home/admin`:
   keep-id containers start (repeatedly: 700 fail / 755 pass / back to 700
   fail). Everything below `/home/admin` fails; identical graphroot under
   `/opt` passes.

   The baseweed anomaly: identical perms, uid_map, bundle spec, kernel and
   packages — but its runc init's `mount(merged, MS_PRIVATE)` **succeeds**.
   Its mounting thread evidently resolves the path under credentials that
   own the directory (i.e., it still sits in the parent rootless userns,
   not the container userns). Same runc version and identical bundle spec,
   so the residual difference is which thread/userns performs the remount —
   left open; not needed for classification.

   Consequences: in the GH image every `--userns=keep-id` container is dead,
   which is all tier-2/podapp launches. Note this is NOT proven to be a GH
   image-only condition — baseweed's admin home is also 700, so a real
   install whose tier-2 launch path matches `podman run --userns=keep-id`
   may hit the same wall on hosts where the remount runs inside the
   container userns. Worth a product-side check of the tier-2 launcher on
   a real baseweed install (qci's bats VMs pass, so something in that path
   differs).

   Workflow-level workaround candidates (harness, not tests): `chmod o+x
   ~admin` during suite setup, or repoint the rootless graphroot to
   `/var/tmp/containers-admin` via `~/.config/containers/storage.conf`.
   Neither is applied — decided to leave the artifact faithful to a stock
   install and report instead.

2. **Tier-3/4 silo tests are timing-sensitive, not missing** — they passed
   on the second complete run (tiered-isolation 19→9 fails); what remains
   is the tier-2 podman failures plus the two clipboard-focus-gate tests
   below and `teardown_file failed`.
3. **Clipboard-focus-gate (tiered-isolation tests 30–31): harness
   artifact — qdlocker idle-lock on the shared guest, root-caused and
   fixed** — first compared against qci run `full-20261002T122430Z`
   (local green); then verified end-to-end by booting the published
   artifact locally under libvirt/KVM (`gh-artifact-suites`,
   /tmp/gh-image-local/run-local.sh). The pair fails identically under
   KVM — not TCG timing — with the same two diagnostic lines. The
   journal reveals the mechanism: `Tier3FocusIPC injectFocus` →
   `qdwin_shell_v1#5: error 3: locked` — qdlocker's production 300 s
   idle timer fires ~5 min after the (input-less) session starts and
   latches `qdwin locked=1`; every privileged request is then refused
   with a *fatal* protocol error that also kills qdshell's binding —
   hence no `set_keyboard_focus`/`CLIPBOARD_FOCUS_GATE` lines and the
   wedged `wl-paste`. qci never sees it: each bats file gets a fresh
   VM whose session is younger than the lock, and
   `tiered-isolation.bats`'s own `setup_file` installs the
   `99-qci-no-idle-lock.conf` drop-in per-file — on the shared guest
   that lands after the lock has already latched (and cannot un-latch
   it). **Both tests PASS on the artifact with an unlocked session**
   (clipboard-r4.tap). Fixed in `07ce955d6`: the suite installs the
   same drop-in before the greeter login so the session's qdlocker
   starts with the 24 h timeout (same as ci gui's suppress_idle_lock).
4. **Optional surfaces absent from this minimal image** — pwd-print-recall's
   probes for print-VM helpers, browser bridge, snapper bridge and phone
   ("not installed (legacy bake)"); browser-9e-daemons (browser not shipped);
   gui-fixes-verify (print-proxy, bystander FIFO, Xwayland absent). Local
   qci run is green on all of these files — pure image-gap.
5. **Missing kernel/env pieces** — kernel-default-base has no `uinput`
   (ydotool paths fail); kiwi-ci-base tests 2/9 need a libvirt template
   domain on the runner, which only exists on the qci host.
6. **Unit tests that read the real system** — 6 deterministic unit failures
   (session-manager autostart/persistence, relay dbus policy, user-relay
   installed layout) + `test_polkit_ignores_override` (presentation).
   Expected on an installed image — paths resolve where they don't on a
   dev host.
7. **shell-modules s3d/s3f/s3c — identical local failure, product-side** —
   compared against qci run `full-20261002T122430Z`: the same three tests
   fail at the same TAP positions in both environments with materially
   identical diagnostics — s3d `routed=0 picked=proxy pick_matched=1
   active_input_proxy_matched=0` (proxy visually picked but never armed as
   active input proxy), s3f the same after the real allow transition, s3c
   `keyboard-grab install log never appeared`. Also confirmed earlier on
   run 37034194820 (identical signatures). Not a GH artifact — an open
   qdwin nested-input bug: `pick_view` matches the proxy view but
   `active_input_proxy` is never set, and the keyboard-grab object is not
   installed on RDP connect. Belongs in the product bug list.
8. **presentation-four-apps `qfileman worker exited 1 before ready`** —
   still unexplained; appears GH-only (not in the local qci failures).
   Likely a worker that needs an optional surface missing from the image
   (same class as item 4) — needs one confirmation pass.

`shell-modules` also leaves the broker stopped and admin's qdwin session down
(its `setup()` stops the broker for every test; qci never notices because the
file gets a disposable VM). The shared-guest harness restores the baseline
between files; bdc7f18e6 makes that restore keep re-issuing starts instead of
one attempt in 60 s.

## Findings carried forward

- The image boots and installs green on TCG within the free-runner budget;
  the artifact is published before the suites run, so suite outcomes never
  gate availability.
- Tests that depend on podman, silos, the browser, print/phone/backup
  surfaces, uinput, or a libvirt template domain cannot pass on this image;
  that is ~55 bats assertions of expected-vs-missing capability, which the
  summary now enumerates per file.
- Adversarial review (codex astra, rounds 1–5 in `reviews/`) drove: bats
  presence on the runner, results-vs-expected accounting, bounded runs, no
  exclusion list, budget-from-job-time-left. Round 5: **SHIP** on e41d9952f
  with one minor (boot-probe budget classification, fixed in 16ed922c7).
- todo commit 7b45389 (rotation row for the 20260930 pin) is on
  `origin/main`; nothing left to push.

## Local validation of the published artifact (2026-10-03)

Booted `qdistro-test-vm.qcow2` (run 37052929450) under libvirt/KVM as
`gh-artifact-suites` (qemu:///session, q35+OVMF, virtio-vga, user net +
hostfwd ssh; harness mirror in `/tmp/gh-image-local/run-local.sh`). The
full qci-lane guest setup replicated: test packages via zypper,
`qdistro-test` copy, dev profile, tier installers, probes, RDP cert,
greeter login via `sendkey`, `vm-exec` works natively over the real
qemu-guest-agent channel.

Results vs the GH run — identical at every position:

| File | GH (TCG) | Local (KVM) |
|---|---|---|
| compositor-shell | 1 ok + 1 skip | same |
| broker-e2e | pass | pass |
| disposables-e2e | podman `remount-private` fails | identical |
| tiered-isolation | 9 fails (tier-2 podman, 30/31) | identical positions |
| shell-modules | s3d/s3f/s3c | s3d/s3f/s3c |

The artifact behaves byte-for-byte like the GH guest — the suites'
failures are content/harness deterministic, not runner flukes. With the
idle-lock suppressed, the two clipboard-focus tests pass on the same
artifact (see triage item 3).

Fresh-build check (workflow_dispatch run 37101442488, same commit):
sha256 verified, boots identically (kernel 7.2.8, permissive, podman
6.0.2, admin home 700), keep-id failure and `chmod o+x` workaround both
reproduce.

## Open

- Podman keep-id / 700-home interaction (triage item 1): the GH artifact
  reproduces it deterministically; baseweed does not despite identical
  visible state. Residual question is which userns performs runc's
  `remount-private` on each system. Follow-ups: (a) check whether a real
  baseweed install's tier-2 launch path can hit the same wall (qci's bats
  VMs pass, so something differs), (b) decide whether the workflow applies
  a workaround (`chmod o+x ~admin` or a `/var/tmp` graphroot) in suite
  setup — currently unapplied, artifact stays stock.
- presentation-four-apps `qfileman worker exited 1 before ready` — GH-only
  so far; probably a missing optional surface, needs one confirmation pass.
- shell-modules s3d/s3f/s3c is now classified product-side (triage item 7)
  — file/track it in the product bug list if not already there.
- Clipboard-focus-gate resolved (triage item 3): harness fix `07ce955d6`
  confirmed locally; run 37102833917 validated it partially — no lock
  events in the serial log at all, but the run stopped on a new failure
  mode below.

## Main-merge runs (2026-10-03)

- **37101442488** (`35a675c93`, workflow_dispatch): success; artifact
  sha256-verified, booted and re-tested locally — byte-identical
  behaviour to the earlier build.
- **37102833917** (`07ce955d6`, idle-lock fix): suites exit 2 after
  `pwd-print-recall` (~35/53 files). The idle-lock fix **held** — zero
  `lock_requested`/`set_locked`/`error 3` lines in the serial log. New
  failure: qdshell died during/after `pwd-print-recall` (which never
  touches it; residue of earlier podapp churn is likely), and every
  restart was rejected by qdwin with `layer-shell bind REJECTED — not
  the shell client`, pinning it in auto-restart while
  `qdwin-session.target` stayed `active`. The restore loop's `start` is
  a no-op on an active target, so baseline gave up → suite stop. Root
  mechanism: qdwin's `shell_bound && shell_resource` gate keeps the
  slot while the *wl_client* connection survives — a child process that
  inherited the wayland socket keeps the dead shell's client alive.
  Product-side question filed below; harness fix below.
- **37104111298** (`6cdfe5b8c`, merge of main `676f0c9fd`): failed at
  guest install (5 min). `ccd7afe63` added `qdistro-admin-tui` to the
  `admin-app` chain step; `install-admin-cli-for-vm.sh` hard-fails the
  strict chain without `python313-textual`/`python313-rich`.
  `a9abd7e15` deliberately removed them from `scripts/vm/install-deps.sh`
  (that list feeds the baked base's recipe digest → forced 15-25 min
  rebake per host), so the pair goes to the GH-only `pkgs` additions in
  `test-vm-guest-install.sh` instead.
- Fixes in `e13947d3a`: textual+rich in the guest-install package set;
  `baseline()` escalates to `restart qdwin-compositor.service` (drops
  every wl_client, frees the stale shell slot) when qdshell stays down
  after member restarts; baseline-failure dump now tails qdshell's own
  journal.
- **37109215627** (`e13947d3a`): the install fix held — guest install,
  offline checks, and the install-time headless qdshell smoke (all
  layer surfaces bound) passed. Failed at the consumer check's final
  step: session reported `qdwin-session.target`/compositor/qdshell/
  qdlocker active, but the desktop screenshot 15 s later was a uniform
  frame (1 colour — the bare qdwin background; greeter painted fine at
  10 colours). No guest journal was captured on that failure path, so
  product-regression vs TCG-slowness is undecidable from this run;
  the timing hypothesis is live (this guest was ~5× slower overall:
  SSH up after 86 s vs 15 s in the passing run).
- Fix in `47f822dc8`: the desktop check now polls like the greeter
  check (36×5 s) instead of one fixed 15 s sleep, and dumps the
  qdshell/compositor/qdlocker journals before failing — so a real
  regression leaves its evidence in the run log.
- **37111198372** (`47f822dc8`): consumer check passed — the desktop
  painted 47 colours ~6 s after session-active (matching the green
  run's signature exactly), confirming the earlier blank was TCG
  slowness, not a merged-main regression. pytest identical to prior
  complete runs (10,146 tests, same 7 failures). bats ran 50/55 files,
  36 failed cases — mostly the container-dependent set the keep-id
  breakage predicts (disposables, disp-open/export, podapp-wiretag,
  taskbar-isolation, templates-browser/promotion,
  disposable-secctx-wiretag) plus `permissions-headless` (6) and
  `pwd-print-recall` probes (6) — the new-from-main files to compare
  against a local qci run. Stopped after `tier5b-ops-hardening` on the
  SAME stale-shell-slot signature (`layer-shell bind REJECTED`):
  the compositor-restart escalation was gated on a point-in-time
  `is-active qdshell` which raced a respawn window and skipped. Also
  found: the baseline dump's `journalctl -M admin@` is machined syntax,
  not `systemctl -M` — it failed as "non-root" and produced no qdshell
  journal.
- Fixes in `0dd68afe7`: the compositor restart is unconditional once
  the healthy-poll exhausts (at that point the alternative is a
  baseline-failed stop anyway); the journal dumps filter on
  `_UID=1000 _SYSTEMD_USER_UNIT=` (both scripts).
- **37115937113** (`0dd68afe7`): **all 55 bats files ran.** pytest
  identical again (same 7 failures). The escalation DID fire — but
  after `shell-modules` (deliberately last; wrecks the guest by
  design) the session target itself was down: the standalone
  compositor restart could only fail (`libseat: could not open seat`
  — the seat had fallen back to greetd), churned into start-limit,
  and the greeter relogin then could not recover in time. Net: suite
  marked incomplete on teardown debris after the final file.
  `tiered-isolation` finally ran (43 tests): no `error 3 locked`
  anywhere — the idle-lock fix held on GH — but 30/31 failed on
  `qdshell not running` after the broken-podman tests churned the
  session mid-file.
- Fixes in `3471b9816` (+ `b94c03573`): skip the post-file baseline
  restore after the LAST bats file (nothing left to protect; the
  guest powers off next); gate the compositor restart on
  `qdwin-session.target` still holding the seat (relog-in path owns
  seat loss) + reset-failed before a greeter relogin; `chmod o+x
  /home/admin` in the qci-lane overlay so keep-id containers run —
  the verified workaround for the mode-700 home / subuid traversal
  failure, applied overlay-only with the product question left open.
- **37121328013** (`3471b9816`): **all-green run, every suite
  completed.** pytest identical again (same 7 failures). bats: 633
  planned, 570 ok, 23 failed, 40 skipped, **0 not executed** — the
  first run where nothing was lost to teardown debris. The seat-gated
  restart fired once (after `podapp-launch-wiretag`) and worked;
  `tiered-isolation` ran all 43 tests with only 2 failures
  (tier2-launcher-click, tier2-template-snapshot-e2e) — the mass
  `qdshell not running` collapse is gone. `shell-modules` ran its 24
  tests cleanly as the last file (5 ok/3 failed/16 skipped).
  Remaining failures are the usual product-side set:
  browser-9e-daemons 2, gui-fixes-verify 1, kiwi-ci-base 2,
  permissions-headless 6, presentation-four-apps 1, pwd-print-recall
  6, tiered-isolation 2, shell-modules 3.
  Local `gh-artifact-suites` VM rebuilt on this run's artifact
  (sha256 `f6ebd399…`, verified) — full setup done, admin session up
  on wayland-1, ready for local bats via `run-local.sh`.

## Open (product-side, from the runs above)

- qdwin keeps `shell_resource`/shell-client authority tied to the
  wl_client connection, not process liveness — a qdshell that dies with
  an inherited-socket child (or any surviving fd) leaves the shell slot
  held forever and the compositor can never accept a new shell. On qci's
  fresh-VM-per-file lane this never mattered; on any long-lived session
  (or a crashed-then-restarted desktop, i.e. real usage) it means the
  compositor wedging the shell permanently. Worth a product fix:
  clear the slot on shell_resource destroy AND verify the recorded
  shell pid is still the binding process (starttime bracket like the
  layer-shell allowlist does), or kill orphaned wl_clients whose
  recorded pid vanished.
- Clipboard-focus-gate resolved (triage item 3): harness fix `07ce955d6`
  confirmed locally; run 37102833917 showed no lock events — GH-side
  confirmation pending the tests reaching `tiered-isolation` in
  37109215627.

## 2026-10-03: final astra review and merge

Astra review (`todo/reviews/2026-10-03-gh-test-vm-merge-review.md`, brief
alongside it) of the full `origin/main...HEAD` delta at `3471b9816` returned
**MERGE-AFTER-FIXES** with one blocking finding:

- **[P1] Greeter relogin sent the obsolete image password.** The suites
  setup changes admin/root passwords to `QCI_PASSWORD` (`Pa_ssw0rd45`)
  before the bats loop, but the seat-loss recovery at the greeter still
  sent `IMAGE_PASSWORD` (`qdistro`) — so the greeter-relogin path could
  never have succeeded. Explains why relogin timed out in run
  37115937113 (the seat-loss case after `shell-modules`-era churn).
  Fixed: `send_text "$QCI_PASSWORD"`; stub-verified (the recovery block
  now supplies the current password). The unused-fallback caveat stands:
  the green run exercised the seat-gated compositor restart but not the
  greeter relogin.
- Nonblocking: `actions/*` are `@v4`-tagged not commit-pinned; push
  trigger stays restricted to `experiment/github-qdistro-image` (main
  needs `workflow_dispatch` — deliberate for the experimental lane);
  the consumer check only *warns* on stale SSH host keys (the journal
  here overstated it); this in-repo journal copy was a stale snapshot
  and is re-synced in the fix commit.
- Verified clean: no remaining single-quote/expansion traps in the
  multiline SSH blocks; fail-closed accounting probes passed (truncated
  middle TAP, missing exit marker, bailout, baseline marker, missing
  pytest batch all return exit 2); overlay mutations (`chmod o+x`,
  password change, test packages) provably cannot leak into the
  uploaded artifact; no secrets in image or logs.
