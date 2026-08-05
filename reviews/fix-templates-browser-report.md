# Root-cause: templates-browser.bats (full-20260805T162915Z-1550111)

## Symptom

| # | Test | Result |
|---|------|--------|
| 1 | baseline | ok |
| 2 | state isolation | ok |
| 3 | **update-flip** | **FAIL: `update-flip genB silo did not render`** |
| 4 | broken-update | ok (uses genB digest on disk only) |
| 5 | login-regression | guest agent not responding (+ host uptime jump) |
| 6 | breakage-matrix | exit 124 timeout |
| 7 | rollback | cascade: B-era sentinel absent (update-flip incomplete) |
| 8 | GC | ok |

Primary real failure: **test 3 update-flip** at the genB file:// `RENDER-OK-GENB` dump-dom check.
Tests 5–7 are cascade / guest thrash after the same memory event.

## Evidence

### Journals (guest)

Path: `ci/runs/full-20260805T162915Z-1550111/journals/bats-templates-browser-*.log`

1. **genB silo was running** (`qdistro-silo-browserdemo`, image `sha256:e0d896e2…`) and promotion/restart checks passed (failure is specifically the render sentinel).
2. A long `podman exec` on that silo started ~17:12:31Z and only died ~17:21:54Z (**~9 minutes**) despite `silo_chromium` wall timeout of 90s — host-side `timeout` kills the **podman client** but under pressure does not reliably kill the in-container chromium (same rootless conmon reality the suite already documents in breakage-matrix).
3. At **17:21:53Z** the guest kernel logged:

   ```
   podman invoked oom-killer
   Out of memory: Killed process 1097 (qs) … anon-rss:1411372kB
   ```

   The admin shell (`qs`) alone held ~1.4 GiB; Chromium dump-dom on top of nested weston + podman in a **4 GiB** bats VM OOMs.

4. Stop path after failure: `StopSignal SIGTERM failed … SIGKILL`, systemd `qdistro-tier2-silo@browserdemo` stop **timeout**, later `database is locked` / corrupt container state JSON.

### Host / CI context

- Full run: `bats_jobs_effective=8` (8 parallel bats VMs × ~4 GiB).
- Same suite **passed earlier the same day** under lighter load:
  - `bats-20260805T121216Z-598920` (8/8)
  - `full-20260805T131123Z-810555` (templates-browser pass)
- Morning `full-20260805T075805Z-83338` failed a *different* setup-time pasta/slirp reachability issue (already fixed by pasta migration on main); not this failure mode.

### Not the product flip path

Binding/digest flip, `restart_pending`, pre-migration pin, and pre-activation snapshot checks all passed before the render assertion. Product promotion/restart is not implicated; the renderer under guest OOM is.

## Root cause

**Guest OOM under parallel full-CI load** during headless Chromium `dump-dom` after the genB restart. The single-shot 90s host-only timeout + full persisted profile load is brittle: chromium can hang/OOM, leave orphans, and the probe reports empty DOM as `genB silo did not render`. Subsequent scenarios hit a dead/starved guest agent.

## Fix (probe hardening — keep scenario invariants)

File: `tests/integration/vm/probes/templates-browser-probe.sh`

1. **Dual timeout** (host `timeout` + in-container `timeout --kill-after=5`) so the renderer is SIGKILL’d even when the podman client wedges.
2. **`assert_silo_file_render`**: up to 3 attempts with backoff; **ephemeral** user-data-dir for file:// RENDER-OK checks (session/cookie still uses the real profile).
3. **Memory-capped Chromium flags**: `--renderer-process-limit=1 --disable-extensions` (robustness only).
4. **Stronger `ensure_profile_free`**: kill chromium/chrome, clear Singleton locks on real + ephemeral profiles.
5. **`stop_silo`**: profile-free before stop (avoid SIGTERM hang on wedged dump-dom).
6. **`launch_silo`**: wait for Running **and** `podman exec true`.
7. Louder fail diagnostics (dom snippet, stderr, `free -m`).

Security/promotion invariants unchanged (digest flip, restart_pending, pins, cookie survival, rollback sentinel story).

## Residual risk

- Extreme host thrash can still OOM the 4 GiB guest (killing `qs` destroys the session). Hardening reduces peak and recovers from single failed dump-dom attempts; it does not add RAM.
- If full-CI keeps OOMing, consider suite-specific VM memory or lower `bats_jobs` for browser-heavy suites (out of this minimal fix).

## Retest

```bash
cd /home/play2/qdistro/qdistro-fix-templates-browser
QCI_JOBS=1 ./ci/bin/qci bats --file tests/integration/vm/templates-browser.bats
```

- Run: `ci/runs/bats-20260805T180402Z-2173697`
- **exit=0 class=pass** — 8/8 ok
- Log: `bats/templates-browser.bats.log` (all scenarios including update-flip green)

