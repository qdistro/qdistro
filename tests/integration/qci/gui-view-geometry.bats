#!/usr/bin/env bats
#
# Host tests for the blank-screenshot fix (todo/test-blankscreenshots/SPEC.md,
# acceptance test 3): view-unique geometry (F1), view-copy (F2), the codex
# rollout adapter (F3), the zero-look gate (F4) and the prompt rule (F6).
#
# Every test drives PRODUCTION code: the real vm-gui with a fake virsh, the
# real scripts/vm/lib/view-geometry.sh, the real ci/lib/gui_rollout_views.py
# on rollouts built from a REAL codex-cli 0.156.1 rollout captured on this host
# (fixtures/codex-rollout/), and the real gui_run_scenario with a scripted
# driver behind the real run_agent_command. Nothing here re-implements a rule
# it checks. The mutations each test was seen to catch are recorded in
# todo/test-blankscreenshots/impl-notes.md.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    VM_GUI="$REPO_ROOT/scripts/vm/vm-gui"
    VIEWLIB="$REPO_ROOT/scripts/vm/lib/view-geometry.sh"
    ADAPTER="$REPO_ROOT/ci/lib/gui_rollout_views.py"
    FIX="$REPO_ROOT/tests/integration/qci/fixtures/codex-rollout"
    command -v magick >/dev/null 2>&1 || skip "ImageMagick magick not installed"
    TDIR="$(mktemp -d "${BATS_TMPDIR:-/tmp}/gui-view.XXXXXX")"
    ADIR="$TDIR/art"
    mkdir -p "$ADIR" "$TDIR/bin"
    SCREEN="$TDIR/screen.png"
    render_screen "screen one"
    cat > "$TDIR/bin/virsh" <<'EOF'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$FAKE_VIRSH_LOG"
case " $* " in
    *" screenshot "*) cp "$FAKE_SCREENSHOT" "${!#}" ;;
    *" qemu-monitor-command "*) ;;
    *) echo "unexpected fake virsh invocation: $*" >&2; exit 2 ;;
esac
EOF
    chmod +x "$TDIR/bin/virsh"
    export PATH="$TDIR/bin:$PATH"
    export FAKE_SCREENSHOT="$SCREEN" FAKE_VIRSH_LOG="$TDIR/virsh.log"
    : > "$FAKE_VIRSH_LOG"
    export QCI_GUI_ARTIFACT_DIR="$ADIR"
    export QCI_GUI_VIEW_STATE="$ADIR/.qci-view-state"
    : > "$QCI_GUI_VIEW_STATE"
    export LIBVIRT_DEFAULT_URI=qemu:///session
    CAPVM=testvm
}

teardown() {
    [ -n "${TDIR:-}" ] && rm -rf -- "$TDIR"
}

# A real 1280x800 screen with some structure (usable), deterministic bytes.
render_screen() {
    magick -size 1280x800 xc:'#20252b' -fill white -draw 'rectangle 430,480 650,560' \
        -pointsize 30 -annotate +40+60 "$1" -define png:exclude-chunks=date,time "$SCREEN"
}

dims() { magick identify -format '%w %h' "$1"; }

# The raw canonical pixel sha of an image, computed by the production helper.
pix() { bash -c '. "$1"; qci_view_pix_sha "$2"' _ "$VIEWLIB" "$1"; }

new_ledger() {
    CAPLOG="$TDIR/captures.tsv"
    # shellcheck disable=SC1090
    ( source "$REPO_ROOT/ci/lib/gates/gui.sh"; gui_capture_log_init "$CAPLOG" "$CAPVM" )
    export QCI_GUI_CAPTURE_LOG="$CAPLOG"
}

# ---------------------------------------------------------------- F1 --------

@test "F1: identical raws get distinct (W,H), each frame keeps its raw identity (I6)" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s1.png" >/dev/null
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s2.png" >/dev/null
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3.png" >/dev/null
    [ "$(dims "$ADIR/s1.png")" = "1281 801" ]
    [ "$(dims "$ADIR/s2.png")" = "1282 802" ]
    [ "$(dims "$ADIR/s3.png")" = "1283 803" ]
    local raw f rw rh k sha rest
    raw=$(pix "$SCREEN")
    for f in s1 s2 s3; do
        read -r rw rh k sha rest < "$ADIR/$f.png.raw"
        [ "$rw $rh" = "1280 800" ]
        [ "$sha" = "$raw" ]
        # I6: the frame cropped to raw_w x raw_h +0+0 has the raw pixels.
        magick "$ADIR/$f.png" -crop "${rw}x${rh}+0+0" +repage "$TDIR/$f-crop.png"
        [ "$(pix "$TDIR/$f-crop.png")" = "$raw" ]
    done
    # The margin is black.
    [ "$(magick "$ADIR/s3.png" -crop 3x803+1280+0 +repage -format '%[fx:maxima]' info:)" = 0 ]
}

@test "F1: mixed raw geometries never collide (1281x801+1 = 1280x800+2)" {
    bash -c '
        . "$1"
        mk() { magick -size "$1" xc:gray50 -fill white -draw "rectangle 1,1 20,20" "$2"; }
        mk 1281x801 "$3/r1.png"; mk 1280x800 "$3/r2.png"
        qci_view_publish "$3/r1.png" "$3/o1.png" test src qci_view_mv_publish || exit 1
        qci_view_publish "$3/r2.png" "$3/o2.png" test src qci_view_mv_publish || exit 1
        mk 1280x800 "$3/r2.png"
        qci_view_publish "$3/r2.png" "$3/o3.png" test src qci_view_mv_publish || exit 1
        mk 1281x801 "$3/r1.png"
        qci_view_publish "$3/r1.png" "$3/o4.png" test src qci_view_mv_publish || exit 1
    ' _ "$VIEWLIB" unused "$TDIR"
    [ "$(dims "$TDIR/o1.png")" = "1282 802" ]
    [ "$(dims "$TDIR/o2.png")" = "1281 801" ]
    # k=2 would be 1282x802, already issued for the 1281x801 raw: skipped.
    [ "$(dims "$TDIR/o3.png")" = "1283 803" ]
    [ "$(dims "$TDIR/o4.png")" = "1284 804" ]
    [ "$(cut -f1,2 "$QCI_GUI_VIEW_STATE" | sort | uniq -d | wc -l)" -eq 0 ]
}

@test "F1: concurrent allocations on one attempt state never collide" {
    local i
    for i in $(seq 1 12); do
        cp "$SCREEN" "$TDIR/raw$i.png"
        bash -c '. "$1"; qci_view_publish "$2" "$3" test src qci_view_mv_publish' \
            _ "$VIEWLIB" "$TDIR/raw$i.png" "$ADIR/c$i.png" &
    done
    wait
    [ "$(wc -l < "$QCI_GUI_VIEW_STATE")" -eq 12 ]
    [ "$(cut -f1,2 "$QCI_GUI_VIEW_STATE" | sort -u | wc -l)" -eq 12 ]
    for i in $(seq 1 12); do
        local W H
        read -r W H <<<"$(dims "$ADIR/c$i.png")"
        grep -q "^$W	$H	" "$QCI_GUI_VIEW_STATE"
        [ -f "$ADIR/c$i.png.raw" ]
    done
}

@test "F1: a near-black raw frame is still rejected (usability judged on raw)" {
    magick -size 1280x800 xc:black -define png:exclude-chunks=date,time "$SCREEN"
    QCI_SCREENSHOT_ATTEMPTS=2 run "$VM_GUI" "$CAPVM" screenshot "$ADIR/black.png"
    [ "$status" -ne 0 ]
    [[ "$output" == *"near-black"* ]]
    [ ! -e "$ADIR/black.png" ]
    [ ! -e "$ADIR/black.png.raw" ]
    [ -f "$ADIR/black.png.attempt-1.rejected" ]
    # nothing was reserved for a frame that was never published
    [ ! -s "$QCI_GUI_VIEW_STATE" ]
}

