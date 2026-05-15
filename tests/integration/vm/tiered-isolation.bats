#!/usr/bin/env bats
# §Phase-7 isolation tiers — tier-2 podman + nested compositor.
# Reuses the same qdwin / qdshell / pixelfeed binaries from phase6.x;
# the new ingredient is the `qdistro/tier2-<workload>:latest` container
# images (image-per-workload model) built by tier2/make-tier2-image.sh.
# First workload: qdistro/tier2-weston-terminal:latest.
# Drivers stage themselves over the bats http server (port 8768) the
# same way broker-e2e.bats stages s90; s32 builds the image on-demand
# inside the VM if missing. Skips on VMs without podman.

load helpers

setup() {
    vm_run "pgrep -x pipewire >/dev/null"
    assert_success
    vm_run "systemctl stop qdistro-admin-broker.service 2>/dev/null || true"
}

# Stage the in-VM driver script onto the host's bats http server
# (port 8768 by convention) so the VM can fetch it. Mirrors the
# self-staging pattern in broker-e2e.bats.
stage_tier2_driver() {
    local script_name="$1"
    local script_path
    script_path="$(dirname "$BATS_TEST_FILENAME")/$script_name"
    [ -f "$script_path" ] || fail_loud "driver script not found at $script_path"

    cp "$script_path" "$(dirname "$BATS_TEST_FILENAME")/../"

    if ! ss -tln 2>/dev/null | grep -q ":8768 "; then
        local stage_dir
        stage_dir="$(dirname "$BATS_TEST_FILENAME")/.."
        (cd "$stage_dir" && nohup python3 -m http.server 8768 \
            >/tmp/qdistro-bats-http.log 2>&1 &)
        sleep 1
    fi
}

@test "phase7-tier2-podman: in-container weston-terminal becomes a peer toplevel on outer" {
    stage_tier2_driver "s32-tier2-podman.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -s -o /tmp/s32.sh http://10.0.2.2:8768/s32-tier2-podman.sh && chmod +x /tmp/s32.sh && bash /tmp/s32.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman or qdistro/tier2 image absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: container 'tier2-c1' running"
    assert_output_contains "PASS: container's weston-terminal advertised through the nested-mode publisher"
    assert_output_contains "PASS: outer admin shell created chromed proxy for in-container app"
    assert_output_contains "PASS: qdshell spawned pixelfeed for in-container toplevel"
    assert_output_contains "PASS: origin_uid=1000 (--userns=keep-id mapping verified)"
    assert_output_contains "PASS: §Phase-7 tier-2 podman + nested compositor end-to-end"
}

@test "phase7-tier2-input: QDNI input forwards into the in-container nested compositor" {
    stage_tier2_driver "s33-tier2-input.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -s -o /tmp/s33.sh http://10.0.2.2:8768/s33-tier2-input.sh && chmod +x /tmp/s33.sh && bash /tmp/s33.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman or qdistro/tier2 image absent on this VM"
    fi
    assert_output_contains "PASS: in-container nested compositor decoded QDNI events"
    assert_output_contains "PASS: pointer button (S3B) reached in-container nested compositor"
    # S3B/S3C keyboard sub-PASSes depend on the RDP backend's seat having
    # a keyboard at proxy_create time; sdl-freerdp /v: dummy doesn't
    # always negotiate keyboard caps before the burst fires (the burst
    # is fire-once on advertise_toplevel; no retry). The S3C keyboard-
    # grab path is independently exercised by phase6.6.bats
    # `s6.8-s3c-nested-keyboard-grab` against a more controlled seat,
    # which is the canonical assertion. Here we accept either keyboard
    # PASS or none — the pointer-button and QDNI decode lines above are
    # the load-bearing tier-2-input invariants.
    assert_output_contains "PASS: §Phase-7 tier-2 input forwarding end-to-end"
}

@test "phase7-tier2-lifecycle: two containers + stop-cleanup" {
    stage_tier2_driver "s34-tier2-lifecycle.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -s -o /tmp/s34.sh http://10.0.2.2:8768/s34-tier2-lifecycle.sh && chmod +x /tmp/s34.sh && bash /tmp/s34.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman or qdistro/tier2 image absent on this VM"
    fi
    assert_output_contains "PASS: two containers running concurrently"
    assert_output_contains "PASS: stop(A) → toplevel_removed handle="
    assert_output_contains "PASS: container B still running after A stop"
    assert_output_contains "PASS: §Phase-7 tier-2 lifecycle (multi-container + stop-cleanup) end-to-end"
}

