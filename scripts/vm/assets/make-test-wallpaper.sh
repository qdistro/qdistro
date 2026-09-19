#!/usr/bin/env bash
# Render the GUI-test-lane desktop wallpaper: a plain diagonal+orthogonal grid,
# no text, no fonts.
#
# THIS SCRIPT DOES NOT IMPLEMENT THE PATTERN. It extracts and runs the
# generator embedded in spin-test-vm-gui.sh, which is the single authoritative
# implementation. An earlier version of this file drew the pattern again with
# ImageMagick and drifted immediately: `-strokewidth 2` gave 2px orthogonal
# rules where the Python draws 1px, so the two produced measurably different
# frames (sigma 0.1014 vs 0.0977) while claiming to be the same pattern.
#
# WHY A WALLPAPER AT ALL. qdshell ships `wallpaper.enabled: true` with an empty
# `directory` (qdshell Commons/Settings.qml), and the GUI test VMs never
# populated ~/Pictures/Wallpapers -- so the desktop background was BLACK. An
# idle desktop and a compositor that had stopped painting then produced frames
# the gate could not tell apart, which is the condition behind a long line of
# indirect diagnoses of this failure class.
#
# WHAT IT DOES AND DOES NOT ESTABLISH. A structured frame shows the pattern was
# rendered; it is NOT proof the compositor is painting NOW -- a frozen
# framebuffer keeps its last contents, which is precisely the state observed in
# gui-20260919T072913Z, where three retries returned byte-identical frames.
# Repeat-capture comparison remains the liveness test. What the wallpaper buys
# is that the ordinary idle desktop is no longer FLAT, so it is distinguishable
# from the uniform-black signature rather than identical to it.
#
# WHY A PATTERN AND NOT A COLOUR. screenshot_is_usable refuses a frame whose
# grayscale sigma is under FRAME_FLAT_SIGMA (0.01) as FLAT. qdshell's own
# `solidColor` default (#1a1a2e) measures sigma exactly 0, so a solid-colour
# background would have the gate refuse a healthy desktop.
#
# The pattern is 64px-periodic in both axes, so any axis-aligned, unscaled
# 64x64 crop contains each pair of residues exactly once and therefore has the
# histogram of ONE COMPLETE PERIOD. That is only APPROXIMATELY the whole
# frame's: 800 is 12.5 periods, not an integer, so a tile is 496/4096 rule
# pixels (0.121094) against the frame's 123400/1024000 (0.120508). The
# difference does not matter to the gate, but the two are not equal. This is a
# property of the periodicity -- not a measured lower bound on how much
# uncovered desktop a real scenario leaves.
set -euo pipefail

out=${1:?usage: make-test-wallpaper.sh <out.png>}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
spin="$here/../spin-test-vm-gui.sh"
[ -r "$spin" ] || { echo "cannot read $spin" >&2; exit 1; }

gen=$(mktemp -t qdistro-wp-gen.XXXXXX.py)
trap 'rm -f "$gen"' EXIT
awk '/^    runuser -u admin -- python3 - .*qdistro-test-pattern/{f=1;next} /^WPEOF/{f=0} f' \
    "$spin" > "$gen"
[ -s "$gen" ] || { echo "could not extract the generator from $spin" >&2; exit 1; }
python3 "$gen" "$out"
