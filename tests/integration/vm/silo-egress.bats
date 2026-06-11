#!/usr/bin/env bats
# VM-gated functional test for per-silo netns + WireGuard egress
# (todo/fable-networking task 3). Stages probes/silo-egress-probe.sh over the
# bats http server, runs it as root inside the VM, and asserts each PASS line.
#
# The probe drives the REAL production egress backend
# (qdistro_silo_egress.EgressBackend) against real network namespaces + a
# loopback WireGuard peer pair — proving the kill-switch, DNS-no-leak,
# none-dark, and legacy-unchanged contract properties without an external VPN.
#
# Requires the VM bake to provide wireguard-tools + nftables + the wireguard
# kernel module (the probe fails loudly with an actionable message otherwise).
load helpers

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

@test "task3: per-silo netns + WireGuard egress kill-switch end-to-end" {
    stage_vm_driver "probes/silo-egress-probe.sh"
    vm_run "curl -s -o /tmp/silo-egress-probe.sh http://10.0.2.2:8768/silo-egress-probe.sh && chmod +x /tmp/silo-egress-probe.sh && bash /tmp/silo-egress-probe.sh"
    assert_success

    assert_output_contains "PASS: preconditions: wg + nft + wireguard module + egress backend present"
    assert_output_contains "PASS: loopback wg server up on 10.7.0.1"
    assert_output_contains "PASS: EgressBackend.apply(wg:egtest) brought up the silo netns"
    assert_output_contains "PASS: wg silo netns holds only wg-2099 + lo; default route is the tunnel"
    assert_output_contains "PASS: wg silo reaches the tunnel peer (10.7.0.1) when the tunnel is up"
    assert_output_contains "PASS: wg silo cannot reach raw WAN (egress confined to the tunnel)"
    assert_output_contains "PASS: wg silo resolver is the tunnel-bound DNS (10.7.0.1)"
    assert_output_contains "PASS: init-netns nft backstop drops stray uid-2099 traffic"
    assert_output_contains "PASS: tunnel down => silo dark with no raw-WAN fallback (kill-switch by construction)"
    assert_output_contains "PASS: none silo has no route and reaches nothing (default-deny)"
    assert_output_contains "PASS: two silos see only their own resolver (no cross-silo DNS leak)"
    assert_output_contains "PASS: teardown removed the egress devices and the nft backstop"
    assert_output_contains "PASS: legacy (egress unset) silo has no netns — host networking unchanged"
    assert_output_contains "PASS: § task3 per-silo netns + WireGuard egress end-to-end"
}
