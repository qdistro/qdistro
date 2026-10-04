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
| (b20 record attempt) | `qci-tier3s-b20b` running | `e96442f4a` | full 10-file lane on fresh workers with all remediation baked |

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

## DONE bar (`03-implementation-plan.md` Phase B)

- **s123–s129 PASS** — b17 table above (record run, single commit, one run).
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
