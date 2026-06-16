# Two-VM display test harness

Forcing-function test harness for the multi-machine display feature
(`todo/multi-machine/09-test-strategy.md`, codex round 6). Built before most of
the feature so it pins the real contracts — output identity, dock generation,
display lease, evidence format, pixel oracle, failure semantics — early.

> **Boundary (09 guardrail):** these tests prove **compositor/protocol/geometry
> correctness**. They do **not** prove physical display quality or
> native-feeling latency. A VM/host pass means "geometrically/behaviourally
> correct", never "feels native". C1/C2 latency and A5 panel fidelity stay
> physical-bench gates.

## Components

| Module | Role |
|---|---|
| `marker.py` | The deterministic marker **contract** — palette, vertical band layout, the machine-readable corner barcode codec (pure-Python, CRC8), and a numpy reference renderer. The C client `qdwin/test-client/qdwin-marker-client.c` paints the *same* contract; keep them in sync. |
| `oracle.py` | The **strong** pixel oracle — barcode decode, band classification with colour-distance tolerance, independent hidden-scaling detection, stale-generation checks. |
| `evidence.py` | The **evidence-bundle** format — captures named by what they prove (`CaptureClass`) + the honesty rule refusing a remote-proof bundle with no decoded-remote capture. |
| `capture.py` | Image loading (native PPM + PIL PNG → RGB) and capture adapters (`VirshScreenshot` host-side, `WestonScreenshooter` guest-side). |
| `agent.py` | The vision-agent wrapper — runs `QCI_AGENT_CMD` as a **secondary** reviewer over a bundle; never overrides the oracle. |
| `netem.py` | The five **named** netem profiles + `tc` argv builders. |
| `topology.py` | Two-VM topology + `PortLease` (collision-free) + screen-index→output-id `ScreenMap`. |
| `livecheck.py` | **Live render/golden runner** — marker → stock headless weston → `weston-screenshooter` → oracle → evidence bundle. Host-runnable, no VM lock. |
| `../generation.py` | (product contract) dock **generation** + display **lease** state machine; encodes the 06 state×concern table. |

## Run

```sh
# fast pure-Python layer (codec, oracle, evidence, state machine, fuzz)
python3 -m pytest tests/unit/test_mm_*.py -m "not slow"

# + live render/golden through a real compositor (needs weston + the C client)
python3 -m pytest tests/unit/test_mm_*.py

# ad-hoc live render/golden, prints the oracle verdict + bundle path
python3 -m multimachine.harness.livecheck
```

The C marker client is built from the `qdwin` repo
(`meson compile -C <build> qdwin-marker-client`); point the tests at it with
`QDWIN_MARKER_CLIENT=/path/to/qdwin-marker-client` (else common build dirs are
probed). `--dump-ppm` renders offscreen for the cross-language contract test.

## Marker contract (shared with the C client)

- Palette: red `#e02020`, green `#20c060`, blue `#2060e0`, yellow `#e0d020`,
  white, black (high-contrast, survive RDP encode).
- Six vertical bands at known x-ranges: `left-anchor`, `pre-seam`, `seam-left`,
  `seam-right`, `post-seam`, `right-anchor`. `seam-{left,right}` straddle the
  seam; `*-anchor` are always present.
- Corner barcode: 14×14 cells, origin anchor + timing patterns + data cells,
  carrying magic/version/output-id/generation/frame/logical-rect/scale + CRC8.
- 8×8 fiducial checkers per band for hidden-scale detection.

If you change the contract, change **both** `marker.py` and
`qdwin-marker-client.c` and re-run `test_mm_marker_contract.py` (asserts the C
output is pixel-identical to the Python reference) + regenerate the golden.

## Capture honesty rule

Comparing VM-A local against the VM-A RDP-*source* framebuffer hides every
encode/transport defect. A bundle claiming to prove what the *peer monitor*
shows must contain a **decoded-remote** capture (`VM_B_HOST` / `VM_B_GUEST` /
`FREERDP_DECODED`); `EvidenceBundle.assert_remote_proof()` enforces it.
