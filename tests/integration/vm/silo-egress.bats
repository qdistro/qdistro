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

# Stager HTTP port. Overridable so a run on a shared host can dodge a foreign
# process already squatting the default (the previous `ss | grep :8768` reuse
# check could silently serve another user's directory — a 404 then reaches the
# VM as the "probe", failing with a DOCTYPE-HTML syntax error). Pick a free port
# with QDISTRO_BATS_HTTP_PORT=NNNN before invoking bats.
: "${QDISTRO_BATS_HTTP_PORT:=8768}"

stage_vm_driver() {
    local script_name="$1"
    local script_path staged stage_dir
    script_path="$(dirname "$BATS_TEST_FILENAME")/$script_name"
    [ -f "$script_path" ] || fail_loud "driver script not found at $script_path"
    stage_dir="$(dirname "$BATS_TEST_FILENAME")/.."
    staged="$stage_dir/$(basename "$script_name")"
    cp "$script_path" "$staged"
    if ! ss -tln 2>/dev/null | grep -q ":${QDISTRO_BATS_HTTP_PORT} "; then
        (
            cd "$stage_dir" || exit 1
            nohup python3 -m http.server "$QDISTRO_BATS_HTTP_PORT" \
                >"${TMPDIR:-/tmp}/qdistro-bats-http-$(id -u).log" 2>&1 \
                </dev/null 3>&- 4>&- 5>&- &
            disown "$!" 2>/dev/null || true
        )
        sleep 1
    fi
    # Validate from the host that the port actually serves OUR staged script and
    # not a foreign process squatting it (the reuse check above trusts any
    # listener). A mismatch here is the squatted-port case — fail loudly with the
    # remedy instead of shipping a 404 to the VM as the "probe".
    local url="http://127.0.0.1:${QDISTRO_BATS_HTTP_PORT}/$(basename "$script_name")"
    if ! curl -fsS "$url" 2>/dev/null | cmp -s - "$staged"; then
        fail_loud "port ${QDISTRO_BATS_HTTP_PORT} is not serving the staged driver \
(foreign listener?). Set QDISTRO_BATS_HTTP_PORT to a free port and re-run."
    fi
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
