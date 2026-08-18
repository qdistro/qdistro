#!/usr/bin/env bats

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    VM_GUI="$REPO_ROOT/scripts/vm/vm-gui"
    ARTIFACT_DIR="$BATS_TEST_TMPDIR/artifacts"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    FAKE_SCREENSHOT="$BATS_TEST_TMPDIR/screen.png"
    FAKE_VIRSH_LOG="$BATS_TEST_TMPDIR/virsh.log"
    mkdir -p "$ARTIFACT_DIR" "$FAKE_BIN"
    : > "$FAKE_VIRSH_LOG"

    command -v magick >/dev/null 2>&1 || skip "ImageMagick magick not installed"
    magick -size 1280x800 xc:'#20252b' \
        -fill white -draw 'rectangle 430,480 650,560' "$FAKE_SCREENSHOT"

    cat > "$FAKE_BIN/virsh" <<'EOF'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$FAKE_VIRSH_LOG"
case " $* " in
    *" screenshot "*)
        out=${!#}
        cp "$FAKE_SCREENSHOT" "$out"
        ;;
    *" qemu-monitor-command "*)
        ;;
    *)
        echo "unexpected fake virsh invocation: $*" >&2
        exit 2
        ;;
esac
EOF
    chmod +x "$FAKE_BIN/virsh"
    export PATH="$FAKE_BIN:$PATH"
    export FAKE_SCREENSHOT FAKE_VIRSH_LOG
    export QCI_GUI_ARTIFACT_DIR="$ARTIFACT_DIR"
}

preview_manifest() {
    printf '%s/click-targets/click-001.preview\n' "$ARTIFACT_DIR"
}

@test "click-preview moves the pointer without a button event and draws review evidence" {
    run "$VM_GUI" test-vm click-preview 490 522 "Forever prefix radio"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }

    local manifest target_dir raw annotated zoom
    manifest=$(preview_manifest)
    target_dir="$ARTIFACT_DIR/click-targets"
    raw="$target_dir/click-001.raw.png"
    annotated="$target_dir/click-001.annotated.png"
    zoom="$target_dir/click-001.zoom.png"
    [ -f "$manifest" ]
    [ -f "$raw" ]
    [ -f "$annotated" ]
    [ -f "$zoom" ]
    [ "$(magick identify -format '%wx%h' "$annotated")" = 1280x800 ]
    [ "$(magick identify -format '%wx%h' "$zoom")" = 540x420 ]
    [ "$(sha256sum "$raw" | awk '{print $1}')" != "$(sha256sum "$annotated" | awk '{print $1}')" ]
    grep -Fxq 'x=490' "$manifest"
    grep -Fxq 'y=522' "$manifest"
    grep -Fxq 'pointer_moved_before_capture=1' "$manifest"
    grep -Fxq 'button_events_before_confirmation=0' "$manifest"
    grep -Fq $'click-001\tpreview\ttest-vm\t490\t522\t1280\t800' "$target_dir/clicks.tsv"
    grep -Fq $'\tForever prefix radio\t' "$target_dir/clicks.tsv"
    grep -q ' screenshot ' "$FAKE_VIRSH_LOG"
    [ "$(grep -c 'qemu-monitor-command' "$FAKE_VIRSH_LOG")" -eq 1 ]
    ! grep -q '"type":"btn"' "$FAKE_VIRSH_LOG"
}

