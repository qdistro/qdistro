#!/usr/bin/env bats
#
# Host-only tests for the GUI gate's VISUAL-EVIDENCE CONTRACT
# (ci/lib/gates/gui.sh).
#
# Round 2 required the AGENT to leave evidence that it had read the pixels (an
# OCR TSV in the artifact dir, or a structured image-open event in its log). An
# adversarial review reproduced the bypass in one line: the agent has arbitrary
# host shell, so it authors the evidence too. Round 3 moves the observation to
# the harness — after the agent exits, the GATE runs OCR itself over the frames
# harvested for THIS scenario. These tests pin that:
#
#   gui_scenario_visual_mode            - the scenario MUST declare required|none
#   gui_validate_scenarios              - an undeclared scenario is a usage error
#   gui_ocr_backend_probe               - real tesseract only (not a game's binary)
#   gui_visual_frames                   - per-frame linkage to this scenario's dir
#   gui_harness_ocr_frames              - the gate's own OCR pass + manifest
#   gui_apply_visual_evidence_contract  - PASS/FAIL -> ERROR when unreadable
#
# Round 4's review reproduced the residual bypass: a blank/unrelated image the
# AGENT wrote was a "frame", zero-word OCR returned ok, a pre-planted
# visual-evidence/ came back as OCR input after its rename, and deleting a
# damning frame was invisible. Round 5 moves the frame SET off the disk and onto
# a harness capture log written by scripts/vm/vm-gui. These also pin:
#
#   gui_capture_log_init/_verify        - the harness ledger + its hash chain
#   gui_capture_reconcile               - attested-vs-on-disk; each row claims
#                                         its OWN file first (in-tree by exact
#                                         path, out-of-tree by basename), then
#                                         by digest
#
# The producer side is not re-implemented here: attest_row() sources the REAL
# scripts/vm/lib/capture-attest.sh, so producer and verifier are pinned against
# each other rather than against a test-local copy. THREE capture tools use that
# library -- vm-gui (labwc/admin lane), qdwin-helpers.sh (in-guest qdwin lane)
# and qdwin-apps-helpers.sh (qdwin apps lane); this header said "both" (fable,
# B round 8). Only the vm-gui lane is exercised here.
#
# Everything here is filesystem-only: no VM, no agent, no qci run. The OCR
# backend is a STUB whose output depends on the FRAME BYTES -- exactly the
# property the design relies on -- so these tests do not depend on the host's
# OCR either way. (This header used to say the host has no real tesseract. It
# has had tesseract-ocr 5.5.3 since 2026-09-16; the stub is a test-isolation
# choice, not a workaround for an absent backend.)

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    EXIT_USAGE=2
    TDIR="$(mktemp -d "${BATS_TMPDIR:-/tmp}/gui-visual.XXXXXX")"
    ADIR="$TDIR/artifacts"
    mkdir -p "$ADIR" "$TDIR/bin"

    VISUAL_MD="$TDIR/07-visual.md"
    PLAIN_MD="$TDIR/08-headless.md"
    UNDECLARED_MD="$TDIR/09-undeclared.md"
    cat > "$VISUAL_MD" <<'EOF'
# 07 - a pixel-dependent scenario
<!-- qci:visual: required -->
$VMGUI "$VM" screenshot $ARTIFACT_DIR/s1.png
Assert the Approve button is visible.
EOF
    cat > "$PLAIN_MD" <<'EOF'
# 08 - a journal/socket scenario
<!-- qci:visual: none -->
$VMEXEC "$VM" 'journalctl -u qdistro-admin-broker.service | grep -q Denied'
EOF
    # No marker at all, and it DOES capture pixels: round 2's content fallback
    # would have called this `required`; round 3 calls it a registry defect.
    cat > "$UNDECLARED_MD" <<'EOF'
# 09 - captures frames but declares nothing
$VMGUI "$VM" screenshot $ARTIFACT_DIR/s1.png
EOF

    # A stub OCR backend: its TSV is derived from the INPUT FILE's bytes, so a
    # test can build a "frame" that reads as text or as a blank pane, and the
    # gate's decision demonstrably comes from the frame, not from the agent.
    cat > "$TDIR/bin/stubtess" <<'EOF'
#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "tesseract 5.3.4"; echo " leptonica-1.84.1"; exit 0
fi
in=$1; out=$2
[ -r "$in" ] || exit 1
printf 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n' > "$out.tsv"
printf '1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t\n' >> "$out.tsv"
n=0
for w in $(sed -n 's/^TEXT://p' "$in"); do
    n=$((n + 1))
    printf '5\t1\t1\t1\t1\t%d\t10\t20\t60\t14\t93.5\t%s\n' "$n" "$w" >> "$out.tsv"
done
exit 0
EOF
    # A backend that is installed but cannot read anything (broken traineddata,
    # unsupported format): every invocation fails.
    cat > "$TDIR/bin/brokentess" <<'EOF'
#!/bin/sh
if [ "$1" = "--version" ]; then echo "tesseract 5.3.4"; exit 0; fi
echo "Error opening data file" >&2
exit 1
EOF
    chmod +x "$TDIR/bin/stubtess" "$TDIR/bin/brokentess"
    export PATH="$TDIR/bin:$PATH"
    export QCI_OCR_BIN=stubtess

    OCR_HEADER=$'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext'

    # The harness capture log for this "scenario attempt". It lives OUTSIDE the
    # artifact dir, exactly as gui_run_scenario places it in the run tree.
    CAPLIB="$REPO_ROOT/scripts/vm/lib/capture-attest.sh"
    CAPLOG="$TDIR/captures/scenario.tsv"
    CAPVM=testvm
    gui_capture_log_init "$CAPLOG" "$CAPVM"
}

# Append a capture row using the REAL producer from
# scripts/vm/lib/capture-attest.sh, so a drift between the tool that writes the
# ledger and the gate that verifies it fails these tests instead of silently
# disabling the contract. This drives the library's INTERNAL row writer, which
# is what the capture tools reach through their own entry points.
# Args: abs_path [artifact_root] [vm]  (empty root => the capture is out-of-tree)
attest_row() {
    bash -c '
        set -euo pipefail
        . "$1"
        _qci_capture_attest_row "$2" "$3" "$4" "${5-}"
    ' _ "$CAPLIB" "$CAPLOG" "$1" "${3-$CAPVM}" "${2-$ADIR}"
}

# The library's one hand-over entry point (the in-guest qdwin lane's producer),
# driven exactly as that helper drives it. Args: abs_path [artifact_root] [vm]
attest_frame() {
    QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="${2-$ADIR}" bash -c '
        set -euo pipefail
        . "$1"
        capture_attest_frame "$2" "$3"
    ' _ "$CAPLIB" "$1" "${3-$CAPVM}"
}

# What the GATE does between the agent exiting and grading: seal the ledger and
# keep "<vm>\t<rows>\t<head>" in its own memory. Echoes that anchor.
seal_anchor() {
    local sealed
    sealed=$(gui_capture_log_seal "${1-$CAPLOG}" "$CAPVM") || return 1
    printf '%s\t%s' "$CAPVM" "$sealed"
}

# Stand-in for gui_run_scenario's harvest->seal->grade sequence. Seals the
# ledger if it is not sealed yet (otherwise re-reads the existing seal, so a
# test may call it twice), then applies the contract with that anchor.
# Args: status scenario artifact_dir capture_log
apply_contract() {
    local st=$1 scen=$2 adir=$3 clog=${4-} anchor="" sealed="" rows hd
    if [ -n "$clog" ] && [ -f "$clog" ]; then
        if tail -n +3 -- "$clog" | awk -F'\t' '$4 == "seal" { found = 1 } END { exit !found }'; then
            rows=$(tail -n +3 -- "$clog" | grep -c . || true)
            hd=$(tail -1 -- "$clog" | awk -F'\t' '{print $8}')
            sealed="$rows"$'\t'"$hd"
        else
            sealed=$(gui_capture_log_seal "$clog" "$CAPVM") || sealed=""
        fi
        [ -n "$sealed" ] && anchor="$CAPVM"$'\t'"$sealed"
    fi
    # PASS THE CAPTURE ROOT, as production does (gui_run_scenario hands it
    # $art_alias). Round 6 added the exact-path matching pass and NOTHING in
    # this file exercised it: with no root the pass cannot fire, so a mutant
    # that replaced it with `continue` was still 119/119, and every in-tree
    # frame was reported `relocated` because the digest pass matched it --
    # a false note no assertion ever read (fable, B round 6). A test that
    # wants the rootless fallback passes CAPROOT="" explicitly.
    gui_apply_visual_evidence_contract "$st" "$scen" "$adir" "$clog" "$anchor" \
        "${CAPROOT-$ADIR}"
}

teardown() {
    [ -n "${TDIR:-}" ] && rm -rf -- "$TDIR"
}

# Write an image into the artifact dir WITHOUT any harness capture behind it --
# i.e. exactly what an agent with a host shell can do. The stub OCR backend
# reads text out of its bytes. Args: relpath [word...]
# Plant a REAL, decodable PNG carrying the given words as rendered text.
#
# These fixtures used to be a PNG magic header followed by plain text. That was
# fine while nothing decoded them, but round 9 added a decodability probe to the
# gate (an external review produced `ok:` for a file containing the twelve bytes
# `not an image`), and a fake PNG is exactly what that probe is built to reject.
# A fixture that cannot survive the real checks does not test the real code, so
# the fixtures are now real images. `plant_broken_image` below is the deliberate
# counter-fixture for the undecodable case.
# NOTE `png:exclude-chunks=date,time`. ImageMagick stamps `date:create` and
# `date:modify` at ONE-SECOND granularity, so two renders of the same text
# straddling a second boundary differ in bytes. Tests that need two IDENTICAL
# frames from two separate renders then fail intermittently: that is what the
# unexplained 128/1 run was -- K8n, ~2 in 40 unshimmed, reproducible on demand
# with a `sleep 1; exec magick` PATH shim (fable, B round 8). The fake virsh
# already excluded these chunks; the planters did not.
plant_image() {
    local rel=$1; shift
    local txt="" w
    mkdir -p "$(dirname "$ADIR/$rel")"
    for w in "$@"; do txt="$txt$w "; done
    [ -n "$txt" ] || txt="frame"
    if command -v magick >/dev/null 2>&1 \
       && magick -size 320x80 xc:white -pointsize 24 -fill black \
            -annotate +10+40 "$txt" \
            -define png:exclude-chunks=date,time "$ADIR/$rel" 2>/dev/null; then
        # Append the TEXT: markers the stub OCR backend reads, AFTER the PNG's
        # IEND chunk. A decoder stops at IEND and still reports the real
        # dimensions (verified), so the file is simultaneously a valid image for
        # the decodability probe and a scriptable fixture for the stub. The
        # leading newline matters: without it the first marker is glued to the
        # trailing binary byte and `sed` never matches it.
        { printf '\n'; for w in "$@"; do printf 'TEXT:%s\n' "$w"; done; } >> "$ADIR/$rel"
        return 0
    fi
    # No ImageMagick: emit a minimal valid 1x1 PNG so the frame is still
    # decodable. Distinctness by digest is preserved via a trailing comment
    # chunk carrying the words.
    printf '\211PNG\r\n\032\n\0\0\0\rIHDR\0\0\0\1\0\0\0\1\10\6\0\0\0\37\25\304\211\0\0\0\012IDATx\234c\370\17\0\1\1\1\0\30\335\215\260\0\0\0\0IEND\256B`\202' \
        > "$ADIR/$rel"
    { printf '\n'; for w in "$@"; do printf 'TEXT:%s\n' "$w"; done; } >> "$ADIR/$rel"
}

