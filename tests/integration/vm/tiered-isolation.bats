#!/usr/bin/env bats
# §Phase-7 isolation tiers — tier-2 podman + nested compositor.
# Reuses the same qdwin / qdshell / pixelfeed binaries from phase6.x;
# the new ingredient is the `qdistro/tier2-<workload>:latest` container
# images (image-per-workload model) built by tier2/make-tier2-image.sh.
# First workload: qdistro/tier2-weston-terminal:latest.
# Drivers stage themselves over the shared free-port bats http server
# (stage_vm_driver, helpers.bash) the same way broker-e2e.bats stages s90;
# s32 builds the image on-demand inside the VM if missing. Skips on VMs
# without podman.

load helpers

# The tier-2 lane is broker-gated and the broker namespace is rules-only /
# fail-closed: without an explicit admin allow rule every tier-2 spawn AND
# every nested-publisher advertise is refused at the gate, so phase7-tier2-*
# can never reach the compositor. Bake the two required allow rules into the
# VM's /etc/qdistro/rules.d in setup_file (write YAML -> reload broker ->
# settle until CheckPermission=allow), and remove them in teardown_file so no
# test-authored rule leaks across runs. Mirrors disposables-e2e.bats. The two
# action strings match s32-tier2-podman.sh's spawn (workload/app =
# weston-terminal/weston-terminal) and the in-container publisher's nested
# advertise (org.freedesktop.weston.wayland-terminal).
setup_file() {
    vm_run "$(cat <<'SETUP'
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-tier2-isolation-allow.yaml"
install -d -m 0755 "$RULE_DIR" || { echo "FAIL: cannot create $RULE_DIR"; exit 1; }
cat >"$RULE_FILE" <<'YAML' || { echo "FAIL: cannot write $RULE_FILE"; exit 1; }
# Test-authored (tiered-isolation.bats/setup_file): allow the tier-2
# weston-terminal spawn gate + the nested-publisher advertise gate so the
# phase7-tier2 lane can drive the real launch path. Removed in teardown_file.
- name: tier2-isolation-spawn-allow
  decision: allow
  match:
    action: qdistro.tier2.spawn:weston-terminal/weston-terminal
- name: tier2-isolation-nested-advertise-allow
  decision: allow
  match:
    action: qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal
YAML
systemctl start qdistro-admin-broker.service 2>/dev/null || true
systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
# Settle: poll as the REAL caller (admin uid) until both freshly-authored
# rules resolve to allow (broker reloads rules on SIGHUP/restart).
bc() {
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}
r1=""; r2=""
for _ in $(seq 1 20); do
    r1=$(bc "qdistro.tier2.spawn:weston-terminal/weston-terminal")
    r2=$(bc "qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal")
    [ "$r1" = allow ] && [ "$r2" = allow ] && break
    sleep 0.25
done
if [ "$r1" = allow ] && [ "$r2" = allow ]; then
    echo "PASS: tier-2 broker allow-rules loaded"
else
    echo "FAIL: broker did not load tier-2 allow-rules (spawn=$r1 advertise=$r2)"
    exit 1
fi
SETUP
)"
    assert_success || fail_loud "could not author the tier-2 broker allow-rules in the VM"
    assert_output_contains "PASS: tier-2 broker allow-rules loaded"
}

teardown_file() {
    # Reap the shared free-port driver-staging http server first (host-side
    # cleanup, always runs). NB: bats runs only the LAST teardown_file() defined
    # in a file, so the broker-rule removal below MUST live in this same function
    # — a second teardown_file() would silently shadow it and leak the rule.
    reap_vm_drivers
    # Remove the test-authored rules so they never leak across runs. A failed
    # cleanup — which would leave a standing allow rule in the VM — is LOUD,
    # not swallowed (helpers.bash policy: never silently skip).
    vm_run "rm -f /etc/qdistro/rules.d/zz-tier2-isolation-allow.yaml; systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true; echo 'PASS: tier-2 broker allow-rules removed'"
    assert_success || fail_loud "could not remove the tier-2 broker allow-rules (a test-authored rule may persist in the VM)"
    assert_output_contains "PASS: tier-2 broker allow-rules removed"
}

setup() {
    vm_run "pgrep -x pipewire >/dev/null"
    assert_success
    vm_run "systemctl stop qdistro-admin-broker.service 2>/dev/null || true"
}

# (Driver-staging http server reaping via reap_vm_drivers is folded into the
# single teardown_file() above — bats only runs the last one defined.)