@test "F1: the same-screen note fires on identical RAW pixels of padded frames" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s1.png" >/dev/null 2>&1
    run "$VM_GUI" "$CAPVM" screenshot "$ADIR/s2.png"
    [ "$status" -eq 0 ]
    [[ "$output" == *"SAME SCREEN PIXELS as earlier capture(s): s1.png"* ]]
    render_screen "screen two"
    run "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3.png"
    [[ "$output" != *"SAME SCREEN PIXELS"* ]]
}

@test "F1: screenshot-fresh still reports unchanged-baseline against a PADDED baseline" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/base.png" >/dev/null 2>&1
    [ -f "$ADIR/base.png.raw" ]
    run "$VM_GUI" "$CAPVM" screenshot-fresh "$ADIR/after.png" "$ADIR/base.png" 2
    [ "$status" -ne 0 ]
    [[ "$output" == *"unchanged-baseline"* ]]
    [ ! -e "$ADIR/after.png" ]
    render_screen "screen changed"
    run "$VM_GUI" "$CAPVM" screenshot-fresh "$ADIR/after.png" "$ADIR/base.png" 2
    [ "$status" -eq 0 ]
    [ -f "$ADIR/after.png.raw" ]
}

@test "F1: a PNG-only (sidecar-less) copy of a padded baseline still refuses an unchanged screen" {
    # fable code review r1, P2: the driver copies the baseline WITHOUT its .raw.
    "$VM_GUI" "$CAPVM" screenshot "$TDIR/base.png" >/dev/null 2>&1
    cp "$TDIR/base.png" "$ADIR/base.png"
    [ ! -e "$ADIR/base.png.raw" ]
    run "$VM_GUI" "$CAPVM" screenshot-fresh "$ADIR/after.png" "$ADIR/base.png" 2
    [ "$status" -ne 0 ]
    [[ "$output" == *"unchanged-baseline"* ]]
    [ ! -e "$ADIR/after.png" ]
}

@test "F1: the same-screen note finds a sidecar-less copy of an earlier frame" {
    "$VM_GUI" "$CAPVM" screenshot "$TDIR/first.png" >/dev/null 2>&1
    cp "$TDIR/first.png" "$ADIR/copied.png"
    run "$VM_GUI" "$CAPVM" screenshot "$ADIR/second.png"
    [ "$status" -eq 0 ]
    [[ "$output" == *"SAME SCREEN PIXELS as earlier capture(s): copied.png"* ]]
}

@test "F1: click geometry is raw -- margin click rejected, ring/zoom and QMP events unchanged" {
    # Reference run WITHOUT the view state: the pre-padding behaviour.
    local ref="$TDIR/ref"
    mkdir -p "$ref"
    QCI_GUI_VIEW_STATE= QCI_GUI_ARTIFACT_DIR="$ref" "$VM_GUI" "$CAPVM" click-preview 490 522 x >/dev/null
    QCI_GUI_VIEW_STATE= QCI_GUI_ARTIFACT_DIR="$ref" "$VM_GUI" "$CAPVM" click-confirm "$ref/click-targets/click-001.preview" >/dev/null
    cp "$FAKE_VIRSH_LOG" "$TDIR/ref-virsh.log"; : > "$FAKE_VIRSH_LOG"
    "$VM_GUI" "$CAPVM" click-preview 490 522 x >/dev/null
    "$VM_GUI" "$CAPVM" click-confirm "$ADIR/click-targets/click-001.preview" >/dev/null
    local t="$ADIR/click-targets"
    # every view got its own size, and the raw/annotated/zoom/post pixels are
    # exactly the unpadded run's
    [ "$(dims "$t/click-001.raw.png")" = "1281 801" ]
    [ "$(dims "$t/click-001.annotated.png")" = "1281 801" ] || [ "$(dims "$t/click-001.annotated.png")" = "1282 802" ]
    [ "$(cut -f1,2 "$QCI_GUI_VIEW_STATE" | sort -u | wc -l)" -eq 4 ]
    local v
    for v in raw annotated zoom post; do
        bash -c '. "$1"; qci_view_raw_extract "$2" "$3"' _ "$VIEWLIB" "$t/click-001.$v.png" "$TDIR/$v.png"
        [ "$(pix "$TDIR/$v.png")" = "$(pix "$ref/click-targets/click-001.$v.png")" ]
    done
    # QMP events identical (move + click at the reviewed raw coordinates)
    diff <(grep qemu-monitor-command "$TDIR/ref-virsh.log") <(grep qemu-monitor-command "$FAKE_VIRSH_LOG")
    grep -Fxq 'width=1280' "$t/click-001.preview"
    grep -Fxq "annotated_raw=$t/click-001.raw.png" "$t/click-001.preview"
    grep -Fxq "raw_sha256=$(pix "$SCREEN")" "$t/click-001.preview"
    # a coordinate in the padded margin (x=1280 exists only in the padded file)
    : > "$FAKE_VIRSH_LOG"
    run "$VM_GUI" "$CAPVM" click-preview 1280 400 margin
    [ "$status" -eq 2 ]
    [[ "$output" == *"outside screenshot bounds"* ]]
    ! grep -q qemu-monitor-command "$FAKE_VIRSH_LOG"
}

@test "F1: qdlocker screen dimensions are the RAW dimensions" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/lock.png" >/dev/null 2>&1
    [ "$(dims "$ADIR/lock.png")" = "1281 801" ]
    run bash -c '
        export QDWIN_REPO="$1/qdwin"
        . "$1/qdlocker/tests/gui/qdlocker-helpers.sh" >/dev/null 2>&1
        qdlocker_screenshot_dimensions "$2"' _ "$REPO_ROOT" "$ADIR/lock.png"
    [ "$status" -eq 0 ]
    [ "$output" = "1280 800" ]
}

@test "F1: qdwin/apps/10's 1280x800 precondition passes on a padded frame" {
    mkdir -p "$ADIR/screenshots"
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/screenshots/output-precondition.png" >/dev/null 2>&1
    [ "$(dims "$ADIR/screenshots/output-precondition.png")" != "1280 800" ]
    # The scenario's OWN line, executed as written, after its own helper file.
    local line
    line=$(grep -m1 '^CASE_OUTPUT_GEOMETRY=' "$REPO_ROOT/qdwin/tests/apps/10-tk-fltk-swing.md")
    [ -n "$line" ]
    run bash -c '
        export QDWIN_REPO="$1/qdwin"
        . "$1/qdwin/tests/apps/qdwin-apps-helpers.sh" >/dev/null 2>&1
        CASE_ARTIFACT_DIR=$2
        eval "$3"
        printf "%s" "$CASE_OUTPUT_GEOMETRY"' _ "$REPO_ROOT" "$ADIR" "$line"
    [ "$output" = 1280x800 ]
}

@test "F1: shell-capture smoke's equal-size diff passes on two padded captures" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/static-1.png" >/dev/null 2>&1
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/static-2.png" >/dev/null 2>&1
    [ "$(dims "$ADIR/static-1.png")" != "$(dims "$ADIR/static-2.png")" ]
    local smoke="$REPO_ROOT/qdwin/tests/gui/agent-shell-capture-smoke.sh"
    # The smoke's own raw_of helper and its own static-diff block, verbatim.
    sed -n '/^RAWCMP=/,/^}/p' "$smoke" > "$TDIR/rawof.sh"
    sed -n '/^python3 - "$(raw_of "$ART\/static-1.png")"/,/^pass "unchanged scene/p' "$smoke" > "$TDIR/static.sh"
    grep -q 'raw_of' "$TDIR/static.sh"
    run bash -c '
        set -euo pipefail
        . "$1"
        fail() { echo "FAIL: $*"; exit 1; }
        pass() { echo "PASS: $*"; }
        ART=$2
        . "$3"
        . "$4"' _ "$VIEWLIB" "$ADIR" "$TDIR/rawof.sh" "$TDIR/static.sh"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"static_changed_fraction=0.000000 dimensions=1280x800"* ]]
}