# plant_image, but at an ABSOLUTE path (for captures taken outside $ADIR).
plant_image_at() {
    local dest=$1; shift
    local txt="" w
    mkdir -p "$(dirname "$dest")"
    for w in "$@"; do txt="$txt$w "; done
    [ -n "$txt" ] || txt="frame"
    if command -v magick >/dev/null 2>&1 \
       && magick -size 320x80 xc:white -pointsize 24 -fill black \
            -annotate +10+40 "$txt" \
            -define png:exclude-chunks=date,time "$dest" 2>/dev/null; then
        # Append the TEXT: markers the stub OCR backend reads, AFTER the PNG's
        # IEND chunk. A decoder stops at IEND and still reports the real
        # dimensions (verified), so the file is simultaneously a valid image for
        # the decodability probe and a scriptable fixture for the stub. The
        # leading newline matters: without it the first marker is glued to the
        # trailing binary byte and `sed` never matches it.
        { printf '\n'; for w in "$@"; do printf 'TEXT:%s\n' "$w"; done; } >> "$dest"
        return 0
    fi
    printf '\211PNG\r\n\032\n\0\0\0\rIHDR\0\0\0\1\0\0\0\1\10\6\0\0\0\37\25\304\211\0\0\0\012IDATx\234c\370\17\0\1\1\1\0\30\335\215\260\0\0\0\0IEND\256B`\202' > "$dest"
    { printf '\n'; for w in "$@"; do printf 'TEXT:%s\n' "$w"; done; } >> "$dest"
}

# The deliberate undecodable fixture: bytes that are not an image at all.
plant_broken_image() {
    local rel=$1
    mkdir -p "$(dirname "$ADIR/$rel")"
    printf 'not an image' > "$ADIR/$rel"
}

# A real frame: the image AND the harness capture row that attests it.
write_frame() {
    local rel=$1; shift
    plant_image "$rel" "$@"
    attest_row "$ADIR/$rel"
}
write_status() {
    printf '%s\n' "$1" > "$ADIR/status.txt"
    [ -n "${2:-}" ] && touch -d "$2" "$ADIR/status.txt"
    return 0
}

# --- the scenario DECLARATION (no content fallback any more) ------------------

@test "visual-mode: an explicit required marker is honoured" {
    run gui_scenario_visual_mode "$VISUAL_MD"
    [ "$status" -eq 0 ]
    [ "$output" = required ]
}

@test "visual-mode: an explicit none marker is honoured" {
    run gui_scenario_visual_mode "$PLAIN_MD"
    [ "$output" = none ]
}

@test "visual-mode: the value is case-insensitive and spacing-tolerant" {
    printf '<!--qci:visual=REQUIRED-->\n' > "$TDIR/x.md"
    run gui_scenario_visual_mode "$TDIR/x.md"
    [ "$output" = required ]
    printf '<!--   qci:visual   :   None   -->\n' > "$TDIR/y.md"
    run gui_scenario_visual_mode "$TDIR/y.md"
    [ "$output" = none ]
}

# The round-2 fallback grepped the scenario text for `screenshot`/`.png`. It was
# case-sensitive, blind to any other phrasing ("compare the rendered frame") or
# capture helper, and it classified such a scenario `none` - i.e. EXEMPT. There
# is no fallback now.
@test "visual-mode: an UNDECLARED scenario is invalid, not silently exempt" {
    run gui_scenario_visual_mode "$UNDECLARED_MD"
    [[ "$output" == invalid:* ]]
    [[ "$output" == *"no <!-- qci:visual: required|none --> declaration"* ]]
}

@test "visual-mode: a pixel assertion phrased without the magic words is still invalid, never none" {
    printf '# 10\nCompare the rendered frame against the reference.\n' > "$TDIR/z.md"
    run gui_scenario_visual_mode "$TDIR/z.md"
    [[ "$output" == invalid:* ]]
    [ "$output" != none ]
}

@test "visual-mode: conflicting markers fail CLOSED (round 2 failed open to none)" {
    printf '<!-- qci:visual: required -->\n<!-- qci:visual: none -->\n' > "$TDIR/c.md"
    run gui_scenario_visual_mode "$TDIR/c.md"
    [[ "$output" == invalid:* ]]
    [[ "$output" == *conflicting* ]]
}

@test "visual-mode: an unknown declaration value is rejected, not guessed" {
    printf '<!-- qci:visual: maybe -->\n' > "$TDIR/u.md"
    run gui_scenario_visual_mode "$TDIR/u.md"
    [[ "$output" == invalid:* ]]
    [[ "$output" == *"unknown qci:visual value maybe"* ]]
}

@test "visual-mode: a missing scenario file is invalid (it used to report none)" {
    run gui_scenario_visual_mode "$TDIR/does-not-exist.md"
    [[ "$output" == invalid:* ]]
}

# --- the declaration is part of registry validation --------------------------

@test "registry: an undeclared scenario is rejected before any golden/VM/agent work" {
    QCI_MARKERS="$TDIR/markers"; : > "$QCI_MARKERS"
    record_blocked() { printf 'blocked:%s:%s\n' "$2" "$5" >> "$QCI_MARKERS"; }
    agent_scenarios() { printf '%s\n' "$UNDECLARED_MD"; }

    run gui_validate_scenarios
    [ "$status" -eq "$EXIT_USAGE" ]
    grep -q "blocked:$UNDECLARED_MD:" "$QCI_MARKERS"
    grep -q 'qci:visual' "$QCI_MARKERS"
}

@test "registry: conflicting declarations are rejected too" {
    QCI_MARKERS="$TDIR/markers"; : > "$QCI_MARKERS"
    record_blocked() { printf 'blocked:%s:%s\n' "$2" "$5" >> "$QCI_MARKERS"; }
    printf '# c\n<!-- qci:visual: required -->\n<!-- qci:visual: none -->\n' > "$TDIR/c.md"
    agent_scenarios() { printf '%s\n' "$TDIR/c.md"; }

    run gui_validate_scenarios
    [ "$status" -eq "$EXIT_USAGE" ]
    grep -q conflicting "$QCI_MARKERS"
}

@test "registry: a properly declared scenario passes validation" {
    QCI_MARKERS="$TDIR/markers"; : > "$QCI_MARKERS"
    record_blocked() { printf 'blocked:%s:%s\n' "$2" "$5" >> "$QCI_MARKERS"; }
    agent_scenarios() { printf '%s\n%s\n' "$VISUAL_MD" "$PLAIN_MD"; }

    run gui_validate_scenarios
    [ "$status" -eq 0 ]
    [ ! -s "$QCI_MARKERS" ]
}

@test "registry: every shipped GUI scenario carries a valid declaration" {
    local f n=0
    for f in "$REPO_ROOT"/tests/integration/permissions-gui/[0-9][0-9]-*.md \
             "$REPO_ROOT"/tests/integration/qdwin-noctalia/[0-9][0-9]-*.md \
             "$REPO_ROOT"/../qdwin/tests/gui/[0-9][0-9]-*.md \
             "$REPO_ROOT"/../qdwin/tests/apps/[0-9][0-9]-*.md \
             "$REPO_ROOT"/../qdlocker/tests/gui/[0-9][0-9]-*.md; do
        [ -f "$f" ] || continue
        n=$((n + 1))
        local m; m=$(gui_scenario_visual_mode "$f")
        [ "$m" = required ] || [ "$m" = none ] || {
            echo "undeclared/invalid: $f -> $m"; return 1; }
    done
    [ "$n" -ge 60 ]
}

# --- the observer backend ----------------------------------------------------

@test "ocr probe: a binary that is not really tesseract is rejected" {
    # Reproduces this host exactly: a `tesseract` on PATH provided by an
    # unrelated game. `command -v` alone would call the backend present.
    cat > "$TDIR/bin/faketess" <<'EOF'
#!/bin/sh
echo "tesseract-game 1.0 (a puzzle game)"
EOF
    chmod +x "$TDIR/bin/faketess"
    QCI_OCR_BIN=faketess run gui_ocr_backend_probe
    [ "$status" -ne 0 ]
    [ -z "$output" ]
}

@test "ocr probe: a real tesseract banner is accepted and its version reported" {
    run gui_ocr_backend_probe
    [ "$status" -eq 0 ]
    [ "$output" = "tesseract 5.3.4" ]
}

@test "ocr probe: an absent binary is reported as an absent backend" {
    QCI_OCR_BIN=definitely-not-installed-qci run gui_ocr_backend_probe
    [ "$status" -ne 0 ]
    run gui_visual_backend_observation ""
    [[ "$output" == *"text corroboration ABSENT"* ]]
    [[ "$output" == *"zypper -n install tesseract-ocr"* ]]
    # The observation must NOT claim the absence stops scenarios grading: OCR
    # cannot adjudicate colour/geometry/absence, so it is never the gate.
    [[ "$output" == *"Visual scenarios still grade"* ]]
}

@test "preflight: the present-backend line says the GATE runs the OCR" {
    run gui_visual_backend_observation "tesseract 5.3.4"
    [[ "$output" == *"run BY THE GATE"* ]]
}

# --- per-frame linkage -------------------------------------------------------

@test "frames: only images under THIS scenario's artifact dir are enumerated" {
    mkdir -p "$TDIR/neighbour"
    write_frame s1.png Approve
    write_frame sub/s2.PNG Deny
    printf 'TEXT:Approve\n' > "$TDIR/neighbour/other.png"
    run gui_visual_frames "$ADIR"
    [[ "$output" == *"$ADIR/s1.png"* ]]
    [[ "$output" == *"$ADIR/sub/s2.PNG"* ]]
    [[ "$output" != *neighbour* ]]
}

@test "frames: a symlink pointing outside the artifact dir is not a frame" {
    printf 'TEXT:Approve\n' > "$TDIR/elsewhere.png"
    ln -s "$TDIR/elsewhere.png" "$ADIR/s1.png"
    run gui_visual_frames "$ADIR"
    [ -z "$output" ]
}

@test "frames: the harness-owned evidence dir is never re-OCR'd as input" {
    write_frame s1.png Approve
    mkdir -p "$ADIR/visual-evidence"
    : > "$ADIR/visual-evidence/frame-001.png"
    run gui_visual_frames "$ADIR"
    [[ "$output" != *visual-evidence* ]]
}

# --- the harness's own OCR pass ---------------------------------------------

@test "harness ocr: the gate produces a manifest with a sha256 per frame" {
    write_frame s1.png Approve Deny
    write_frame s2.png
    run gui_harness_ocr_frames "$ADIR" "$ADIR/visual-evidence"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^frames=2\ ocr_ok=2\ ocr_fail=0\ text_frames=1\ words=[1-9][0-9]*\ undecodable=0\ dec_unknown=0$ ]]
    [ -f "$ADIR/visual-evidence/manifest.tsv" ]
    grep -q '^sha256	bytes	frame	decodable	tsv	ocr_rc	words$' "$ADIR/visual-evidence/manifest.tsv"
    # every frame is recorded as decodable, by name not by position
    [ "$(awk -F'	' 'NR>1 && $4=="yes"' "$ADIR/visual-evidence/manifest.tsv" | wc -l)" -eq 2 ]
    # the recorded digest is the real digest of the real frame
    local want; want=$(sha256sum "$ADIR/s1.png" | awk '{print $1}')
    grep -q "^$want	" "$ADIR/visual-evidence/manifest.tsv"
    [ "$(grep -c . "$ADIR/visual-evidence/manifest.tsv")" -eq 3 ]
}

@test "harness ocr: word counting ignores tesseract's empty structural rows" {
    write_frame blank.png
    run gui_harness_ocr_frames "$ADIR" "$ADIR/visual-evidence"
    [[ "$output" == *"text_frames=0 words=0"* ]]
    # ... even though the TSV really does carry a data row
    [ "$(grep -c . "$ADIR"/visual-evidence/frame-001-*.tsv)" -eq 2 ]
}

# --- THE BYPASSES THE REVIEW REPRODUCED -------------------------------------

