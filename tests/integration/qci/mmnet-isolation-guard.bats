#!/usr/bin/env bats
#
# Host-only isolation guards for the multi-machine VM lane (mmnet). NO VM is
# booted and NO inter-VM segment is created — these are pure host checks over the
# scripts, the template domain XML, and the qci gate wiring. They exist to make
# the central isolation promise of todo/decisions/vm-multimachine-test-infra.md
# a REGRESSION-PROOF invariant:
#
#   "Single-machine tests MUST NOT require the extra network or multi-VM setup —
#    keep the default single-machine lane simple and unchanged."
#
# Concretely, a careless future edit must not let the second (udp socket) NIC leak
# into the default single-machine clone path, and the opt-in smoke test must not
# leak into the default bats glob. Each @test below fails loudly if it does.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    VM="$REPO_ROOT/scripts/vm"
}

@test "mmnet: clone-baseweed default path adds NO extra NIC (socket NIC only via --extra-nic-xml)" {
    # The socket-NIC splice block must be gated strictly behind a non-empty
    # EXTRA_NIC_XML, which is set ONLY by the --extra-nic-xml flag. Assert both:
    #   (a) the splice is inside `if [ -n "$EXTRA_NIC_XML" ]`,
    #   (b) EXTRA_NIC_XML is assigned ONLY from the --extra-nic-xml CLI flag
    #       (never sourced/inferred from the environment or mmnet config).
    # Catch any assignment form, including `:=` default-assignment that could
    # source a value from the environment.
    run grep -nE 'EXTRA_NIC_XML[:]?=' "$VM/clone-baseweed.sh"
    [ "$status" -eq 0 ]
    # Every assignment is either the empty init or the CLI-flag extraction.
    while IFS= read -r line; do
        case "$line" in
            *'EXTRA_NIC_XML=""'*) : ;;                       # init
            *'--extra-nic-xml=*)'*) : ;;                     # CLI flag case label
            *'EXTRA_NIC_XML="${arg#*=}"'*) : ;;              # CLI flag extraction
            *) echo "unexpected EXTRA_NIC_XML assignment (env leak risk): $line" >&2; return 1 ;;
        esac
    done <<<"$output"

    # The socket-NIC interface splice must be guarded by the EXTRA_NIC_XML test.
    run grep -c 'if \[ -n "\$EXTRA_NIC_XML" \]' "$VM/clone-baseweed.sh"
    [ "$status" -eq 0 ]
    [ "$output" -ge 1 ]

    # clone-baseweed.sh must NOT source mmnet-config.sh nor call its helpers
    # (no implicit segment). A bare mention in a comment is fine; an actual
    # `. .../mmnet-config` / `source` or a helper call is the leak we forbid.
    run grep -E '^[[:space:]]*(\.|source)[[:space:]].*mmnet-config|mmnet_interface_xml[[:space:]]*[^[:alnum:]_]' "$VM/clone-baseweed.sh"
    [ "$status" -ne 0 ]

    # The splice must verify it actually landed a socket NIC (udp/mcast) after
    # splicing, so a silently-empty splice fails loudly rather than defining a
    # one-NIC clone.
    run grep -Eq "type='\\(udp\\|mcast\\)'|type='udp'" "$VM/clone-baseweed.sh"
    [ "$status" -eq 0 ]
}

@test "mmnet: template domain XML carries exactly ONE interface (the user-mode NIC)" {
    # The default clone copies the template's <interface> set verbatim. If the
    # template ever gained a second NIC, EVERY single-machine clone would too —
    # exactly the leak the decision forbids. create-template-domain.sh is the
    # source of the template; assert its heredoc body has one <interface ...> and
    # that it is type='user'.
    run grep -c "<interface type='user'>" "$VM/create-template-domain.sh"
    [ "$status" -eq 0 ]
    [ "$output" -eq 1 ]
    run grep -c '<interface ' "$VM/create-template-domain.sh"
    # grep -c counts lines; the template has exactly one <interface line.
    [ "$output" -eq 1 ]
    # And it must NOT carry an mmnet (udp/mcast) socket NIC.
    run grep -iE "type='(udp|mcast)'" "$VM/create-template-domain.sh"
    [ "$status" -ne 0 ]
}

@test "mmnet: smoke test does NOT live under tests/integration/vm (default bats glob)" {
    # gate_bats globs tests/integration/vm/*.bats. A mmnet test there would be
    # picked up by `qci bats` / `qci full` and silently boot a 2nd VM. Assert no
    # mmnet bats file leaked into that dir.
    run bash -c "ls '$REPO_ROOT'/tests/integration/vm/*mmnet*.bats 2>/dev/null"
    [ "$status" -ne 0 ]
    # The mmnet bats test lives in its own dir instead.
    [ -d "$REPO_ROOT/tests/integration/mmnet" ]
}

