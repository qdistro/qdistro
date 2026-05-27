#!/usr/bin/env bats
# Tier-2 hardening lock-in test.
#
# Asserts the load-bearing isolation invariants inside a live tier-2
# container. Catches regressions from someone loosening a flag during
# debugging and forgetting to revert.
#
# Invariants (mirrors all s40 driver assertions):
#   1. CapEff = 0                    (--cap-drop=ALL)
#   2. NoNewPrivs = 1                (--security-opt=no-new-privileges)
#   3. Only `lo` in /sys/class/net/  (--network=none)
#   4. Root mount is read-only       (--read-only)
#   5. touch / blocked               (cross-check of read-only)
#   6. /run/user/ contains only allowed sockets/logs
#   7. No bus/pipewire-pulse/ssh-agent/gnupg sockets in /run/user/
#   8. qdistro_tier2_token label set (orphan-dir reaper)
#
# Relies on the existing s40-tier2-hardening.sh driver which already
# runs inside the VM. This bats file wraps it in the standard
# stage_vm_driver + vm_run pattern so it integrates with the bats
# suite.

load helpers

# Stage the in-VM driver script onto the host's bats http server
# (port 8768 by convention) so the VM can fetch it. Mirrors the
# self-staging pattern in tiered-isolation.bats.
stage_vm_driver() {
    local script_name="$1"
    local script_path
    script_path="$(dirname "$BATS_TEST_FILENAME")/$script_name"
    [ -f "$script_path" ] || fail_loud "driver script not found at $script_path"

    cp "$script_path" "$(dirname "$BATS_TEST_FILENAME")/../"

    if ! ss -tln 2>/dev/null | grep -q ":8768 "; then
        local stage_dir
        stage_dir="$(dirname "$BATS_TEST_FILENAME")/.."
        (
            cd "$stage_dir" || exit 1
            nohup python3 -m http.server 8768 \
                >/tmp/qdistro-bats-http.log 2>&1 </dev/null 3>&- 4>&- 5>&- &
            disown "$!" 2>/dev/null || true
        )
        sleep 1
    fi
}

setup() {
    # Ensure outer compositor is up.
    vm_run "test -S /run/user/1000/wayland-1"
    assert_success
}

@test "tier2-hardening-lockin: CapEff=0, NoNewPrivs=1, lo-only, ro-root, no leaked sockets" {
    stage_vm_driver "s40-tier2-hardening.sh"
    vm_run "curl -s -o /tmp/s40-lockin.sh http://10.0.2.2:8768/s40-tier2-hardening.sh && chmod +x /tmp/s40-lockin.sh && bash /tmp/s40-lockin.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman / tier-2 image / outer compositor absent on this VM"
    fi

    # Invariant 1: all capabilities dropped
    assert_output_contains "PASS: CapEff=0 (--cap-drop=ALL effective)"

    # Invariant 2: no-new-privileges active
    assert_output_contains "PASS: NoNewPrivs=1 (no-new-privileges effective)"

    # Invariant 3: network isolation — only loopback
    assert_output_contains "PASS: network=none (only lo present)"

    # Invariant 4: read-only root filesystem
    assert_output_contains "PASS: rootfs mounted read-only"
    assert_output_contains "PASS: touch / blocked by read-only"

    # Invariant 5: no host secrets leaked into container runtime dir
    assert_output_contains "PASS: /run/user/1000/ contains only allowed sockets/logs"
    assert_output_contains "PASS: no host bus/pulse/gnupg/ssh-agent in /run/user/1000/"

    # Container label (orphan-dir reaper depends on this)
    assert_output_contains "PASS: qdistro_tier2_token label set"

    assert_output_contains "PASS: §Phase-7 tier-2 hardening invariants enforced"
}