# Verbatim from the round-3 review: "I reproduced this exact case: a JSON line
# {"message":"view_image /tmp/unrelated.png"} written after status.txt made
# gui_apply_visual_evidence_contract PASS return PASS."
@test "BYPASS(reproduced): a forged view_image JSON line after status.txt no longer passes" {
    write_status PASS '2026-09-15 10:00:00'
    cat > "$TDIR/agent.log" <<'EOF'
{"message":"view_image /tmp/unrelated.png"}
EOF
    touch -d '2026-09-15 10:00:30' "$TDIR/agent.log"
    # called both with and without a log argument: the contract no longer takes one
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took NO capture"* ]]
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "BYPASS: the agent-log evidence path is GONE, not merely narrowed" {
    # A well-formed codex --json image-open event is no longer evidence either;
    # leaving a second accepted form alive would leave a second thing to forge.
    # Explicit refutation, not a bare `!`: a leading `!` neither trips errexit
    # nor fails a Bats test mid-body, so it would silently assert nothing here.
    if declare -f gui_visual_json_evidence >/dev/null; then
        echo "gui_visual_json_evidence still exists - the forgeable log path is alive"; return 1
    fi
    if declare -f gui_visual_ocr_evidence >/dev/null; then
        echo "gui_visual_ocr_evidence still exists - the agent-placed TSV path is alive"; return 1
    fi
    write_status PASS
    cat > "$TDIR/agent.log" <<'EOF'
{"type":"item.completed","item":{"type":"function_call","name":"view_image","arguments":{"path":"/tmp/qci-gui-art/abc/s1.png"}}}
EOF
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "BYPASS: a hand-written tesseract TSV in the artifact dir is not evidence" {
    # Round 2 accepted exactly this: valid header + one invented row, mtime set
    # before status.txt. It costs the agent one printf.
    write_status PASS '2026-09-15 10:00:05'
    { printf '%s\n' "$OCR_HEADER"
      printf '5\t1\t1\t1\t1\t1\t10\t20\t60\t14\t93.5\tApprove\n'; } > "$ADIR/s1-ocr.tsv"
    touch -d '2026-09-15 10:00:01' "$ADIR/s1-ocr.tsv"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took NO capture"* ]]
}

@test "BYPASS: a pre-planted visual-evidence/ directory is moved aside, never read" {
    write_status PASS
    mkdir -p "$ADIR/visual-evidence"
    printf 'sha256\tbytes\tframe\ttsv\tocr_rc\twords\n' > "$ADIR/visual-evidence/manifest.tsv"
    printf 'deadbeef\t100\tforged.png\tforged.tsv\t0\t99\n' >> "$ADIR/visual-evidence/manifest.tsv"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [ -f "$ADIR/visual-evidence.agent-supplied/manifest.tsv" ]
    if grep -q deadbeef "$ADIR/visual-evidence/manifest.tsv"; then
        echo "the planted manifest was read back as harness evidence"; return 1
    fi
}

@test "BYPASS: vm-gui's click-preview ledger is not a frame" {
    write_status PASS
    mkdir -p "$ADIR/click-targets"
    { printf 'timestamp_utc\tpreview_id\tphase\tvm\tx\ty\tlabel\n'
      printf '2026-09-15T10:00:00Z\tclick-001\tpreview\tvm\t490\t337\t1 hour\n'; } \
        > "$ADIR/click-targets/clicks.tsv"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

# --- the contract itself -----------------------------------------------------

@test "contract: visual scenario + a readable frame -> the verdict is accepted" {
    write_status PASS
    write_frame step2.png Approve Deny Later
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "$status" -eq 0 ]
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 1 frame(s), 1 distinct"* ]]
    # The exact word count is a tesseract-version/rendering detail; what this
    # test is about is that the gate read text out of the frame at all.
    [[ "$output" =~ read\ text\ in\ 1\ frame\(s\)\ \([1-9][0-9]*\ words ]]
    # the note must NOT over-claim what OCR can establish
    [[ "$output" == *"colour, layout, focus, z-order and absence claims are NOT adjudicated"* ]]
}

@test "contract: ORDERING is irrelevant - a frame newer than status.txt still counts" {
    # Round 2 rejected evidence whose mtime was after status.txt, while the same
    # prompt told every agent to write status.txt first. That contradiction
    # turned fully observed runs into ERROR. The harness reads the frames after
    # the agent has exited, so order cannot matter.
    write_status PASS '2026-09-15 10:00:00'
    write_frame s1.png Approve
    touch -d '2026-09-15 10:05:00' "$ADIR/s1.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "contract: an invented FAIL is rejected the same way as an invented PASS" {
    write_status FAIL
    run apply_contract FAIL "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "contract: no frames at all -> ERROR naming what was missing" {
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took NO capture"* ]]
    [[ "$output" == *"the HARNESS could not read"* ]]
    [[ "$output" == *PASS* ]]
}

# "the harness ran OCR and got nothing" must be distinguishable from "no OCR
# happened". A legitimately blank pane is an observation; round 2 rejected it
# (header-only TSV) and manufactured a false ERROR.
@test "contract: a genuinely textless frame is an OBSERVATION, not a missing one" {
    write_status PASS
    write_frame blank.png
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"read NO text in any of them"* ]]
    [[ "$output" == *"The frames are attested for this VM"* ]]
    [[ "$output" == *"rests on something OCR cannot read"* ]]
}

@test "contract: a backend that fails on every frame does not void an attested verdict" {
    # OCR reads TEXT only. A frame set this gate captured, sealed and harvested
    # is evidence whether or not tesseract could read words in it, so a broken
    # backend degrades the text column and nothing else. Making it ERROR would
    # void verdicts over a tool that could not have adjudicated them anyway.
    write_status PASS
    write_frame s1.png Approve
    QCI_OCR_BIN=brokentess run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"read NO text in any of them"* ]]
}

@test "contract: no OCR backend still grades, and says the text column is skipped" {
    write_status PASS
    write_frame s1.png Approve
    QCI_OCR_BIN=definitely-not-installed-qci run apply_contract \
        PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"no OCR backend on this host"* ]]
    [[ "$output" == *"text column is \`skip\`"* ]]
    # What IS load-bearing stays load-bearing: an absent ledger is still ERROR
    # with no backend present, so this is a demotion of OCR, not of attestation.
    rm -f "$CAPLOG"
    QCI_OCR_BIN=definitely-not-installed-qci run apply_contract \
        PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "contract: an UNDECLARED scenario's PASS is not graded" {
    write_status PASS
    write_frame s1.png Approve
    run apply_contract PASS "$UNDECLARED_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"must declare"* ]]
}

@test "contract: a qci:visual=none scenario is unaffected" {
    write_status PASS
    run apply_contract PASS "$PLAIN_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [ -z "${output#*$'\t'}" ]
    run apply_contract FAIL "$PLAIN_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = FAIL ]
    # ... and the gate did not run OCR for it
    [ ! -d "$ADIR/visual-evidence" ]
}

@test "contract: SKIP and UNKNOWN are passed through untouched" {
    write_status SKIP
    run apply_contract SKIP "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = SKIP ]
    run apply_contract UNKNOWN "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = UNKNOWN ]
}

@test "contract: there is no env bypass that can re-accept an unevidenced PASS" {
    write_status PASS
    for v in QCI_VISUAL_EVIDENCE QCI_SKIP_VISUAL_EVIDENCE QCI_ALLOW_UNVERIFIED_VISUAL \
             QCI_GUI_NO_VISUAL_CONTRACT QCI_ALLOW_TEST_EDITS; do
        eval "export $v=0"
        run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
        [ "${output%%$'\t'*}" = ERROR ]
        eval "export $v=1"
        run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
        [ "${output%%$'\t'*}" = ERROR ]
        eval "unset $v"
    done
}

# ===========================================================================
# ROUND 5 — the bypass the round-4 review spelled out, and the three holes
# behind it.
# ===========================================================================

# Verbatim from the round-4 review:
#   "the agent writes visual-evidence/frame.png containing a blank or unrelated
#    valid image, writes PASS, and leaves no asserted VM capture. The gate
#    renames that directory to visual-evidence.agent-supplied; the frame finder
#    does not exclude the renamed path; Tesseract succeeds with zero words; the
#    contract returns ok."
@test "BYPASS(round-4, exact): blank image planted in visual-evidence/ + PASS + no VM capture -> ERROR" {
    write_status PASS
    mkdir -p "$ADIR/visual-evidence"
    # a VALID image the OCR backend reads successfully and finds no words in
    printf '\211PNG\r\n\032\n' > "$ADIR/visual-evidence/frame.png"
    # no attest_row anywhere: the harness never captured anything
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took NO capture"* ]]
    # and the planted bytes must never have been an OCR input, even after the
    # quarantine rename that round 3 left uncovered
    run gui_visual_frames "$ADIR"
    [[ "$output" != *agent-supplied* ]]
    [[ "$output" != *frame.png* ]]
}

@test "(A) the quarantined agent-supplied tree is excluded from the frame set" {
    # The round-3 exclusion was `-not -path \$adir/visual-evidence/*`, so the
    # rename defeated it in one step. Exclusion is now by directory name prefix.
    mkdir -p "$ADIR/visual-evidence.agent-supplied" "$ADIR/sub/visual-evidence"
    : > "$ADIR/visual-evidence.agent-supplied/planted.png"
    : > "$ADIR/sub/visual-evidence/nested.png"
    plant_image real.png Approve
    run gui_visual_frames "$ADIR"
    [[ "$output" == *"$ADIR/real.png"* ]]
    [[ "$output" != *planted.png* ]]
    [[ "$output" != *nested.png* ]]
}

@test "(A) quarantine + exclusion together: a planted frame cannot become evidence on the next pass" {
    write_status PASS
    mkdir -p "$ADIR/visual-evidence"
    printf '\211PNG\r\n\032\nTEXT:Approve\n' > "$ADIR/visual-evidence/frame.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [ -f "$ADIR/visual-evidence.agent-supplied/frame.png" ]
    # second pass over the same tree: still not evidence
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [ ! -f "$ADIR/visual-evidence/manifest.tsv" ] || \
        grep -qv frame.png "$ADIR/visual-evidence/manifest.tsv"
}

# --- (B) zero-word OCR is no longer what makes a frame evidence -------------

@test "(B) an UNATTESTED blank image is not evidence, however well OCR runs on it" {
    write_status PASS
    plant_image blank.png          # valid image, zero words, zero captures
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took NO capture"* ]]
    [[ "$output" == *"agent-authored"* ]]
}

@test "(B) an ATTESTED blank frame IS evidence — the empty-pane case is not a false ERROR" {
    write_status PASS
    write_frame blank.png
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"read NO text in any of them"* ]]
}

@test "(B) planting extra unattested images alongside one real capture cannot inflate the evidence" {
    write_status PASS
    write_frame real.png Approve
    plant_image fake1.png Deny
    plant_image fake2.png Deny
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    # exactly ONE frame is graded, and it is the attested one
    [[ "$output" == *"harness-captured 1 frame(s), 1 distinct"* ]]
    [ "$(grep -c . "$ADIR/visual-evidence/manifest.tsv")" -eq 2 ]
    if grep -q 'fake' "$ADIR/visual-evidence/manifest.tsv"; then
        echo "an unattested planted image reached the OCR manifest"; return 1
    fi
}

# --- (C) frame omission — "cheaper than image forgery" ----------------------

@test "(C) deleting a captured in-tree frame is DETECTED, not invisible" {
    write_status PASS
    write_frame good.png Approve
    write_frame damning.png Error Denied
    rm -f "$ADIR/damning.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no longer there"* ]]
    [[ "$output" == *"frame omission"* ]]
}