@test "phase7-tier2-hardening: container-runtime isolation flags effective" {
    stage_tier2_driver "s40-tier2-hardening.sh"
    vm_run "curl -s -o /tmp/s40.sh http://10.0.2.2:8768/s40-tier2-hardening.sh && chmod +x /tmp/s40.sh && bash /tmp/s40.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman / tier-2 image / outer compositor absent on this VM"
    fi
    assert_output_contains "PASS: CapEff=0 (--cap-drop=ALL effective)"
    assert_output_contains "PASS: NoNewPrivs=1 (no-new-privileges effective)"
    assert_output_contains "PASS: network=none (only lo present)"
    assert_output_contains "PASS: rootfs mounted read-only"
    assert_output_contains "PASS: touch / blocked by read-only"
    assert_output_contains "PASS: /run/user/1000/ contains only allowed sockets/logs"
    assert_output_contains "PASS: no host bus/pulse/gnupg/ssh-agent in /run/user/1000/"
    assert_output_contains "PASS: qdistro_tier2_token label set"
    assert_output_contains "PASS: §Phase-7 tier-2 hardening invariants enforced"
}

# --- §Phase-7 tier-3: cross-uid silos via waypipe ---------------------
# spec/02 row 3. waypipe-client runs as admin (uid 1000), waypipe-server
# runs as the silo uid; the two halves cross-uid bridge a wl_display
# over a unix socket gated by the qdistro-tier3 group. Skips on VMs
# without waypipe or without the user1/user2 silo accounts (i.e. older
# bootstraps).

@test "phase7-tier3-waypipe: cross-uid bridge smoke (wayland-info as user1)" {
    vm_run "bash /root/s35-tier3-waypipe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "waypipe or silo user absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: silo command actually ran as uid 1001"
    assert_output_contains "PASS: wayland-info enumerated wl_compositor through the bridge"
    assert_output_contains "PASS: bridge socket carried qdistro-tier3:0660"
    assert_output_contains "PASS: §Phase-7 tier-3 waypipe smoke end-to-end"
}

@test "phase7-tier3-app: cross-uid weston-terminal as user1 reaches outer" {
    vm_run "bash /root/s36-tier3-app.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "waypipe or silo user absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: bridged toplevel reached the outer admin compositor"
    assert_output_contains "PASS: weston-terminal genuinely runs as uid 1001"
    assert_output_contains "PASS: §Phase-7 tier-3 cross-uid graphical app end-to-end"
}

@test "phase7-tier3-lifecycle: two silos (user1+user2), kill A leaves B running" {
    vm_run "bash /root/s37-tier3-lifecycle.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "waypipe or silo users absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: two distinct silo toplevels reached the outer compositor"
    assert_output_contains "PASS: both bridge sockets present"
    assert_output_contains "PASS: silo A runs as user1"
    assert_output_contains "PASS: silo B still running after silo A teardown"
    assert_output_contains "PASS: §Phase-7 tier-3 multi-silo lifecycle end-to-end"
}

@test "phase7-tier3-chrome: qdshell parses silo prefix + applies per-silo color override" {
    vm_run "bash /root/s38-tier3-chrome.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "waypipe or silo users absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: qdshell observed two tier-3 toplevels"
    assert_output_contains "PASS: qdshell parsed both silo identifiers from title prefix"
    assert_output_contains "PASS: silo color override applied"
    assert_output_contains "PASS: §Phase-7 tier-3 silo chrome differentiation end-to-end"
}

@test "phase7-secctx: wp_security_context_v1 advertised + waypipe-tagged silo client (§6.10)" {
    vm_run "bash /root/s40-secctx.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "waypipe / silo / qdistro-test-window absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: wp_security_context_manager_v1 advertised by qdwin"
    assert_output_contains "PASS: waypipe bound wp_security_context_manager_v1"
    assert_output_contains "PASS: silo client tagged with secctx"
    assert_output_contains "PASS: secctx app_id propagated via wrapper default"
    assert_output_contains "PASS: §6.10 wp_security_context_v1 end-to-end"
}

