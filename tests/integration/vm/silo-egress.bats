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
# Requires the VM bake to provide wireguard-tools + nftables + dnsmasq + the
# wireguard kernel module (the probe fails loudly with an actionable message
# otherwise). dnsmasq backs the `direct`-egress per-silo resolver (Opt 3-A).
load helpers

# Drivers stage themselves via the shared free-port stage_vm_driver (helpers.bash),
# which content-validates the port and exports QDISTRO_BATS_HTTP_PORT.
teardown_file() {
    reap_vm_drivers
}

@test "task3: per-silo netns + WireGuard egress kill-switch end-to-end" {
    stage_vm_driver "probes/silo-egress-probe.sh"
    # -fsS: a 404/HTTP error becomes a curl failure (non-zero), not an
    # HTML body saved as the "script" that then dies on a shell parse error.
    vm_run "curl -fsS -o /tmp/silo-egress-probe.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/silo-egress-probe.sh && chmod +x /tmp/silo-egress-probe.sh && bash /tmp/silo-egress-probe.sh"
    assert_success

    assert_output_contains "PASS: preconditions: wg + nft + wireguard module + egress backend present"
    assert_output_contains "PASS: loopback wg server up on 10.7.0.1"
    assert_output_contains "PASS: EgressBackend.apply(wg:egtest) brought up the silo netns"
    assert_output_contains "PASS: wg silo netns holds only wg-2099 + lo; default route is the tunnel"
    assert_output_contains "PASS: wg silo reaches the tunnel peer (10.7.0.1) when the tunnel is up"
    # Prefix only: the probe appends the gateway IP when a positive WAN control
    # is reachable ("...raw WAN 10.0.2.2 (egress...)") and omits it otherwise.
    assert_output_contains "PASS: wg silo cannot reach raw WAN"
    assert_output_contains "PASS: wg silo resolver is the tunnel-bound DNS (10.7.0.1)"
    assert_output_contains "PASS: init-netns nft backstop drops stray uid-2099 traffic"
    assert_output_contains "PASS: tunnel down => silo dark with no raw-WAN fallback (kill-switch by construction)"
    assert_output_contains "PASS: none silo has no route and reaches nothing (default-deny)"
    assert_output_contains "PASS: EgressBackend.apply(direct) brought up the routed silo netns"
    assert_output_contains "PASS: direct silo forwards, routes via the veth gateway, resolves via"
    assert_output_contains "PASS: direct silo per-silo resolver (dnsmasq) is listening on"
    assert_output_contains "PASS: direct silo forwards to a non-bogon peer"
    assert_output_contains "PASS: direct silo cannot reach another silo"
    assert_output_contains "PASS: direct silo cannot reach the host (input chain protects host services)"
    assert_output_contains "PASS: direct silo per-silo resolver answers DNS on"
    assert_output_contains "PASS: nft fwd + input isolation rules are installed"
    assert_output_contains "PASS: direct teardown stopped the resolver and dropped NAT/forward enrolment"
    assert_output_contains "PASS: two silos see only their own resolver (no cross-silo DNS leak)"
    assert_output_contains "PASS: teardown removed the egress devices and the nft backstop"
    assert_output_contains "PASS: legacy (egress unset) silo has no netns — host networking unchanged"
    assert_output_contains "PASS: § task3 per-silo netns + WireGuard egress end-to-end"
}
