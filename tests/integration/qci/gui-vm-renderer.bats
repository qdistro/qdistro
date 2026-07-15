#!/usr/bin/env bats

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SPINNER="$REPO_ROOT/scripts/vm/spin-test-vm-gui.sh"
}

@test "qdwin GUI workers pin pixman and restart the prestarted compositor" {
    grep -Fq "sed -i 's/^renderer=.*/renderer=pixman/'" "$SPINNER"
    grep -Fq "grep -qx 'renderer=pixman' /home/admin/weston.ini" "$SPINNER"
    grep -Fq "systemctl --user restart qdwin-compositor.service" "$SPINNER"
}

@test "qdwin GUI clone verification fails closed without the pixman pin" {
    local clone_block
    clone_block=$(sed -n '/GOLDEN-CLONE FAST-PATH/,/The baked install surface/p' "$SPINNER")
    [[ "$clone_block" == *"renderer=pixman"* ]]
}
