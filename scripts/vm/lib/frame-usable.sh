# shellcheck shell=bash
#
# frame-usable.sh -- the ONE definition of "is this frame usable / is it dark"
# for the qci GUI harness. SOURCE-SAFE: functions and two threshold variables,
# no `set -e/-u`, no side effects at load. Sourced by scripts/vm/vm-gui (the
# capture-time usability check) and by ci/lib/gates/gui.sh (the F5 darkness
# contradiction diagnostic), so both read the same thresholds and the same
# measurement. Moved verbatim out of vm-gui, which is an executable that
# consumes positional arguments and dispatches commands and so cannot be
# sourced as a library (astra, blankss spec r1).

# Is FILE a frame an agent could actually form a verdict from?
#
# THREE WAYS A CAPTURE IS USELESS, and they are NOT the same failure:
#   2  UNDECODABLE -- truncated/corrupt PNG. Nothing can read it, including any
#      OCR backend. This check exists independently of OCR precisely because OCR
#      is no longer a grading precondition: when it was, a corrupt file failed
#      the OCR gate and was noticed by accident. It no longer is, so readability
#      has to be checked on its own or an undecodable frame reaches the agent
#      looking like a normal capture.
#   3  NEAR-BLACK -- an empty framebuffer: no structure AND dark.
#   4  FLAT -- no structure, but not dark: an all-WHITE or all-grey frame,
#      just as empty and just as useless.
#
# EMPTINESS IS DECIDED BY STRUCTURE, NOT BY BRIGHTNESS, and brightness only
# picks which of the two labels an empty frame gets. This is the opposite of
# what the check did until 2026-09-18, and the reversal is measured, not a
# preference. The old rule refused any frame under 15% bright pixels, with a
# comment claiming "a real dark-theme TUI frame measured well above it and a
# stale black qterminal capture measured 12.6%". The first live capture ever
# taken through this gate refused SIX consecutive frames of the qdlocker lock
# screen -- clock, date, password field, all plainly legible -- at 3.7% bright.
# The stale BLACK frame the cutoff was chosen for is BRIGHTER than a good dark
# one, so no brightness threshold can separate them.
#
# Structure separates them completely. Over 700 frames harvested by the
# 2026-09-16/17 runs: 169 sit under the 15% cutoff, and their standard
# deviation takes exactly two values among the genuinely empty ones -- 0.00000
# (4 frames) and 0.00625 (98, the signature of a black frame with a mouse
# cursor) -- while the next value up is 0.01792. Nothing occupies the gap. The
# 67 remaining sub-15% frames are real content (qdlocker steps, qdshell
# panels, annotated click targets) that the old rule threw away: 9.6% of every
# frame the harness takes.
#
# WHAT THIS GIVES UP, stated because it is a real cost: the brightness cutoff
# was also a crude staleness proxy, and a stale frame showing real content now
# passes the blankness check. Staleness is screenshot_fresh's baseline-hash
# job, and the plain `screenshot` lane has no baseline -- so for that lane the
# proxy is gone rather than replaced. Tune with QCI_FRAME_FLAT_SIGMA rather
# than by restoring a brightness floor.
#
# Return 0 when the frame is usable, or one of the codes above. Return 5 when
# ImageMagick is absent: "cannot tell" must never be silently read as "fine".
FRAME_FLAT_SIGMA=${QCI_FRAME_FLAT_SIGMA:-0.01}
FRAME_BRIGHT_MIN=${QCI_FRAME_BRIGHT_MIN:-0.15}
# The two measurements, defined once: grayscale standard deviation, and the
# fraction of pixels above 10% gray. Each echoes its value; nonzero = the image
# could not be measured.
qci_frame_sigma() {
    magick "$1" -colorspace gray -format '%[fx:standard_deviation]' info: 2>/dev/null
}
qci_frame_bright() {
    magick "$1" -colorspace gray -threshold 10% -format '%[fx:mean]' info: 2>/dev/null
}
screenshot_is_usable() {
    local file=$1 dims bright sigma
    command -v magick >/dev/null 2>&1 || return 5
    dims=$(magick identify -quiet -format '%w %h' "$file" 2>/dev/null) || return 2
    case "$dims" in ''|*' 0'|'0 '*) return 2 ;; esac
    sigma=$(qci_frame_sigma "$file") || return 2
    # Varied? Then treat it as a frame, however dark it looks.
    #
    # WHAT THIS ACTUALLY MEASURES, stated plainly because the previous comment
    # here did not: standard deviation is VARIATION, not spatial structure and
    # not legibility. Nothing in this test distinguishes a dark UI from a
    # broken framebuffer whose variation is meaningless. The counterexample is
    # constructible: 99.9% black pixels with 0.1% white scattered at random has
    # a thresholded bright fraction near 0.001, which the old rule refused,
    # and sigma near sqrt(.001*.999) = 0.0316, which this accepts. Hot pixels,
    # a larger cursor or sensor-style noise land the same way. The 700-frame
    # sample below justifies the THRESHOLD for frames this harness has
    # actually seen; it does not make variance into structure (sol, B11).
    #
    # This is a deliberate trade, and the terms are: the variance gate newly
    # accepts every sub-15%-bright frame whose sigma clears the threshold.
    # That class CONTAINS the 67 observed real-content frames counted below,
    # and it also contains noise-like frames meeting the same threshold --
    # they are not two separate classes, they are the wanted and unwanted
    # members of one. How often the unwanted ones occur in practice is NOT
    # established here. A spatial or content measure could separate cases
    # this one cannot; calling it strictly better would need a defined error
    # metric and evidence, and there is neither (sol, C2 rounds 1 and 2).
    awk -v s="$sigma" -v m="$FRAME_FLAT_SIGMA" 'BEGIN { exit !(s < m) }' || return 0
    bright=$(qci_frame_bright "$file") || return 2
    # Empty. Dark empty is near-black (3); bright empty is flat (4).
    awk -v b="$bright" -v m="$FRAME_BRIGHT_MIN" 'BEGIN { exit !(b < m) }' && return 3
    return 4
}

