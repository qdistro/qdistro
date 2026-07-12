# Multi-machine display scenarios (feat/multi-machine-display)

`NN-*.md` scenarios for the multi-machine display feature
(`todo/multi-machine/`). They drive the Python harness in
`qdistro/multimachine/` (oracle, bridge, side-channel, generation/lease) against
qdwin + the shipped per-view RDP path.

**Runner contract** (same as the other GUI scenario dirs): a graphic-aware visual
agent runs the scenario against a VM/host session, saves captures + logs + the
oracle's evidence bundle under the run dir, and writes `status.txt` with one of
PASS/FAIL/ERROR/SKIP. The **deterministic pixel oracle is the gate**; the vision
agent is a secondary reviewer (`multimachine/harness/agent.py`).

**Honesty guardrails (09):** a VM/host pass proves geometry/protocol/decoded-pixel
correctness, never "feels native" / input-to-photon / real-panel fidelity. Always
record which **capture class** (`multimachine/harness/evidence.py:CaptureClass`)
and which **netem profile** a result proves. A scenario claiming what the *peer
monitor* shows must use a **decoded-remote** capture, never a VM-A RDP-source
framebuffer.

**Prereqs:** these need `qdistro-forward` (libfreerdp-shadow3) + a FreeRDP client
+ a qdwin pipewire output pool. They run on the **qci VM bake**; on a bare host
without `freerdp3-devel`/`pipewire` they SKIP (see each scenario's Prerequisites).
Never weaken a gate to make it pass — flag a missing prereq as SKIP, loudly.