@test "(C) deleting an INTERIOR capture row breaks the hash chain" {
    write_status PASS
    write_frame first.png One
    write_frame damning.png Error Denied
    write_frame last.png Three
    rm -f "$ADIR/damning.png"
    # the agent also edits the ledger the prompt never mentions
    sed -i '/damning/d' "$CAPLOG"
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -eq 1 ]
    [[ "$output" == bad:* ]]
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"capture ledger"* || "$output" == *"out of sequence"* ]]
}

@test "(C) reordering capture rows breaks the hash chain" {
    write_status PASS
    write_frame a.png One
    write_frame b.png Two
    { head -2 "$CAPLOG"; tail -1 "$CAPLOG"; sed -n '3p' "$CAPLOG"; } > "$CAPLOG.x"
    mv "$CAPLOG.x" "$CAPLOG"
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -eq 1 ]
}

# TAIL TRUNCATION. Round 6 called this a real hole, not a bookkeeping residual,
# and asked for it to be closed. It is closed for every moment the gate can
# reach: once the agent has exited, the gate SEALS the ledger with a nonce it
# generated in its own process and keeps the row count and chain head in shell
# variables only. Reproducing that head after dropping a row would need a
# sha256 preimage.
#
# What remains open is named precisely in the contract block and pinned by the
# next test: an agent deleting its OWN last row before it exits is the same act
# as never taking that screenshot, and no ledger can tell those apart. The
# scenario-declared capture count is the mechanism for that, and it is pinned
# below too.
@test "(C) truncating the LAST capture row AFTER the gate sealed the ledger is DETECTED" {
    write_status PASS
    write_frame good.png Approve
    write_frame damning.png Error Denied
    local anchor
    anchor=$(seal_anchor)          # <- the agent has exited; the gate freezes it
    rm -f "$ADIR/damning.png"
    sed -i '$d' "$CAPLOG"          # drops the seal
    sed -i '$d' "$CAPLOG"          # drops the damning capture row
    run gui_capture_log_verify "$CAPLOG" testvm "${anchor#*$'\t'}"
    [ "$status" -eq 1 ]
    run gui_apply_visual_evidence_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG" "$anchor"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "(C) removing only the gate's SEAL row is DETECTED" {
    write_status PASS
    write_frame good.png Approve
    local anchor
    anchor=$(seal_anchor)
    sed -i '$d' "$CAPLOG"
    run gui_apply_visual_evidence_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG" "$anchor"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"after the agent exited"* ]]
}

@test "(C) rebuilding the WHOLE ledger from its own seed is DETECTED by the seal" {
    # The agent can read the seed, so it can re-derive a perfectly consistent
    # chain. It cannot re-derive the head the gate holds in memory.
    write_status PASS
    write_frame good.png Approve
    write_frame damning.png Error Denied
    local anchor seed
    anchor=$(seal_anchor)
    seed=$(head -1 "$CAPLOG" | awk -F'\t' '{print $3}')
    { head -2 "$CAPLOG"; } > "$CAPLOG.x"
    mv "$CAPLOG.x" "$CAPLOG"
    rm -f "$ADIR/damning.png"
    attest_row "$ADIR/good.png"
    run gui_apply_visual_evidence_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG" "$anchor"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "(C) an UNSEALED ledger is not graded at all (fail closed)" {
    write_status PASS
    write_frame good.png Approve
    run gui_apply_visual_evidence_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG" ""
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"could not SEAL"* ]]
}

# The honest residual, stated: truncation BEFORE the agent exits is
# indistinguishable from never capturing, and only the scenario's own declared
# capture count can close it.
@test "(C) RESIDUAL: a row dropped BEFORE the gate seals is indistinguishable from never capturing" {
    write_status PASS
    write_frame good.png Approve
    write_frame damning.png Error Denied
    rm -f "$ADIR/damning.png"
    sed -i '$d' "$CAPLOG"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "(C) a scenario that DECLARES its capture count turns that residual into ERROR" {
    printf '# 11\n<!-- qci:visual: required -->\n<!-- qci:visual-captures: 2 -->\n' > "$TDIR/two.md"
    run gui_scenario_min_captures "$TDIR/two.md"
    [ "$output" = 2 ]
    write_status PASS
    write_frame good.png Approve
    write_frame damning.png Error Denied
    rm -f "$ADIR/damning.png"
    sed -i '$d' "$CAPLOG"
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"visual-captures: 2"* ]]
    [[ "$output" == *"only 1 capture"* ]]
}

@test "(C) an undeclared scenario floors at one capture" {
    run gui_scenario_min_captures "$VISUAL_MD"
    [ "$output" = 1 ]
    run gui_scenario_min_captures "$TDIR/does-not-exist.md"
    [ "$output" = 1 ]
}

@test "(C) an overwritten capture (same path, different bytes) is omission too" {
    write_status PASS
    write_frame s1.png Error Denied
    printf '\211PNG\r\n\032\nTEXT:Approve\n' > "$ADIR/s1.png"   # substituted
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no longer there"* ]]
}

@test "(C) a capture written OUTSIDE the artifact tree is reported, not punished" {
    write_status PASS
    write_frame s1.png Approve
    # vm-gui's default destination is /tmp/vm-screenshot.png: a real capture that
    # was never meant to be harvested. Its absence must not manufacture an ERROR.
    printf '\211PNG\r\n\032\nTEXT:Scratch\n' > "$TDIR/vm-screenshot.png"
    attest_row "$TDIR/vm-screenshot.png" ""
    rm -f "$TDIR/vm-screenshot.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"1 captured outside the artifact dir and not graded"* ]]
}

@test "(C) capture-then-copy still counts: attestation matches by DIGEST, not path" {
    write_status PASS
    plant_image_at "$TDIR/scratch.png" Approve
    attest_row "$TDIR/scratch.png" ""
    cp "$TDIR/scratch.png" "$ADIR/s1.png"
    rm -f "$TDIR/scratch.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 1 frame(s)"* ]]
}

# --- the ledger itself ------------------------------------------------------

@test "capture log: a fresh log has a seeded, VM-BOUND header and no rows" {
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -eq 0 ]
    [ "$output" = "rows=0 in_tree=0 out_tree=0 rejected=0" ]
    head -1 "$CAPLOG" | grep -q '^#qci-capture-log	2	[0-9a-f]\{64\}	testvm$'
}

@test "capture log: a ledger cannot be provisioned without a bound VM" {
    # Losing the binding by omission would silently restore the cross-VM route,
    # so the gate refuses to create an unbound ledger at all.
    run gui_capture_log_init "$TDIR/unbound.tsv" ""
    [ "$status" -ne 0 ]
    [ ! -f "$TDIR/unbound.tsv" ]
}

@test "capture tools: every lane's capture tool sources the shared attestation library" {
    # vm-gui (virsh, labwc/admin lane): the library TAKES the screenshot.
    grep -q 'lib/capture-attest.sh' "$REPO_ROOT/scripts/vm/vm-gui"
    # The plain screenshot lane reaches the library through the usable-frame
    # gate (round 9), which rejects black/flat/undecodable captures before the
    # agent ever sees them. Assert the chain, not one spelling of it.
    grep -q 'capture_usable_screenshot "\$OUT"' "$REPO_ROOT/scripts/vm/vm-gui"
    # A retry lane captures candidates UNATTESTED and writes the one row when
    # it PUBLISHES the accepted frame; a lane that captures straight to its
    # final path still attests in place.
    grep -q 'capture_virsh_shot "\$VM" "\$candidate"' "$REPO_ROOT/scripts/vm/vm-gui"
    grep -q 'capture_publish_frame "\$src" "\$dst"' "$REPO_ROOT/scripts/vm/vm-gui"
    grep -q 'capture_virsh_screenshot "\$VM" "\$raw"' "$REPO_ROOT/scripts/vm/vm-gui"
    grep -q 'capture_virsh_screenshot "\$VM" "\$post"' "$REPO_ROOT/scripts/vm/vm-gui"
    # qdwin_screenshot (in-guest qdshell capture, qdwin/qdlocker lane). Without
    # this, every qci:visual=required scenario in those repos would be ERROR.
    local qh="$REPO_ROOT/../qdwin/tests/gui/qdwin-helpers.sh"
    if [ -r "$qh" ]; then
        grep -q 'lib/capture-attest.sh' "$qh"
        grep -q 'capture_attest_frame "\$out" "\$VMNAME"' "$qh"
    fi
    # qdwin_apps_screenshot (virsh, qdwin apps lane) -- the third capture tool.
    local ah="$REPO_ROOT/../qdwin/tests/apps/qdwin-apps-helpers.sh"
    if [ -r "$ah" ]; then
        grep -q 'lib/capture-attest.sh' "$ah"
        grep -q 'capture_virsh_screenshot "\$VMNAME" "\$out"' "$ah"
    fi
}

# Round 6 caught the earlier version of this test as inadequate: it only looked
# for a `vm-gui` case arm while the SAME suite demonstrated a generic
# `capture_attest <file>` entry point under the name attest_via_env. The
# property that actually matters is not "no subcommand" but "no function
# anywhere blesses bytes the library did not itself capture, and the one
# hand-over helper that must exist for the in-guest lane is bound to the
# assigned VM". That is what this pins.
@test "capture tools: the GENERIC 'attest any file' entry point is gone" {
    # No command-line route ...
    if grep -qE '^[[:space:]]*attest\)' "$REPO_ROOT/scripts/vm/vm-gui"; then
        echo "vm-gui grew an 'attest' subcommand - that IS the bypass"; return 1
    fi
    # ... and no ungated library function under the old name, in the library or
    # in any capture tool that sources it.
    if grep -qE '^[[:space:]]*capture_attest\(\)' "$CAPLIB"; then
        echo "the ungated capture_attest() primitive is back"; return 1
    fi
    local f
    for f in "$REPO_ROOT/scripts/vm/vm-gui" \
             "$REPO_ROOT/../qdwin/tests/gui/qdwin-helpers.sh" \
             "$REPO_ROOT/../qdwin/tests/apps/qdwin-apps-helpers.sh"; do
        [ -r "$f" ] || continue
        if grep -qE 'capture_attest[[:space:]]+"' "$f"; then
            echo "$f still calls the ungated capture_attest"; return 1
        fi
    done
    # The old name is not merely unused: it does not exist, so a scenario that
    # calls it gets an error and NO row.
    run bash -c '. "$1"; declare -f capture_attest >/dev/null' _ "$CAPLIB"
    [ "$status" -ne 0 ]
}

@test "FORGERY(round 6): calling the removed capture_attest on an uncaptured image ERRORs" {
    # The exact round-6 CRITICAL: the agent writes a blank PNG under the
    # artifact dir, sources the library and asks for a row.
    write_status PASS
    plant_image forged.png Approve
    run env QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="$ADIR" bash -c \
        '. "$1"; capture_attest "$2" testvm 2>/dev/null; echo "rc=$?"' \
        _ "$CAPLIB" "$ADIR/forged.png"
    [ "$output" = "rc=127" ]   # the primitive does not exist any more
    # no row was written, so the scenario has no evidence at all
    run gui_capture_log_verify "$CAPLOG"
    [ "$output" = "rows=0 in_tree=0 out_tree=0 rejected=0" ]
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took NO capture"* ]]
}

# DOCUMENTED RESIDUAL, pinned so the contract's claim and the code agree.
#
# The in-guest qdwin lane's capture is a guest protocol that cannot live inside
# the library, so ONE hand-over helper (capture_attest_frame) must exist. At the
# same uid it cannot be made unforgeable: the agent can call it, and can equally
# re-implement the published chain format itself. It is GATED on the bound VM,
# and the contract claims exactly that and no more.
@test "RESIDUAL: the gated hand-over helper still accepts bytes it did not capture" {
    write_status PASS
    plant_image forged.png Approve
    attest_frame "$ADIR/forged.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "VM BINDING: the hand-over helper REFUSES a capture of another worker's VM" {
    plant_image other.png Approve
    run attest_frame "$ADIR/other.png" "$ADIR" other-worker-vm
    [ "$status" -ne 0 ]
    [[ "$output" == *REFUSED* ]]
    [[ "$output" == *testvm* ]]
    run gui_capture_log_verify "$CAPLOG"
    [ "$output" = "rows=0 in_tree=0 out_tree=0 rejected=0" ]
}