@test "F1: /tmp capture copied into the artifact dir, and alias->canonical harvest" {
    new_ledger
    local tmpd="$TDIR/tmpcap"
    mkdir -p "$tmpd"
    "$VM_GUI" "$CAPVM" screenshot "$tmpd/x.png" >/dev/null 2>&1
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/y.png" >/dev/null 2>&1
    render_screen "screen two"
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/z.png" >/dev/null 2>&1
    # x.png is copied WITHOUT its .raw (fable code review r1, P2): its raw
    # identity must still resolve through the attempt's view state.
    cp "$tmpd/x.png" "$ADIR/"
    echo PASS > "$ADIR/status.txt"
    # Harvest the (alias) directory into a canonical one with the real harvest.
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    local canon="$TDIR/run/gui/slug"
    mkdir -p "$canon"
    export QCI_GUI_ART_ALIAS_ROOT="$TDIR/aliases"
    local alias
    alias=$(gui_make_artifact_alias "$canon")
    cp -a "$ADIR/." "$alias/"
    gui_harvest_agent_artifacts "$canon" slug "" "$alias"
    local f
    [ ! -e "$canon/x.png.raw" ]
    for f in x.png y.png y.png.raw z.png z.png.raw; do
        cmp "$ADIR/$f" "$canon/$f"
    done
    [ ! -e "$canon/.qci-view-state" ]
    # Reconcile on the canonical dir: 3 attested frames present, and TWO
    # distinct screens (x and y are the same raw screen at different sizes).
    run gui_capture_reconcile "$canon" "$CAPLOG" "$TDIR/list.txt" "$ADIR"
    [[ "$output" == *"present=3 distinct=2"* ]] || { echo "$output" >&2; return 1; }
}

@test "F1: a failed publication leaves neither frame nor sidecar, and restores an untouched frame's sidecar" {
    bash -c '
        . "$1"
        failpub() { return 2; }
        cp "$2" "$3/r.png"
        qci_view_publish "$3/r.png" "$3/out.png" test src failpub' _ "$VIEWLIB" "$SCREEN" "$TDIR" && false
    [ ! -e "$TDIR/out.png" ]
    [ ! -e "$TDIR/out.png.raw" ]
    [ -z "$(find "$TDIR" -maxdepth 1 -name '.qci-view*')" ]
    # An existing published frame whose re-publication fails keeps its sidecar.
    bash -c '. "$1"; cp "$2" "$3/r.png"; qci_view_publish "$3/r.png" "$3/keep.png" test src qci_view_mv_publish' \
        _ "$VIEWLIB" "$SCREEN" "$TDIR"
    local before
    before=$(cat "$TDIR/keep.png.raw")
    bash -c '. "$1"; failpub() { return 2; }; cp "$2" "$3/r.png"; qci_view_publish "$3/r.png" "$3/keep.png" test src failpub' \
        _ "$VIEWLIB" "$SCREEN" "$TDIR" && false
    [ "$(cat "$TDIR/keep.png.raw")" = "$before" ]
    # A stale sidecar whose frame is gone is removed by a failed publication.
    rm -f "$TDIR/keep.png"
    bash -c '. "$1"; failpub() { return 2; }; cp "$2" "$3/r.png"; qci_view_publish "$3/r.png" "$3/keep.png" test src failpub' \
        _ "$VIEWLIB" "$SCREEN" "$TDIR" && false
    [ ! -e "$TDIR/keep.png.raw" ]
}

@test "F1: without QCI_GUI_VIEW_STATE (manual use) publication is a pass-through" {
    QCI_GUI_VIEW_STATE= "$VM_GUI" "$CAPVM" screenshot "$ADIR/m.png" >/dev/null 2>&1
    [ "$(dims "$ADIR/m.png")" = "1280 800" ]
    [ ! -e "$ADIR/m.png.raw" ]
}

# ---------------------------------------------------------------- F2 --------

@test "F2: view-copy re-pads from RAW content with a fresh size and records lineage" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s1.png" >/dev/null 2>&1
    run "$VM_GUI" "$CAPVM" view-copy "$ADIR/s1.png"
    [ "$status" -eq 0 ]
    local v=$output rw rh k sha kind src
    [ "$v" = "$ADIR/s1.view-1.png" ]
    [ "$(dims "$v")" = "1282 802" ]
    read -r rw rh k sha kind src < "$v.raw"
    [ "$rw $rh $kind $src" = "1280 800 view $ADIR/s1.png" ]
    [ "$sha" = "$(pix "$SCREEN")" ]
    # a view of a view is still raw content, never a re-pad of a padded file
    run "$VM_GUI" "$CAPVM" view-copy "$v"
    read -r rw rh k sha kind src < "$output.raw"
    [ "$rw $rh" = "1280 800" ]
    [ "$(dims "$output")" = "1283 803" ]
}

@test "F2: a scenario crop's lineage is verified; a wrong one is refused" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/cap.png" >/dev/null 2>&1
    magick "$ADIR/cap.png" -crop 1280x48+0+0 +repage "$ADIR/bar.png"
    run "$VM_GUI" "$CAPVM" view-copy "$ADIR/bar.png" --source "$ADIR/cap.png" --crop 1280x48+0+0
    [ "$status" -eq 0 ]
    local rw rh k sha kind src
    read -r rw rh k sha kind src < "$output.raw"
    [ "$kind $src" = "crop:1280x48+0+0 $ADIR/cap.png" ]
    run "$VM_GUI" "$CAPVM" view-copy "$ADIR/bar.png" --source "$ADIR/cap.png" --crop 1280x48+0+10
    [ "$status" -eq 2 ]
    [[ "$output" == *"lineage NOT recorded"* ]]
}

@test "F2: a derivative inherits rejected and stale status" {
    magick -size 1280x800 xc:black "$TDIR/b.png"
    cp "$TDIR/b.png" "$ADIR/x.png.attempt-1.rejected"
    run "$VM_GUI" "$CAPVM" view-copy "$ADIR/x.png.attempt-1.rejected" --out "$ADIR/rej-view.png"
    [ "$status" -eq 0 ]
    grep -q ' view,rejected ' "$ADIR/rej-view.png.raw"
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/st.png" >/dev/null 2>&1
    echo 'live=0 age_ms=5 msc=1' > "$ADIR/st.png.meta"
    run "$VM_GUI" "$CAPVM" view-copy "$ADIR/st.png"
    grep -q ' view,stale ' "$output.raw"
    cmp "$ADIR/st.png.meta" "$output.meta"
}

@test "F2: view-copy without an attempt state refuses; --attempt-dir supplies one" {
    run env QCI_GUI_VIEW_STATE= "$VM_GUI" "$CAPVM" view-copy "$SCREEN"
    [ "$status" -eq 2 ]
    [[ "$output" == *"--attempt-dir"* ]]
    mkdir -p "$TDIR/manual"
    run env QCI_GUI_VIEW_STATE= "$VM_GUI" "$CAPVM" view-copy "$SCREEN" --attempt-dir "$TDIR/manual" --out "$TDIR/manual/v.png"
    [ "$status" -eq 0 ]
    [ -s "$TDIR/manual/.qci-view-state" ]
}

# ---------------------------------------------------------------- F3 --------

# Build an attempt: a log with the real header, a rollout from the real
# fixture. Args: name [make-rollout args...]
mk_attempt() {
    local name=$1; shift
    local sid
    sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
    sed "s/^session id: .*/session id: $sid/" "$FIX/agent-log-header-0.156.1.txt" > "$TDIR/$name.log"
    echo "... driver output ..." >> "$TDIR/$name.log"
    mkdir -p "$TDIR/codex/sessions/2026/09/25"
    python3 "$FIX/make-rollout.py" --sid "$sid" --cwd "$TDIR/cwd" \
        --out "$TDIR/codex/sessions/2026/09/25/rollout-2026-09-25T00-00-00-$sid.jsonl" "$@"
    printf '%s\n' "$sid"
}

