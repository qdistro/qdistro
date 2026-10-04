# Phase B (B-iii milestone) GUI waypipe-bridge evidence, 2026-10-03/04

Acceptance lane: `ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-*.bats`
with `QCI_ALLOW_TEST_EDITS=1`, run under `systemd-run --user` from a disposable
tree pinned to the tested commit (`/var/tmp/t3s-qci-b-*`). Each worker is a
fresh qci VM cloned from the run's golden (built WITHOUT `QDISTRO_TIER3S=1`);
the file's `setup_file` stages `git archive HEAD`, the sha512-pinned runsc
tarball and the sha256-manifested OCI workload archives over a private
HTTP server, then runs `tier3s-guest-setup.sh` in the guest (installer with
`QDISTRO_TIER3S=1`, offline runsc provision, probe PASS, image loads).
GUI files (`--gui weston-terminal,foot`) additionally bring up the admin
qdwin session first. Every check prints one `PASS:`/`FAIL:` line; the
driver exits nonzero on any FAIL and the bats wrapper also requires
`[sNNN] N passes, 0 failures`.

Tested HEAD: `eac49ad16` (`claude/tier3s-b`). Record run: **b17**.

## qci runs

| Dir | qci run | Commit | Result |
|---|---|---|---|
| `s123-waypipe-probe/` | `bats-20261003T162318Z-1944312` | `a1e16e5bb` | s123 first green (165 PASS lines) after the bridge_live series — final form asserts the in-sandbox waypipe server (container pid 1) holds ≥2 socket fds (channel + app); the mapped tagged toplevel is the end-to-end proof |
| `b12-first-acceptance/` | `bats-20261003T163624Z-2151098` | `a1e16e5bb` | first full 10-file run: 5/10 (s120–s122, s123, s126 pass; s124/s125/s127/s128/s129 fail on driver defects — cumulative focus count, unscoped toplevel count, dup clip-source instance-id, in-memory LaunchRecordStore wiped by the test's own broker restart, no named in-sandbox wayland socket to hose) |
| `b13-provision-fail/` | `bats-20261003T173009Z-2934791` | `4729fce58` | provisioning failure only — the re-pinned tree lacked `.git` and golden provisioning runs git inside it |
| `b14-five-file/` | `bats-20261003T182601Z-2998574` | `4729fce58` | 4/5: s124/s125/s128/s129 pass after the driver fixes; s127 still fails (png offer swallowed, no gate line; verdict never flips; +1 `.call-*` settle race) |
| `b15-s127/` | `bats-20261003T192421Z-3265587` | `0f09b88a3` | s127 still 3+1 fails; live forensics on the preserved worker overturn the instance-id theory — the real mechanism is libweston's stale-serial guard in `weston_seat_set_selection` (tagged serial=0 offers dropped once `selection_serial` advanced past them via deny/focus clears; tagged+high-serial emits, untagged+serial=0 emits) |
| `b16-s127-pass/` | `bats-20261004T060910Z-2896733` | `eac49ad16` | s127 PASS — the serial sweep (`qdistro-test-clipboard-source` now tries 0x40000001/0x80000002/0xC0000003 like `qdwin-test-clipboard-emit`) beats the guard |
| `b17-acceptance/` | `bats-20261004T095918Z-16239` | `eac49ad16` | **RECORD RUN: 10/10 pass** — see below |
| `b18-s124-s126/` | (run dir TBD — `qci-tier3s-b18`) | `aa2e3afd4` | Sol-r1 remediation rerun: s124 + s126 with the new oracle/seccomp steps |
| `b19-fable-remediation/` | `bats-20261004T123605Z-3058184` | `920351f70` | 6/10 — s123/s124/s127/s129 driver+product defects root-caused and closed; preserved-VM replays green (s123 76/0, s124 75/0, s127 94/0, s129 78/0). Remediation commits `36d779097` `7133aee42` `b6523e801` `45f7bda59` `e96442f4a`; see `b19-fable-remediation/MANIFEST.md` |
| b20b (aborted) | `bats-20261004T150440Z-3427784` | `77336415f` | env failure, not product: the disposable tree shares the worktree gitdir (`cp -a` of `.git`), so committing the b19 pack mid-run dirtied it — all 10 workers failed `t3s_setup_file` clean-tree check. No test assertions ran |
| `b20c-second-acceptance/` | `bats-20261004T151608Z-3563706` | `493312a25` | **8/10** — s123 + s129 green on fresh workers. s127 driver 93/1 (snapshot-too-early: transient `broker-unavailable` deny raced the post-restart emit; broker-evaluated `broker:deny` landed on the next emit). s124 driver 75/0 but bats wrapper still expected the pre-`e96442f4a` assertion names. Fixed in `5a494c448` |
| `b21-third-acceptance/` | `bats-20261004T152921Z-3732618` | `5a494c448` | **9/10** — s127 green on a fresh worker (93/1→driver-complete). s124 74/1: `toplevel_title` event lands ~60ms after `toplevel_added`; the prefix check raced the journal. Fixed in `c1f9ec5e6` (+ mutation E2 snippet refresh) |
| `b22-fourth-acceptance/` | `bats-20261004T153647Z-3857896` | `c1f9ec5e6` | **9/10** — s124 green on a fresh worker (75/0). s127 91/3: A's one-shot source emitted only while `busctl --timeout=200ms` calls still exceeded budget → `broker-unavailable` (fail-closed), no `broker:deny` ever landed. Fixed in `2e23d02a3` (`--emit-interval` for A + scoped kill) |
| `b23-fifth-acceptance/` | `bats-20261004T154612Z-3988839` | `2e23d02a3` | **9/10** — s127 green on a fresh worker (94/0). s122 151/1: a SIGKILLed cleanup run's `.call-*` dir outlived the 15s settle wait — by design such dirs are swept by the NEXT `--reap-stale`, so `assert_all_clear` now invokes the designed sweep before counting (`65a79151c`) |
| `b24-acceptance/` | `bats-20261004T155536Z-4113067` | `65a79151c` | **RECORD RUN: 10/10 pass** — see below |
| `b25-sixth-acceptance/` | `bats-20261004T171430Z-298415` | `bb0b25c6b` | **7/10** — first Sol-r2 remediation acceptance. s127: PRODUCT BUG — qdwin advertised `qdwin_shell_v1` at a hardcoded 34, so the v35 source-peer sidecar never fired (fixed `323e3eec2` + advertised-version floor guard). s126: driver hung to vm-exec's 1800 s (unbounded `wait_for` inner call); preserved-VM replay (70/16) proved WAYLAND_SOCKET fd-inheritance means no attach socket exists → probes now run AS the workload (`1ffe9467a`); plus 5b `comp_log`-in-`bash -c` visibility bug + secctx-listener race. s129: driver 78/0 green, wrapper expected the pre-`bb0b25c6b` PASS name |
| b26 (aborted) | `bats-20261004T181436Z-571884` | `1ffe9467a` | aborted at ~14 min — `timeout 30 <shell-function>` can't exec a function, every wait_for failed on sight; bound moved to a killable subshell + rc-file (`24540c6f0`) |
| `b27-seventh-acceptance/` | `bats-20261004T183215Z-962731` | `24540c6f0` | **9/10** — s127 99/0 (v35 live cross-silo `broker:allow` + source-pid relay green), s129 78/0. s126 driver **90/1**: ALL bridge-path + single-attach assertions passed (probe-as-workload works); sole FAIL = `probe_ctrs_gone` missing `return 0` (journal: all containers died+removed <1 s). Held-client orphan also held qemu-ga's exec pipes ~40 min — both fixed `6e0c677c3` |
| `b28-eighth-acceptance/` | `bats-20261004T185713Z-1153353` | `6e0c677c3` | **9/10** — s126 setup flake only (81/1, driver never ran): `busctl list` owned the SessionManager1 name while a single introspect timed out (name registered before the object serves, under load) — serving-wait added in `4d7629a61`. s127+s129 green again |
| `b29-acceptance/` | `bats-20261004T190703Z-1331658` | `4d7629a61` | **10/10** — the first Sol-r2 record run (same counts as b30); Sol r3 then found the opcode-renumber + descendant-leak defects, fixed `b524ac0c8`/`421e0bf75` |
| `b30-acceptance/` | `bats-20261004T192608Z-1670740` | `421e0bf75` | **10/10 pass** — s120 196/0, s121 155/0, s122 152/0, s123 76/0, s124 75/0, s125 97/0, s126 91/0, s127 99/0, s128 54/0, s129 78/0 |
| `b31-acceptance/` | `bats-20261004T195521Z-2046457` | `85f6ab26d` | **RECORD RUN: 10/10 pass** — same per-driver counts as b30; the Sol-r4 acceptance (process-group-contained `wait_for_bounded`) |

## b17 record run (`eac49ad16`) — 10/10 files PASS

`b17-acceptance/report.md`, per-file TAP logs under `b17-acceptance/bats/*.bats.log`,
driver transcripts under `b17-acceptance/bats/*.bats.scratch/{t3s-setup,sNNN}.log`,
VM journals under `b17-acceptance/journals/`:

| Driver | Passes | Key evidence (transcript) |
|---|---|---|
| s120 headless | 196/0 | `s120.log` — install/provision/probe/lifecycle invariants |
| s121 denied | 155/0 | `s121.log` — launch refuses without rules; probe-failure INFO lines are the expected refusal text |
| s122 sigkill-cleanup | 152/0 | `s122.log` — SIGKILL of the launch pid reconciles everything |
| s123 waypipe | 61/0 | `s123.log` — bridge client+wrapper pids recorded in the launch record, run as admin under secctx in the launch-unit cgroup, `link.sock` live (sandbox waypipe server holds channel+app sockets), tagged toplevel observed |
| s124 app | 63/0 | `s124.log` — `[3s:s124w]`/`[3s:s124f]` title prefixes on the toplevels; injectFocus → new seat_focus_changed per handle; ydotool-typed command lands inside the container's /tmp |
| s125 lifecycle | 97/0 | `s125.log` — concurrent GUI silos, tear one down, the other unaffected; refused launches (`no-compositor`, `no-launch-record`) leave the silo Stopped |
| s126 chrome-secctx | 65/0 | `s126.log` — observed line carries attested secctx app_id+colour+handle; tagged client sees `wl_compositor` but NOT `qdwin_shell_v1`, `zwlr_layer_shell_v1`, `qdwin_nested_manager_v1` (privileged globals absent over the bridge) |
| s127 clipboard-gate | 70/0 | `s127.log` — default-deny at set-time; png-only offer denied `tier3s-no-allowed-mimes` (mime-strip logged); `SaveRule` flips the live verdict to allow; focus-cross clears the selection; receive-side default-deny, text/plain rule allows text only; audit rows for deny/allow/receive probes |
| s128 lineage | 54/0 | `s128.log` — after a broker restart under `lineage_enforce` + re-registration: real pid + drifted starttime → deny; forged claim of another silo → the attested silo wins (deny + journal override) |
| s129 hostile-stream | 72/0 | `s129.log` — garbage over the runsc sentry's socket fds (malformed waypipe frames at the trusted client parser) and over the bridge client's tagged fd kills only that connection; refused re-dial; secctx-listener flood contained |

## b24 record run (`65a79151c`) — 10/10 files PASS

Superseded by b29 (Sol-r2 remediation record). Fresh workers;
`b24-acceptance/report.md`, per-file TAP logs + driver transcripts under
`b24-acceptance/bats/`:

| Driver | Passes | Notes |
|---|---|---|
| s120 headless | 196/0 | |
| s121 denied | 155/0 | |
| s122 sigkill-cleanup | 152/0 | incl. the `assert_all_clear` designed-sweep call site |
| s123 waypipe | 76/0 | 0600 `link.sock` observed pre-attach; gofer-netns channel evidence |
| s124 app | 75/0 | title-prefix waits; per-workload seccomp argv + live syscall probes |
| s125 lifecycle | 97/0 | |
| s126 chrome-secctx | 73/0 | per-interface oracle (output-manager deny, stream-claim INVALID_TOKEN) |
| s127 clipboard-gate | 94/0 | bound same-silo `verdict=allow` + `identity.verify` audit rows; re-offer cadence covers `busctl --timeout=200ms` transients |
| s128 lineage | 54/0 | |
| s129 hostile-stream | 78/0 | gofer-netns fd attribution |

## b31 record run (`85f6ab26d`) — 10/10 files PASS

**Current record** — the Sol-r4 acceptance: `wait_for_bounded` now confines each
check to its own process group (`set -m` job isolation + group kill), closing the
sequential-`bash -c` fork-mid-enumeration leak Sol reproduced. Fresh workers;
`b31-acceptance/report.md`, per-file TAP logs + driver transcripts under
`b31-acceptance/bats/` — per-driver counts identical to b30 (table below):

| Driver | Passes | Notes |
|---|---|---|
| s120 headless | 196/0 | |
| s121 denied | 155/0 | |
| s122 sigkill-cleanup | 152/0 | |
| s123 waypipe | 76/0 | |
| s124 app | 75/0 | |
| s125 lifecycle | 97/0 | |
| s126 chrome-secctx | 91/0 | probe-as-workload + 5b single-attach green again |
| s127 clipboard-gate | 99/0 | step 4a live cross-silo `broker:allow` + v35 source-pid relay |
| s128 lineage | 54/0 | |
| s129 hostile-stream | 78/0 | byte-count assertions |

## b30 run (`421e0bf75`) — 10/10 files PASS

Superseded by b31 (Sol-r4 record). The Sol-r3 remediation acceptance
(opcode-stable v35 + descendant-kill wait_for). Fresh workers;
`b30-acceptance/report.md`, per-file TAP logs + driver transcripts under
`b30-acceptance/bats/`:

| Driver | Passes | Notes |
|---|---|---|
| s120 headless | 196/0 | |
| s121 denied | 155/0 | |
| s122 sigkill-cleanup | 152/0 | |
| s123 waypipe | 76/0 | |
| s124 app | 75/0 | |
| s125 lifecycle | 97/0 | |
| s126 chrome-secctx | 91/0 | probes run AS the workload through the waypipe server — test/apply implementation-error denials + INVALID_TOKEN each rode their launch's tagged channel; 5b single-attach: second connect live-EOF + refusal log + one accepted client |
| s127 clipboard-gate | 99/0 | step 4a live cross-silo `CLIPBOARD_GATE ... verdict=allow reason=broker:allow` (s127a→s127b) with the v35 sidecar relaying the source's own compositor-observed pid |
| s128 lineage | 54/0 | |
| s129 hostile-stream | 78/0 | byte-count assertions — only actually-written bytes count |

## DONE bar (`03-implementation-plan.md` Phase B)

- **s123–s129 PASS** — b31 table above (record run at `85f6ab26d`, single
  commit, one run; earlier green: b30 at `421e0bf75`, b29, b24, b17).
- **Screenshots** — `shots/t3s-weston-terminal.png`, `shots/t3s-foot.png`:
  dev VM `t3s-shot-261004-121311-155087-24437` cloned from the b17 golden
  (`qci-golden-bats-261004-115935-17667-18553`), tier3s installed at
  `eac49ad16` via the same staged setup (88/0). weston shot: `[3s:shotw]`
  taskbar chrome + Wayland Terminal window. foot shot: `[3s:shotf] foot`
  taskbar chrome + foot window showing `TIER3S-FOOT-LIVE ===[3s:shotf]===`
  typed through the bridge (ydotool → sandboxed shell → rendered back).
  Provenance: `shots/qdshell-observed.log` (qdshell's tagged-toplevel lines
  for both), `shots/list-silos.log` (ListSilos: both `kind=tier3s`,
  `network=none`, `launcher-running`). Images are runtime artifacts —
  gitignored, not committed.
- **Threat-model paragraph** — `doc/threat-model.md` +30 lines in the branch
  diff (tier3s GUI bridge: attested bridge client, `host-uds=open` surface,
  privileged-global absence, clipboard default-deny, hostile-stream scope,
  honest residuals incl. bridge-compromise tag inheritance).
- **Astra milestone review** — O13 (2026-10-03): stands; runs after the
  sol end-of-B review of `main..claude/tier3s-b`.

## Sol end-of-B review — r1 REVISE → remediation

`todo/paravirt/reviews/2026-10-04-tier3s-b-end-sol-review.md` (brief +
run.log alongside): `VERDICT: REVISE`, three evidence gaps:

1. **s126 per-interface oracle** — registry visibility was only half the
   proof. Fixed in `aa2e3afd4`: s126 step 5 drives the denials through a
   real tagged peer (secctx wrap, unique instance-id per connection):
   `zwlr_output_manager_v1` stays enumerable but `qdwin-output-probe
   --test/--apply --expect-denied` get the interface's protocol error;
   `qdwin_stream_input_v1` (public by design — the claim() token is the
   gate) refuses a bogus-token claim `INVALID_TOKEN` via the new
   installed probe `qdistro-test-stream-claim-probe`. No authorization
   override is configured anywhere; the denials are the proof.
2. **GUI seccomp decisions unexercised** — fixed in `aa2e3afd4`: s124
   step 3 `pm exec`s into each live container, asserts the podman seccomp
   annotation names the workload's own profile file, and exercises the
   coreutils-reachable decisions per workload — fchmodat ALLOW, the
   `chmod -h` (fchmodat2) path EPERM with mode unchanged, link/linkat
   EPERM, llistxattr ALLOW, NoNewPrivs + `Seccomp: 2`.
3. **host-side evidence unarchived** — `host-evidence/` (MANIFEST.md
   lists exact commands + results): unit tests (tier3s-relevant set 1074
   green; full suite log included — its 30 `test_mm_*` failures are a
   cross-run port-5556 collision with a concurrent `qci host`, unrelated
   files), qdshell JS (61 files green), mutation harness (184/184 after
   the E3 snippet refresh in `5bbcec7b7`).

## Latent product notes (recorded, not blocking)

- A `set_selection` landing while the shell can't receive v11 (qdshell
  restart gap) is accepted silently and leaves a stuck source that starves
  later serial=0 offers — fail-closed; worth a CONTRACT note (s127 forensics).
- A broker restart wipes the in-memory LaunchRecordStore: extant attested
  launches hard-deny until relaunch/re-registration (s128; the driver
  re-registers the live pid). Persistence is a future product decision.
