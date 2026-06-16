"""Two-VM agent-driven GUI test harness for multi-machine display.

See ``todo/multi-machine/09-test-strategy.md`` (codex round 6). The harness
is a forcing function: it pins the real contracts (output identity,
generation, lease, evidence format, oracle, failure semantics) before the
feature is built.

Components (build-now order):

- ``marker`` — the deterministic marker **contract**: palette, vertical band
  layout, the machine-readable corner barcode codec, and a numpy reference
  renderer. The C marker client (``qdwin/test-client/qdwin-marker-client.c``)
  and the golden/oracle tests all key off this single contract.
- ``oracle`` — the **pixel oracle**: barcode decode, region (band)
  classification with colour-distance tolerance, hidden-scale detection, and
  stale-generation checks. Deterministic pass/fail (the strong oracle); the
  vision agent is a secondary reviewer.
- ``evidence`` — the **evidence-bundle** format (captures + logs + topology +
  generation/output/frame ids + netem profile + scenario step + oracle
  result).
- ``capture`` — capture adapters naming each framebuffer by what it proves
  (VM-A guest/host, VM-B guest/host, FreeRDP decoded).
- ``agent`` — the vision-agent wrapper over the same evidence bundle.

Deterministic markers are the strong oracle; ``silos.md``: "screenshots are
evidence, not the strongest oracle." A VM pass means geometry/behaviour is
correct, never "feels native" (09 guardrails).
"""
