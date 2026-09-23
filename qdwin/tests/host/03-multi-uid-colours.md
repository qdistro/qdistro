# 03 — per-uid border colour distinguishes two qdshell instances

**What**: in production each uid runs its own qdshell against its
own per-uid weston instance, with a distinct border colour from the
admin's `--colors` map. Verify that two separate test compositors,
each with a different `--colors`, produce visually distinct chrome
borders.

**Why**: per-uid colour is a load-bearing visual isolation
indicator (`the architecture doc` § "Responsibilities of admin
compositor" #4). A regression that paints all uids in the same
colour would silently break the security UX cue that lets a user
tell at a glance which window came from which identity.

## Setup

```bash
HT=tests/host
ID_A=03-multi-uid-colours-A
ID_B=03-multi-uid-colours-B

# Two compositors. Each uses the host's real uid (we can't actually
# fork a second uid without root) but maps that uid to a different
# colour per instance — the test verifies qdshell honours the map,
# not that uids differ at the kernel level.
$HT/start.sh $ID_A --colors "$(id -u)=#22aaff"
$HT/start.sh $ID_B --colors "$(id -u)=#ff8800"
```

## Steps

### S1 — capture both

```bash
SHOT_A=$($HT/screenshot.sh $ID_A 01-admin-blue)
SHOT_B=$($HT/screenshot.sh $ID_B 01-work-orange)
```

**Read `$SHOT_A` and `$SHOT_B`.**

**Assert (distinct colours):**
- `$SHOT_A` shows the chrome border in a **cyan/blue** colour
  (around #22aaff). The visible strips on the left + bottom of
  the terminal window are blue.
- `$SHOT_B` shows the chrome border in **orange** (around #ff8800).
  The visible strips on the left + bottom are orange.
- The two screenshots are visually distinguishable purely by
  border colour — same window content, same layout, different
  decoration tint.

### S2 — interaction works in both

```bash
$HT/ctrl.sh $ID_A max 1
$HT/ctrl.sh $ID_B max 1
sleep 0.4
SHOT_A=$($HT/screenshot.sh $ID_A 02-admin-blue-max)
SHOT_B=$($HT/screenshot.sh $ID_B 02-work-orange-max)
```

**Read both.**

**Assert (independent maximise):**
- Both windows are now maximised (filling their respective
  framebuffers).
- Border colour is preserved per instance — `$SHOT_A` still blue,
  `$SHOT_B` still orange. The colour didn't reset on the resize.

## Teardown

```bash
$HT/stop.sh $ID_A
$HT/stop.sh $ID_B
```

## Notes for the runner

- Two compositors run in parallel — fine, the harness is
  per-test-id-isolated (separate XDG_RUNTIME_DIR, separate wayland
  socket name).
- "Cyan vs orange" is a coarse visual assertion; pixel-exact match
  to the hex values isn't required. If both windows look the same
  colour to you, FAIL — that's the regression we care about.
- If you see any cyan in `$SHOT_B` or any orange in `$SHOT_A`
  (cross-contamination from one qdshell into the other's
  framebuffer), FAIL with both screenshot paths.
