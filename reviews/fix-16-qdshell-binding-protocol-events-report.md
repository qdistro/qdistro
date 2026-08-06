# Fix report: `16-qdshell-binding-protocol-events` GUI FAIL

**Full run:** `full-20260806T120748Z-3331037` (exit 70 class=gui)  
**Failed subject:** `qdwin/tests/gui/16-qdshell-binding-protocol-events.md`  
**Agent:** codex gpt-5.6-luna, status=FAIL (rc=0; required asserts green)

## Root cause

1. **Product/infra:** gui-qdwin goldens baked with `QDWIN_APP_DEPS=0` (default since `5f48e17`) no longer installed `dejavu-fonts` / `liberation-fonts`. Guest fontconfig was empty; qdshell FontService logged `Loaded 1 fonts, 1 monospace` (synthetic system-default only). The bar then painted **icons without any text** — Clock, ActiveWindow title, workspace numbers all blank.

2. **Evidence chain:**
   - Steps 1–2, 4–5 all PASS (bind, `toplevel_added`, `seat_focus_changed`, remove, Alt+Tab).
   - Step 3 screenshot: centered test window present, top bar has icons only, **no** `qd16-step2`.
   - Aug 1 full run (fonts present, `Loaded 11 fonts`): title visible, 3.1 PASS.
   - Aug 5 full runs (fonts gone, `Loaded 1 fonts`): 3.1 soft-fail; agents still overall PASS.
   - Aug 6 Luna agent: same blank bar, but treated soft 3.1 as **hard FAIL**.

3. **Not a binding regression:** protocol path (qdwin → QML plugin → ListModel) is healthy; UI text cannot render without faces.

## Fix

| Repo | Branch | Commit | Change |
|------|--------|--------|--------|
| `qdistro` | `fix/gui-16-bar-fonts` | `3785bb5` | `fresh-vm-bootstrap.sh`: always install dejavu+liberation (+ `fc-cache`) **before** the opt-in APP_DEPS lane |
| `qdwin` | `fix/gui-16-bar-fonts` | `f8eb85f` | Scenario 16: setup installs fonts when `fc-list` is empty; assert 3.1 explicitly SOFT / NON-BLOCKING (3.1-only → overall PASS) |

Heavy app packages (firefox/vlc/foot/…) remain `QDWIN_APP_DEPS=1` opt-in.

## Verification

Static:

```bash
# baseline fonts precede APP_DEPS gate; bash -n clean
python3 -c '...'  # OK
bash -n scripts/vm/fresh-vm-bootstrap.sh  # OK
```

Not run here (heavy / golden rebake):

```bash
# After merge + golden rebake (or on a clone where scenario setup can zypper):
QCI_JOBS=1 QCI_GUI_JOBS=1 \
  QCI_AGENT_CMD='timeout 1800 codex --yolo exec -m gpt-5.6-luna --skip-git-repo-check --ephemeral - < {prompt}' \
  QCI_AGENT_MODEL=gpt-5.6-luna \
  ./ci/bin/qci gui --scenario qdwin/tests/gui/16-qdshell-binding-protocol-events.md
```

Expect: journal still shows bind + toplevel events; bar text returns once fonts land (scenario setup heals old goldens; new goldens get fonts from bootstrap). Even if OCR flakes, agent must write `status.txt=PASS` when only 3.1 misses.

## Merge readiness

- **Ready to merge** both branches to main (no force-push; clean commits on top of current main).
- **Residual risk:**
  - Existing gui-qdwin goldens without fonts still need either rebake or scenario setup's zypper (needs network/repos in guest). Offline guests → 3.1 stays soft-fail but required asserts pass.
  - Restoring bar text expands left-bar width (Clock etc.); any pixel-hardcoded click tests that assumed a text-less bar may need retune (historically compositor-shell.bats with foot was the concern — foot stays opt-in).
  - No full `qci full` / agent retest executed in this fix-pass.

## Do not merge/push decision for parent

Parent should merge when convenient; recommend merging **qdistro first** (bootstrap), then **qdwin** (scenario). Optional golden rebake of gui-qdwin after qdistro lands so new clones skip the runtime font install.