observe() {
    CODEX_HOME="$TDIR/codex" python3 "$ADAPTER" --log "$TDIR/$1.log" --out "$TDIR/$1.views" \
        --agent-cmd "${AGENT_CMD:-codex --yolo exec -m gpt-5.6-luna --skip-git-repo-check - < {prompt}}" \
        --ledger "${CAPLOG:-}" --state "$QCI_GUI_VIEW_STATE" >/dev/null
    sed -n 's/^reason=//p' "$TDIR/$1.views"
}

view_js() { printf 'const a = await tools.view_image({path:"%s", detail:"high"});\nimage(a.image_url);\n' "$1"; }

@test "F3: real-schema rollout -> the exact list of opens, credited to attested frames" {
    new_ledger
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s1.png" >/dev/null 2>&1
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s2.png" >/dev/null 2>&1
    mkdir -p "$TDIR/cwd"
    mk_attempt a1 \
        --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 1}))' "$(view_js "$ADIR/s1.png")")" \
        --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1] + sys.argv[2], "images": 2}))' "$(view_js "$ADIR/s2.png")" "$(view_js ../cwd/rel.png)")" >/dev/null
    [ "$(observe a1)" = observed ]
    grep -Fxq 'opens=3' "$TDIR/a1.views"
    grep -Fxq 'credited=2' "$TDIR/a1.views"
    [ "$(grep $'^open\t' "$TDIR/a1.views" | cut -f2,4,5 | tr '\t' ' ')" = "1 $ADIR/s1.png full
2 $ADIR/s2.png full
3 $TDIR/cwd/rel.png none" ]
}

@test "F3: every unsupported shape is unobservable, never zero" {
    mkdir -p "$TDIR/cwd"
    local js2 comp
    js2="$(view_js /a.png)$(view_js /b.png)"
    comp='const p = ["/a.png"]; const r = await Promise.all(p.map(path => tools.view_image({path}))); for (const x of r) image(x.image_url);'
    mk_attempt multi --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 1}))' "$js2")" >/dev/null
    [ "$(observe multi)" = unobservable:count-mismatch ]
    grep -Fxq 'credited=unobservable' "$TDIR/multi.views"
    mk_attempt computed --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 1}))' "$comp")" >/dev/null
    [ "$(observe computed)" = unobservable:computed-path ]
    mk_attempt cond --call "$(python3 -c 'import json,sys; print(json.dumps({"input": "if (ok) {" + sys.argv[1] + "}", "images": 1}))' "$(view_js /a.png)")" >/dev/null
    [ "$(observe cond)" = unobservable:control-flow ]
    mk_attempt failed --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 0, "status": "failed"}))' "$(view_js /a.png)")" >/dev/null
    [ "$(observe failed)" = unobservable:failed-call ]
    mk_attempt trunc --truncate --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 1}))' "$(view_js /a.png)")" >/dev/null
    [ "$(observe trunc)" = unobservable:truncated ]
    mk_attempt old --raw-line "$FIX/function-call-view-image-0.130.0.jsonl" >/dev/null
    [ "$(observe old)" = unobservable:unsupported-shape ]
    # a function_call NAMED exec carrying view_image (fable code review r1, P1)
    mk_attempt fnexec --raw-line "$FIX/function-call-exec-view-image.jsonl" >/dev/null
    [ "$(observe fnexec)" = unobservable:unsupported-shape ]
    grep -Fxq 'credited=unobservable' "$TDIR/fnexec.views"
    local sid
    sid=$(mk_attempt two --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 1}))' "$(view_js /a.png)")")
    mkdir -p "$TDIR/codex/sessions/2026/09/26"
    cp "$TDIR/codex/sessions/2026/09/25/"*"-$sid.jsonl" "$TDIR/codex/sessions/2026/09/26/"
    [ "$(observe two)" = unobservable:two-rollouts ]
    sid=$(mk_attempt gone --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 1}))' "$(view_js /a.png)")")
    rm -f "$TDIR/codex/sessions/2026/09/25/"*"-$sid.jsonl"
    [ "$(observe gone)" = unobservable:no-rollout ]
    mk_attempt eph --call "$(python3 -c 'import json,sys; print(json.dumps({"input": sys.argv[1], "images": 1}))' "$(view_js /a.png)")" >/dev/null
    [ "$(AGENT_CMD='codex --yolo exec -m gpt-5.6-luna --ephemeral - < {prompt}' observe eph)" = unobservable:ephemeral ]
    [ "$(AGENT_CMD='CODEX_HOME=/elsewhere codex exec - < {prompt}' observe eph)" = unobservable:codex-home ]
    printf 'no header at all\n' > "$TDIR/nohdr.log"
    [ "$(observe nohdr)" = unobservable:no-session-id ]
}

# ---------------------------------------------------------------- F4 --------
#
# The REAL gui_run_scenario, with the REAL run_agent_command (bwrap sandbox,
# per-attempt cwd), the real harvest, ledger seal, evidence contract, F3
# observation, view gate, classifier and retry loop. Only the VM lifecycle and
# the run-record writers are stubbed, and the "driver" is a script that does
# what a codex driver does: captures with the real vm-gui, maybe looks (writes
# a rollout from the real fixture), writes status.txt. Its plan file names one
# behaviour per attempt.

f4_setup() {
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    RDIR="$TDIR/run"; mkdir -p "$RDIR/gui"
    export QCI_GUI_ART_ALIAS_ROOT="$TDIR/aliases"
    export CODEX_HOME="$TDIR/codex"; mkdir -p "$CODEX_HOME/sessions/2026/09/25"
    export QDISTRO_REPO="$REPO_ROOT"
    unset QCI_GUI_VIEW_STATE QCI_GUI_ARTIFACT_DIR QCI_GUI_CAPTURE_LOG
    EXIT_VM_PROVISION=40; EXIT_GUI=50
    export FAKE_PLAN="$TDIR/plan" FAKE_N="$TDIR/plan.n" FIX VM_GUI
    : > "$FAKE_N"
    VMSEQ=0
    acquire_vm() { VMSEQ=$((VMSEQ + 1)); printf 'qci-gui-test-vm-%s\n' "$VMSEQ"; }
    release_vm() { :; }
    collect_vm_artifacts() { :; }
    install_gui_waiters() { :; }
    prepare_guest_scratch() { :; }
    suppress_idle_lock() { :; }
    record_timing() { :; }
    record_host_load() { :; }
    record_result() { printf '%s\n' "$*" >> "$TDIR/results"; }
    record_attempt() { printf '%s\t%s\t%s\t%s\t%s\n' "$3" "$4" "$5" "$6" "$9" >> "$TDIR/attempts"; }
    record_flake() { printf '%s\n' "$*" >> "$TDIR/flakes"; }
    scenario_scratch_dir() { printf '%s/%s/%s.scratch' "$RDIR" "$1" "$2"; }
    SCEN="$TDIR/07-visual.md"
    printf '# 07\n<!-- qci:visual: required -->\n' > "$SCEN"
    SCEN_NONE="$TDIR/08-none.md"
    printf '# 08\n<!-- qci:visual: none -->\n' > "$SCEN_NONE"
    cat > "$TDIR/bin/fake-driver" <<'DRV'
#!/usr/bin/env bash
# One scripted "codex" attempt. Behaviour: line N of $FAKE_PLAN.
set -u
n=$(( $(wc -l < "$FAKE_N") + 1 )); echo x >> "$FAKE_N"
mode=$(sed -n "${n}p" "$FAKE_PLAN")
verdict=${mode%% *}; look=${mode#* }
sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
sed "s/^session id: .*/session id: $sid/" "$FIX/agent-log-header-0.156.1.txt"
A=$QCI_GUI_ARTIFACT_DIR
calls=()
view() { calls+=(--call "$(python3 -c 'import json,sys; print(json.dumps({"input": "const a = await tools.view_image({path:\"%s\"});\nimage(a.image_url);\n" % sys.argv[1], "images": 1}))' "$1")"); }
case "$look" in
    nocapture) ;;
    *) "$VM_GUI" "$VMNAME" screenshot "$A/s1.png" >/dev/null 2>&1 || echo "capture failed" ;;
esac
case "$look" in
    look) view "$A/s1.png" ;;
    nolook|nocapture) ;;
    tooling) echo "bash: -c: option requires an argument" ;;
    viewcopy) v=$("$VM_GUI" "$VMNAME" view-copy "$A/s1.png"); view "$v" ;;
    click) "$VM_GUI" "$VMNAME" click-preview 490 522 t >/dev/null 2>&1
           view "$A/click-targets/click-001.annotated.png"; view "$A/click-targets/click-001.zoom.png" ;;
    tmpcopy) "$VM_GUI" "$VMNAME" screenshot "$QCI_SCENARIO_TMPDIR/t.png" >/dev/null 2>&1
             cp "$QCI_SCENARIO_TMPDIR/t.png" "$A/t-copy.png"; view "$A/t-copy.png" ;;
    crop) magick "$A/s1.png" -crop 400x48+0+0 +repage "$A/bar.png"
          v=$("$VM_GUI" "$VMNAME" view-copy "$A/bar.png" --source "$A/s1.png" --crop 400x48+0+0); view "$v" ;;
    rawcrop) magick "$A/s1.png" -crop 400x48+0+0 +repage "$A/bar.png"; view "$A/bar.png" ;;
