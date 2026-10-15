#!/usr/bin/env bats
#
# Host-only contract for scripts/vm/vm-gui after the labwc lane was retired.
#
# Carried forward from vm-gui-labwc-refresh.bats ("vm-gui takes no host frame
# outside capture_presented_shot"): every host frame vm-gui takes still goes
# through ONE sanctioned function -- now capture-attest.sh's
# capture_virsh_shot, the labwc refresh in front of it being gone -- and is then
# published with its ledger row; there is no raw `virsh screenshot` in vm-gui.
# Also pinned: the X11 window-management actions are gone, and keyboard input
# without ydotool fails loudly instead of falling back to xdotool.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    VM_GUI_SRC="$REPO_ROOT/scripts/vm/vm-gui"
}

@test "vm-gui takes every host frame through capture_virsh_shot, nowhere else" {
    # Exactly the four capture sites: screenshot, screenshot-fresh,
    # click-preview (raw) and click-confirm (post).
    run grep -c 'capture_virsh_shot "\$VM" ' "$VM_GUI_SRC"
    [ "$output" -eq 4 ]
    grep -q 'capture_virsh_shot "\$VM" "\$candidate"' "$VM_GUI_SRC"
    grep -q 'capture_virsh_shot "\$VM" "\$scratch/raw.png"' "$VM_GUI_SRC"
    grep -q 'capture_virsh_shot "\$VM" "\$scratch/post.png"' "$VM_GUI_SRC"
    # ... and no frame is taken any other way.
    run grep -nE '^[^#]*virsh[^#]* screenshot' "$VM_GUI_SRC"
    [ "$status" -ne 0 ] || { echo "raw virsh screenshot in vm-gui: $output" >&2; return 1; }
    # The function is the shared attestation library's, not a local copy.
    grep -q '^capture_virsh_shot() {' "$REPO_ROOT/scripts/vm/lib/capture-attest.sh"
    run grep -n '^capture_virsh_shot() {' "$VM_GUI_SRC"
    [ "$status" -ne 0 ]
}

@test "vm-gui has no X11 path left" {
    run grep -nE '^[^#]*(xdotool|DISPLAY=:0|run_as_jan)' "$VM_GUI_SRC"
    [ "$status" -ne 0 ] || { echo "$output" >&2; return 1; }
    local cmd
    for cmd in start activate windowsize windowmove; do
        run grep -nE "^    $cmd\)" "$VM_GUI_SRC"
        [ "$status" -ne 0 ] || { echo "vm-gui still dispatches '$cmd'" >&2; return 1; }
    done
}

@test "type/key without ydotool fail loudly; no silent X11 fallback" {
    local d="$BATS_TEST_TMPDIR/vm"
    mkdir -p "$d/lib"
    cp "$VM_GUI_SRC" "$d/vm-gui"
    cp -r "$REPO_ROOT/scripts/vm/lib/." "$d/lib/"
    # The guest has no ydotool: the probe fails. Any OTHER guest command would
    # be a fallback being tried -- record it so the test can see it.
    cat > "$d/vm-exec" <<'SHEOF'
#!/bin/bash
printf '%s\n' "$2" >> "$(dirname "$0")/calls"
exit 1
SHEOF
    chmod +x "$d/vm-exec"
    run "$d/vm-gui" test-vm key ctrl+n
    [ "$status" -eq 2 ]
    [[ "$output" == *"needs ydotool"*"there is no X11 fallback"* ]]
    run "$d/vm-gui" test-vm type hello
    [ "$status" -eq 2 ]
    [ "$(grep -vc 'command -v ydotool' "$d/calls")" -eq 0 ]
    run "$d/vm-gui" test-vm activate 'admin approvals'
    [ "$status" -ne 0 ]
    [[ "$output" == *"Unknown command: activate"* ]]
}
