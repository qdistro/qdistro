#!/usr/bin/env bats
#
# Host-only regression guard for the QDWIN_APP_DEPS host->spinner->guest plumbing
# (scripts/vm/spin-test-vm.sh). The opt-in that installs the qdwin app-test
# desktop apps (foot/xterm/gnome-text-editor/...) was DEAD PLUMBING: the stage-5
# in-guest fresh-vm-bootstrap invocation forwarded only QDISTRO_HTTP_HOST and
# QDISTRO_BUILD_TIER2_IMAGES, so QDWIN_APP_DEPS set on the host never reached the
# guest and every golden built lean. This pins the forwarding so it can't rot
# back to dead plumbing. NO VM is booted (static source assertion).

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SPINNER="$REPO_ROOT/scripts/vm/spin-test-vm.sh"
}

@test "spin-test-vm.sh forwards QDWIN_APP_DEPS into the in-guest bootstrap env" {
    [ -f "$SPINNER" ] || skip "spinner not found at $SPINNER"
    # The stage-5 vm-exec line that runs fresh-vm-bootstrap.sh must carry
    # QDWIN_APP_DEPS alongside the existing forwarded knobs.
    run grep -nE "fresh-vm-bootstrap\.sh" "$SPINNER"
    [ "$status" -eq 0 ]
    # The bootstrap invocation line embeds QDWIN_APP_DEPS in its env prefix.
    run grep -E "QDWIN_APP_DEPS='?\\\$_APP_DEPS'?.*bash /root/fresh-vm-bootstrap\.sh" "$SPINNER"
    [ "$status" -eq 0 ]
}

@test "spin-test-vm.sh normalizes QDWIN_APP_DEPS to a bare 0/1" {
    [ -f "$SPINNER" ] || skip "spinner not found at $SPINNER"
    run grep -E "_APP_DEPS=1" "$SPINNER"
    [ "$status" -eq 0 ]
}
