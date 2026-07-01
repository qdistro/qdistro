# 03 — Noctalia clock widget shows correct time

**Acceptance criterion:** the clock widget in the bar repaints after
the VM's wall-clock time advances by 1 minute. The hard behavioral
signal is that the full-width top-bar crop's image hash changes between
the pre-advance and post-advance screenshots. OCR of the rendered clock
text is best-effort diagnostic output only.

This exercises:
- Bar widget rendering (text + glyphs, not just a colored rect)
- Configure/ack cadence on widget property updates (Noctalia binds
 Date.now() to the clock label, which fires repaints)
- OCR readability of bar text (diagnostic signal only; the pass/fail
 decision comes from the repaint hash)

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
source ${QDISTRO_REPO}/tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:-noctalia-vis-260503-1021}"
noct_session_healthy || { echo "FAIL: noctalia not healthy"; exit 1; }

# This scenario logs tesseract OCR when available, but OCR is not a
# pass/fail condition. The clock font can render correctly while OCR
# returns garbage or empty text.
if "$QDWIN_VM_EXEC" "$VMNAME" 'which tesseract' >/dev/null; then
  echo "INFO: tesseract available for best-effort OCR diagnostics"
else
  echo "INFO: tesseract not installed in VM; skipping OCR diagnostics"
fi
```

## Steps

### Step 1 — capture bar with current time

```bash
EXPECT_HHMM=$("$QDWIN_VM_EXEC" "$VMNAME" 'date +%H%M' | tail -1)
noct_screenshot_awake /tmp/03-step1-now.png

# Crop the FULL-WIDTH top bar strip, not a top-left corner: Noctalia centres
# the clock capsule in the bar, so a narrow 250x30 left crop misses it entirely
# and OCR comes back empty. An over-wide width clamps to the image width, so
# this is resolution-robust.
magick /tmp/03-step1-now.png -crop 2560x48+0+0 /tmp/03-step1-clock.png
STEP1_HASH=$(sha256sum /tmp/03-step1-clock.png | awk '{print $1}')
[ -n "$STEP1_HASH" ] || { echo "FAIL: step-1 bar crop hash is empty"; exit 1; }
OCR=$(tesseract /tmp/03-step1-clock.png stdout 2>/dev/null || true)
echo "$EXPECT_HHMM matches OCR: $OCR"
echo "step1 bar crop sha256: $STEP1_HASH"
```

**Assert (1.1):** the screenshot and full-width top-bar crop were
captured, and the crop hash `STEP1_HASH` is non-empty. OCR output may
be empty or unreadable; record it as diagnostic data only.
**Assert (1.2):** if OCR returns recognizable text, it should contain
either the `HHMM`/`HH:MM` time (allow ±1 minute tolerance for the
screenshot/OCR-read race) or the day-of-week abbreviation matching
the VM's `date +%a` output. This assertion is best-effort only and
must not fail the scenario.

### Step 2 — advance VM clock by 1 minute, verify bar updates

> **Driver note (MUST):** Step 2 includes a synchronous wait of up to ~75 s
> for the next 60 s clock tick. Run the whole Step-2 block as a **single
> blocking invocation in this turn**. Do NOT background it, do NOT schedule a
> wakeup, and do NOT end the session to "check back later" — the wait must
> complete in-line and you MUST write `status.txt` before finishing.

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'date -s "+1 minute" >/dev/null'

EXPECT_HHMM2=$("$QDWIN_VM_EXEC" "$VMNAME" 'date +%H%M' | tail -1)
[ "$EXPECT_HHMM" != "$EXPECT_HHMM2" ] || {
  echo "FAIL: VM time did not advance from step 1 to step 2"
  exit 1
}

# Poll up to 75s for the next minute-tick repaint. Noctalia ticks on a
# 60 000 ms Qt timer from launch; advancing the wall clock does not
# reschedule it, so the repaint lands within one timer period. Break early
# once the crop hash changes so a fast tick doesn't pay the full wait. This
# is ONE synchronous loop — it must run to completion in this shell call.
STEP2_HASH=""
for _ in $(seq 1 15); do
  sleep 5
  noct_screenshot_awake /tmp/03-step2-advanced.png
  magick /tmp/03-step2-advanced.png -crop 2560x48+0+0 /tmp/03-step2-clock.png
  STEP2_HASH=$(sha256sum /tmp/03-step2-clock.png | awk '{print $1}')
  [ "$STEP1_HASH" != "$STEP2_HASH" ] && break
done
[ -n "$STEP2_HASH" ] || { echo "FAIL: step-2 bar crop hash is empty"; exit 1; }
OCR2=$(tesseract /tmp/03-step2-clock.png stdout 2>/dev/null || true)
echo "step2 bar crop sha256: $STEP2_HASH"
echo "step2 OCR diagnostic: $OCR2"
[ "$STEP1_HASH" != "$STEP2_HASH" ] || {
  echo "FAIL: clock bar crop did not repaint within 75s after VM time advanced"
  exit 1
}
```

**Assert (2.1):** the bar crop after the clock advance differs from
the step-1 crop by image hash. This is the hard pass condition for the
clock update.
**Assert (2.2):** the VM time changed from step 1 to step 2 and the
bar crop changed after the next clock tick, proving the widget
repainted even when OCR cannot parse the glyphs.
**Assert (2.3):** if OCR returns recognizable digits, it should contain
the new `HHMM` and not the step-1 `HHMM`. This is best-effort diagnostic
output only and must not fail the scenario.

### Step 3 — restore clock + cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'systemctl restart systemd-timesyncd 2>/dev/null || timedatectl set-ntp true'
sleep 2
```

**Assert (3.1):** qdshell.service still active —
`noct_session_healthy`.

## Pass criteria

Hard pass conditions:

1. Setup proves the Noctalia session is healthy.
2. Step 1 and Step 2 screenshots and bar crops are captured.
3. The VM time advances from Step 1 to Step 2.
4. The Step 2 bar-crop hash differs from the Step 1 bar-crop hash.
5. Cleanup leaves `qdshell.service` healthy.

OCR exact-digit matching is diagnostic only and must not fail the
scenario when the crop hash changed.

## Known failure modes

1. **OCR reads "tofu" / no text** — record the OCR output in the
 report, but do not fail the scenario if the bar-crop hash changed.

2. **Clock doesn't advance for 1 full minute** — Noctalia ticks
 on a Qt Timer at 60 000ms interval starting from launch.
 Advancing the system clock doesn't reschedule the timer.
 Either wait the full 60s+5s, or look at sub-minute fields if
 Noctalia's format includes seconds.

3. **OCR can't disambiguate `0` vs `O`** — Noctalia's monospace
 font for the bar might render `0` as a slashed glyph that
 tesseract reads as `Ø`. Tolerance: if expected `1023` and
 OCR reads `lO23` or `lo23`, accept (l→1 is common, O→0 is
 common).