esac
python3 "$FIX/make-rollout.py" --sid "$sid" --cwd "$PWD" \
    --out "$CODEX_HOME/sessions/2026/09/25/rollout-2026-09-25T00-00-00-$sid.jsonl" "${calls[@]}"
# F5: an optional per-attempt report (plan file + ".report<N>").
[ -f "$FAKE_PLAN.report$n" ] && cp "$FAKE_PLAN.report$n" "$A/report.md"
echo "$verdict" > "$A/status.txt"
[ "$verdict" = PASS ]
DRV
    chmod +x "$TDIR/bin/fake-driver"
    export QCI_AGENT_CMD="$TDIR/bin/fake-driver {prompt}"
    unset QCI_GUI_RETRY
}

run_scen() {
    printf '%s\n' "$@" > "$FAKE_PLAN"
    gui_run_scenario "$SCEN" "${PROVIDED:-}" 2>"$TDIR/stderr" || true
}

@test "F4: zero-look PASS -> ERROR agent-unviewed-verdict (retriable, report-only by default)" {
    f4_setup
    run_scen "PASS nolook"
    [ "$(cut -f2,4 "$TDIR/attempts")" = "ERROR	agent-unviewed-verdict" ]
    grep -q ' fail ' "$TDIR/results"
    grep -q 'agent-unviewed-verdict' "$TDIR/results"
    grep -q 'would-retry' "$TDIR/flakes"
    grep -Fxq 'reason=observed' "$RDIR/gui/"*.views.txt
}

@test "F4: a zero-look PASS that looks on retry is green, with a flake row" {
    f4_setup
    export QCI_GUI_RETRY=2
    run_scen "PASS nolook" "PASS look"
    [ "$(wc -l < "$TDIR/attempts")" -eq 2 ]
    grep -q ' pass ' "$TDIR/results"
    grep -q 'retried-pass' "$TDIR/flakes"
}

@test "F4: zero-look FAIL -> ERROR, not retriable, never green after retries" {
    f4_setup
    export QCI_GUI_RETRY=3
    run_scen "FAIL nolook" "PASS look" "PASS look"
    [ "$(wc -l < "$TDIR/attempts")" -eq 1 ]
    [ "$(cut -f2,4 "$TDIR/attempts")" = "ERROR	agent-unviewed-verdict" ]
    grep -q ' fail ' "$TDIR/results"
    ! grep -q ' pass ' "$TDIR/results"
}

@test "F4: product FAIL + zero looks + infra (tooling) marker keeps the marker's classification" {
    f4_setup
    run_scen "FAIL tooling"
    [ "$(cut -f2,4 "$TDIR/attempts")" = "ERROR	agent-tooling" ]
}

@test "F4: supplied VM -> a zero-look PASS is never retried" {
    f4_setup
    export QCI_GUI_RETRY=2
    PROVIDED=qci-given-vm run_scen "PASS nolook" "PASS look"
    [ "$(wc -l < "$TDIR/attempts")" -eq 1 ]
    grep -q ' fail ' "$TDIR/results"
}

@test "F4: exhaustion -> fail after every retry is a zero-look PASS" {
    f4_setup
    export QCI_GUI_RETRY=2
    run_scen "PASS nolook" "PASS nolook" "PASS nolook"
    [ "$(wc -l < "$TDIR/attempts")" -eq 3 ]
    grep -q 'classified retry exhausted' "$TDIR/results"
    grep -q 'retried-fail' "$TDIR/flakes"
}

@test "F4: a retry ending in a looked-at FAIL is adopted, not re-rolled" {
    f4_setup
    export QCI_GUI_RETRY=3
    run_scen "PASS nolook" "FAIL look" "PASS look"
    [ "$(wc -l < "$TDIR/attempts")" -eq 2 ]
    [ "$(sed -n 2p "$TDIR/attempts" | cut -f2,4)" = "FAIL	product-fail" ]
    grep -q ' fail ' "$TDIR/results"
}

@test "F4: missing evidence keeps precedence over the view gate" {
    f4_setup
    run_scen "PASS nocapture"
    [ "$(cut -f2,4 "$TDIR/attempts")" = "ERROR	product-error" ]
    grep -q 'visual-evidence contract' "$TDIR/results"
}