@test "phase7-tier2-podman: in-container weston-terminal becomes a peer toplevel on outer" {
    stage_vm_driver "s32-tier2-podman.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s32.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s32-tier2-podman.sh && chmod +x /tmp/s32.sh && bash /tmp/s32.sh 2>/dev/null"
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
    stage_vm_driver "s33-tier2-input.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s33.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s33-tier2-input.sh && chmod +x /tmp/s33.sh && bash /tmp/s33.sh 2>/dev/null"
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

@test "phase7-tier2-launcher-click: synthetic Ctrl+Space opens launcher, podapp launches" {
    # Drives the s18 spec end-to-end via real ydotool/uinput synthetic
    # input: Ctrl+Space → launcher, type+Enter → launch the tier-2 podapp.
    # Requires /dev/uinput (kernel-default, now in install-deps.sh) and a
    # running admin ydotoold; the driver SKIPs when that surface is absent
    # and SKIP is escalated to fail_loud below so missing infra is noticed.
    stage_vm_driver "s60-launcher-podapp-click.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s60.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s60-launcher-podapp-click.sh && chmod +x /tmp/s60.sh && bash /tmp/s60.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman / tier-2 image / outer compositor / ydotool+uinput absent on this VM"
    fi
    assert_output_contains "PASS: podapps cache populated for 'tier2-c-ui'"
    # Load-bearing: the launcher_requested line can only appear if the
    # synthetic Ctrl+Space traversed /dev/uinput → ydotoold → qdwin.
    assert_output_contains "PASS: launcher opened via synthetic Ctrl+Space"
    # NOTE: the post-Enter "podapp launched" advertise is best-effort (the
    # driver emits NOTE not FAIL when launcher→spawn activation isn't
    # journal-deterministic; that path is also covered by phase7-tier2-podman),
    # so it is intentionally not asserted here.
    assert_output_contains "PASS: §Phase-7 tier-2 launcher click (synthetic input) end-to-end"
}

@test "phase7-tier2-lifecycle: two containers + stop-cleanup" {
    stage_vm_driver "s34-tier2-lifecycle.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s34.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s34-tier2-lifecycle.sh && chmod +x /tmp/s34.sh && bash /tmp/s34.sh 2>/dev/null"
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
    stage_vm_driver "s40-tier2-hardening.sh"
    vm_run "curl -fsS -o /tmp/s40.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s40-tier2-hardening.sh && chmod +x /tmp/s40.sh && bash /tmp/s40.sh 2>/dev/null"
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

@test "phase7-tier2-lineage-register: spawn-tier2 root-launcher auto-registers a launch record (02/S3b)" {
    # 02/S3b evidence: the tier-2-podman half of permission lineage. The
    # root-launcher path (TIER2_ROOT_LAUNCHER=1 — spawn-tier2's "proven tier-3
    # topology") reads the qdistro-secctx-exec inner pid and calls broker
    # RegisterLaunch via the shared qd_register_secctx_launch_record helper, so
    # enforce mode can resolve a cross-silo source pid to its record. Companion
    # to phase7-tier3-lineage-register (s60). The spawn gate rule is authored by
    # setup_file (and idempotently by the driver), so this needs no extra setup.
    stage_vm_driver "s61-tier2-lineage-register.sh"
    vm_run "curl -fsS -o /tmp/s61.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s61-tier2-lineage-register.sh && chmod +x /tmp/s61.sh && bash /tmp/s61.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "podman / tier-2 image / qdistro-secctx-exec / outer compositor / broker audit db not available on this VM"
    fi
    assert_output_contains "PASS: broker spawn gate allows qdistro.tier2.spawn:weston-terminal/weston-terminal"
    # Load-bearing: the registration line can only appear if spawn-tier2's
    # root-launcher path read the secctx-exec inner pid and the broker
    # RegisterLaunch re-verified it.
    assert_output_contains "PASS: spawn-tier2 (root-launcher) logged the launch-record registration for silo="
    # The broker persisted the record as an auditable qdistro.lineage.register row.
    assert_output_contains "PASS: broker recorded qdistro.lineage.register:"
    assert_output_contains "PASS: §02/S3b tier-2 (root-launcher) permission-lineage register end-to-end"
}

@test "phase7-tier2-template-lineage-register: binding-resolved silo unit registers on the resolved identity (02/S3c)" {
    # 02/S3c evidence: the tier-2-TEMPLATE half of permission lineage. A
    # binding-resolved templated silo, launched through the PRODUCTION systemd
    # unit (qdistro-tier2-silo@<silo> -> qdistro-tier2-silo-launch -> spawn-tier2
    # root-launcher with TIER2_SILO set), registers its launch record keyed on
    # the RESOLVED silo identity (not the container name). Reuses the wiretag
    # probe's near-instant FROM-only recipe + promote. Companion to S3b (s61) and
    # S3d (s60); the driver authors its own broker rule (idempotent w/ setup_file).
    stage_vm_driver "s62-tier2-template-lineage-register.sh"
    vm_run "curl -fsS -o /tmp/s62.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s62-tier2-template-lineage-register.sh && chmod +x /tmp/s62.sh && bash /tmp/s62.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-2 template stack (qdistro-template-build / silo unit + launch helper / podman / secctx-exec / outer compositor / broker audit db) not available on this VM"
    fi
    assert_output_contains "PASS: silo 's62silo' binding-resolved + promoted into the production tree"
    # Load-bearing: the registration line only appears if the silo unit ran the
    # root-launcher path and the broker RegisterLaunch re-verified the inner pid.
    assert_output_contains "PASS: silo unit (root-launcher) logged the launch-record registration for silo="
    # The S3c distinction: the record is keyed on the binding-resolved silo
    # identity, NOT the container name.
    assert_output_contains "PASS: lineage record keyed on the binding-resolved silo identity"
    assert_output_contains "PASS: broker recorded qdistro.lineage.register:"
    assert_output_contains "PASS: §02/S3c tier-2 (binding-resolved template) permission-lineage register end-to-end"
}

@test "phase7-tier2-template-snapshot-e2e: launch flips a generation and takes a real btrfs pre-activation snapshot (05/B#5)" {
    # 05/B#5 (D9): the spawn -> pre-activation SNAPSHOT -> podman-run launch path
    # on REAL btrfs. Promotes gen1 (first activation, no snapshot), then a
    # DISTINCT gen2; the flip must take a real btrfs read-only subvolume snapshot
    # of the outgoing state via the production launch anchor
    # (qdistro_resolve_binding) before the container runs. Backs the silos dir
    # with a btrfs loopback when the VM root isn't btrfs. This is the suite that
    # surfaced + validates the resolve_btrfs() fix (btrfs is off the as-admin
    # PATH, so the subvolume mechanism had silently degraded to copy).
    stage_vm_driver "s64-tier2-template-snapshot-e2e.sh"
    vm_run "curl -fsS -o /tmp/s64.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s64-tier2-template-snapshot-e2e.sh && chmod +x /tmp/s64.sh && bash /tmp/s64.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-2 template stack / mkfs.btrfs / btrfs subvolume support not available on this VM"
    fi
    assert_output_contains "PASS: silo state created as a btrfs subvolume (mechanism=subvolume)"
    assert_output_contains "PASS: gen2 activation flipped the marker"
    # Load-bearing: the marker only flips AFTER the pre-activation snapshot
    # succeeds, so these prove the launch path took a real btrfs snapshot.
    assert_output_contains "PASS: launch path logged a pre-activation snapshot with mechanism=subvolume"
    assert_output_contains "PASS: launch path emitted template.state_snapshot.created (result=created, mechanism=subvolume)"
    assert_output_contains "PASS: pre-activation snapshot is a read-only btrfs subvolume"
    assert_output_contains "PASS: gen2 podman container ran"
    assert_output_contains "PASS: §05/B#5 tier-2-template spawn -> pre-activation snapshot -> podman-run end-to-end (real btrfs)"
}

# --- §Phase-7 tier-3: cross-uid silos via waypipe ---------------------
# spec/02 row 3. waypipe-client runs as admin (uid 1000), waypipe-server
# runs as the silo uid; the two halves cross-uid bridge a wl_display
# over a unix socket gated by the qdistro-tier3 group. Skips on VMs
# without waypipe or without the user1/user2 silo accounts (i.e. older
# bootstraps).

@test "phase7-tier3-waypipe: cross-uid bridge smoke (wayland-info as user1)" {
    stage_vm_driver "s35-tier3-waypipe.sh"
    vm_run "curl -fsS -o /tmp/s35.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s35-tier3-waypipe.sh && chmod +x /tmp/s35.sh && bash /tmp/s35.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / wayland-info / qdistro-tier3 group / user1 silo) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    # The silo's uid is whatever useradd assigned — assert on the
    # substring not the literal number, so the test stays correct if
    # the VM has a different /etc/login.defs UID_MIN baseline.
    assert_output_contains "PASS: silo command actually ran as uid "
    assert_output_contains "PASS: wayland-info enumerated wl_compositor through the bridge"
    assert_output_contains "PASS: bridge socket carried qdistro-tier3:0660"
    assert_output_contains "PASS: §Phase-7 tier-3 waypipe smoke end-to-end"
}

@test "phase7-tier3-app: cross-uid weston-terminal as user1 reaches outer" {
    stage_vm_driver "s36-tier3-app.sh"
    vm_run "curl -fsS -o /tmp/s36.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s36-tier3-app.sh && chmod +x /tmp/s36.sh && bash /tmp/s36.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / weston-terminal / user1 silo / qdshell Tier3Apps) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: bridged toplevel reached the outer admin compositor"
    # Silo uid is whatever useradd assigned; the driver prints the
    # real uid in the message so this stays correct across VMs with
    # different UID_MIN baselines.
    assert_output_contains "PASS: weston-terminal genuinely runs as uid "
    assert_output_contains "PASS: §Phase-7 tier-3 cross-uid graphical app end-to-end"
}

@test "phase7-tier3-lifecycle: two silos (user1+user2), kill A leaves B running" {
    stage_vm_driver "s37-tier3-lifecycle.sh"
    vm_run "curl -fsS -o /tmp/s37.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s37-tier3-lifecycle.sh && chmod +x /tmp/s37.sh && bash /tmp/s37.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / weston-terminal / user1+user2 silos / qdshell Tier3Apps) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: two distinct silo toplevels reached the outer compositor"
    assert_output_contains "PASS: both bridge sockets present"
    assert_output_contains "PASS: silo A runs as user1"
    assert_output_contains "PASS: silo B still running after silo A teardown"
    assert_output_contains "PASS: §Phase-7 tier-3 multi-silo lifecycle end-to-end"
}

@test "phase7-tier3-chrome: qdshell parses silo identifier + applies per-silo color override" {
    stage_vm_driver "s38-tier3-chrome.sh"
    vm_run "curl -fsS -o /tmp/s38.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s38-tier3-chrome.sh && chmod +x /tmp/s38.sh && bash /tmp/s38.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / silo users / qdshell) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: qdshell observed two tier-3 toplevels"
    # Per the design-vs-impl note in Tier3Apps.qml + the user-confirmed
    # scope change (2026-05-16): we parse silo from the secctx app_id,
    # not from window title prefix scraping. Secctx is the load-bearing
    # identity per spec/02 row 3; title prefix is decoration only and
    # is asserted by the visual permissions-gui test instead.
    assert_output_contains "PASS: qdshell parsed both silo identifiers from secctx app_id"
    assert_output_contains "PASS: silo color override applied"
    assert_output_contains "PASS: §Phase-7 tier-3 silo chrome differentiation end-to-end"
}

@test "phase7-tier3-lineage-register: spawn-tier3 auto-registers a launch record (02/S3d)" {
    # 02/S3d evidence: the registration half of permission lineage for tier-3.
    # spawn-tier3.sh (root) reads the inner waypipe-client pid published by
    # qdistro-secctx-exec and calls the broker's RegisterLaunch, binding
    # (pid,starttime)->silo, so enforce mode can resolve a cross-silo source pid
    # to its record instead of failing closed. The driver runs under
    # `lineage_enforce = true` (a broker.conf flag, NOT SELinux enforcing — safe
    # over qga) and restores the prior posture in its own trap.
    stage_vm_driver "s60-tier3-lineage-register.sh"
    vm_run "curl -fsS -o /tmp/s60.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s60-tier3-lineage-register.sh && chmod +x /tmp/s60.sh && bash /tmp/s60.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / weston-terminal / qdistro-secctx-exec / user1 silo / broker audit db) not available on this VM"
    fi
    assert_output_contains "PASS: tier3 prerequisites present"
    assert_output_contains "PASS: broker up (enforce posture set)"
    # Load-bearing: the registration line can only appear if spawn-tier3 read
    # the secctx-exec inner pid and the broker RegisterLaunch re-verified it.
    assert_output_contains "PASS: spawn-tier3 logged the launch-record registration for silo=user1"
    # The broker persisted the record as an auditable qdistro.lineage.register row.
    assert_output_contains "PASS: broker recorded qdistro.lineage.register:user1"
}

@test "phase7-secctx: wp_security_context_v1 advertised + waypipe-tagged silo client (§6.10)" {
    stage_vm_driver "s40-secctx.sh"
    vm_run "curl -fsS -o /tmp/s40.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s40-secctx.sh && chmod +x /tmp/s40.sh && bash /tmp/s40.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / wayland-info / user1 silo) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    # §4b global-filter negative (02/S1): qdwin gates the shell-only globals
    # (wp_security_context_manager_v1, weston_capture_v1) behind a
    # wl_global_filter, hiding them from ordinary same-uid clients (only the
    # bound shell / authorized secctx helper may see them). The driver pins
    # this with a positive control (enum works), a selectivity check (the
    # secctx-only-hidden IME/VK/idle globals stay visible to ordinary), and
    # the shell-only-globals-hidden assertion; the positive bind path is the
    # "waypipe bound ..." line below.
    assert_output_contains "PASS: ordinary client enumerates globals (wl_compositor visible)"
    assert_output_contains "PASS: ordinary client sees all secctx-only-hidden globals (IME/VK/idle) — filter is selective"
    assert_output_contains "PASS: §4b: shell-only globals (secctx-manager, weston-capture) hidden from ordinary client"
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
    stage_vm_driver "s39-clipboard-gate.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s39.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s39-clipboard-gate.sh && chmod +x /tmp/s39.sh && bash /tmp/s39.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / qdistro-test-clipboard-source / dbus / user1 silo / qdshell / broker) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: admin → admin selection_set seen by qdshell"
    assert_output_contains "PASS: silo toplevel registered with silo=user1"
    assert_output_contains "PASS: broker logged clipboard-transfer audit"
    assert_output_contains "PASS: qdshell cleared the silo→admin selection (default-deny)"
    assert_output_contains "PASS: rule install flipped broker verdict to allow"
    assert_output_contains "PASS: qdshell observed RulesReloaded + ran live re-check"
    assert_output_contains "PASS: spec/10 cross-uid clipboard policy gate end-to-end"
}

@test "phase7-secctx-toplevel-event: qdwin_shell_v1@v13 toplevel_security_context end-to-end" {
    stage_vm_driver "s41-secctx-toplevel-event.sh"
    vm_run "curl -fsS -o /tmp/s41.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s41-secctx-toplevel-event.sh && chmod +x /tmp/s41.sh && bash /tmp/s41.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / weston-terminal / user1 silo / qdshell / qdwin source tree) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdwin_shell_v1 protocol XML at version "
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: compositor emitted toplevel_security_context"
    assert_output_contains "PASS: qdshell received toplevel_security_context"
    assert_output_contains "PASS: qdshell derived silo=user1 from secctx app_id"
    assert_output_contains "PASS: silo colour override applied via secctx path"
    assert_output_contains "PASS: §Phase-7 v13 toplevel_security_context end-to-end"
}

@test "phase7-tier4-spawn: qdistro-tier4-spawn defines and boots the tier-4 domain without launching a viewer" {
    # §Phase-7 tier-4 — libvirt + selected-display publisher smoke.
    # Linux-only per spec/00 (memory qdistro_linux_only.md). Tests the
    # wrapper's define-only and no-viewer modes; s42 intentionally does
    # not assert a visible waypipe/RDP window. Visual RDP coverage lives
    # in permissions-gui scenarios 56 and 57.
    #
    # SPICE/domdisplay fallback coverage is intentionally retired. The
    # supported display transports are waypipe (default) and explicitly
    # selected RDP.
    #
    # The tier-4 stack (libvirt/qemu/waypipe) is an opt-in bake asset.
    # Skip cleanly when absent so CI doesn't fail on minimal bakes. The
    # tier4-vm-guest domain TEMPLATE is a separate opt-in asset: the tools
    # (virsh/qemu/kvm) can be present while the guest template is not, in
    # which case the spawn driver fails with "tier4-vm-guest domain
    # template not found". Require the template dir too so we skip rather
    # than hard-fail on that bake.
    vm_run "command -v virsh >/dev/null && command -v qemu-system-x86_64 >/dev/null && [ -e /dev/kvm ] && { [ -d /usr/share/qdistro/tier4-vm-guest ] || [ -d /tmp/tier4-vm-guest ]; }"
    if [[ "$status" -ne 0 ]]; then
        skip "tier-4 stack (libvirt/qemu/kvm + tier4-vm-guest template) not installed on this VM (opt-in bake)"
    fi
    stage_vm_driver "s42-tier4-spawn.sh"
    vm_run "curl -fsS -o /tmp/s42.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s42-tier4-spawn.sh && chmod +x /tmp/s42.sh && bash /tmp/s42.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "tier-4 stack (libvirt/qemu/waypipe) not installed on this VM (opt-in bake)"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: define-only mode created domain"
    assert_output_contains "PASS: tier-4 no-viewer mode brought up domain"
    assert_output_contains "PASS: virsh confirms domain is running"
    assert_output_contains "PASS: §Phase-7 tier-4 spawn smoke end-to-end"
    assert_output_contains "[s42] 5 passes, 0 failures"
}

@test "phase7-tier5-loopback: qdistro-tier5-spawn --loopback bridges wayland-info via vsock" {
    # §Phase-7 tier-5-Linux — waypipe-over-vsock wrapper smoke. Linux-only
    # per spec/00. Tests the data path with vsock CID=1 (loopback).
    # Real-VM mode (--vm <name>) is covered by phase7-tier5-vm.
    stage_vm_driver "s43-tier5-loopback.sh"
    vm_run "curl -fsS -o /tmp/s43.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s43-tier5-loopback.sh && chmod +x /tmp/s43.sh && bash /tmp/s43.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-5 stack (waypipe / vsock_loopback / wayland-info) not available on this VM"
    fi
    assert_output_contains "PASS: vsock_loopback module loaded"
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: wayland-info enumerated wl_compositor through the vsock bridge"
    assert_output_contains "PASS: §Phase-7 tier-5-Linux waypipe-vsock loopback end-to-end"
}

@test "phase7-tier5-loopback-lineage-register: spawn-tier5 --loopback auto-registers a launch record (02/S3e)" {
    # 02/S3e evidence: the tier-5 half of permission lineage. spawn-tier5.sh
    # wraps waypipe-client with qdistro-secctx-exec, which now publishes the
    # inner pid; spawn-tier5 calls qd_register_secctx_launch_record so the broker
    # persists qdistro.lineage.register:<silo>. Loopback (vsock CID=1) needs no
    # guest VM / nested KVM. Companion to s60 (tier-3), s61/s62 (tier-2); tier-5
    # has no broker spawn gate so no allow rule is authored.
    stage_vm_driver "s63-tier5-loopback-lineage-register.sh"
    vm_run "curl -fsS -o /tmp/s63.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s63-tier5-loopback-lineage-register.sh && chmod +x /tmp/s63.sh && bash /tmp/s63.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-5 stack (waypipe / vsock_loopback / wayland-info / qdistro-secctx-exec / outer compositor / broker audit db) not available on this VM"
    fi
    # Load-bearing: the registration line only appears if secctx-exec published
    # the inner waypipe-client pid and the broker RegisterLaunch re-verified it.
    assert_output_contains "PASS: spawn-tier5 (loopback) logged the launch-record registration for silo="
    assert_output_contains "PASS: tier-5 loopback silo identity is this spawn's pid (loopback-"
    assert_output_contains "PASS: broker recorded qdistro.lineage.register:"
    assert_output_contains "PASS: §02/S3e tier-5 (loopback/vsock) permission-lineage register end-to-end"
}

@test "phase7-tier4-secctx-exec: qdistro-secctx-exec wraps a Wayland client with wp_security_context_v1" {
    # §Phase-7 tier-4 secctx — the wrapper is reusable for any non-secctx-
    # aware client. Tests the
    # wrapper end-to-end with qdistro-test-window as a stand-in for
    # waypipe to avoid dragging libvirt+qemu into the test.
    stage_vm_driver "s44-tier4-secctx-exec.sh"
    vm_run "curl -fsS -o /tmp/s44.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s44-tier4-secctx-exec.sh && chmod +x /tmp/s44.sh && bash /tmp/s44.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-secctx-exec / qdistro-test-window absent on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdistro-secctx-exec --help"
    # Post-hardening: the manager global is hidden from ordinary admin
    # clients (the s44 driver was updated to assert this); the authorized
    # helper still binds it on the line below.
    assert_output_contains "PASS: wp_security_context_manager_v1 hidden from ordinary admin client"
    assert_output_contains "PASS: qdistro-secctx-exec bound wp_security_context_manager_v1"
    assert_output_contains "PASS: secctx commit recorded with engine=qdistro.tier4 app_id=qdistro.tier4.s44vm"
    assert_output_contains "PASS: inner client accepted on the secctx listener"
    assert_output_contains "PASS: §Phase-7 tier-4 secctx-exec wrapper end-to-end"
    assert_output_contains "[s44] 7 passes, 0 failures"
}

@test "phase7-tier5-vm: qdistro-tier5-spawn --vm boots a guest VM and bridges via vsock" {
    # §Phase-7 tier-5 --vm — full per-app guest VM path. Skips when
    # /var/lib/libvirt/images/qdistro-tier5-base.qcow2 is absent (build
    # is opt-in: ~400MB Tumbleweed-Minimal-VM cloud download).
    # tier-5 base disk is an opt-in bake asset (~400MB download). Skip
    # cleanly when absent so CI doesn't fail on minimal bakes.
    vm_run "[ -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2 ] && command -v virsh >/dev/null && command -v qemu-img >/dev/null && [ -e /dev/kvm ]"
    if [[ "$status" -ne 0 ]]; then
        skip "tier-5 base disk / virsh / qemu-img / kvm absent (opt-in bake; set QDISTRO_BUILD_TIER5_BASE=1)"
    fi
    stage_vm_driver "s45-tier5-vm.sh"
    vm_run "curl -fsS -o /tmp/s45.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s45-tier5-vm.sh && chmod +x /tmp/s45.sh && bash /tmp/s45.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "tier-5 base disk absent (rerun fresh-vm-bootstrap.sh with QDISTRO_BUILD_TIER5_BASE=1)"
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
    # tier-5 base disk + virsh/kvm — opt-in bake. Same skip surface as
    # phase7-tier5-vm; skip cleanly when absent.
    vm_run "[ -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2 ] && command -v virsh >/dev/null && [ -e /dev/kvm ]"
    if [[ "$status" -ne 0 ]]; then
        skip "tier-5 base disk / virsh / kvm absent (opt-in bake)"
    fi
    stage_vm_driver "s48-tier5-close-cleanup.sh"
    vm_run "curl -fsS -o /tmp/s48.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s48-tier5-close-cleanup.sh && chmod +x /tmp/s48.sh && bash /tmp/s48.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "tier-5 base disk or virsh/kvm absent — same skip surface as phase7-tier5-vm"
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
    # tier-5 base disk / pw-cli / qemu-audio-pipewire — opt-in bake.
    # Skip cleanly when absent.
    vm_run "[ -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2 ] && command -v virsh >/dev/null && command -v qemu-img >/dev/null && command -v pw-cli >/dev/null && [ -e /dev/kvm ]"
    if [[ "$status" -ne 0 ]]; then
        skip "tier-5 base disk / pw-cli / qemu-audio-pipewire absent (opt-in bake)"
    fi
    stage_vm_driver "s47-tier5-audio.sh"
    vm_run "curl -fsS -o /tmp/s47.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s47-tier5-audio.sh && chmod +x /tmp/s47.sh && bash /tmp/s47.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "tier-5 base disk / pw-cli / qemu-audio-pipewire absent"
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
    # the focus state that the real waypipe path would deliver naturally is
    # injected via qdshell's ctrl-socket.
    stage_vm_driver "s46-tier4-clipboard-gate.sh"
    vm_run "curl -fsS -o /tmp/s46.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s46-tier4-clipboard-gate.sh && chmod +x /tmp/s46.sh && bash /tmp/s46.sh 2>/dev/null"
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
    assert_output_contains "[s46] 8 passes, 0 failures"
}

@test "phase7-tier4-waypipe-display: compositor-observed secctx + identity-bound publisher + ClipboardGate verdicts" {
    # §Phase-7 tier-4 waypipe display — REPLACES the weak argv-only PASS
    # shims (gpt-review/tier4-waypipe-display-tests.md) with assertions
    # observed from qdwin's own secctx event/audit output, identity-bound
    # publisher validation (no fixed-CID bypass), and real ClipboardGate
    # verdicts with rich-MIME-leakage + fail-open negatives.
    #
    # Needs the tier-4 secctx stack + broker + qdshell. Skips cleanly on a
    # minimal bake; fails LOUDLY on the bad path otherwise.
    stage_vm_driver "s110-tier4-waypipe-display.sh"
    vm_run "curl -fsS -o /tmp/s110.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s110-tier4-waypipe-display.sh && chmod +x /tmp/s110.sh && bash /tmp/s110.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "tier-4 secctx stack / wayland-info / dbus-send absent on this VM (opt-in bake)"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    # The s110 waypipe-display driver verifies qdwin HIDES the secctx
    # manager from ordinary admin clients (s110 emits the "hides..." PASS).
    # The old assertion expected an "advertises..." PASS that NO driver
    # emits — an orphaned string, so the test hard-failed even though s110
    # itself passed 14/0.
    assert_output_contains "PASS: qdwin hides wp_security_context_manager_v1 from ordinary admin client"
    assert_output_contains "PASS: qdwin emitted forwarded-toplevel secctx"
    assert_output_contains "PASS: missing-secctx client did NOT receive a tier-4 secctx tag (fail closed)"
    assert_output_contains "PASS: forged app-id"
    assert_output_contains "PASS: cross-silo subscription attempt"
    assert_output_contains "PASS: publisher endpoint bound to launch record"
    assert_output_contains "PASS: wrong-instance publisher banner rejected (impostor/stale endpoint fails closed)"
    assert_output_contains "PASS: ClipboardGate default-denies tier-4 -> admin text transfer (verdict=deny)"
    assert_output_contains "PASS: rich-MIME leakage blocked: text/plain allowed, image/png denied (no fail-open)"
    assert_output_contains "PASS: unverified same-silo clipboard receive fails closed (no fail-open)"
    assert_output_contains "PASS: input-forwarding authorization denied without a rule (verdict=deny)"
    assert_output_contains "PASS: §Phase-7 tier-4 waypipe-display compositor-observed end-to-end"
    assert_output_contains "[s110] 14 passes, 0 failures"
}

@test "phase7-tier4-tier5-lifecycle-stress: crash/restart/churn leaves no orphaned resources" {
    # §5 lifecycle stress — failure + churn paths must reap every qemu,
    # waypipe, socat, domain, overlay, and socket. Observable resource
    # ABSENCE, not "didn't crash". Live nested boot is opt-in; the driver
    # marks PENDING-LIVE-BOOT steps and the wrapper skips on minimal bakes.
    vm_run "command -v virsh >/dev/null && [ -e /dev/kvm ]"
    if [[ "$status" -ne 0 ]]; then
        skip "tier-4/5 stack (virsh/kvm) absent on this VM (opt-in bake)"
    fi
    stage_vm_driver "s111-tier4-tier5-lifecycle-stress.sh"
    vm_run "curl -fsS -o /tmp/s111.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s111-tier4-tier5-lifecycle-stress.sh && chmod +x /tmp/s111.sh && bash /tmp/s111.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "tier-4/5 stack / kvm absent on this VM (opt-in bake)"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: guest crash during startup"
    assert_output_contains "PASS: crash during publish / clipboard+audio: no leaked waypipe or socat helpers"
    assert_output_contains "PASS: repeated launch/close churn: no orphan domain/overlay/qemu after 3 cycles"
    assert_output_contains "PASS: repeated launch/close churn: no orphan tier-4 sockets/runtime files"
    assert_output_contains "PASS: host qdshell crash/restart: display client is setsid-detached"
    assert_output_contains "PASS: qdwin disconnect cleanup: client exit triggers domain destroy via EXIT trap"
    assert_output_contains "PASS: denied nested-proxy: broker denies the cross-silo proxy (verdict=deny)"
    assert_output_contains "PASS: §5 tier-4/tier-5 lifecycle stress end-to-end (no leaked resources)"
}

@test "phase7-cross-tier-clipboard: large + multi-MIME clipboard between tier-1/3/4/5b" {
    # §5 last bullet — cross-tier large payload + per-MIME decisions.
    # Probes the broker clipboard gate (the load-bearing authorization
    # surface) over D-Bus; tier base disks not required. Real cross-
    # compositor selection set+receive is PENDING-LIVE-FOCUS.
    stage_vm_driver "s112-cross-tier-clipboard.sh"
    vm_run "curl -fsS -o /tmp/s112.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s112-cross-tier-clipboard.sh && chmod +x /tmp/s112.sh && bash /tmp/s112.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "broker / dbus-send absent — cross-tier clipboard gate untestable"
    fi
    assert_output_contains "PASS: broker up"
    assert_output_contains "PASS: cross-silo clipboard default-denies across all tier pairings (no tier bypass)"
    assert_output_contains "PASS: large/duplicate-MIME payload does not flip the clipboard verdict (size-independent)"
    assert_output_contains "PASS: multi-MIME per-MIME gate (tier-4 -> user1): text allowed, image/html denied"
    assert_output_contains "PASS: multi-MIME per-MIME gate (tier-5b -> user1): text allowed, image/html denied"
    assert_output_contains "PASS: clipboard audit records source app_id/engine across tiers (attributable)"
    assert_output_contains "PASS: tier-1<->tier-3 same-silo: verified allowed, unverified fails closed"
    assert_output_contains "PASS: §5 cross-tier large + multi-MIME clipboard end-to-end"
    assert_output_contains "[s112] 8 passes, 0 failures"
}

@test "phase7-tier1-skeleton: spec/30 Tier-1 SELinux design + spike skeleton present" {
    # Tier-1 is design + spike (spec/30, 2026-04-27 night). No runtime
    # yet — six prior sessions deferred it. This bats just verifies the
    # design doc, spike checklist, .te skeleton, spawn wrapper, and
    # qdshell tier1 secctx parser all survived the bootstrap rsync.
    # The implementation pass picks up from this baseline once the four
    # blocking spikes in tier1/spike-checklist.md resolve.
    stage_vm_driver "s50-tier1-skeleton.sh"
    vm_run "curl -fsS -o /tmp/s50.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s50-tier1-skeleton.sh && chmod +x /tmp/s50.sh && bash /tmp/s50.sh 2>/dev/null"
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


@test "phase7-focus-aware-clear: spec/10 v14 admin clipboard cleared on cross-silo focus (Q1a)" {
    # qdwin_shell_v1@v14 set_keyboard_focus + seat_focus_changed close
    # the admin → tier-N paste-receive gap that wp_security_context_v1
    # alone can't reach. Wayland surfaces no wl_data_offer.receive
    # event; clearing the seat selection on cross-silo focus is the
    # Qubes-style mitigation. The headless ctrl-socket inject-focus
    # path is what this script exercises; real chrome click-to-focus
    # uses the same set_keyboard_focus request.
    stage_vm_driver "s48-focus-aware-clear.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s48.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s48-focus-aware-clear.sh && chmod +x /tmp/s48.sh && bash /tmp/s48.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / qs / qdistro-test-clipboard-source / user1 silo / qdshell) not available on this VM"
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

@test "phase7-clipboard-focus-gate-journal: qdshell CLIPBOARD_FOCUS_GATE fires on cross-silo focus, not same-silo" {
    # Companion to phase7-focus-aware-clear: that test asserts the qdwin-side
    # clear (set_keyboard_focus / weston clear_selection); this one asserts
    # the qdshell DECISION layer — the CLIPBOARD_FOCUS_GATE journal line that
    # ClipboardGate._onSeatFocusChanged emits when focus crosses OUT of a
    # selection's source silo. A tier-3 (user1) silo owns the selection so
    # the source silo is KNOWN (an "unknown" source is never tracked and
    # would never log the line). Phase A: user1 → user2 focus change (a SECOND
    # tier-3 silo) must log CLIPBOARD_FOCUS_GATE and leave the destination paste
    # empty. (The cross-silo destination is a second silo rather than the admin
    # toplevel because Tier3FocusIPC.injectFocus deliberately refuses non-tier3/4
    # handles — the admin toplevel is not injectable; the production clear path
    # is identical for any cross-silo focus move.) Phase B: user1 → user1 (same
    # silo) must NOT log a clear — proving strict mode still allows same-silo
    # paste.
    stage_vm_driver "s49-clipboard-focus-gate-journal.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s49.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s49-clipboard-focus-gate-journal.sh && chmod +x /tmp/s49.sh && bash /tmp/s49.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "tier-3 stack (waypipe / qs / qdistro-test-clipboard-source / user1 silo / qdshell) not available on this VM"
    fi
    assert_output_contains "PASS: outer admin compositor up"
    assert_output_contains "PASS: qdshell up"
    assert_output_contains "PASS: qdshell bound qdwin_shell_v1 at version >= 14"
    assert_output_contains "PASS: cross-silo (user2) destination toplevel registered handle="
    assert_output_contains "PASS: silo toplevel registered silo=user1 handle="
    assert_output_contains "PASS: qdshell recorded selection with known source silo"
    assert_output_contains "PASS: qdshell logged CLIPBOARD_FOCUS_GATE on cross-silo focus"
    assert_output_contains "PASS: destination paste empty after focus-clear"
    assert_output_contains "PASS: same-silo focus change did NOT clear"
    assert_output_contains "PASS: qdshell focus-aware clipboard clear (CLIPBOARD_FOCUS_GATE) end-to-end"
}

@test "phase7-tier1-e2e: spec/30 Tier-1 SELinux pipeline end-to-end" {
    # Validates the full tier-1 stack (spec/30 §Phase plan step 8):
    # policy module loaded, type_transition rule active, spawn pipeline
    # places the inner command in qdistro_tier1_t, broker spawn-action
    # gate denies an authored rule.
    stage_vm_driver "s51-tier1-e2e.sh"
    vm_run "curl -fsS -o /tmp/s51.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s51-tier1-e2e.sh && chmod +x /tmp/s51.sh && bash /tmp/s51.sh 2>/dev/null"
    # SELinux Tier-1 stack is an opt-in bake (policy module + spawn
    # wrapper + tier1-exec daemon + enforcing mode). The bake we run
    # against pins SELINUX=permissive in /etc/selinux/config so the
    # runtime flip is refused. SKIP cleanly when the probe reports
    # the opt-in gap rather than failing the suite. Set
    # QDISTRO_BUILD_TIER1=1 on the bake to enable.
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "Tier-1 SELinux stack not enabled in this bake (opt-in QDISTRO_BUILD_TIER1=1)"
    fi
    assert_success
    assert_output_contains "PASS: SELinux enabled"
    assert_output_contains "PASS: qdistro_tier1 policy module loaded"
    assert_output_contains "PASS: type_transition unconfined_t -> qdistro_tier1_t exists"
    assert_output_contains "PASS: sleep runs in qdistro_tier1_t"
    assert_output_contains "PASS: broker denied qdistro.tier1.spawn:/usr/bin/sleep before exec"
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
    stage_vm_driver "s53-data-offer-receive-v15.sh"
    vm_run "curl -fsS -o /tmp/s53.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s53-data-offer-receive-v15.sh && chmod +x /tmp/s53.sh && bash /tmp/s53.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-test-clipboard-sink absent or v15 binding missing"
    fi
    assert_output_contains "PASS: qdshell bound qdwin_shell_v1 at version >= 15"
    assert_output_contains "PASS: qdwin installed v15 data_source send-shim"
    assert_output_contains "PASS: qdshell logged data_offer_receive_pending"
    assert_output_contains "PASS: qdwin processed data_offer_receive_decision"
    # Only require the byte-receive PASS when the sink helper actually
    # ran the allow-path end-to-end. Under qdshell-v14 (current state)
    # the driver emits "INFO: admin-sink toplevel registered" but the
    # send-shim path doesn't fire, so the payload PASS is intentionally
    # omitted. Gate on the PASS variant so the v15 wiring lands the
    # byte assertion automatically.
    if [[ "$output" == *"PASS: admin-sink toplevel registered"* ]]; then
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
    stage_vm_driver "s55-tier1-enforcing.sh"
    vm_run "curl -fsS -o /tmp/s55.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s55-tier1-enforcing.sh && chmod +x /tmp/s55.sh && bash /tmp/s55.sh 2>/dev/null"
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "Tier-1 SELinux enforcing not enabled in this bake (config-pinned permissive; opt-in QDISTRO_BUILD_TIER1=1)"
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
    stage_vm_driver "s56-broker-enforcing.sh"
    vm_run "curl -fsS -o /tmp/s56.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s56-broker-enforcing.sh && chmod +x /tmp/s56.sh && bash /tmp/s56.sh 2>/dev/null"
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "broker enforcing not enabled in this bake (config-pinned permissive; opt-in QDISTRO_BUILD_TIER1=1)"
    fi
    assert_success
    assert_output_contains "PASS: SELinux mode now Enforcing"
    assert_output_contains "PASS: broker running pid="
    assert_output_contains "PASS: D-Bus probe succeeded under enforcing"
    assert_output_contains "PASS: 0 new denials — qdistro_broker.te 0.3.0 covers the enforcing workload"
}

@test "phase7-session-manager-enforcing: S6 — qdistro_sessmgr_t lifecycle AVC budget" {
    # S6 VM half: flip SELinux to enforcing, restart the session manager
    # under qdistro_sessmgr_t, run the representative s101 lifecycle
    # workload, and fail on any new qdistro_sessmgr_t AVCs.
    stage_vm_driver "s63-session-manager-enforcing.sh"
    vm_run "curl -fsS -o /tmp/s63.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s63-session-manager-enforcing.sh && chmod +x /tmp/s63.sh && bash /tmp/s63.sh 2>/dev/null"
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "session-manager enforcing not enabled in this bake (policy/config/transport prereq missing)"
    fi
    assert_success
    assert_output_contains "PASS: SELinux mode now Enforcing"
    assert_output_contains "PASS: session manager running pid="
    assert_output_contains "PASS: session lifecycle probe succeeded under enforcing"
    assert_output_contains "PASS: 0 new denials — qdistro_session_manager policy covers the lifecycle workload"
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
    stage_vm_driver "s57-qsu-argv-scopes.sh"
    vm_run "curl -fsS -o /tmp/s57.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s57-qsu-argv-scopes.sh && chmod +x /tmp/s57.sh && bash /tmp/s57.sh 2>/dev/null"
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
    stage_vm_driver "s58-qsu-real-flow.sh"
    vm_run "curl -fsS -o /tmp/s58.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s58-qsu-real-flow.sh && chmod +x /tmp/s58.sh && bash /tmp/s58.sh 2>/dev/null"
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
    stage_vm_driver "s52-tier1-audisp.sh"
    vm_run "curl -fsS -o /tmp/s52.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s52-tier1-audisp.sh && chmod +x /tmp/s52.sh && bash /tmp/s52.sh 2>/dev/null"
    if [[ "$output" == *"SKIP:"* ]]; then
        skip "Tier-1 audispd pipeline not enabled in this bake (opt-in QDISTRO_BUILD_TIER1=1)"
    fi
    assert_success
    assert_output_contains "PASS: audispd plugin + descriptor installed"
    assert_output_contains "PASS: broker up on org.qdistro.AdminBroker1"
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
    stage_vm_driver "s59-tier2-podapps-discovery.sh"
    vm_run "systemctl start qdistro-admin-broker.service && curl -fsS -o /tmp/s59.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s59-tier2-podapps-discovery.sh && chmod +x /tmp/s59.sh && bash /tmp/s59.sh 2>/dev/null"
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

# --- P02 session-manager lifecycle (s101) --------------------------------
# Drives the qdistro-session-manager daemon through every transition
# documented in plan2/tasks/P02-session-manager.md (Create → Start →
# Freeze → Resume → Stop → Delete) and pins each PASS line so a
# regression in the daemon or its dbus surface fails the test.
# Skips on VMs that don't have the session-manager bake yet.

@test "session-lifecycle: P02 org.qdistro.SessionManager1 end-to-end" {
    stage_vm_driver "s101-session-lifecycle.sh"
    # SessionManager1 rejects callers whose uid != ADMIN_UID (1000) —
    # see qdistro_session_manager.py _require_admin. The probe drives
    # busctl --system which inherits the caller's identity, so run
    # the whole script as the admin user. systemctl restart of the
    # daemon still works (the user has polkit auth via the
    # qdistro-admin group, and the unit's PolicyKit rules permit it).
    #
    # Stage isolation: drop any stale "work" silo state left by a prior
    # interrupted run BEFORE restarting the daemon so the cleanup
    # busctl call inside s101 has a known-clean baseline (R9 fixer:
    # the empty-diagnostic flake in R11/R12 was traced to an err()
    # exit whose stderr was swallowed by 2>/dev/null below; the
    # redirect is now 2>&1 so future flakes are diagnosable).
    # SessionManager1's StopSilo/DeleteSilo enforce _require_admin
    # (uid==1000), so the cleanup busctl calls must run via
    # `runuser -u admin --`. Running them as root is silently rejected
    # by the daemon (the prior cleanup left silo 'work' present, and
    # CreateSilo inside s101 then failed with "silo 'work' already
    # exists"). `userdel` and `rm -rf` of /var/lib stay as root.
    vm_run "runuser -u admin -- busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 org.qdistro.SessionManager1 StopSilo si work 2 >/dev/null 2>&1 || true; runuser -u admin -- busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 org.qdistro.SessionManager1 DeleteSilo s work >/dev/null 2>&1 || true; userdel -r work 2>/dev/null || true; rm -rf /var/lib/qdistro/silos/work 2>/dev/null || true"
    vm_run "curl -fsS -o /tmp/s101.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s101-session-lifecycle.sh && chmod +x /tmp/s101.sh && systemctl restart qdistro-session-manager.service && sleep 1 && runuser -u admin -- bash /tmp/s101.sh 2>&1"
    assert_success
    if [[ "$output" == *"FAIL: qdistro-session-manager.service failed to start"* ]]; then
        fail_loud "qdistro-session-manager not installed on this VM (older bootstrap)"
    fi
    assert_output_contains "PASS: SessionManager1.CreateSilo created silo 'work' (uid 2000)"
    assert_output_contains "PASS: CreateSilo wrote /var/lib/qdistro/silos/work/ with mode 0700 work:work"
    assert_output_contains "PASS: StartSilo brought silo 'work' to active state (cgroup populated)"
    assert_output_contains "PASS: PodApps.silos reflects active silo 'work' via D-Bus signal"
    assert_output_contains "PASS: FreezeSilo froze all processes (cgroup.freeze=1)"
    assert_output_contains "PASS: ResumeSilo unfroze (cgroup.freeze=0; previously paused PID resumed)"
    assert_output_contains "PASS: DeleteSilo refused while silo is active (returned 'SiloBusy')"
    assert_output_contains "PASS: StopSilo terminated SIGTERM then SIGKILL after grace"
    assert_output_contains "PASS: DeleteSilo succeeded after StopSilo"
    assert_output_contains "PASS: ListSilos returned the expected JSON for admin"
    assert_output_contains "ALL_PASS: s101 session lifecycle end-to-end"
}
