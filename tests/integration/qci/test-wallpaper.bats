#!/usr/bin/env bats
# The GUI golden's patterned desktop wallpaper. qdshell ships
# `wallpaper.enabled: true` with an empty directory (~/Pictures/Wallpapers),
# so without it the idle desktop is flat black and vm-gui/qdwin captures of a
# healthy desktop look like a dead compositor. scripts/vm/spin-test-vm-gui.sh
# carries the one generator (install_test_wallpaper);
# scripts/vm/assets/make-test-wallpaper.sh extracts and runs it.
# What only a provisioned VM can show (qdshell actually painting it) is
# verified live, not here.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SPINNER="$REPO_ROOT/scripts/vm/spin-test-vm-gui.sh"
    TDIR="$(mktemp -d)"
}

teardown() { rm -rf "$TDIR"; }

@test "the wallpaper generator is still extractable after moving into the shared function" {
    local spin_out="$TDIR/wp.png"
    bash "$REPO_ROOT/scripts/vm/assets/make-test-wallpaper.sh" "$spin_out"
    [ "$(head -c 8 "$spin_out" | od -An -tx1 | tr -d ' \n')" = 89504e470d0a1a0a ]
}

@test "the golden bake renders the wallpaper where qdshell reads it" {
    # Defined once, rendered into ~admin/Pictures/Wallpapers (qdshell's
    # default wallpaper directory), and CALLED in the bake -- not just defined.
    [ "$(grep -c '^install_test_wallpaper() {$' "$SPINNER")" -eq 1 ]
    grep -Fq 'runuser -u admin -- python3 - /home/admin/Pictures/Wallpapers/qdistro-test-pattern.png' "$SPINNER"
    grep -Eq '^install_test_wallpaper$' "$SPINNER"
}