@test "click-confirm injects the reviewed coordinates and records post-click evidence" {
    "$VM_GUI" test-vm click-preview 490 522 "Forever prefix radio" >/dev/null
    local manifest target_dir
    manifest=$(preview_manifest)
    target_dir="$ARTIFACT_DIR/click-targets"

    run "$VM_GUI" test-vm click-confirm "$manifest"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"confirmed click click-001: x=490 y=522"* ]]
    [ -f "$target_dir/click-001.confirmed" ]
    [ -f "$target_dir/click-001.post.png" ]
    grep -Fq $'click-001\tconfirmed\ttest-vm\t490\t522\t1280\t800' "$target_dir/clicks.tsv"
    [ "$(grep -c 'qemu-monitor-command' "$FAKE_VIRSH_LOG")" -eq 4 ]

    run "$VM_GUI" test-vm click-confirm "$manifest"
    [ "$status" -eq 2 ]
    [[ "$output" == *"already confirmed"* ]]
    [ "$(grep -c 'qemu-monitor-command' "$FAKE_VIRSH_LOG")" -eq 4 ]
}

@test "click-preview rejects coordinates outside the captured frame" {
    run "$VM_GUI" test-vm click-preview 1280 522 "outside"
    [ "$status" -eq 2 ]
    [[ "$output" == *"outside screenshot bounds"* ]]
    ! grep -q 'qemu-monitor-command' "$FAKE_VIRSH_LOG"
}

@test "click-preview rejects a framebuffer that disagrees with QMP mapping" {
    magick -size 1024x768 xc:black "$FAKE_SCREENSHOT"
    run "$VM_GUI" test-vm click-preview 400 300 "wrong-size"
    [ "$status" -eq 2 ]
    [[ "$output" == *"screenshot is 1024x768"* ]]
    [[ "$output" == *"QMP click mapping expects 1280x800"* ]]
    [ "$(grep -c 'qemu-monitor-command' "$FAKE_VIRSH_LOG")" -eq 1 ]
    ! grep -q '"type":"btn"' "$FAKE_VIRSH_LOG"
}

@test "generated GUI prompt requires preview review before click confirmation" {
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    RDIR="$BATS_TEST_TMPDIR/run"
    mkdir -p "$RDIR/agent-notes"
    local prompt="$RDIR/agent-notes/test.prompt.md"
    local scenario="$REPO_ROOT/tests/integration/permissions-gui/06-qt-admin-app-mouse.md"
    write_agent_prompt test-vm "$scenario" "$prompt" \
        "$RDIR/gui/test" "$RDIR/gui/test.scratch" test
    grep -Fq 'click-preview X Y "visible target label"' "$prompt"
    grep -Fq 'Read BOTH ImageMagick outputs' "$prompt"
    grep -Fq 'click-confirm <preview-manifest>' "$prompt"
    grep -Fq 'A preview moves' "$prompt"
    grep -Fq 'but never clicks' "$prompt"
}

@test "GUI workers export the artifact directory used by click previews" {
    # Contract (post short-alias harvest): agents get QCI_GUI_ARTIFACT_DIR set to
    # the short real art_alias directory under /tmp/qci-gui-art/... (not the long
    # canonical adir). Harvest still grades adir after recovery. Primary attempt uses
    # art_alias; retry attempts use art_aliasN.
    grep -Fq 'QCI_GUI_ARTIFACT_DIR="$art_alias"' "$REPO_ROOT/ci/lib/gates/gui.sh"
    grep -Fq 'QCI_GUI_ARTIFACT_DIR="$art_aliasN"' "$REPO_ROOT/ci/lib/gates/gui.sh"
    # Alias is produced from the canonical adir (must stay wired).
    grep -Eq 'art_alias=\$\(gui_make_artifact_alias "\$adir"\)' "$REPO_ROOT/ci/lib/gates/gui.sh"
    grep -Eq 'art_aliasN=\$\(gui_make_artifact_alias "\$adirN"\)' "$REPO_ROOT/ci/lib/gates/gui.sh"
    # Must not regress to exporting the long canonical path to agents.
    ! grep -Fq 'QCI_GUI_ARTIFACT_DIR="$adir"' "$REPO_ROOT/ci/lib/gates/gui.sh"
    ! grep -Fq 'QCI_GUI_ARTIFACT_DIR="$adirN"' "$REPO_ROOT/ci/lib/gates/gui.sh"
}