@test "mmnet: gate_mmnet is NOT part of gate_full (opt-in only)" {
    # gate_full must not call gate_mmnet — the default battery never boots the
    # 2-VM lane.
    run grep -n 'gate_mmnet' "$REPO_ROOT/ci/lib/dispatch.sh"
    [ "$status" -eq 0 ]
    # The only reference is inside the `mmnet)` dispatch case, NEVER inside
    # gate_full(). Extract gate_full's body and assert gate_mmnet is absent.
    run bash -c "awk '/^gate_full\(\)/{f=1} f{print} /^}/{if(f)exit}' '$REPO_ROOT/ci/lib/dispatch.sh' | grep -c gate_mmnet"
    [ "$output" -eq 0 ]
}

@test "mmnet: config emits a single loopback UDP interface for a peer" {
    # mmnet_interface_xml a -> exactly one <interface type='udp'>, bound to the
    # loopback localaddr, with a 52:54:00:99:* MAC.
    run bash -c ". '$VM/mmnet-config.sh'; MMNET_SEED=12345 mmnet_interface_xml a 12345"
    [ "$status" -eq 0 ]
    echo "$output" | grep -q "<interface type='udp'>"
    echo "$output" | grep -q "address='127.0.0.1'"
    echo "$output" | grep -qE "mac address='52:54:00:99:"
    # Exactly one interface block.
    run bash -c ". '$VM/mmnet-config.sh'; MMNET_SEED=12345 mmnet_interface_xml a 12345 | grep -c '<interface'"
    [ "$output" -eq 1 ]
}

@test "mmnet: A and B are crossed UDP mirrors (A.local == B.remote and vice versa)" {
    # The two peers must form a point-to-point pair: what A binds is what B sends
    # to, and what B binds is what A sends to — otherwise their datagrams never
    # meet. Distinct local ports; crossed remote ports.
    run bash -c ". '$VM/mmnet-config.sh'; echo \"\$(mmnet_local_port a 7) \$(mmnet_local_port b 7) \$(mmnet_remote_port a 7) \$(mmnet_remote_port b 7)\""
    [ "$status" -eq 0 ]
    read -r la lb ra rb <<<"$output"
    [ "$la" != "$lb" ]      # distinct local ports
    [ "$ra" = "$lb" ]       # A sends to B's local port
    [ "$rb" = "$la" ]       # B sends to A's local port
}

@test "mmnet: allocatable seeds map BIJECTIVELY to port pairs (no aliasing)" {
    # The allocator (mmnet-alloc.sh) reserves a base-port INDEX in
    # 0..MMNET_SEED_SPACE-1. Two distinct reserved indices MUST yield distinct
    # base ports, or two concurrent runs could collide on the same loopback UDP
    # pair despite holding "different" reservations. Assert mmnet_base_port is
    # injective over the whole index space, and that the allocator's SEED_SPACE
    # default matches the config's modulus.
    run bash -c ". '$VM/mmnet-config.sh'
        n=\${MMNET_SEED_SPACE}
        # One base port per line across the whole index space; raw vs sorted-uniq.
        raw=\$(for i in \$(seq 0 \$((n-1))); do mmnet_base_port \$i; echo; done | grep -c .)
        uniq=\$(for i in \$(seq 0 \$((n-1))); do mmnet_base_port \$i; echo; done | sort -u | grep -c .)
        echo \"raw=\$raw uniq=\$uniq space=\$n\"
        [ \"\$raw\" = \"\$uniq\" ] && [ \"\$uniq\" = \"\$n\" ]"
    [ "$status" -eq 0 ]
    # The allocator must default to the same SEED_SPACE the config uses (5000).
    run grep -q 'MMNET_SEED_SPACE:-5000' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    run grep -q 'MMNET_SEED_SPACE:-5000' "$VM/mmnet-config.sh"
    [ "$status" -eq 0 ]
}

@test "mmnet: distinct peers get distinct IPs and MACs under the same seed" {
    run bash -c ". '$VM/mmnet-config.sh'; echo \"\$(mmnet_ip a) \$(mmnet_ip b) \$(mmnet_mac a 7) \$(mmnet_mac b 7)\""
    [ "$status" -eq 0 ]
    read -r ip_a ip_b mac_a mac_b <<<"$output"
    [ "$ip_a" != "$ip_b" ]
    [ "$mac_a" != "$mac_b" ]
}