@test "F4: opens credited through view-copy, the click manifest, a /tmp copy and a declared crop" {
    f4_setup
    local m
    for m in look viewcopy click tmpcopy crop; do
        : > "$TDIR/attempts"; : > "$TDIR/results"; : > "$FAKE_N"; rm -rf "$RDIR/gui"; mkdir -p "$RDIR/gui"
        run_scen "PASS $m"
        [ "$(cut -f2,4 "$TDIR/attempts")" = "PASS	" ] || { echo "mode $m: $(cat "$TDIR/attempts")" >&2; cat "$RDIR"/gui/*.views.txt >&2; return 1; }
        grep -q ' pass ' "$TDIR/results"
        grep $'^open\t' "$RDIR"/gui/*.views.txt | cut -f5,6 > "$TDIR/credit-$m"
        cp "$TDIR/results" "$TDIR/results-$m"
    done
    # The gate resolves the PNG-only /tmp copy's raw identity through the
    # saved view state: s1 and the copy show one screen (fable r1, P2).
    grep -q 'harness-captured 2 frame(s), 1 distinct' "$TDIR/results-tmpcopy"
    # WHAT each open was credited to, not merely that it was.
    grep -Eq $'^full\t.*/s1\.png$' "$TDIR/credit-look"
    grep -Eq $'^full\t.*/s1\.png$' "$TDIR/credit-viewcopy"
    [ "$(cut -f1 "$TDIR/credit-click" | tr '\n' ' ')" = "full crop " ]
    [ "$(cut -f2 "$TDIR/credit-click" | sort -u | wc -l)" -eq 1 ]
    grep -Eq $'^full\t.*/click-targets/click-001\.raw\.png$' "$TDIR/credit-click"
    # the /tmp copy is credited to the OUT-OF-TREE ledger row by its sha
    grep -Eq $'^full\t.*\.scratch/t\.png$' "$TDIR/credit-tmpcopy"
    grep -Eq $'^crop\t.*/s1\.png$' "$TDIR/credit-crop"
}

@test "F4: an undeclared crop (unknown lineage) earns no credit" {
    f4_setup
    run_scen "PASS rawcrop"
    [ "$(cut -f2,4 "$TDIR/attempts")" = "ERROR	agent-unviewed-verdict" ]
    grep -q 'not-an-attested-frame' "$RDIR"/gui/*.views.txt
}

@test "F4: visual:none and an unobservable attempt are untouched" {
    f4_setup
    printf '%s\n' "PASS nolook" > "$FAKE_PLAN"
    gui_run_scenario "$SCEN_NONE" "" 2>/dev/null || true
    [ "$(cut -f2,4 "$TDIR/attempts")" = "PASS	" ]
    : > "$TDIR/attempts"; : > "$FAKE_N"
    export QCI_AGENT_CMD="$TDIR/bin/fake-driver --ephemeral {prompt}"
    run_scen "PASS nolook"
    [ "$(cut -f2,4 "$TDIR/attempts")" = "PASS	" ]
    grep -q 'unobservable:ephemeral' "$TDIR/results"
}

@test "F4: the classifier flag is an argument; only an original PASS:0 is retriable" {
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    [ "$(gui_classify_failure PASS 0 0 0 0 0 1)" = agent-unviewed-verdict ]
    [ "$(gui_classify_failure FAIL 1 0 0 0 0 1)" = agent-unviewed-verdict ]
    [ "$(gui_classify_failure FAIL 1 0 0 0 0)" = product-fail ]
    gui_classifier_retriable agent-unviewed-verdict PASS:0
    ! gui_classifier_retriable agent-unviewed-verdict FAIL:1
    ! gui_classifier_retriable agent-unviewed-verdict PASS:1
    ! gui_classifier_retriable agent-unviewed-verdict
}

# ---------------------------------------------------------------- F6 --------

@test "F6: the prompts carry the repeat-view rules" {
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    RDIR="$TDIR/run"; mkdir -p "$RDIR/agent-notes"
    local p="$RDIR/agent-notes/t.prompt.md" f
    write_agent_prompt test-vm "$REPO_ROOT/tests/integration/permissions-gui/06-qt-admin-app-mouse.md" \
        "$p" "$RDIR/gui/t" "$RDIR/gui/t.scratch" t
    for f in "$p" "$REPO_ROOT/ci/prompts/gui-scenario-agent.md"; do
        grep -q 'NEVER RE-OPEN A PATH' "$f"
        grep -q 'view-copy <image>' "$f"
        grep -q -- '--source <capture' "$f"
        grep -q 'process state' "$f"
        grep -q 'rejected' "$f"
        grep -q 'same screen pixels' "$f" || grep -q 'SAME SCREEN PIXELS' "$f"
        grep -q '\.post\.png' "$f"
        grep -q 'cp F F.raw' "$f"
    done
}

# ------------------------------------------- astra code review r1 fixes --------

# The frame at $1 must either be absent together with its sidecar, or carry a
# sidecar whose raw_pix_sha matches its own raw pixels (I6).
assert_frame_consistent() {
    local f=$1 rw rh k sha rest
    if [ ! -e "$f" ]; then
        [ ! -e "$f.raw" ] || { echo "orphan sidecar beside missing $f" >&2; return 1; }
        return 0
    fi
    [ -f "$f.raw" ] || { echo "$f has no sidecar" >&2; return 1; }
    read -r rw rh k sha rest < "$f.raw"
    magick "$f" -crop "${rw}x${rh}+0+0" +repage "$TDIR/i6.png"
    [ "$(pix "$TDIR/i6.png")" = "$sha" ] || { echo "I6 broken for $f" >&2; return 1; }
}

@test "review r1.1: a REFUSED capture through capture_virsh_screenshot leaves no frame and no stale sidecar" {
    new_ledger     # bound to $CAPVM
    local cap="$REPO_ROOT/scripts/vm/lib/capture-attest.sh"
    # (a) no existing destination: refused -> nothing at the path
    run bash -c '. "$1"; capture_virsh_screenshot wrongvm "$2"' _ "$cap" "$ADIR/f.png"
    [ "$status" -ne 0 ]
    [ ! -e "$ADIR/f.png" ]
    [ ! -e "$ADIR/f.png.raw" ]
    # (b) an existing attested frame (red), then a refused publication (blue)
    magick -size 64x48 xc:red -define png:exclude-chunks=date,time "$SCREEN"
    run bash -c '. "$1"; capture_virsh_screenshot "$2" "$3"' _ "$cap" "$CAPVM" "$ADIR/g.png"
    [ "$status" -eq 0 ]
    assert_frame_consistent "$ADIR/g.png"
    magick -size 64x48 xc:blue -define png:exclude-chunks=date,time "$SCREEN"
    run bash -c '. "$1"; capture_virsh_screenshot wrongvm "$2"' _ "$cap" "$ADIR/g.png"
    [ "$status" -ne 0 ]
    assert_frame_consistent "$ADIR/g.png"
    # the refused blue bytes are not left behind under the image name
    if [ -e "$ADIR/g.png" ]; then
        [ "$(magick "$ADIR/g.png" -format '%[pixel:p{1,1}]' info:)" != "srgb(0,0,255)" ]
    fi
    [ -z "$(find "$ADIR" -maxdepth 1 -name '.qci-*' ! -name .qci-view-state)" ]
}

@test "review r1.2: simultaneous default view-copies of one source get distinct surviving paths" {
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s1.png" >/dev/null 2>&1
    local i
    for i in $(seq 1 6); do
        ( "$VM_GUI" "$CAPVM" view-copy "$ADIR/s1.png" > "$TDIR/vc-$i.out" 2>"$TDIR/vc-$i.err" ) &
    done
    wait
    cat "$TDIR"/vc-*.out | sort > "$TDIR/paths"
    [ "$(wc -l < "$TDIR/paths")" -eq 6 ]
    [ "$(sort -u "$TDIR/paths" | wc -l)" -eq 6 ]
    local p
    while IFS= read -r p; do
        [ -f "$p" ]
        assert_frame_consistent "$p"
        dims "$p" >> "$TDIR/vdims"; echo >> "$TDIR/vdims"
    done < "$TDIR/paths"
    [ "$(sort -u "$TDIR/vdims" | grep -c .)" -eq 6 ]
    [ -z "$(find "$ADIR" -maxdepth 1 -name '.qci-view-name.*')" ]
    # explicit --out is unchanged: exactly that path
    run "$VM_GUI" "$CAPVM" view-copy "$ADIR/s1.png" --out "$ADIR/explicit.png"
    [ "$output" = "$ADIR/explicit.png" ]
}

# noctalia/03's own crop/hash lines, with /tmp/ redirected into $TDIR.
noct03_block() {  # $1 = step1|step2
    local md="$REPO_ROOT/tests/integration/qdwin-noctalia/03-clock-updates.md"
    case "$1" in
        step1) awk '/^read -r RAW_W/{on=1} on{print} /^\[ -n "\$STEP1_HASH" \]/{exit}' "$md" ;;
        step2) awk '/^  magick \/tmp\/03-step2-advanced.png/{on=1} on{print} on && /step-2 bar crop pixel hash failed/{exit}' "$md" ;;
    esac | sed "s#/tmp/#$TDIR/#g"
}


@test "review r1.3: noctalia/03 compares bar PIXELS, not crop-file timestamps" {
    [ -n "$(noct03_block step1)" ] && [ -n "$(noct03_block step2)" ]
    noct03_block step1 | grep -q qci_view_pix_sha
    "$VM_GUI" "$CAPVM" screenshot "$TDIR/03-step1-now.png" >/dev/null 2>&1
    "$VM_GUI" "$CAPVM" screenshot "$TDIR/03-step2-advanced.png" >/dev/null 2>&1
    # step 1 now, step 2's crop written >1 s later: new PNG timestamps
    bash -c '. "$1"; eval "$2"; echo "$STEP1_HASH" > "$3"; echo "$BAR_CROP" > "$4"' \
        _ "$VIEWLIB" "$(noct03_block step1)" "$TDIR/h1" "$TDIR/barcrop"
    [ "$(cat "$TDIR/barcrop")" = "1280x48+0+0" ]
    cp "$TDIR/03-step1-clock.png" "$TDIR/crop1.png"
    sleep 1.2
    run bash -c '. "$1"; BAR_CROP=$(cat "$3"); eval "$2"; echo "$STEP2_HASH"' _ "$VIEWLIB" "$(noct03_block step2)" "$TDIR/barcrop"
    [ "$status" -eq 0 ]
    # the hazard is real here: the two crop FILES differ ...
    ! cmp -s "$TDIR/crop1.png" "$TDIR/03-step2-clock.png"
    # ... and the scenario's hashes are still equal
    [ "$output" = "$(cat "$TDIR/h1")" ]
    # a pixel change inside the bar makes them unequal
    magick "$SCREEN" -fill red -draw 'point 5,5' -define png:exclude-chunks=date,time "$SCREEN"
    "$VM_GUI" "$CAPVM" screenshot "$TDIR/03-step2-advanced.png" >/dev/null 2>&1
    run bash -c '. "$1"; BAR_CROP=$(cat "$3"); eval "$2"; echo "$STEP2_HASH"' _ "$VIEWLIB" "$(noct03_block step2)" "$TDIR/barcrop"
    [ "$status" -eq 0 ]
    [ "$output" != "$(cat "$TDIR/h1")" ]
}

# --------------------------------- prose image-open diagnostic vs rollout --------

@test "diagnostic: an observed rollout overrides the prose 'never MENTIONED' note" {
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    local req="$REPO_ROOT/tests/integration/permissions-gui/13-cross-user-sendto-deny.md"
    local obs="$TDIR/a.views.txt"
    # pg/13, 2026-09-25: prose count 0, rollout observed 2 credited opens.
    printf 'parser_version=1\nreason=observed\nopens=2\ncredited=2\n' > "$obs"
    [ -z "$(gui_image_opens_diag_note "$req" 0 "$obs")" ]
    # Observed with zero credited opens: the rollout's own diagnostic.
    printf 'parser_version=1\nreason=observed\nopens=0\ncredited=0\n' > "$obs"
    run gui_image_opens_diag_note "$req" 5 "$obs"
    [[ "$output" == *"rollout shows no opened attested frame"* ]]
    [[ "$output" != *MENTIONED* ]]
    # Unobservable / missing / malformed: the prose count is the fallback.
    printf 'parser_version=1\nreason=unobservable:ephemeral\nopens=unobservable\ncredited=unobservable\n' > "$obs"
    run gui_image_opens_diag_note "$req" 0 "$obs"
    [[ "$output" == *"never MENTIONED"* ]]
    [ -z "$(gui_image_opens_diag_note "$req" 3 "$obs")" ]
    run gui_image_opens_diag_note "$req" 0 "$TDIR/missing.views.txt"
    [[ "$output" == *"never MENTIONED"* ]]
    printf 'reason=observed\ncredited=two\n' > "$obs"
    run gui_image_opens_diag_note "$req" 0 "$obs"
    [[ "$output" == *"never MENTIONED"* ]]
    # visual: none scenarios never get the diagnostic.
    local none; none=$(grep -l 'qci:visual: none' "$REPO_ROOT"/tests/integration/permissions-gui/*.md | head -1)
    [ -n "$none" ]
    [ -z "$(gui_image_opens_diag_note "$none" 0 "$TDIR/missing.views.txt")" ]
    # Both attempt paths use the helper; no inline copy of the prose note remains.
    [ "$(grep -c 'gui_image_opens_diag_note "\$scenario"' "$REPO_ROOT/ci/lib/gates/gui.sh")" -eq 2 ]
    [ "$(grep -c 'DIAGNOSTIC: the driver never MENTIONED' "$REPO_ROOT/ci/lib/gates/gui.sh")" -eq 1 ]
}

# ---------------------------------------------------------------- F5 --------
#
# The darkness contradiction DIAGNOSTIC (advisory only). The positive case is
# the recorded class-A misgrade: 2026-09-22 full-20260922T193137Z-881799,
# permissions-gui/06 attempt r2, whose report.md (verbatim in
# fixtures/darkness-claim/pg06-report.md) calls `s3-r2.png` "fully black";
# the frame (fixtures/darkness-claim/pg06-s3-r2.png, md5 4f737201...) measures
# sigma 0.486, bright 0.56. Frames go through the REAL vm-gui (fake virsh), the
# ledger is the real one sealed by the real gui_capture_log_seal, and the
# function under test is the production gui_darkness_contradiction_note.

DARKFIX="${BATS_TEST_DIRNAME}/fixtures/darkness-claim"

dark_setup() {
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    new_ledger
    cp "$DARKFIX/pg06-s3-r2.png" "$SCREEN"
}

# Seal the ledger (once) and run the production diagnostic over $ADIR.
dark_note() {
    local sealed
    sealed=$(gui_capture_log_seal "$CAPLOG" "$CAPVM")
    DNOTE=$(gui_darkness_contradiction_note "$ADIR" "$CAPLOG" "$CAPVM"$'\t'"$sealed" "$ADIR" "$TDIR/agent.log")
}

@test "F5: the recorded pg/06 'fully black' report on a bright attested frame -> DIAGNOSTIC with text, frame and metrics" {
    dark_setup
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3-r2.png" >/dev/null 2>&1
    [ -f "$ADIR/s3-r2.png.raw" ]
    cp "$DARKFIX/pg06-report.md" "$ADIR/report.md"
    echo FAIL > "$ADIR/status.txt"
    dark_note
    [[ "$DNOTE" == "; DIAGNOSTIC: darkness claim contradicted: report.md:7 \"frame was fully black\" names s3-r2.png, whose raw pixels measure sigma=0.485913 bright=0.561448, not dark"* ]]
    [[ "$DNOTE" == *"verdict NOT changed"* ]]
    # exactly one: the "rejected near-black captures" line names no frame
    [ "$(grep -o 'DIAGNOSTIC' <<<"$DNOTE" | wc -l)" -eq 1 ]
    grep -q '^qci_gui_darkness: CONTRADICTION report.md:7 "frame was fully black" names s3-r2.png' "$TDIR/agent.log"
    # nothing written into the evidence tree
    [ -z "$(find "$ADIR" -newer "$ADIR/report.md" -name '*dark*')" ]
}

@test "F5: negations and pane/region-only claims are not whole-frame darkness claims" {
    dark_setup
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3-r2.png" >/dev/null 2>&1
    cat > "$TDIR/lines" <<'EOF'
- S3: the frame was not black this time. Evidence: `s3-r2.png`.
- S3: `s3-r2.png` is not a black frame.
- S3: `s3-r2.png` is a non-black frame; the app is visible.
- S3: the screenshot showed the dialog rather than a black screen (`s3-r2.png`).
- S3: the frame isn't black at all (`s3-r2.png`).
- S3: `s3-r2.png` shows a blank details pane.
- S3: the screenshot shows a black terminal in `s3-r2.png`.
- S3: the details pane of the frame was blank (`s3-r2.png`).
- S3: a black screen area remains at the bottom of `s3-r2.png`.
EOF
    # Each line on its own, against one sealed ledger, so every line is
    # individually shown to be excluded.
    local sealed line bad=0
    sealed=$(gui_capture_log_seal "$CAPLOG" "$CAPVM")
    while IFS= read -r line; do
        printf '%s\n' "$line" > "$ADIR/report.md"
        if [ -n "$(gui_darkness_contradiction_note "$ADIR" "$CAPLOG" "$CAPVM"$'\t'"$sealed" "$ADIR" "$TDIR/agent.log")" ]; then
            echo "flagged: $line" >&2; bad=$((bad + 1))
        fi
    done < "$TDIR/lines"
    [ "$bad" -eq 0 ]
    # control: the same ledger and frame DO produce it for a plain claim
    printf -- '- S3: the frame was fully black. Evidence: `s3-r2.png`.\n' > "$ADIR/report.md"
    [ -n "$(gui_darkness_contradiction_note "$ADIR" "$CAPLOG" "$CAPVM"$'\t'"$sealed" "$ADIR" "$TDIR/agent.log")" ]
}

@test "F5: a rejected capture (*.attempt-N.rejected) never resolves to the attested frame of the same name" {
    dark_setup
    magick -size 1280x800 xc:black "$TDIR/black.png"
    cp "$SCREEN" "$TDIR/bright.png"
    cp "$TDIR/black.png" "$SCREEN"
    QCI_SCREENSHOT_ATTEMPTS=1 "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3.png" >/dev/null 2>&1 || true
    [ -f "$ADIR/s3.png.attempt-1.rejected" ]
    cp "$TDIR/bright.png" "$SCREEN"
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3.png" >/dev/null 2>&1
    # A claim the pattern DOES match; only the whole-token referent rule keeps
    # it off the attested s3.png.
    printf -- '- S3 `s3.png.attempt-1.rejected`: the capture was fully black.\n' > "$ADIR/report.md"
    dark_note
    [ -z "$DNOTE" ]
    # control: naming the attested frame itself does produce the diagnostic
    # (a fresh ledger, since this one is sealed).
    rm -rf "$ADIR"; mkdir -p "$ADIR"; : > "$QCI_GUI_VIEW_STATE"; new_ledger
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3.png" >/dev/null 2>&1
    printf -- '- S3: the capture `s3.png` was fully black.\n' > "$ADIR/report.md"
    dark_note
    [[ "$DNOTE" == *'"capture `s3.png` was fully black" names s3.png, whose raw pixels'* ]]
}

@test "F5: genuinely dark attested frames (noisy black, dark UI) are not contradicted" {
    dark_setup
    # 99.9% black with scattered white: usable (sigma ~0.03) but bright ~0.001.
    magick -size 1280x800 xc:gray50 +noise Random -colorspace gray -threshold 99.9% "$SCREEN"
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/noisy.png" >/dev/null 2>&1
    [ -f "$ADIR/noisy.png" ]
    # a legible dark UI (lock-screen shape): varied, but under FRAME_BRIGHT_MIN.
    magick -size 1280x800 xc:'#101418' -fill white -pointsize 60 -annotate +500+400 '12:34' "$SCREEN"
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/lock.png" >/dev/null 2>&1
    [ -f "$ADIR/lock.png" ]
    printf -- '- `noisy.png` is a fully black frame.\n- `lock.png`: the screen was near-black.\n' > "$ADIR/report.md"
    dark_note
    [ -z "$DNOTE" ]
}

@test "F5: a frame not attested in this attempt, an unsealed ledger, and no ImageMagick -> no diagnostic" {
    dark_setup
    "$VM_GUI" "$CAPVM" screenshot "$ADIR/s3-r2.png" >/dev/null 2>&1
    # an agent-authored bright image the harness never captured
    magick -size 1280x800 xc:white -fill black -draw 'rectangle 100,100 900,500' "$ADIR/own.png"
    local sealed out
    sealed=$(gui_capture_log_seal "$CAPLOG" "$CAPVM")
    printf -- '- the frame `own.png` was fully black.\n' > "$ADIR/report.md"
    [ -z "$(gui_darkness_contradiction_note "$ADIR" "$CAPLOG" "$CAPVM"$'\t'"$sealed" "$ADIR" "$TDIR/agent.log")" ]
    # unsealed: no anchor -> nothing is attested; a reason is logged
    printf -- '- the frame `s3-r2.png` was fully black.\n' > "$ADIR/report.md"
    : > "$TDIR/agent.log"
    [ -z "$(gui_darkness_contradiction_note "$ADIR" "$CAPLOG" "" "$ADIR" "$TDIR/agent.log")" ]
    grep -q '^qci_gui_darkness: skipped: the capture ledger is unsealed' "$TDIR/agent.log"
    # no ImageMagick: a PATH with every tool but magick
    mkdir -p "$TDIR/nomagick"
    local t
    for t in /usr/bin/* /bin/*; do
        [ "${t##*/}" = magick ] || ln -sf "$t" "$TDIR/nomagick/${t##*/}" 2>/dev/null || true
    done
    : > "$TDIR/agent.log"
    out=$(PATH="$TDIR/nomagick" gui_darkness_contradiction_note "$ADIR" "$CAPLOG" "$CAPVM"$'\t'"$sealed" "$ADIR" "$TDIR/agent.log")
    [ -z "$out" ]
    grep -q "^qci_gui_darkness: skipped: ImageMagick 'magick' is not installed" "$TDIR/agent.log"
    # and the same sealed ledger with magick back: the diagnostic fires
    out=$(gui_darkness_contradiction_note "$ADIR" "$CAPLOG" "$CAPVM"$'\t'"$sealed" "$ADIR" "$TDIR/agent.log")
    [[ "$out" == *'"frame `s3-r2.png` was fully black" names s3-r2.png'* ]]
}

# The INVARIANT, through the real gui_run_scenario on both attempt paths: the
# diagnostic lands in the result note and changes no attempt row (status,
# classifier), no retry decision and no verdict. Each case is compared with the
# SAME plan run without the report.
f5_run() {
    local tag=$1; shift
    : > "$TDIR/attempts"; : > "$TDIR/results"; : > "$TDIR/flakes"; : > "$FAKE_N"
    rm -rf "$RDIR/gui"; mkdir -p "$RDIR/gui"
    run_scen "$@"
    cut -f1-4 "$TDIR/attempts" > "$TDIR/attempts-$tag"
    cut -d' ' -f1-4 "$TDIR/results" > "$TDIR/verdict-$tag"
    cut -d' ' -f1-8 "$TDIR/flakes" > "$TDIR/flakes-$tag"
    cp "$TDIR/results" "$TDIR/results-$tag"
}

@test "F5: invariant -- first attempt and retry: diagnostic recorded, status/classifier/retry/verdict unchanged" {
    f4_setup
    cp "$DARKFIX/pg06-s3-r2.png" "$SCREEN"
    sed 's/s3-r2\.png/s1.png/g' "$DARKFIX/pg06-report.md" > "$TDIR/claim.md"
    local plan
    for plan in "FAIL look" "PASS look"; do
        rm -f "$FAKE_PLAN".report*
        f5_run ctl "$plan"
        cp "$TDIR/claim.md" "$FAKE_PLAN.report1"
        f5_run dia "$plan"
        grep -q 'darkness claim contradicted: report.md:7 "frame was fully black" names s1.png' "$TDIR/results-dia"
        ! grep -q 'darkness claim' "$TDIR/results-ctl"
        cmp "$TDIR/attempts-ctl" "$TDIR/attempts-dia"
        cmp "$TDIR/verdict-ctl" "$TDIR/verdict-dia"
        cmp "$TDIR/flakes-ctl" "$TDIR/flakes-dia"
    done
    [ "$(cut -f2,4 "$TDIR/attempts-dia")" = "PASS	" ]
    grep -Fq 'qci_gui_darkness: CONTRADICTION' "$RDIR/gui/"*.agent.log
    # RETRY PATH: attempt 1 is a zero-look PASS (retriable), attempt 2 looks
    # and FAILs with the claim. The claim must not stop, extend or re-classify.
    export QCI_GUI_RETRY=2
    rm -f "$FAKE_PLAN".report*
    f5_run ctl "PASS nolook" "FAIL look" "PASS look"
    cp "$TDIR/claim.md" "$FAKE_PLAN.report2"
    f5_run dia "PASS nolook" "FAIL look" "PASS look"
    [ "$(wc -l < "$TDIR/attempts-dia")" -eq 2 ]
    [ "$(sed -n 2p "$TDIR/attempts-dia" | cut -f2,4)" = "FAIL	product-fail" ]
    grep -q 'darkness claim contradicted' "$TDIR/results-dia"
    grep -q ' fail ' "$TDIR/results-dia"
    cmp "$TDIR/attempts-ctl" "$TDIR/attempts-dia"
    cmp "$TDIR/verdict-ctl" "$TDIR/verdict-dia"
    cmp "$TDIR/flakes-ctl" "$TDIR/flakes-dia"
    grep -Fq 'qci_gui_darkness: CONTRADICTION' "$RDIR/gui/"*.retry1.agent.log
    # Both attempt paths call the one helper.
    [ "$(grep -c 'gui_darkness_contradiction_note "\$adir' "$REPO_ROOT/ci/lib/gates/gui.sh")" -eq 2 ]
}