@test "VM BINDING: a hand-appended row for another VM is rejected by the verifier" {
    # The producer refuses; this is the belt-and-braces check for a row the
    # agent appended itself with the chain arithmetic done by hand.
    write_status PASS
    write_frame mine.png Approve
    local seed prev payload chain
    prev=$(tail -1 "$CAPLOG" | awk -F'\t' '{print $8}')
    plant_image neighbour.png Approve
    payload=$(printf '2\t2026-09-15T10:00:00Z\tother-worker-vm\tin-tree\t10\t%s\t%s' \
        "$(sha256sum "$ADIR/neighbour.png" | awk '{print $1}')" "$ADIR/neighbour.png")
    chain=$(printf '%s\t%s' "$prev" "$payload" | sha256sum | awk '{print $1}')
    printf '%s\t%s\n' "$payload" "$chain" >> "$CAPLOG"
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -eq 1 ]
    [[ "$output" == *"records VM other-worker-vm"* ]]
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "VM BINDING: a ledger bound to a VM this scenario was not assigned is rejected" {
    write_status PASS
    write_frame s1.png Approve
    local anchor
    anchor=$(seal_anchor)
    # the gate knows, from its OWN memory, which VM it assigned
    run gui_capture_log_verify "$CAPLOG" someone-elses-vm
    [ "$status" -eq 1 ]
    [[ "$output" == *"bound to VM testvm"* ]]
    run gui_apply_visual_evidence_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG" \
        "someone-elses-vm"$'\t'"${anchor#*$'\t'}"
    [ "${output%%$'\t'*}" = ERROR ]
}

# The seal is only a boundary if the GATE actually performs it. This pins the
# wiring, because a future edit that drops the seal would silently reopen tail
# truncation while every unit test above still passed.
@test "wiring: gui_run_scenario binds, seals, and passes the anchor on both attempt paths" {
    local g="$REPO_ROOT/ci/lib/gates/gui.sh"
    # the ledger is bound to the attempt's VM (first attempt and retry)
    grep -q 'gui_capture_log_init "$caplog" "$vm"' "$g"
    grep -q 'gui_capture_log_init "$caplogN" "$vmN"' "$g"
    # the ledger is sealed after harvest, before grading
    grep -q 'capanchor=$(gui_capture_log_seal "$caplog" "$vm")' "$g"
    grep -q 'capanchorN=$(gui_capture_log_seal "$caplogN" "$caplogN_vm")' "$g"
    # and the anchor reaches the contract
    grep -q '"$status" "$scenario" "$adir" "$caplog" "$capanchor"' "$g"
    grep -q '"$statusN" "$scenario" "$adirN" "$caplogN" "$capanchorN"' "$g"
}

@test "capture log: the gated hand-over entry point writes accepted rows" {
    plant_image e.png Hello
    attest_frame "$ADIR/e.png"
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -eq 0 ]
    [ "$output" = "rows=1 in_tree=1 out_tree=0 rejected=0" ]
}

@test "capture log: the real producer writes rows this verifier accepts" {
    write_frame a.png One
    write_frame b.png Two
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -eq 0 ]
    [ "$output" = "rows=2 in_tree=2 out_tree=0 rejected=0" ]
    # the recorded digest is the real digest of the real file
    local want; want=$(sha256sum "$ADIR/a.png" | awk '{print $1}')
    grep -q "	$want	" "$CAPLOG"
}

@test "capture log: an absent log is ERROR, never an exemption" {
    write_status PASS
    plant_image s1.png Approve
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$TDIR/nope.tsv"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"capture log is missing"* ]]
    # ... and so is "no log was provisioned at all"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" ""
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no capture log was provisioned"* ]]
}

@test "capture log: a log the agent rebuilt with its own header is rejected" {
    write_status PASS
    plant_image s1.png Approve
    { printf 'seq\tts_utc\tvm\tscope\tbytes\tsha256\tpath\tchain\n'
      printf '1\t2026-09-15T10:00:00Z\ttestvm\tin-tree\t10\t%s\t%s/s1.png\tdeadbeef\n' \
        "$(sha256sum "$ADIR/s1.png" | awk '{print $1}')" "$ADIR"; } > "$CAPLOG"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"header is missing or was rewritten"* ]]
}

@test "capture log: the gate copies the ledger into its own evidence dir for audit" {
    write_status PASS
    write_frame s1.png Approve
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [ -f "$ADIR/visual-evidence/captures.tsv" ]
    [ -f "$ADIR/visual-evidence/attested-frames.txt" ]
    cmp -s "$CAPLOG" "$ADIR/visual-evidence/captures.tsv"
}

# --- the structural facts the harness computes for NON-TEXT assertions ------

@test "structural: identical captures are REPORTED as 1 distinct, not failed" {
    # qdwin-noctalia/05 "bar stays after idle" asserts the frame does NOT change.
    # A diff oracle that FAILED on identical frames would invent a wrong verdict.
    write_status PASS
    write_frame before.png
    cp "$ADIR/before.png" "$ADIR/after.png"
    attest_row "$ADIR/after.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 2 frame(s), 1 distinct"* ]]
}

@test "structural: frames that really changed are reported as distinct" {
    write_status PASS
    write_frame before.png Approve
    write_frame after.png Approved Revoke
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 2 frame(s), 2 distinct"* ]]
}

# --- B round 1: the guards the reviewers found UNTESTED -----------------------
#
# Two independent reviewers mutated production and found that removing the
# decodability gate entirely failed ZERO of these tests: `plant_broken_image`
# was written as the deliberate counter-fixture and then never called. These
# tests call it, and pin the missing-decoder case that let a 12-byte file
# reading `not an image` carry a graded PASS.

@test "decodable: an absent decoder is 'unknown', never a silent yes" {
    plant_image real.png Approve
    # The function needs only `command -v` and `magick`; pointing PATH at an
    # empty dir removes the decoder without disturbing anything else it uses.
    mkdir -p "$TDIR/empty"
    PATH="$TDIR/empty" run gui_frame_is_decodable "$ADIR/real.png"
    [ "$status" -eq 0 ]
    [ "$output" = unknown ]
}

@test "decodable: real bytes decode and junk bytes do not" {
    if ! command -v magick >/dev/null 2>&1; then skip "no ImageMagick on this host"; fi
    plant_image real.png Approve
    plant_broken_image junk.png
    run gui_frame_is_decodable "$ADIR/real.png"
    [ "$output" = yes ]
    run gui_frame_is_decodable "$ADIR/junk.png"
    [ "$output" = no ]
}

@test "contract: ALL frames undecodable -> ERROR, not a graded verdict" {
    if ! command -v magick >/dev/null 2>&1; then skip "no ImageMagick on this host"; fi
    write_status PASS
    plant_broken_image junk.png
    attest_row "$ADIR/junk.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"UNDECODABLE"* ]]
}

@test "contract: NO DECODER installed -> ERROR naming the missing capability" {
    # The escape astra reproduced in B round 1: `unknown` is not `no`, so the
    # old all-undecodable test could not fire and a sealed, correctly VM-bound
    # ledger over junk bytes kept its PASS. The leaf probe is stubbed to report
    # what it reports on a host with no ImageMagick; everything downstream of it
    # -- the manifest, the counts, and the verdict logic under test -- is the
    # real production code.
    write_status PASS
    plant_broken_image junk.png
    attest_row "$ADIR/junk.png"
    gui_frame_is_decodable() { printf 'unknown\n'; }
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"NO IMAGE DECODER"* ]]
    [[ "$output" == *"never read a pixel"* ]]
}

@test "contract: a decoder-less run of REAL frames is still ERROR, not PASS" {
    # Not a corrupt-capture case: the frames are fine, the gate simply cannot
    # look at them. The verdict consequence is identical and fail-closed.
    write_status PASS
    write_frame step2.png Approve Deny
    gui_frame_is_decodable() { printf 'unknown\n'; }
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"NO IMAGE DECODER"* ]]
}

@test "contract: broken AND unchecked together, none decoded -> ERROR" {
    write_status PASS
    plant_broken_image junk1.png
    plant_broken_image junk2.png
    # Two identical junk files share a digest and reconcile to ONE frame, which
    # would exercise the all-undecodable branch instead of the mixed one.
    printf 'second' >> "$ADIR/junk2.png"
    attest_row "$ADIR/junk1.png"
    attest_row "$ADIR/junk2.png"
    # The probe runs in a command substitution, so the call counter has to live
    # on disk rather than in a shell variable.
    : > "$TDIR/deccalls"
    gui_frame_is_decodable() {
        printf 'x' >> "$TDIR/deccalls"
        if [ "$(wc -c < "$TDIR/deccalls")" -eq 1 ]; then printf 'no\n'; else printf 'unknown\n'; fi
    }
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"NOT ONE of the"* ]]
}

@test "contract: ONE decoded frame among broken ones is enough to grade" {
    # The floor is one POSITIVELY decoded frame -- a flaky capture beside a good
    # one must not turn a real verdict into ERROR.
    if ! command -v magick >/dev/null 2>&1; then skip "no ImageMagick on this host"; fi
    write_status PASS
    write_frame good.png Approve
    plant_broken_image junk.png
    attest_row "$ADIR/junk.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"UNDECODABLE"* ]]
}

# --- B round 1: the image-open diagnostic could never read zero ---------------

@test "image opens: the ECHOED PROMPT is not the driver looking" {
    # The prompt tells the driver to call `view_image`; codex echoes its prompt
    # into the log. Counting the raw log therefore returned >= 1 on every
    # attempt, so the one reading this diagnostic exists for -- a lane-wide drop
    # to zero -- was unreachable (fable, B round 1).
    local prompt="$TDIR/prompt.md" log="$TDIR/agent.log"
    cat > "$prompt" <<'EOP'
Open the capture with your image tool (`view_image` or equivalent).
This is mandatory and OCR is not a substitute.
EOP
    cp "$prompt" "$log"
    printf 'I graded the scenario from the step list.\n' >> "$log"
    run gui_count_image_opens "$log" "$prompt"
    [ "$status" -eq 0 ]
    [ "$output" -eq 0 ]
}

@test "image opens: the driver's OWN mention still counts" {
    local prompt="$TDIR/prompt.md" log="$TDIR/agent.log"
    printf 'Open the capture with view_image.\n' > "$prompt"
    cp "$prompt" "$log"
    printf 'Calling view_image on s1.png; the Approve button is visible.\n' >> "$log"
    run gui_count_image_opens "$log" "$prompt"
    [ "$output" -eq 1 ]
}

@test "image opens: with no prompt given the raw count is unchanged" {
    # The second argument is optional; older callers must keep working.
    local log="$TDIR/agent.log"
    printf 'view_image s1.png\n' > "$log"
    run gui_count_image_opens "$log"
    [ "$output" -eq 1 ]
}

@test "image opens: a missing log is zero, not an error" {
    run gui_count_image_opens "$TDIR/nope.log" "$TDIR/nope.md"
    [ "$status" -eq 0 ]
    [ "$output" -eq 0 ]
}

# --- B round 1: frame omission was undetectable on the REAL capture path ------
#
# vm-gui takes every candidate into a private scratch dir and copies only the
# accepted one into the artifact dir, so EVERY ledger row named a scratch path
# and graded as out-of-tree. Out-of-tree rows whose bytes are absent are
# reported and never punished -- correctly, because a rejected attempt the agent
# tidied away must not read as an omission -- so deleting the ACCEPTED frame
# produced the same benign note as tidying a rejected one. A scenario that
# captured two frames and deleted the damning one still graded PASS (fable,
# B round 1). These run the REAL vm-gui against a fake virsh.

