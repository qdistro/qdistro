#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase B milestone B-iii:
# compositor security-context chrome (driver s126-tier3s-chrome-secctx.sh).
# DONE bar items: "per-interface compositor security checks validate the
# actual tagged peer" — the compositor's toplevel_security_context /
# toplevel_peer_identity journal lines name the real bridge client's
# (pid, starttime, uid); qdshell derives the silo and paints the
# deterministic palette colour; the same secctx tag hides the privileged
# wl_registry globals (shell/layer-shell/locker/nested-manager/secctx
# manager/capture) from tier3s-tagged clients while an untagged admin
# client still sees them.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s126-tier3s-chrome-secctx.sh weston-terminal
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-chrome-secctx: tagged-peer metadata, chrome colour, per-interface global gating" {
    t3s_run_driver s126-tier3s-chrome-secctx.sh
    t3s_log s126
    assert_success
    t3s_no_failures s126
    assert_output_contains "PASS: launch up ("
    assert_output_contains "PASS: compositor: toplevel_security_context carries engine/app_id/instance=token"
    assert_output_contains "PASS: compositor: toplevel_peer_identity names pid+starttime+uid of the real bridge client"
    assert_output_contains "PASS: qdshell logged the deterministic palette colour for s126a"
    assert_output_contains "PASS: Tier3FocusIPC rejects a non-tier handle (9999)"
    assert_output_contains "PASS: audit: register row for s126a"
    for g in qdwin_shell_v1 zwlr_layer_shell_v1 qdwin_nested_manager_v1 qdwin_locker_v1 \
             zwp_input_method_manager_v2 zwp_virtual_keyboard_manager_v1; do
        assert_output_contains "PASS: plain admin sees $g"
        assert_output_contains "PASS: tier3s-tagged client does NOT see $g"
    done
    for g in wp_security_context_manager_v1 weston_capture_v1; do
        assert_output_contains "PASS: plain admin does NOT see $g (shell-only)"
        assert_output_contains "PASS: tier3s-tagged client does NOT see $g"
    done
    assert_output_contains "PASS: compositor logged the probe's tagged client acceptance"
    assert_output_contains "PASS: running compositor carries NO --qdwin-allowed-uid authorization override"
    assert_output_contains "PASS: wlprobe image staged (probes baked into a local layer)"
    assert_output_contains "PASS: wlprobe workload profile + seccomp installed"
    assert_output_contains "PASS: bridge probe launch up (s126p1: output test)"
    assert_output_contains "PASS: bridge probe launch up (s126p2: output apply)"
    assert_output_contains "PASS: bridge probe launch up (s126p3: stream claim)"
    assert_output_contains "PASS: bridge path: output-manager test refused (implementation error)"
    assert_output_contains "PASS: bridge path: test denial rode s126p1's tagged channel"
    assert_output_contains "PASS: bridge path: output-manager apply refused (implementation error)"
    assert_output_contains "PASS: bridge path: apply denial rode s126p2's tagged channel"
    assert_output_contains "PASS: bridge path: stream-input claim(bogus) -> INVALID_TOKEN"
    assert_output_contains "PASS: bridge path: stream claim rode s126p3's tagged channel"
    assert_output_contains "PASS: all three bridge-probe containers exited with their probes"
    assert_output_contains "PASS: tagged peer still enumerates zwlr_output_manager_v1"
    assert_output_contains "PASS: tagged peer: output-manager test refused (implementation error)"
    assert_output_contains "PASS: tagged peer: output-manager apply refused (implementation error)"
    assert_output_contains "PASS: tagged peer enumerates qdwin_stream_input_v1 (public by design)"
    assert_output_contains "PASS: tagged peer: stream-input claim(bogus) -> INVALID_TOKEN"
    assert_output_contains "PASS: compositor logged INVALID_TOKEN for the tagged claim"
    assert_output_contains "PASS: secctx listener path for the held tagged client"
    assert_output_contains "PASS: second connect on the consumed context is refused (live EOF)"
    assert_output_contains "PASS: compositor logged the refused extra connection"
    assert_output_contains "PASS: the held context still has exactly ONE accepted client"
}
