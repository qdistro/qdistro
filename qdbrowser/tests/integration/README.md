# qdbrowser integration tests

Two layers:

## 1. Local agent-driven scenarios (`scenarios/`)

Python scripts that drive a *running* qdbrowser via its `agent_control`
Unix socket. Run them against any qdbrowser launched with
`QDBROWSER_AGENT_CONTROL=1`. No VM required.

```bash
# Terminal A
QDBROWSER_AGENT_CONTROL=1 python3 -m qdbrowser

# Terminal B
python3 tests/integration/scenarios/runner.py
```

Each scenario asserts on:
- the JSON-RPC response shape,
- the PNG screenshot saved per step,
- `journalctl --user -t qdbrowser` lines if you log under that tag.

Following qdistro's discipline, the *load-bearing* assertion is the
journal line, not the pixel; screenshots are for human debugging.

## 2. VM-gated bats scenarios (`vm/`)

`vm/qdbrowser-smoke.bats` and friends launch the browser inside a
libvirt VM cloned from a baseweed template (see `QDWIN_VM_TEMPLATE`),
then drive it via the agent_control socket forwarded over SSH.

Mirrors `qdistro/tests/integration/vm/`:

```bash
just test-vm                       # full sweep
tests/integration/vm/run-parallel.sh smoke.bats
```

Required env vars:

```
QDWIN_VM_TEMPLATE=baseweed-qdbrowser
QDBROWSER_VM_PASSWORD=...
```

`vm/run-parallel.sh` is a thin wrapper around `qdistro`'s pattern: one
bats file per VM, parallel up to `--jobs N`.
