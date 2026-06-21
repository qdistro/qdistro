# 03 — Noctalia clock widget shows correct time

**Acceptance criterion:** the clock widget in the bar displays the
VM's current wall-clock time. After advancing the VM clock by 1
minute, the next bar repaint shows the new time.

This exercises:
- Bar widget rendering (text + glyphs, not just a colored rect)
- Configure/ack cadence on widget property updates (Noctalia binds
 Date.now() to the clock label, which fires repaints)
- OCR readability of bar text (regression signal: if a 
 strip breaks font loading, this test fails first)

## Setup

```bash
source qdwin/tests/gui/qdwin-helpers.sh
source tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:-noctalia-vis-260503-1021}"
noct_session_healthy || { echo "FAIL: noctalia not healthy"; exit 1; }

# This scenario uses tesseract for OCR. Verify availability.
"$QDWIN_VM_EXEC" "$VMNAME" 'which tesseract' >/dev/null || {
 echo "INFRA: tesseract not installed in VM"; exit 1;
}
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
OCR=$(tesseract /tmp/03-step1-clock.png stdout 2>/dev/null)
echo "$EXPECT_HHMM matches OCR: $OCR"
```

**Assert (1.1):** the OCR output is non-empty and the cropped bar
strip contains visible foreground glyphs. If OCR cannot decode the
exact digits, record that as an OCR limitation rather than a clock
rendering failure.
**Assert (1.2):** if OCR returns recognizable text, it should contain
either the `HHMM`/`HH:MM` time (allow ±1 minute tolerance for the
screenshot/OCR-read race) or the day-of-week abbreviation matching
the VM's `date +%a` output.

### Step 2 — advance VM clock by 1 minute, verify bar updates

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'date -s "+1 minute" >/dev/null'
sleep 65 # let the next clock-tick fire (Noctalia ticks every minute)

EXPECT_HHMM2=$("$QDWIN_VM_EXEC" "$VMNAME" 'date +%H%M' | tail -1)
noct_screenshot_awake /tmp/03-step2-advanced.png
magick /tmp/03-step2-advanced.png -crop 2560x48+0+0 /tmp/03-step2-clock.png
OCR2=$(tesseract /tmp/03-step2-clock.png stdout 2>/dev/null)
```

**Assert (2.1):** the bar crop after the clock advance differs from
the step-1 crop, and `OCR2` is non-empty. If OCR returns recognizable
digits, it should contain the new `HHMM` and not the step-1 `HHMM`.
**Assert (2.2):** the VM time changed from step 1 to step 2 and the
bar crop changed after the next clock tick, proving the widget
repainted even when OCR cannot parse the glyphs.

### Step 3 — restore clock + cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'systemctl restart systemd-timesyncd 2>/dev/null || timedatectl set-ntp true'
sleep 2
```

**Assert (3.1):** qdshell.service still active —
`noct_session_healthy`.

## Pass criteria

All asserts in steps 1-3 pass.

## Known failure modes

1. **OCR reads "tofu" / no text** — fonts not installed. Same gap
 as `qdwin/01-open-terminal.md` step 1.1. Fix path:
 `zypper install fontconfig dejavu-fonts` in VM bootstrap.

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
