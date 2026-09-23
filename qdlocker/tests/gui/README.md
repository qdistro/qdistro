# qdlocker VM GUI tests

Literate test scenarios run against a libvirt/qemu guest. Same shape
as `qdwin/tests/gui/` — each `NN-*.md` is sourceable bash interleaved
with assertions a human or LLM agent verifies against screenshots and
ctrl-socket output.

## Prerequisites

- A qdistro tier4 guest image with `qdlocker.service` enabled. Build
  with the include flag:

  ```bash
  cd ../../../tier4-vm
  ./build-guest-image.sh --include qdlocker
  ```

  The flag wires up `pip install /repos/qdlocker` and
  `systemctl --user enable qdlocker.service` inside the guest, and
  installs the qdistro-fprintd-fake helper used by 02.

- The guest must be running. Spawn with:

  ```bash
  ../../../tier4-vm/spawn-tier4.sh
  ```

- `virsh`, `qemu`, and `socat` on the host. (Same as qdwin's tests.)

## Running

```bash
source qdlocker-helpers.sh
qdwin_set_vm "$(virsh -c qemu:///session list --name --state-running | head -1)"

# Drive the cycle test step-by-step (each step's bash block runs
# under the same shell):
bash -x 01-lock-cycle.md     # or feed step-by-step to an agent
```

## Files

- `qdlocker-helpers.sh` — sources qdwin's helpers, adds
  `qdlocker_ctrl`, `qdlocker_wait_for_lock`,
  `qdlocker_wait_for_unlock`, `qdlocker_assert_prompt_len`.
- `01-lock-cycle.md` — manual lock → password type → unlock.
- `02-fprintd-fallback.md` — fingerprint path on the system bus.
- `09-capture-indicators.md` — J28 live-capture / egress indicators.
  Unconditional: quiet lock, a real `pw-record` mic capture started and
  stopped *while locked*, observer timeout failing **visible**, silo
  egress including transient `Stopping` and an unreachable session
  manager, and a locked-state restart. Conditional (SKIP with a printed
  reason): system audio (needs a default sink), camera (needs a
  `Video/Source` node + gstreamer), screencast (needs a **manually**
  driven qdwin view stream — the scenario does not reimplement a Wayland
  client), and the second-output step, which documents the known
  multi-output gap (`todo/fable-release/12-j28-multi-output-lock-indicators.md`).

## What these tests are NOT

- They are **not** end-to-end with a real fingerprint reader.
  Hardware-backed fprintd lives in
  `qdistro/tests/integration/hw-fprintd/` (host-side smoke).
- They are **not** a substitute for unit tests of the controller.
  Pytest unit tests live under `../unit/`.

The assertions here are the load-bearing ones: a regression in the
overlay-key routing or the lock-surface destroy path shows up in
step 3 and step 4 of `01-lock-cycle.md`. Per project convention
([feedback_logging_discipline](../../README.md)), the journal /
ctrl-socket assertions are authoritative — screenshot asserts back
them up.