# A `virsh` that writes a real PNG instead of talking to libvirt. Args as the
# library calls it: -c <uri> screenshot <vm> <out>
install_fake_virsh() {
    cat > "$TDIR/bin/virsh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out=""; prev=""
for a in "$@"; do
    [ "$prev" = screenshot ] && vm="$a"
    out="$a"; prev="$a"
done
if command -v magick >/dev/null 2>&1; then
    magick -size 320x80 xc:white -pointsize 24 -fill black \
        -annotate +10+40 "frame $(date +%s%N)" "$out" 2>/dev/null && exit 0
fi
printf '\211PNG\r\n\032\n\0\0\0\rIHDR\0\0\0\1\0\0\0\1\10\6\0\0\0\37\25\304\211\0\0\0\012IDATx\234c\370\17\0\1\1\1\0\30\335\215\260\0\0\0\0IEND\256B`\202' > "$out"
printf '%s\n' "$(date +%s%N)" >> "$out"
EOF
    chmod +x "$TDIR/bin/virsh"
}

vmgui_screenshot() {
    QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="$ADIR" \
    LIBVIRT_DEFAULT_URI=qemu:///session \
        "$REPO_ROOT/scripts/vm/vm-gui" "$CAPVM" screenshot "$1"
}

@test "vm-gui: the DELIVERED frame gets an in-tree ledger row" {
    install_fake_virsh
    run vmgui_screenshot "$ADIR/s1.png"
    [ "$status" -eq 0 ]
    [ -f "$ADIR/s1.png" ]
    # scope is column 4, path is column 7
    run awk -F'\t' -v p="$ADIR/s1.png" '$4 == "in-tree" && $7 == p { found = 1 } END { exit !found }' "$CAPLOG"
    [ "$status" -eq 0 ]
}

@test "vm-gui: deleting a delivered frame is a detected OMISSION, not a note" {
    install_fake_virsh
    vmgui_screenshot "$ADIR/keep.png"
    vmgui_screenshot "$ADIR/damning.png"
    write_status PASS
    # The agent grades, then removes the frame that contradicts its verdict.
    rm -f "$ADIR/damning.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "vm-gui: an UNTOUCHED delivered set grades normally" {
    install_fake_virsh
    vmgui_screenshot "$ADIR/keep.png"
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "vm-gui: a published frame counts ONCE, not as capture + delivery" {
    # The delivered frame has two rows with one digest (the scratch capture and
    # the in-tree delivery). Counting both reported a single screenshot as
    # "2 frame(s), 1 distinct" and inflated the declared-capture floor.
    install_fake_virsh
    vmgui_screenshot "$ADIR/s1.png"
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 1 frame(s), 1 distinct"* ]]
    [[ "$output" != *"captured outside the artifact dir"* ]]
}

@test "vm-gui: two genuinely identical in-tree frames are still two captures" {
    # Only the out-of-tree half of a PUBLISHED PAIR collapses. A stability
    # scenario that captures a before and an after must keep reporting two.
    write_status PASS
    write_frame a.png Approve
    cp "$ADIR/a.png" "$ADIR/b.png"
    attest_row "$ADIR/b.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [[ "$output" == *"harness-captured 2 frame(s), 1 distinct"* ]]
}

# --- B round 2 (sol): capture MULTIPLICITY, not just digest presence ----------

@test "floor: a published pair does not satisfy a declared count of two" {
    # One vm-gui frame writes two rows (scratch capture + in-tree delivery).
    # The floor used to be checked against the verifier's RAW row count, before
    # the pair collapse, so one screenshot satisfied `qci:visual-captures: 2`.
    install_fake_virsh
    cat > "$TDIR/two.md" <<'EOF'
# 10 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    vmgui_screenshot "$ADIR/s1.png"
    write_status PASS
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took only 1 capture(s)"* ]]
}

@test "floor: two real published frames DO satisfy a declared count of two" {
    install_fake_virsh
    cat > "$TDIR/two.md" <<'EOF'
# 10 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    vmgui_screenshot "$ADIR/s1.png"
    vmgui_screenshot "$ADIR/s2.png"
    write_status PASS
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "omission: deleting one of two IDENTICAL in-tree captures is detected" {
    # Rows used to match against the one file carrying their digest, so the
    # surviving twin satisfied both rows and the deletion was invisible. Rows
    # now consume files.
    write_status PASS
    write_frame a.png Approve
    cp "$ADIR/a.png" "$ADIR/b.png"
    attest_row "$ADIR/b.png"
    rm -f "$ADIR/b.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no longer there"* ]]
}

@test "omission: both identical twins present is still a clean pass" {
    write_status PASS
    write_frame a.png Approve
    cp "$ADIR/a.png" "$ADIR/b.png"
    attest_row "$ADIR/b.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 2 frame(s), 1 distinct"* ]]
}

# --- B round 2 (fable): what the vm-gui comments claimed vs what happened -----

# A `virsh` whose first screenshot is unusably black and whose later ones are
# fine, so the retry path runs for real.
install_flaky_virsh() {
    cat > "$TDIR/bin/virsh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out="${@: -1}"
n=$(cat "$QCI_TEST_ATTEMPTS" 2>/dev/null || echo 0); n=$((n + 1))
printf '%s' "$n" > "$QCI_TEST_ATTEMPTS"
if [ "$n" -eq 1 ]; then
    magick -size 320x80 xc:black "$out"
else
    magick -size 320x80 xc:white -pointsize 24 -fill black \
        -annotate +10+40 "frame $(date +%s%N)" "$out"
fi
EOF
    chmod +x "$TDIR/bin/virsh"
    export QCI_TEST_ATTEMPTS="$TDIR/attempts"
}

@test "vm-gui: a REJECTED attempt is kept for triage but never graded" {
    # Candidates carry no row until they are published or rejected, and a
    # rejected attempt copied back in-tree under an IMAGE name used to be
    # matched to a row by digest and graded -- a frame the harness had already
    # judged unusable entering
    # the evidence set, where it could satisfy the decoded-frame floor alone.
    if ! command -v magick >/dev/null 2>&1; then skip "no ImageMagick on this host"; fi
    install_flaky_virsh
    run vmgui_screenshot "$ADIR/s1.png"
    [ "$status" -eq 0 ]
    [ -f "$ADIR/s1.png.attempt-1.rejected" ]
    [ ! -f "$ADIR/s1.png.attempt-1.png" ]
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    # the black attempt is NOT one of the graded frames
    [[ "$output" == *"harness-captured 1 frame(s), 1 distinct"* ]]
    # the ledger still RECORDS it -- that is the forensic point -- under a
    # scope the gate counts nowhere
    run awk -F'\t' '$4 == "rejected" { n++ } END { exit !(n == 1) }' "$CAPLOG"
    [ "$status" -eq 0 ]
}

@test "vm-gui: a frame that could not be attested is NOT left in the frame set" {
    # The comment said an unattestable delivery "would not be graded"; leaving
    # the file in place did not achieve that, because an unattested file whose
    # bytes match some row is matched by digest and graded anyway.
    run bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        # vm-gui runs under `set -euo pipefail`; sourcing turns it back on, and
        # a function returning 1 would abort before the status is reported.
        set +e
        capture_publish_frame() { cp -T -- "$1" "$2"; return 1; }
        VM=testvm
        printf "bytes" > "$2/src.png"
        deliver_attested_frame "$2/src.png" "$3/out.png"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR" "$ADIR"
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [ ! -f "$ADIR/out.png" ]
    [ -f "$ADIR/out.png.unattested" ]
}

# --- B round 4: one row per frame, path-first reconciliation -----------------
#
# Rounds 1-3 attested every candidate, so a delivered frame carried two rows and
# the gate had to pair them from their bytes. Both round-3 reviewers broke that
# from opposite sides: the scratch+copy shape never collapsed (a floor of 2
# passed on one screenshot) while two genuinely identical captures collapsed
# wrongly (a floor of 2 failed on two real frames). These pin the replacement.

@test "one row: the /tmp-then-copy shape counts ONCE, not twice" {
    # 42 of the shipped `required` scenarios prescribe exactly this: capture to
    # /tmp, copy into the artifact dir.
    install_fake_virsh
    cat > "$TDIR/two.md" <<'EOF'
# 11 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="$ADIR" \
    LIBVIRT_DEFAULT_URI=qemu:///session \
        "$REPO_ROOT/scripts/vm/vm-gui" "$CAPVM" screenshot "$TDIR/outside.png" >/dev/null
    cp "$TDIR/outside.png" "$ADIR/s1.png"
    write_status PASS
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took only 1 capture(s)"* ]]
}

@test "one row: two identical captures in DIFFERENT scopes are two frames" {
    # The old digest-wide collapse turned these into one and failed a floor of
    # two on two real captures.
    write_status PASS
    plant_image_at "$TDIR/outside.png" Approve
    cp "$TDIR/outside.png" "$ADIR/copied.png"
    attest_row "$TDIR/outside.png"
    cp "$TDIR/outside.png" "$ADIR/second.png"
    attest_row "$ADIR/second.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 2 frame(s), 1 distinct"* ]]
}

@test "supersede: re-capturing to the SAME path is one frame, not an omission" {
    # One reference-run log re-captures to the same path 34 times. Demanding a
    # separate file per row reported the surviving frame as deleted.
    install_fake_virsh
    vmgui_screenshot "$ADIR/s1.png"
    vmgui_screenshot "$ADIR/s1.png"
    vmgui_screenshot "$ADIR/s1.png"
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 1 frame(s), 1 distinct"* ]]
}

@test "supersede: the LAST capture to a path is the one that must still exist" {
    install_fake_virsh
    vmgui_screenshot "$ADIR/s1.png"
    vmgui_screenshot "$ADIR/s1.png"
    rm -f "$ADIR/s1.png"
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no longer there"* ]]
}

@test "digest always: a frame OVERWRITTEN at its path is not present" {
    # A file existing at the recorded path is not presence -- an agent that
    # replaces a frame with an edited image leaves a file at that path.
    install_fake_virsh
    vmgui_screenshot "$ADIR/s1.png"
    printf 'not the captured frame' > "$ADIR/s1.png"
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "moved: a frame relocated within the artifact tree still counts" {
    install_fake_virsh
    vmgui_screenshot "$ADIR/s1.png"
    mkdir -p "$ADIR/sub"
    mv "$ADIR/s1.png" "$ADIR/sub/renamed.png"
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "triage names are type-safe: a DIRECTORY at the destination cannot smuggle a frame in" {
    # `cp SRC DEST` puts SRC's basename INSIDE DEST when DEST is a directory,
    # which put a graded image at a path nobody intended. `cp -T` refuses.
    if ! command -v magick >/dev/null 2>&1; then skip "no ImageMagick on this host"; fi
    install_flaky_virsh
    mkdir -p "$ADIR/s1.png.attempt-1.rejected"
    run vmgui_screenshot "$ADIR/s1.png"
    [ "$status" -eq 0 ]
    [ ! -f "$ADIR/s1.png.attempt-1.rejected/attempt-1.png" ]
    write_status PASS
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 1 frame(s), 1 distinct"* ]]
}

@test "rejected rows count NOWHERE, including toward a declared floor" {
    # The forensic record must not become evidence: a frame the capture tool
    # already refused cannot help satisfy `qci:visual-captures`.
    if ! command -v magick >/dev/null 2>&1; then skip "no ImageMagick on this host"; fi
    install_flaky_virsh
    cat > "$TDIR/two.md" <<'EOF'
# 12 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    vmgui_screenshot "$ADIR/s1.png"
    write_status PASS
    # one delivered frame + one rejected attempt in the ledger
    run awk -F'\t' '$4 == "rejected" { r++ } $4 == "in-tree" { t++ } END { exit !(r == 1 && t == 1) }' "$CAPLOG"
    [ "$status" -eq 0 ]
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took only 1 capture(s)"* ]]
}

# --- B round 5: the shapes both round-4 reviewers reproduced -----------------

@test "supersede applies to OUT-OF-TREE paths too, not just in-tree" {
    # Round 4 keyed supersession on in-tree rows only, so three publishes to one
    # /tmp name -- the dominant scenario shape -- reconciled as three captures
    # and satisfied a declared floor of two on one frame (sol, B round 4).
    #
    # NOTE WHAT THIS DOES AND DOES NOT COVER: the fake virsh writes a
    # TIMESTAMPED frame per call, so the three captures differ in bytes. The
    # identical-bytes case -- what a settled screen actually produces -- is not
    # reachable here and went wrong independently; K0 covers it (fable,
    # B round 6).
    install_fake_virsh
    cat > "$TDIR/two.md" <<'EOF'
# 13 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    local i
    for i in 1 2 3; do
        QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="$ADIR" \
        LIBVIRT_DEFAULT_URI=qemu:///session \
            "$REPO_ROOT/scripts/vm/vm-gui" "$CAPVM" screenshot "$TDIR/frame.png" >/dev/null
    done
    cp "$TDIR/frame.png" "$ADIR/s1.png"
    write_status PASS
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"took only 1 capture(s)"* ]]
}