@test "phase7-clipboard-gate: spec/10 cross-uid clipboard policy gate end-to-end" {
    # qdwin_shell_v1@v14 (task 037) added inject-focus via the qdshell
    # ctrl-socket: s39 now injects focus on the silo toplevel before
    # firing the silo's set_selection helper, which resolves the
    # 3-issue stack documented in spec/10 §"e2e gate exercise":
    #   1. focused-toplevel resolution finds silo=user1.
    #   2. inject side-effect (weston_seat_set_selection NULL +
    #      fresh_serial) drops weston's stale-serial guard so the
    #      silo helper's serial=0 set_selection succeeds.
    #   3. waypipe wl_client mismatch is moot once focus is set —
    #      qdwin emits source_handle = silo_toplevel.handle.
    vm_run "bash /root/s39-clipboard-gate.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "wl-clipboard / waypipe / silo / sdl-freerdp absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: admin → admin selection_set seen by qdshell"
    assert_output_contains "PASS: silo toplevel registered with silo=user1"
    assert_output_contains "PASS: broker logged clipboard-transfer audit"
    assert_output_contains "PASS: qdshell cleared the silo→admin selection (default-deny)"
    # task(067): round 3 now probes broker via D-Bus instead of firing
    # a second wire set_selection. See s39-clipboard-gate.sh §round-3
    # for the rationale (stale-serial-guard + waypipe oneshot bridge).
    assert_output_contains "PASS: rule install flipped broker verdict to allow"
    assert_output_contains "PASS: qdshell observed RulesReloaded + ran live re-check"
    assert_output_contains "PASS: spec/10 cross-uid clipboard policy gate end-to-end"
}

@test "phase7-secctx-toplevel-event: qdwin_shell_v1@v13 toplevel_security_context end-to-end" {
    vm_run "bash /root/s41-secctx-toplevel-event.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "waypipe / silo / qdistro-test-window absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdwin_shell_v1 protocol XML at version"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: compositor emitted toplevel_security_context"
    assert_output_contains "PASS: qdshell received toplevel_security_context"
    assert_output_contains "PASS: qdshell derived silo=user1 from secctx app_id"
    assert_output_contains "PASS: silo colour override applied via secctx path"
    assert_output_contains "PASS: §Phase-7 v13 toplevel_security_context end-to-end"
}

@test "phase7-tier4-spawn: qdistro-tier4-spawn brings up libvirt domain + spice display" {
    # §Phase-7 tier-4 — libvirt + virt-viewer wrapper smoke. Linux-only
    # per spec/00 (memory qdistro_linux_only.md). Tests the wrapper's
    # define-only and no-viewer modes; full virt-viewer-on-wayland-1
    # integration is exercised manually until the chrome path is stable.
    stage_tier2_driver "s42-tier4-spawn.sh"
    vm_run "curl -s -o /tmp/s42.sh http://10.0.2.2:8768/s42-tier4-spawn.sh && chmod +x /tmp/s42.sh && bash /tmp/s42.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-4 stack (libvirt/qemu/virt-viewer) not installed on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: define-only mode created domain"
    assert_output_contains "PASS: tier-4 no-viewer mode brought up domain"
    assert_output_contains "PASS: virsh confirms domain is running"
    assert_output_contains "PASS: §Phase-7 tier-4 spawn smoke end-to-end"
}

@test "phase7-tier5-loopback: qdistro-tier5-spawn --loopback bridges wayland-info via vsock" {
    # §Phase-7 tier-5-Linux — waypipe-over-vsock wrapper smoke. Linux-only
    # per spec/00. Tests the data path with vsock CID=1 (loopback).
    # Real-VM mode (--vm <name>) is covered by phase7-tier5-vm.
    stage_tier2_driver "s43-tier5-loopback.sh"
    vm_run "curl -s -o /tmp/s43.sh http://10.0.2.2:8768/s43-tier5-loopback.sh && chmod +x /tmp/s43.sh && bash /tmp/s43.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-5 stack (waypipe / vsock_loopback / wayland-info) not available on this VM"
    fi
    assert_output_contains "PASS: vsock_loopback module loaded"
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: wayland-info enumerated wl_compositor through the vsock bridge"
    assert_output_contains "PASS: §Phase-7 tier-5-Linux waypipe-vsock loopback end-to-end"
}

@test "phase7-tier4-secctx-exec: qdistro-secctx-exec wraps a Wayland client with wp_security_context_v1" {
    # §Phase-7 tier-4 secctx — the wrapper is reusable for any non-secctx-
    # aware client (virt-viewer being the motivating case). Tests the
    # wrapper end-to-end with qdistro-test-window as a stand-in for
    # virt-viewer to avoid dragging libvirt+qemu into the test.
    stage_tier2_driver "s44-tier4-secctx-exec.sh"
    vm_run "curl -s -o /tmp/s44.sh http://10.0.2.2:8768/s44-tier4-secctx-exec.sh && chmod +x /tmp/s44.sh && bash /tmp/s44.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-secctx-exec / qdistro-test-window absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdistro-secctx-exec --help"
    assert_output_contains "PASS: wp_security_context_manager_v1 advertised by qdwin"
    assert_output_contains "PASS: qdistro-secctx-exec bound wp_security_context_manager_v1"
    assert_output_contains "PASS: secctx commit recorded with engine=qdistro.tier4 app_id=qdistro.tier4.s44vm"
    assert_output_contains "PASS: inner client accepted on the secctx listener"
    assert_output_contains "PASS: §Phase-7 tier-4 secctx-exec wrapper end-to-end"
}

