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

# Crop the top-left corner where Noctalia shows the date+clock
# (approximate region: 0,0 → 250,30).
magick /tmp/03-step1-now.png -crop 250x30+0+0 /tmp/03-step1-clock.png
OCR=$(tesseract /tmp/03-step1-clock.png stdout 2>/dev/null)
echo "$EXPECT_HHMM matches OCR: $OCR"
```

**Assert (1.1):** the OCR output contains a substring matching the
4-digit `HHMM` time, possibly with separator (`HH:MM` or `HHMM`).
Allow ±1 minute tolerance for the screenshot/OCR-read race.
**Assert (1.2):** the OCR output also contains the day-of-week
abbreviation (`SUN`/`MON`/.../`SAT`) matching the VM's
`date +%a` output.

### Step 2 — advance VM clock by 1 minute, verify bar updates

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'date -s "+1 minute" >/dev/null'
sleep 65 # let the next clock-tick fire (Noctalia ticks every minute)

EXPECT_HHMM2=$("$QDWIN_VM_EXEC" "$VMNAME" 'date +%H%M' | tail -1)
noct_screenshot_awake /tmp/03-step2-advanced.png
magick /tmp/03-step2-advanced.png -crop 250x30+0+0 /tmp/03-step2-clock.png
OCR2=$(tesseract /tmp/03-step2-clock.png stdout 2>/dev/null)
```

**Assert (2.1):** `OCR2` contains the new `HHMM` (different from
step 1's `HHMM`).
**Assert (2.2):** `OCR2` does NOT still show step 1's `HHMM`.

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
