# tests/host/

Vision-driven GUI scenarios for qdwin + qdshell, runnable headless on
the host. No VM, no popups.

- `AGENTS.md` — for graphic-aware subagents executing scenarios.
  Read this first if you're a runner or orchestrator.
- `lib.sh` — shared bash helpers (state paths, build-on-demand).
- `start.sh` / `stop.sh` / `screenshot.sh` / `ctrl.sh` — the harness.
- `NN-*.md` — user-authored scenarios. One per file.

Quick check the harness still works:

```bash
ID=demo
./start.sh $ID
./screenshot.sh $ID baseline
./ctrl.sh $ID max 1
sleep 0.5
./screenshot.sh $ID maxed
./stop.sh $ID
```

State for each test-id lives at `/tmp/qdwin-host-tests/<id>/`. See
`AGENTS.md` for the layout.