@test "phase7-tier5-vm: qdistro-tier5-spawn --vm boots a guest VM and bridges via vsock" {
    # §Phase-7 tier-5 --vm — full per-app guest VM path. Skips when
    # /var/lib/libvirt/images/qdistro-tier5-base.qcow2 is absent (build
    # is opt-in: ~400MB Tumbleweed-Minimal-VM cloud download).
    stage_tier2_driver "s45-tier5-vm.sh"
    vm_run "curl -s -o /tmp/s45.sh http://10.0.2.2:8768/s45-tier5-vm.sh && chmod +x /tmp/s45.sh && bash /tmp/s45.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-5 base disk absent (rerun fresh-vm-bootstrap.sh with QDISTRO_BUILD_TIER5_BASE=1, or invoke qdistro-tier5-build-guest-image manually)"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: host-side waypipe-client vsock listener ready"
    assert_output_contains "PASS: guest qemu-guest-agent responded"
    assert_output_contains "PASS: guest publisher pid reported via qga guest-exec"
    assert_output_contains "PASS: §Phase-7 tier-5-Linux --vm end-to-end smoke"
}

@test "phase7-tier5-close-cleanup: SIGTERM teardown reclaims libvirt domain + per-VM overlay" {
    # §Phase-7 tier-5 cleanup — orphan-resource budget. The wrapper's
    # EXIT trap MUST destroy+undefine the domain and rm the overlay.
    # Anything left after SIGTERM is a leak; the bats variant catches
    # the regression class the user-facing close button can't see.
    # Pairs with permissions-gui/21-tier5-close-cleanup.md (visual).
    stage_tier2_driver "s48-tier5-close-cleanup.sh"
    vm_run "curl -s -o /tmp/s48.sh http://10.0.2.2:8768/s48-tier5-close-cleanup.sh && chmod +x /tmp/s48.sh && bash /tmp/s48.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-5 base disk or virsh/kvm absent — same skip surface as phase7-tier5-vm"
    fi
    assert_output_contains "PASS: domain"
    assert_output_contains "PASS: libvirt domain"
    assert_output_contains "PASS: per-VM overlay"
    assert_output_contains "PASS: §Phase-7 tier-5 SIGTERM teardown reclaims all per-VM resources"
}

@test "phase7-tier5-audio: qemu -audiodev pipewire bridges guest audio to host PipeWire" {
    # §Phase-7 tier-5 audio — spec/29 §3 picked path. Skips when the
    # tier-5 base disk is absent OR pipewire-tools / qemu-audio-pipewire
    # are missing.
    stage_tier2_driver "s47-tier5-audio.sh"
    vm_run "curl -s -o /tmp/s47.sh http://10.0.2.2:8768/s47-tier5-audio.sh && chmod +x /tmp/s47.sh && bash /tmp/s47.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-5 base disk / pw-cli / qemu-audio-pipewire absent"
    fi
    assert_output_contains "PASS: host preconditions met"
    assert_output_contains "PASS: domain template carries <audio type='pipewire'>"
    assert_output_contains "PASS: domain"
    assert_output_contains "PASS: qga ready"
    assert_output_contains "PASS: guest speaker-test started"
    assert_output_contains "PASS: host PipeWire saw QEMU audio stream"
    assert_output_contains "PASS: §Phase-7 tier-5-Linux audio (qemu -audiodev pipewire) end-to-end"
}

@test "phase7-tier4-clipboard-gate: spec/10 cross-uid clipboard gate from a tier-4-tagged toplevel" {
    # §Phase-7 tier-4 clipboard — exercises the qdshell silo-resolution
    # path that recognises `qdistro.tier4.<vm>` secctx app_ids and
    # derives silo=`vm-<vm>`. The broker default-deny + allow-rule path
    # is identical to tier-3's s39 except the source toplevel is wrapped
    # by qdistro-secctx-exec (no waypipe). With v14 inject-focus (task
    # 037) the test runs end-to-end on sdl-freerdp /v: dummy as well —
    # the focus state that real virt-viewer would deliver naturally is
    # injected via qdshell's ctrl-socket.
    stage_tier2_driver "s46-tier4-clipboard-gate.sh"
    vm_run "curl -s -o /tmp/s46.sh http://10.0.2.2:8768/s46-tier4-clipboard-gate.sh && chmod +x /tmp/s46.sh && bash /tmp/s46.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-secctx-exec / qdistro-test-window / qdistro-test-clipboard-source absent"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: tier-4 toplevel registered with silo=vm-s46vm"
    assert_output_contains "PASS: broker logged clipboard-transfer audit"
    assert_output_contains "PASS: qdshell cleared the tier-4 → admin selection (default-deny)"
    # task(067): round 3 broker D-Bus probe — see s46 §round-3.
    assert_output_contains "PASS: rule install flipped broker verdict to allow"
    assert_output_contains "PASS: qdshell observed RulesReloaded + ran live re-check"
    assert_output_contains "PASS: §Phase-7 tier-4 clipboard gate end-to-end"
}