@test "reservation: an earlier row cannot steal a later row's own file" {
    # Delete an in-tree frame while an identical out-of-tree frame survives.
    # Greedy in-ledger-order matching let the in-tree row consume the survivor,
    # turning a fatal omission into a benign non-harvest (sol and fable).
    write_status PASS
    plant_image deleted.png Approve
    attest_row "$ADIR/deleted.png"
    plant_image_at "$TDIR/survivor.png" Approve
    cp "$TDIR/survivor.png" "$ADIR/survivor.png"
    attest_row "$TDIR/survivor.png"
    rm -f "$ADIR/deleted.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no longer there"* ]]
}

@test "reservation: an out-of-tree row FIRST does not steal the in-tree file" {
    # The same shape in the other ledger order -- a peek to /tmp before the
    # evidence capture -- which fable found reported a delivered frame as
    # deleted (a false ERROR with a false message).
    write_status PASS
    plant_image_at "$TDIR/peek.png" Approve
    attest_row "$TDIR/peek.png"
    plant_image kept.png Approve
    attest_row "$ADIR/kept.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "path key: deleting one of two nested same-basename frames is detected" {
    # NOTE: this passes with a bare-basename key too -- a match also requires
    # digest equality, so the two keys agree here. It pins the BEHAVIOUR, not
    # the relative-path mechanism; see gui_capture_reconcile for why that
    # distinction is written down rather than assumed.
    write_status PASS
    plant_image first/frame.png Approve
    attest_row "$ADIR/first/frame.png"
    cp "$ADIR/first/frame.png" "$ADIR/second/frame.png" 2>/dev/null || {
        mkdir -p "$ADIR/second"; cp "$ADIR/first/frame.png" "$ADIR/second/frame.png"; }
    attest_row "$ADIR/second/frame.png"
    rm -f "$ADIR/second/frame.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no longer there"* ]]
}

@test "path key: the relative path survives the artifact dir being renamed" {
    # Harvest renames the artifact directory, so the ABSOLUTE path each row
    # recorded is stale by grading time.
    write_status PASS
    plant_image sub/deep.png Approve
    attest_row "$ADIR/sub/deep.png"
    local moved="$TDIR/harvested"
    cp -a "$ADIR" "$moved"
    run apply_contract PASS "$VISUAL_MD" "$moved" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "verifier: an UNKNOWN scope is fatal, not silently an ordinary capture" {
    # A producer that misspells `rejected` must not promote a frame the harness
    # already refused into evidence (sol, B round 4).
    plant_image junk.png Approve
    bash -c '
        set -euo pipefail
        . "$1"
        _qci_capture_attest_row "$2" "$3" "$4" "$5" rejectd
    ' _ "$CAPLIB" "$CAPLOG" "$ADIR/junk.png" "$CAPVM" "$ADIR"
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown scope"* ]]
}

@test "verifier: rejected rows are counted apart from captures" {
    # `rows=` must stay a count of CAPTURES: an all-rejected ledger has to read
    # as "took no capture", not as a floor shortfall (fable, B round 4).
    plant_image junk.png Approve
    bash -c '
        set -euo pipefail
        . "$1"
        _qci_capture_attest_row "$2" "$3" "$4" "$5" rejected
    ' _ "$CAPLIB" "$CAPLOG" "$ADIR/junk.png" "$CAPVM" "$ADIR"
    run gui_capture_log_verify "$CAPLOG"
    [ "$status" -eq 0 ]
    [ "$output" = "rows=0 in_tree=0 out_tree=0 rejected=1" ]
}

@test "contract: an ALL-REJECTED ledger says no capture was taken" {
    write_status PASS
    plant_image junk.png Approve
    bash -c '
        set -euo pipefail
        . "$1"
        _qci_capture_attest_row "$2" "$3" "$4" "$5" rejected
    ' _ "$CAPLIB" "$CAPLOG" "$ADIR/junk.png" "$CAPVM" "$ADIR"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"NO capture"* ]]
    [[ "$output" != *"qci:visual-captures"* ]]
}

@test "vm-gui: a pre-existing DIRECTORY at the destination is left untouched" {
    # `-e` quarantined whatever already occupied the path, so a directory
    # cp -T had correctly refused to overwrite was renamed to .unattested and
    # the error claimed bytes had been captured there (sol, B round 4).
    mkdir -p "$ADIR/out.png"
    printf 'preexisting\n' > "$ADIR/out.png/marker.txt"
    run bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        set +e
        VM=testvm
        printf "bytes" > "$2/src.png"
        deliver_attested_frame "$2/src.png" "$3/out.png"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR" "$ADIR"
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [ -f "$ADIR/out.png/marker.txt" ]
    [ ! -e "$ADIR/out.png.unattested" ]
    [[ "$output" == *"left untouched"* ]]
}

@test "vm-gui: a rejected-row write failure is LOUD, not swallowed" {
    run bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        set +e
        VM=testvm
        capture_attest_rejected() { return 1; }
        printf "bytes" > "$2/cand.png"
        retain_rejected_candidate "$2/cand.png" "$2/kept.rejected"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR"
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [[ "$output" == *"INCOMPLETE"* ]]
    [ -f "$TDIR/kept.rejected" ]
}

# --- B round 6: shapes both round-5 reviewers built -------------------------

@test "S1c: two captures to one scratch name, each copy preserved, are TWO" {
    # Round 5 discarded every earlier row for a reused path BEFORE matching, so
    # a before/after pair taken through one /tmp name counted as one capture and
    # failed a floor of two (sol and fable, B round 5).
    install_fake_virsh
    cat > "$TDIR/two.md" <<'EOF'
# 14 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="$ADIR" LIBVIRT_DEFAULT_URI=qemu:///session \
        "$REPO_ROOT/scripts/vm/vm-gui" "$CAPVM" screenshot "$TDIR/frame.png" >/dev/null
    cp "$TDIR/frame.png" "$ADIR/before.png"
    QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="$ADIR" LIBVIRT_DEFAULT_URI=qemu:///session \
        "$REPO_ROOT/scripts/vm/vm-gui" "$CAPVM" screenshot "$TDIR/frame.png" >/dev/null
    cp "$TDIR/frame.png" "$ADIR/after.png"
    write_status PASS
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 2 frame(s)"* ]]
}

@test "W3: a peek to /tmp does not make a RENAMED in-tree frame look deleted" {
    # Identical bytes out-of-tree and in-tree, with the in-tree file renamed.
    # A ledger-ordered digest pass reported the delivered frame as missing.
    write_status PASS
    plant_image_at "$TDIR/peek.png" Approve
    attest_row "$TDIR/peek.png"
    plant_image kept.png Approve
    attest_row "$ADIR/kept.png"
    mv "$ADIR/kept.png" "$ADIR/renamed.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "nested collision: a deleted in-tree frame whose twin was copied in is ERROR" {
    # sol's nested_basename_digest_collision: in-tree first/frame.png deleted,
    # an identical out-of-tree capture copied in as second/frame.png. The
    # survivor belongs to the out-of-tree row; the in-tree capture is missing.
    write_status PASS
    plant_image first/frame.png Approve
    attest_row "$ADIR/first/frame.png"
    mkdir -p "$TDIR/outside"
    cp "$ADIR/first/frame.png" "$TDIR/outside/frame.png"
    attest_row "$TDIR/outside/frame.png"
    mkdir -p "$ADIR/second"
    cp "$TDIR/outside/frame.png" "$ADIR/second/frame.png"
    rm -f "$ADIR/first/frame.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"no longer there"* ]]
}

@test "W2: a NESTED row's key is its whole path, so a top-level move is REPORTED" {
    # Round 5 keyed in-tree rows on a path SUFFIX, so `sub/frame.png`'s row
    # claimed a top-level `frame.png` as if it were its own file.
    #
    # What that costs is the note, not the verdict, and saying otherwise is how
    # this test came to pin nothing. A moved frame is matched either way -- by
    # the suffix under round 5, by the in-tree digest pass under round 6 -- so
    # both PASS. The difference is that only the digest pass sets `relocated`,
    # which is the signal the ruling promised a human would see. Round 6's
    # version asserted the verdict and therefore also passed on round-5 code and
    # on a mutant with the exact-path pass deleted (fable, B round 6). K8 is the
    # test that pins the key for a VERDICT.
    write_status PASS
    plant_image sub/frame.png Approve
    attest_row "$ADIR/sub/frame.png"
    mv "$ADIR/sub/frame.png" "$ADIR/frame.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"found by digest at a path other than"* ]]
}

@test "K8: the exact-path key keeps a peek from taking an in-tree row's OWN file" {
    # A never-copied /tmp peek carrying the same bytes as an UNTOUCHED in-tree
    # frame. With the capture root the in-tree row claims its own path and the
    # peek is merely un-harvested; without a root the basename pass takes that
    # file and a frame nobody touched is reported deleted (fable, B round 6,
    # K8/K8n). This is the shape that proves the exact-path pass is
    # load-bearing FOR A VERDICT, which no other test in this file does.
    write_status PASS
    plant_image frame.png Approve
    attest_row "$ADIR/frame.png"
    plant_image_at "$TDIR/frame.png" Approve
    attest_row "$TDIR/frame.png"
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    # ... and the untouched frame is NOT reported as relocated.
    [[ "$output" != *"found by digest at a path other than"* ]]
}

@test "K8n: WITHOUT a capture root the same shape is a false ERROR" {
    # Pins the rootless fallback as the hazard it is, rather than the safe
    # degradation the comment used to claim. Production always passes a root.
    write_status PASS
    plant_image frame.png Approve
    attest_row "$ADIR/frame.png"
    plant_image_at "$TDIR/frame.png" Approve
    attest_row "$TDIR/frame.png"
    CAPROOT="" run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
}

@test "vm-gui: a SYMLINK destination is refused and its target untouched" {
    # `-f` dereferences, so a pre-existing symlink was treated as a regular
    # file: the copy followed it and overwrote a file outside the artifact tree
    # (sol, B round 5).
    printf 'external\n' > "$TDIR/outside-target"
    ln -s "$TDIR/outside-target" "$ADIR/link.png"
    printf 'bytes' > "$TDIR/src.png"
    run bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        set +e
        VM=testvm
        deliver_attested_frame "$2" "$3"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR/src.png" "$ADIR/link.png"
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [[ "$output" == *"not a regular file"* ]]
    [ "$(cat "$TDIR/outside-target")" = external ]
    [ -h "$ADIR/link.png" ]
}

@test "vm-gui: a FIFO destination is refused before the copy can block" {
    # The type was only acted on AFTER the copy, so `cp -T` blocked forever.
    mkfifo "$ADIR/fifo.png"
    printf 'bytes' > "$TDIR/src.png"
    run timeout 10 bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        set +e
        VM=testvm
        deliver_attested_frame "$2" "$3"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR/src.png" "$ADIR/fifo.png"
    # 124 would be the timeout firing -- i.e. the block.
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [ -p "$ADIR/fifo.png" ]
}

# --- B round 7: sol's round-6 reservation defects ---------------------------

# The REAL library entry point a retry lane publishes through.
publish_frame() {
    QCI_GUI_CAPTURE_LOG="$CAPLOG" QCI_GUI_ARTIFACT_DIR="$ADIR" bash -c '
        set -euo pipefail
        . "$1"
        capture_publish_frame "$2" "$3" "$4"
    ' _ "$CAPLIB" "$1" "$2" "$CAPVM"
}

@test "supersession: an IDENTICAL re-capture loop to one in-tree path is ONE frame" {
    # Reserving in ledger order gave the sole surviving file to the OLDEST row.
    # The newest row was then unmatched, and supersession only ever drops a row
    # a LATER row superseded -- so the newest row counted as a real omission and
    # the gate said a frame had been removed when none had (sol, B round 6).
    # The existing loop test uses DISTINCT timestamped frames and cannot see it.
    write_status PASS
    plant_image_at "$TDIR/src.png" Approve
    publish_frame "$TDIR/src.png" "$ADIR/frame.png"
    publish_frame "$TDIR/src.png" "$ADIR/frame.png"
    publish_frame "$TDIR/src.png" "$ADIR/frame.png"
    run gui_capture_reconcile "$ADIR" "$CAPLOG" "$TDIR/flist" "$ADIR"
    [ "$output" = "attested=1 present=1 distinct=1 missing_in_tree=0 unharvested=0 relocated=0" ]
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "supersession: an IDENTICAL loop to one /tmp path cannot satisfy a floor of 2" {
    # The same defect out of tree, and this direction is a false PASS: the
    # unmatched newest row counted as a second attested capture, so ONE frame
    # satisfied a floor of two -- the exact bug round 5 set out to kill,
    # reintroduced from the other end (sol, B round 6).
    write_status PASS
    cat > "$TDIR/two.md" <<'EOF'
# 15 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    plant_image_at "$TDIR/src.png" Approve
    publish_frame "$TDIR/src.png" "$TDIR/out.png"
    publish_frame "$TDIR/src.png" "$TDIR/out.png"
    publish_frame "$TDIR/src.png" "$TDIR/out.png"
    cp "$TDIR/out.png" "$ADIR/kept.png"
    run gui_capture_reconcile "$ADIR" "$CAPLOG" "$TDIR/flist" "$ADIR"
    [ "$output" = "attested=1 present=1 distinct=1 missing_in_tree=0 unharvested=0 relocated=0" ]
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"only 1 capture"* ]]
}