# Human-readable reason for a screenshot_is_usable code.
screenshot_unusable_reason() {
    case "$1" in
        2) echo "undecodable (truncated or corrupt image)" ;;
        3) echo "near-black (stale framebuffer)" ;;
        4) echo "flat (a single colour fills the frame)" ;;
        5) echo "cannot analyse (ImageMagick 'magick' is not installed)" ;;
        *) echo "unusable (code $1)" ;;
    esac
}

# THE DARKNESS ORACLE the F5 contradiction diagnostic asks, built from the two
# screenshot_is_usable thresholds above and nothing else. screenshot_is_usable
# alone is NOT one: it returns 0 for any sigma >= FRAME_FLAT_SIGMA without ever
# measuring brightness, so a 99.9%-black frame with scattered white pixels is
# "usable" (see its comment). A frame is NOT DARK only when it is usable (has
# variation) AND its thresholded bright fraction, measured here independently
# with the same -threshold 10% measure, reaches FRAME_BRIGHT_MIN. Everything
# else -- near-black, flat (a "blank" claim about an all-white frame is true),
# noisy-black, a legitimately dark UI such as the qdlocker lock screen at 3.7%
# bright -- counts as dark, so a darkness claim about it is never contradicted.
#
# Echoes "sigma=<s> bright=<b>" when it could measure both. Returns 0 = NOT
# dark, 1 = dark/blank, 2 = undecodable, 5 = no ImageMagick ("cannot tell" is
# never "not dark"). Args: file (measure RAW content: qci_view_raw_extract).
qci_frame_not_dark() {
    local file=$1 rc=0 sigma bright
    screenshot_is_usable "$file" || rc=$?
    case "$rc" in
        0) ;;
        2|5) return "$rc" ;;
        *) return 1 ;;
    esac
    sigma=$(qci_frame_sigma "$file") || return 2
    bright=$(qci_frame_bright "$file") || return 2
    printf 'sigma=%s bright=%s\n' "$sigma" "$bright"
    awk -v b="$bright" -v m="$FRAME_BRIGHT_MIN" 'BEGIN { exit !(b >= m) }' || return 1
    return 0
}