@test "phase7-tier1-skeleton: spec/30 Tier-1 SELinux design + spike skeleton present" {
    # Tier-1 is design + spike (spec/30, 2026-04-27 night). No runtime
    # yet — six prior sessions deferred it. This bats just verifies the
    # design doc, spike checklist, .te skeleton, spawn wrapper, and
    # qdshell tier1 secctx parser all survived the bootstrap rsync.
    # The implementation pass picks up from this baseline once the four
    # blocking spikes in tier1/spike-checklist.md resolve.
    stage_tier2_driver "s50-tier1-skeleton.sh"
    vm_run "curl -s -o /tmp/s50.sh http://10.0.2.2:8768/s50-tier1-skeleton.sh && chmod +x /tmp/s50.sh && bash /tmp/s50.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier1 source not staged in this VM"
    fi
    assert_output_contains "PASS: tier1 README present"
    assert_output_contains "PASS: spike-checklist.md covers Spikes 1-5"
    assert_output_contains "PASS: qdistro_tier1.te declares type + Wayland connect interface"
    assert_output_contains "PASS: spawn-tier1.sh syntax-checks"
    assert_output_contains "PASS: spec/30 Tier-1 design + skeleton present"
}

@test "phase7-tier4-spice-clipboard: spec/10 SPICE main-channel clipboard defense-in-depth" {
    # Defense-in-depth around the SPICE main channel: tier-4 domains
    # ship with <clipboard copypaste='no'/> by default (forcing all
    # host ↔ guest clipboard via wayland selection, which qdistro
    # already gates) and a per-launch 16-hex SPICE ticket. Admin
    # opts in to the SPICE clipboard via TIER4_SPICE_CLIPBOARD=allowed.
    stage_tier2_driver "s49-tier4-spice-clipboard.sh"
    vm_run "curl -s -o /tmp/s49.sh http://10.0.2.2:8768/s49-tier4-spice-clipboard.sh && chmod +x /tmp/s49.sh && bash /tmp/s49.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-tier4-spawn / virsh absent on this VM"
    fi
    assert_output_contains "PASS: default XML disables SPICE clipboard channel"
    assert_output_contains "PASS: default XML carries 16-hex SPICE password"
    assert_output_contains "PASS: opt-in TIER4_SPICE_CLIPBOARD=allowed enables clipboard channel"
    assert_output_contains "PASS: SPICE password rotates per spawn"
    assert_output_contains "PASS: spec/10 SPICE clipboard gate (tier-4 defense-in-depth) end-to-end"
}

@test "phase7-focus-aware-clear: spec/10 v14 admin clipboard cleared on cross-silo focus (Q1a)" {
    # qdwin_shell_v1@v14 set_keyboard_focus + seat_focus_changed close
    # the admin → tier-N paste-receive gap that wp_security_context_v1
    # alone can't reach. Wayland surfaces no wl_data_offer.receive
    # event; clearing the seat selection on cross-silo focus is the
    # Qubes-style mitigation. The headless ctrl-socket inject-focus
    # path is what this script exercises; real chrome click-to-focus
    # uses the same set_keyboard_focus request.
    vm_run "bash /root/s48-focus-aware-clear.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack / qdistro-test-window / qdistro-test-clipboard-source absent"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: qdshell bound qdwin_shell_v1 at version >= 14"
    assert_output_contains "PASS: admin toplevel registered handle="
    assert_output_contains "PASS: silo toplevel registered silo=user1"
    assert_output_contains "PASS: admin selection recorded by qdshell"
    assert_output_contains "PASS: ctrl selection-state shows admin clipboard source"
    assert_output_contains "PASS: qdshell cleared the admin selection on cross-silo focus"
    assert_output_contains "PASS: weston processed clear_selection request"
    assert_output_contains "PASS: ctrl selection-state cleared post-focus-change"
    assert_output_contains "PASS: spec/10 v14 focus-aware selection clear end-to-end"
}