@test "supersession: PRESERVED copies of a reused path are still counted apart" {
    # The guard on the fix: newest-first reservation must not undo round 6's
    # own correction. Two captures to one scratch name, each copy preserved
    # under a different artifact name, are still TWO.
    write_status PASS
    cat > "$TDIR/two.md" <<'EOF'
# 16 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    plant_image_at "$TDIR/a.png" One
    plant_image_at "$TDIR/b.png" Two
    publish_frame "$TDIR/a.png" "$TDIR/out.png"
    cp "$TDIR/out.png" "$ADIR/before.png"
    publish_frame "$TDIR/b.png" "$TDIR/out.png"
    cp "$TDIR/out.png" "$ADIR/after.png"
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
    [[ "$output" == *"harness-captured 2 frame(s)"* ]]
}

@test "K7c: a 34-iteration STATIC in-tree loop is one frame, not an omission" {
    # The shape the supersession comment itself cites. A wait-loop that
    # re-captures to one name produces identical bytes by construction once the
    # screen settles, which is precisely when ledger-order reservation misfired
    # (fable, B round 6). The existing loop test uses distinct timestamped
    # frames and cannot reach this.
    write_status PASS
    plant_image_at "$TDIR/src.png" Approve
    local i
    for (( i=0; i<34; i++ )); do
        publish_frame "$TDIR/src.png" "$ADIR/poll.png"
    done
    run gui_capture_reconcile "$ADIR" "$CAPLOG" "$TDIR/flist" "$ADIR"
    [ "$output" = "attested=1 present=1 distinct=1 missing_in_tree=0 unharvested=0 relocated=0" ]
    run apply_contract PASS "$VISUAL_MD" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = PASS ]
}

@test "K0: five STATIC captures to one /tmp name cannot satisfy a floor of two" {
    # sol's round-4 P1 for the static case. Round 6's fix covered it only when
    # the bytes differed, which the fake virsh's timestamped frames always do
    # (fable, B round 6).
    write_status PASS
    cat > "$TDIR/two.md" <<'EOF'
# 17 - declares two captures
<!-- qci:visual: required -->
<!-- qci:visual-captures: 2 -->
EOF
    plant_image_at "$TDIR/src.png" Approve
    local i
    for (( i=0; i<5; i++ )); do
        publish_frame "$TDIR/src.png" "$TDIR/shot.png"
    done
    cp "$TDIR/shot.png" "$ADIR/kept.png"
    run apply_contract PASS "$TDIR/two.md" "$ADIR" "$CAPLOG"
    [ "${output%%$'\t'*}" = ERROR ]
    [[ "$output" == *"only 1 capture"* ]]
}

@test "D1 equal bytes: a read-only file holding the SOURCE's bytes is still not ours" {
    # Round 7 recovered "did the copy happen?" by comparing the destination's
    # CONTENT to the source. That proves what the bytes ARE, not who wrote them:
    # when the pre-existing read-only file already held identical bytes, `cp`
    # failed with EACCES, nothing was captured, and the agent's file was
    # quarantined anyway under a message claiming a capture (sol and fable,
    # B round 7). The publisher now reports WHICH half failed.
    #
    # The round-7 test could not reach this: its source and destination differ,
    # so its `cmp` is necessarily false.
    printf 'identical bytes\n' > "$TDIR/src.png"
    printf 'identical bytes\n' > "$ADIR/s1.png"
    chmod 0444 "$ADIR/s1.png"
    run bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        set +e
        VM=testvm
        QCI_GUI_CAPTURE_LOG=/nonexistent/ledger.tsv
        export QCI_GUI_CAPTURE_LOG
        deliver_attested_frame "$2" "$3"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR/src.png" "$ADIR/s1.png"
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [[ "$output" != *"captured"*"could NOT record"* ]]
    [[ "$output" == *"left untouched"* ]]
    [ ! -e "$ADIR/s1.png.unattested" ]
    [ -f "$ADIR/s1.png" ]
    [ "$(cat "$ADIR/s1.png")" = "identical bytes" ]
}

@test "publisher: a failed COPY and a failed ROW are distinguishable to the caller" {
    # The fact vm-gui needs and cannot reconstruct afterwards. rc=2 copy, rc=1
    # row (sol and fable, B round 7).
    printf 'bytes' > "$TDIR/src.png"
    mkdir -p "$ADIR/adir.png"          # cp -T refuses to overwrite a directory
    run bash -c '
        set +e
        . "$1"
        capture_publish_frame "$2" "$3" testvm >/dev/null 2>&1
        echo "copyfail=$?"
    ' _ "$CAPLIB" "$TDIR/src.png" "$ADIR/adir.png"
    [ "$output" = "copyfail=2" ]
    # An ABSENT ledger is a documented silent success (an ordinary by-hand run
    # outside qci), so it cannot produce a row failure. A ledger bound to
    # ANOTHER VM refuses the row, which is the real shape.
    run bash -c '
        set +e
        QCI_GUI_CAPTURE_LOG="$4"
        export QCI_GUI_CAPTURE_LOG
        . "$1"
        capture_publish_frame "$2" "$3" a-neighbours-vm >/dev/null 2>&1
        echo "rowfail=$?"
    ' _ "$CAPLIB" "$TDIR/src.png" "$TDIR/dest.png" "$CAPLOG"
    [ "$output" = "rowfail=1" ]
    # the copy DID happen in the second case, so the bytes are ours
    [ -f "$TDIR/dest.png" ]
}

@test "D1: a read-only pre-existing file is left alone, and nothing claims a capture" {
    # `cp -T` fails EACCES, so nothing is written -- but the AGENT's file was
    # renamed to `.unattested` under a message saying a frame had been captured
    # and could not be recorded. Both halves were false (fable, B rounds 5/6).
    printf 'the agent wrote this\n' > "$ADIR/s1.png"
    chmod 0444 "$ADIR/s1.png"
    printf 'bytes' > "$TDIR/src.png"
    # Make the ledger refuse the row, so publication fails AFTER the type check.
    run bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        set +e
        VM=testvm
        QCI_GUI_CAPTURE_LOG=/nonexistent/ledger.tsv
        export QCI_GUI_CAPTURE_LOG
        deliver_attested_frame "$2" "$3"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR/src.png" "$ADIR/s1.png"
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [[ "$output" != *"captured"*"could NOT record"* ]]
    [[ "$output" == *"left untouched"* ]]
    [ ! -e "$ADIR/s1.png.unattested" ]
    [ "$(cat "$ADIR/s1.png")" = "the agent wrote this" ]
}

@test "publisher: a LATE copy failure leaves the destination untouched" {
    # rc=2 claims the destination was not touched. Round 8 implemented it as a
    # plain `cp -T` whose non-zero exit was reported as rc=2 -- but `cp`
    # truncates and writes a PREFIX before failing late, so a 1024-byte fragment
    # replaced the file that was there while vm-gui said "nothing was written"
    # (sol and fable, B round 8, reproduced with no shim under `ulimit -f 1`).
    # The round-8 test could only reach a PRE-write failure (cp onto a
    # directory), so it could not establish what rc=2 asserts.
    head -c 8192 /dev/zero > "$TDIR/big.png"
    printf 'the agent wrote this\n' > "$ADIR/keep.png"
    run bash -c '
        set +e
        ulimit -f 1          # SIGXFSZ part-way through an 8 KiB copy
        . "$1"
        capture_publish_frame "$2" "$3" testvm >/dev/null 2>&1
        echo "rc=$?"
    ' _ "$CAPLIB" "$TDIR/big.png" "$ADIR/keep.png"
    grep -qx 'rc=2' <<<"$output"
    # the destination is EXACTLY as it was, and no staging file was left behind
    [ "$(cat "$ADIR/keep.png")" = "the agent wrote this" ]
    [ -z "$(find "$ADIR" -name '.qci-publish.*' -print -quit)" ]
}

@test "vm-gui: a NON-WRITABLE destination is refused, now on purpose" {
    # Publication stages and renames, and a rename needs only the DIRECTORY to
    # be writable -- so the read-only file that `cp` used to refuse by accident
    # would now be replaced silently. The refusal is explicit instead.
    printf 'the agent wrote this\n' > "$ADIR/ro.png"
    chmod 0444 "$ADIR/ro.png"
    printf 'bytes' > "$TDIR/src.png"
    run bash -c '
        set +e
        source "$1" testvm wait >/dev/null 2>&1
        set +e
        VM=testvm
        deliver_attested_frame "$2" "$3"
        echo "rc=$?"
    ' _ "$REPO_ROOT/scripts/vm/vm-gui" "$TDIR/src.png" "$ADIR/ro.png"
    grep -qx 'rc=1' <<<"$output"      # not rc=127: a substring match accepted it
    [[ "$output" == *"not writable"* ]]
    [ "$(cat "$ADIR/ro.png")" = "the agent wrote this" ]
}
