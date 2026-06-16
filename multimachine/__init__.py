"""Multi-machine display feature (feat/multi-machine-display).

Home for the multi-machine display work tracked in
``todo/multi-machine/`` (research proposal → GO-IF → implementation).

Layout:

- ``multimachine.generation`` — the dock-session **generation** + **display
  lease** reference state machine (productizes the D3 logic sim;
  ``todo/multi-machine/08-probe-d3-generation-sim.py``). The eventual daemon
  code must match this contract; the unit tests pin it.
- ``multimachine.harness`` — the two-VM agent-driven GUI **test harness**
  (``todo/multi-machine/09-test-strategy.md`` "build now"): the deterministic
  marker contract, the pixel oracle, the evidence-bundle format, capture
  adapters, and the vision-agent wrapper. Test tooling, not product code.

Nothing here ships to a device yet; it is gated behind the feature branch.
"""