@test "phase7-tier1-e2e: spec/30 Tier-1 SELinux pipeline end-to-end" {
    # Validates the full tier-1 stack (spec/30 §Phase plan step 8):
    # policy module loaded, type_transition rule active, spawn pipeline
    # places the inner command in qdistro_tier1_t, broker spawn-action
    # gate denies an authored rule.
    stage_tier2_driver "s51-tier1-e2e.sh"
    vm_run "curl -s -o /tmp/s51.sh http://10.0.2.2:8768/s51-tier1-e2e.sh && chmod +x /tmp/s51.sh && bash /tmp/s51.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "SELinux disabled / policy not loaded / qdistro-tier1-spawn absent"
    fi
    assert_output_contains "PASS: SELinux enabled"
    assert_output_contains "PASS: qdistro_tier1 policy module loaded"
    assert_output_contains "PASS: type_transition unconfined_t -> qdistro_tier1_t exists"
    assert_output_contains "PASS: sleep runs in qdistro_tier1_t"
    assert_output_contains "PASS: broker denied qdistro.tier1.spawn:sleep before exec"
    assert_output_contains "PASS: spec/30 Tier-1 SELinux end-to-end"
}

@test "phase7-data-offer-receive-v15: spec/10 v15 wl_data_offer.receive wire flow" {
    # Validates the v15 send-shim → data_offer_receive_pending →
    # data_offer_receive_decision wire path. Same-silo allow is the
    # load-bearing assertion; cross-silo at receive time is pre-empted
    # by v14 focus-aware-clear so this test exercises the wire path
    # under the permitted-flow case (admin → admin via wl-paste).
    #
    # spec/10 v16 closed the qdwin focus→data_device race (task 054):
    # set_keyboard_focus_v2 + per-seat last_target_silo gate the
    # unconditional clear on cross-silo only, so same-silo focus
    # injections preserve the active selection and the sink helper's
    # wl_data_device receives the offer. The byte-for-byte payload
    # assertion is now load-bearing whenever the sink helper is built.
    stage_tier2_driver "s53-data-offer-receive-v15.sh"
    vm_run "curl -s -o /tmp/s53.sh http://10.0.2.2:8768/s53-data-offer-receive-v15.sh && chmod +x /tmp/s53.sh && bash /tmp/s53.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-test-clipboard-sink absent or v15 binding missing"
    fi
    assert_output_contains "PASS: qdshell bound qdwin_shell_v1 at version >= 15"
    assert_output_contains "PASS: qdwin installed v15 data_source send-shim"
    assert_output_contains "PASS: qdshell logged data_offer_receive_pending"
    assert_output_contains "PASS: qdwin processed data_offer_receive_decision"
    if [[ "$output" == *"admin-sink toplevel registered"* ]]; then
        assert_output_contains "PASS: sink received exact payload through allow path"
    fi
    assert_output_contains "PASS: spec/10 v15 wl_data_offer.receive end-to-end wire flow"
}

@test "phase7-tier1-enforcing: spec/30 enforcing-mode pass — zero new AVCs" {
    # Flips SELinux to enforcing, runs the tier-1 happy-path workload
    # (id, /etc/passwd read, dbus session-bus ListNames), captures any
    # new qdistro_tier1_t AVCs against the audit log baseline. Any
    # delta indicates qdistro_tier1.te is missing an allow rule for a
    # workload that should succeed; iterate via audit2allow + add the
    # interface to selinux/tier1/qdistro_tier1.te.
    #
    # Restoration: trap re-issues setenforce 0 on natural exit OR
    # signal so a failed run still leaves the VM in permissive mode.
    stage_tier2_driver "s55-tier1-enforcing.sh"
    vm_run "curl -s -o /tmp/s55.sh http://10.0.2.2:8768/s55-tier1-enforcing.sh && chmod +x /tmp/s55.sh && bash /tmp/s55.sh 2>/dev/null"
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "SELinux disabled, policy not loaded, or config pins permissive"
    fi
    assert_success
    assert_output_contains "PASS: SELinux mode now Enforcing"
    assert_output_contains "PASS: 0 new denials — qdistro_tier1.te covers the enforcing workload"
}

