# disposables-e2e test 7 proctree-sweep — fix report

## Failure (full-20260805T162915Z-1550111)

`tests/integration/vm/disposables-e2e.bats` test 7:
`disposables: process-tree-empty sweep reaps only the PID1-only disposable past grace`

```
not ok 7 ...
podman rm -f 'disp-ptempty-...' timed out; cleaning up stuck descendants then treating as a failed (retryable) removal
stuck-descendant cleanup: top 'disp-ptempty-...' hpid failed: podman top timed out after 10s
proctree sweep: podman top 'disp-ptbusy-...' failed: podman top timed out after 30s
ENUM_OK
TOP_OK
AssertionError: proctree sweep reaped [], expected exactly ['disp-ptempty-...']
FAIL: proctree-sweep — sweep_empty_proctrees did not reap EXACTLY the empty-tree disposable
```

Tests 1–6 and 8 PASSED. Exit class bats (35), raw_rc=1.

## Root cause

Not a security-predicate bug. The two-phase proctree sweep correctly:

1. Enumerated opted-in fixtures (`ENUM_OK`)
2. Discriminated PID1-only vs busy via real `podman top` (`TOP_OK`)
3. Selected only the empty-tree fixture for dispose

Under host load (parallel full QCI), the subsequent `podman rm -f` of the empty fixture **timed out at 30s**. Stuck-descendant cleanup then failed because `podman top hpid` also timed out (10s). `disp_container_remove` returned `False` (retryable failure), so `sweep_empty_proctrees` reported `reaped=[]` even though:

- The empty tree had been correctly identified
- Under load the server-side remove may still complete after the CLI client is killed on timeout
- Production relies on the *next* periodic sweep to retry (correct for the daemon; fatal for this one-shot probe assertion)

A second symptom in the same pass: `podman top` for the busy fixture also timed out → fail-closed SKIP (correct; never reaps on a guess).

**Invariants held:** only PID1-only past-grace opted-in disposables are reaped candidates; top/rm failures SKIP or retry, never fail-open.

## Fix (minimal)

Branch: `fix/disposables-proctree-timeout`
Worktree: `/home/play2/qdistro/.worktrees/qdistro-disposables-proctree`

### Product (`session_manager/qdistro_session_manager.py`)

1. **`disp_container_top_pids`**: one retry on `TimeoutExpired` (0.5s backoff), still fail-closed after two timeouts. Transient host load must not permanently skip an empty tree for a whole sweep pass.

2. **`disp_container_remove`**: after a timed-out `rm` + stuck-descendant cleanup, call new **`_disp_container_gone`** (`podman container exists`, rc==1 only). If the container is *definitively* absent, return `True` so `dispose()` / sweeps honour the documented post-condition ("True if the container is gone afterward"). Uncertain exists (timeout/error/present) still returns `False` (retryable).

### Probe (`tests/integration/vm/probes/disp-probe.sh`)

- One short retry of `sweep_empty_proctrees` if the first pass does not reap exactly the empty fixture. Still asserts **exactly** `[empty]` — does not relax which fixtures may be reaped.

### Unit tests (`tests/unit/test_session_manager.py`)

- top: two timeouts → None; one timeout then success → stdout
- remove: timeout + gone → True; timeout + present → False; timeout + exists error → False

## Security contract

Unchanged:

- `proctree_empty` still requires unambiguous PID1-only
- top failure still SKIP
- exists promotion only when rc==1 (gone), never on error/timeout
- non-disposable names still refused

## Retest

Unit (related classes/keywords): **37 passed**

Bats (worktree sources via spinner parent symlink layout):

```
QCI_JOBS=1 ./ci/bin/qci bats --file tests/integration/vm/disposables-e2e.bats
```

Run: `bats-20260805T180121Z-2155594`
- **exit=0 class=pass**
- All 8 tests **ok**, including test 7 proctree-sweep
- Log: `ci/runs/bats-20260805T180121Z-2155594/bats/disposables-e2e.bats.log`

## Residual risk

- Sustained podman wedging (both top attempts and rm+exists all time out) still yields `reaped=[]` for that pass; the daemon's next timer tick retries. Probe has only one extra attempt.
- Longer wall-clock on a pathologically slow podman (2× top timeout + short sleep).
- Retest ran under lighter load than the parallel full QCI that triggered the original flake; product hardening still applies under load.