@test "phase7-broker-enforcing: spec/30 broker Phase 3 — qdistro_broker_t enforces clean" {
    # task(068) — flips SELinux to enforcing, restarts the broker
    # under the new tag, exercises CheckClipboardTransfer + ListRules
    # + ListPending + ListHistory D-Bus paths AND the inotify rule-
    # reload + SIGHUP reload paths. Any new qdistro_broker_t denial
    # against the audit baseline counts as DIRTY (audit2allow output
    # is dumped for the next iteration).
    #
    # Same SKIP semantics as phase7-tier1-enforcing: when /etc/selinux/
    # config pins SELINUX=permissive, the runtime flip is refused.
    stage_tier2_driver "s56-broker-enforcing.sh"
    vm_run "curl -s -o /tmp/s56.sh http://10.0.2.2:8768/s56-broker-enforcing.sh && chmod +x /tmp/s56.sh && bash /tmp/s56.sh 2>/dev/null"
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "SELinux disabled, policy not loaded, or config pins permissive"
    fi
    assert_success
    assert_output_contains "PASS: SELinux mode now Enforcing"
    assert_output_contains "PASS: broker running pid="
    assert_output_contains "PASS: D-Bus probe succeeded under enforcing"
    assert_output_contains "PASS: 0 new denials — qdistro_broker.te 0.3.0 covers the enforcing workload"
}

@test "phase7-tier4-spice-clipboard-live: spec/10 SPICE clipboard live guest validation" {
    # Boots a tier-4 guest from the SPICE-capable base image, polls
    # qga, asserts spice-vdagentd is active inside the guest, and
    # verifies running domain XML carries the configured copypaste
    # value (default 'no', opt-in 'yes'). Skips when the base image
    # isn't built (qdistro-tier4-base.qcow2 absent).
    stage_tier2_driver "s54-tier4-spice-clipboard-live.sh"
    vm_run "curl -s -o /tmp/s54.sh http://10.0.2.2:8768/s54-tier4-spice-clipboard-live.sh && chmod +x /tmp/s54.sh && bash /tmp/s54.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-4 SPICE base image absent (run build-guest-image.sh) or virsh missing"
    fi
    assert_output_contains "PASS: running domain XML carries copypaste='no'"
    assert_output_contains "PASS: qga reachable inside"
    assert_output_contains "PASS: spice-vdagentd.service active inside"
    assert_output_contains "PASS: opt-in running domain XML carries copypaste='yes'"
    assert_output_contains "PASS: spec/10 SPICE clipboard live guest validation end-to-end"
}

@test "phase7-qsu-argv-scopes: tasks(069/072) — argv-aware scopes round-trip end-to-end" {
    # Closes the qsu argv-aware approval flow: cache backend (task 069)
    # + broker _VALID_SCOPES (task 072) + admin-app surface (072/077)
    # exercised through real D-Bus + real sqlite. For each new scope,
    # request → admin-decide → CheckPermission with the same and
    # different argv, asserting cache match-kind semantics:
    #
    #   forever_argv     — only the exact argv tuple matches.
    #   forever_basename — any argv with the same basename(argv[0]).
    #   forever_prefix   — any argv whose stored argv is a prefix.
    #   forever_exe      — any argv with the same exe (legacy).
    #
    # Re-run-safe: probe revokes its own rows on entry + exit.
    stage_tier2_driver "s57-qsu-argv-scopes.sh"
    vm_run "curl -s -o /tmp/s57.sh http://10.0.2.2:8768/s57-qsu-argv-scopes.sh && chmod +x /tmp/s57.sh && bash /tmp/s57.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"FAIL:"*"qdistro-admin-broker.service not active"* ]]; then
        fail_loud "broker service inactive on this VM"
    fi
    assert_output_contains "PASS: forever_argv same"
    assert_output_contains "PASS: forever_argv install→argv differs"
    assert_output_contains "PASS: forever_basename same"
    assert_output_contains "PASS: forever_basename different argv[0] same basename"
    assert_output_contains "PASS: forever_basename different basename"
    assert_output_contains "PASS: forever_prefix exact prefix"
    assert_output_contains "PASS: forever_prefix one trailing arg"
    assert_output_contains "PASS: forever_prefix different argv[0]"
    assert_output_contains "PASS: forever_exe — same caller_exe any argv"
    assert_output_contains "PASS: forever_exe — same caller_exe even with very different argv[0]"
    assert_output_contains "ALL_PASS"
    assert_output_contains "PASS: forever_argv / forever_basename / forever_prefix / forever_exe round-trip OK"
}

@test "phase7-qsu-real-flow: tasks(069/072/078) — real qsu argv-pinned cache + re-prompt" {
    # End-to-end through the actual qsu CLI (RequestPermissionAs,
    # delegated path) rather than s57's RequestPermission probe:
    #
    #   1. admin DecideRequest('forever_argv') for `qsu /bin/true`
    #   2. second `qsu /bin/true` cache-hits (no admin)
    #   3. `qsu /bin/echo hello` re-prompts (different argv)
    #
    # Closes the qsu argv-leak: tasks(069/072) shipped cache + UI but
    # `_DELEGATED_FORBIDDEN_SCOPES` still blocked argv-aware scopes
    # on the qsu path until task(078). This test wedges that fix in
    # place — a regression in the delegated gate would either time
    # out step B (re-prompt instead of cache) or fail step A's
    # DecideRequest with "forbidden scope on delegated request".
    stage_tier2_driver "s58-qsu-real-flow.sh"
    vm_run "curl -s -o /tmp/s58.sh http://10.0.2.2:8768/s58-qsu-real-flow.sh && chmod +x /tmp/s58.sh && bash /tmp/s58.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"FAIL: /usr/local/bin/qsu absent"* ]]; then
        fail_loud "qsu not installed on this VM (older bootstrap)"
    fi
    if [[ "$output" == *"FAIL: /run/qdistro-root-exec/sock not present"* ]]; then
        fail_loud "qdistro-root-exec.socket inactive on this VM"
    fi
    assert_output_contains "PASS: pending rid="
    assert_output_contains "PASS: qsu /bin/true rc=0 after admin allow forever_argv"
    assert_output_contains "PASS: second qsu /bin/true cache-hit"
    assert_output_contains "PASS: qsu /bin/echo re-prompted"
    assert_output_contains "PASS: qsu /bin/echo rc=0 stdout='hello-from-s58' after admin allow once"
    assert_output_contains "PASS: s58 — qsu real-flow argv-aware cache + re-prompt end-to-end"
}

@test "phase7-tier1-audisp: spec/30 step 7 audispd → broker AVC ingestion" {
    # Validates the audispd plugin pipeline (spec/30 step 7):
    # AVC denial in qdistro_tier1_t → audispd plugin → broker
    # RecordSelinuxAvc → audit DB row with selinux_subj_type set.
    stage_tier2_driver "s52-tier1-audisp.sh"
    vm_run "curl -s -o /tmp/s52.sh http://10.0.2.2:8768/s52-tier1-audisp.sh && chmod +x /tmp/s52.sh && bash /tmp/s52.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "SELinux disabled / policy not loaded / audisp plugin absent / auditd down"
    fi
    assert_output_contains "PASS: audispd plugin + descriptor installed"
    assert_output_contains "PASS: broker up on com.qdistro.AdminBroker1"
    assert_output_contains "PASS: audit DB qdistro_tier1_t rows after="
    assert_output_contains "PASS: row carries selinux.avc:* action + verdict source"
    assert_output_contains "PASS: qdistro-audisp-plugin still running"
    assert_output_contains "PASS: spec/30 step 7 audispd → broker AVC ingestion end-to-end"
}

# spec/10 e2e bats not yet enabled — see s39-clipboard-gate.sh leading
# comment. The unit tests in tests/unit/test_broker_clipboard_transfer.py
# pin the broker policy; the compositor-side selection_signal listener
# is verified to fire (search weston log for "qdwin: selection_signal
# fired"); the qdshell handler follows the proven nested_proxy_pending
# pattern. End-to-end exercise needs a real RDP client to deliver
# wl_keyboard.enter so wl-clipboard can publish a selection — sdl-
# freerdp /v: dummy connect is enough for tier-3 (s37) but not for
# clipboard since wl-clipboard waits on focus before set_selection.
# A future bats can drive xfreerdp instead, or use a focus-bypass
# helper plus a separate paste-side helper.

# --- §Phase-7 tier-2 podapps discovery (s59) -----------------------------
# Once a tier-2 container is running, qdistro/tier2/podapps-scan.sh
# enumerates its .desktop files into /var/lib/qdistro/podapps/<name>/
# apps.json. This is what the qdshell PodApps service / launcher reads.
# The s32 image-build prerequisite is shared; if missing this test
# skips with the same fail_loud as the rest.

@test "phase7-tier2-podapps-discovery: podman exec scan writes apps.json" {
    stage_tier2_driver "s59-tier2-podapps-discovery.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -s -o /tmp/s59.sh http://10.0.2.2:8768/s59-tier2-podapps-discovery.sh && chmod +x /tmp/s59.sh && bash /tmp/s59.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman or qdistro/tier2 image absent on this VM"
    fi
    assert_output_contains "PASS: container running for scan"
    assert_output_contains "PASS: podapps cache file present"
    assert_output_contains "PASS: podapps cache parses + has entry"
    assert_output_contains "PASS: entry carries silo tier2/tier2-c-discovery"
    assert_output_contains "PASS: §Phase-7 tier-2 podapps discovery end-to-end"
}
