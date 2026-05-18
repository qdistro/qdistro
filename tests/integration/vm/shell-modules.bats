#!/usr/bin/env bats
# §6.6 PyQt shell migration suite. One scenario per staged milestone;
# runs the same qdwin / qdshell binaries bats-6.5 does, so a fresh
# baseweed clone with fresh-vm-bootstrap.sh covers both files.

load helpers

# Round 6 Phase 3 (2026-05-18): §6.6 and §6.8 probes that drive the
# legacy interactive Python qdshell (qdshell.py + ctrl-socket + panel
# /notifications/launcher/locker modules) are archived under
# qdistro-old-plan/compositor/{qdshell,spike-6.5}/. The current
# production qdshell is QML/Quickshell and does not expose a ctrl
# socket; rewriting these probes against QML qdshell belongs to a
# separate Phase-3 ticket. Probes whose deploy-stage src files are
# also archived (bootstrap-qdwin-in-vm.sh, com.qdistro.Notifications1
# .conf, qdistro-admin-approval-app.py) are skipped for the same
# reason.
SKIP_LEGACY_QDSHELL_PY="legacy §6.6 qdshell.py interactive probe (panel/notifications/launcher/locker/ctrl-socket) — current qdshell is QML-based; needs Phase-3 rewrite ticket"
SKIP_LEGACY_DEPLOY_FILES="legacy §6.6 deploy files (bootstrap-qdwin-in-vm.sh / com.qdistro.Notifications1.conf / qdistro-admin-approval-app.py) archived under qdistro-old-plan/compositor/; needs Phase-3 rewrite ticket"
SKIP_LEGACY_NESTED_QDSHELL="legacy §6.8 nested probe drives qdshell.py for broker/pixel/dmabuf path — needs Phase-3 rewrite ticket"

setup() {
    vm_run "pgrep -x pipewire >/dev/null"
    assert_success
    vm_run "systemctl stop qdistro-admin-broker.service 2>/dev/null || true"
}

@test "s6.6-s1-panel: attach_panel places view + reserves work-area" {
    skip "$SKIP_LEGACY_QDSHELL_PY"
    vm_run "bash /root/s10-panel.sh"
    assert_success
    assert_output_contains "PASS: attach_panel wired end-to-end"
    assert_output_contains "PASS: panel geometry = geom=0,"
    assert_output_contains "PASS: maximise respects 32px panel exclusive zone"
    assert_output_contains "PASS: §6.6 S1 panel end-to-end"
}

@test "s6.6-s2-notifications: Notify bubble round-trip + tray watcher" {
    skip "$SKIP_LEGACY_QDSHELL_PY"
    vm_run "bash /root/s11-notifications.sh"
    assert_success
    assert_output_contains "PASS: notifications daemon claimed org.freedesktop.Notifications"
    assert_output_contains "PASS: attach_notification reached compositor"
    assert_output_contains "PASS: bubble visible via ctrl-socket"
    assert_output_contains "PASS: bubble expired after 3s"
    assert_output_contains "PASS: SNI watcher accepts RegisterStatusNotifierItem"
    assert_output_contains "PASS: §6.6 S2 notifications + tray end-to-end"
}

@test "s6.6-s3-s4-launcher-switcher: toggle + filter + cycle + commit" {
    skip "$SKIP_LEGACY_QDSHELL_PY"
    vm_run "bash /root/s12-launcher.sh"
    assert_success
    assert_output_contains "PASS: launcher module installed"
    assert_output_contains "PASS: launcher attached on toggle"
    assert_output_contains "PASS: indexed "
    assert_output_contains "PASS: filter applied"
    assert_output_contains "PASS: launcher toggles off"
    assert_output_contains "PASS: switcher attached + cycling"
    assert_output_contains "PASS: switcher dismissed on commit"
    assert_output_contains "PASS: §6.6 S3/S4 launcher + switcher end-to-end"
}

@test "s6.6-s5-locker: lock + unlock drives set_locked round-trip" {
    skip "$SKIP_LEGACY_QDSHELL_PY"
    vm_run "bash /root/s13-locker.sh"
    assert_success
    assert_output_contains "PASS: locker module installed"
    assert_output_contains "PASS: lock drove attach_lock_surface + set_locked=1"
    assert_output_contains "PASS: locker state reports locked"
    assert_output_contains "PASS: unlock drove set_locked=0"
    assert_output_contains "PASS: locker unlocked"
    assert_output_contains "PASS: §6.6 S5 locker end-to-end"
}

@test "s6.6-s5-locker-auth: PAM + fprint + no-auth refusal" {
    skip "$SKIP_LEGACY_QDSHELL_PY"
    vm_run "bash /root/s14-locker-auth.sh"
    assert_success
    assert_output_contains "PASS: locker module installed (auth mode)"
    assert_output_contains "PASS: lock armed"
    assert_output_contains "PASS: no-auth unlock refused (lock stays armed)"
    assert_output_contains "PASS: bad password refused"
    assert_output_contains "PASS: fprint call did not crash"
    assert_output_contains "PASS: §6.6 S5 full locker auth end-to-end"
}

@test "s6.6-s6-nested-hosting: nested weston registers as outer toplevel" {
    skip "$SKIP_LEGACY_QDSHELL_PY"
    vm_run "bash /root/s15-nested-hosting.sh"
    assert_success
    assert_output_contains "PASS: qdshell reachable; baseline toplevel count="
    assert_output_contains "PASS: nested weston appears as outer toplevel"
    assert_output_contains "PASS: nested teardown cleaned up outer toplevel"
    assert_output_contains "PASS: §6.6 S6 nested-compositor hosting"
}

@test "s6.6-s7-greetd-cutover: unit deploys on tty3 + qdshell installed" {
    skip "$SKIP_LEGACY_DEPLOY_FILES"
    vm_run "bash /root/s16-greetd-cutover.sh"
    assert_success
    assert_output_contains "PASS: deploy bootstrap completed"
    assert_output_contains "PASS: greetd-qdwin.service loaded"
    assert_output_contains "PASS: unit TTYPath=/dev/tty3 (S7 cutover)"
    assert_output_contains "PASS: greetd config vt=3"
    assert_output_contains "PASS: qdistro-start-qdwin installed"
    assert_output_contains "PASS: qdshell deployed at /usr/share/qdshell"
    assert_output_contains "PASS: qdwin_shell_v1 bindings generated"
    assert_output_contains "PASS: §6.6 S7 greetd-qdwin-on-tty3 cutover deployed"
}

@test "s6.6-notify-relay: cross-uid notification via system-bus relay" {
    skip "$SKIP_LEGACY_DEPLOY_FILES"
    vm_run "bash /root/s17-notify-relay.sh"
    assert_success
    assert_output_contains "PASS: notifications module installed"
    assert_output_contains "PASS: system-bus relay owned by"
    assert_output_contains "PASS: relay bubble reached admin's compositor"
    assert_output_contains "PASS: §6.6 per-uid notification relay end-to-end"
}

@test "s6.6-approval-app: app installed + broker round-trip" {
    skip "$SKIP_LEGACY_DEPLOY_FILES"
    vm_run "bash /root/s18-approval-app.sh"
    assert_success
    assert_output_contains "PASS: approval app installed at"
    assert_output_contains "PASS: approval app imports cleanly"
    assert_output_contains "PASS: broker active"
    assert_output_contains "PASS: GetPending round-trip ok"
    assert_output_contains "PASS: pending queue has"
    assert_output_contains "PASS: §6.6 admin-approval-app broker round-trip end-to-end"
}

@test "s6.8-s0-nested-v1-bind: advertise_toplevel round-trip stub" {
    vm_run "bash /root/s21-nested-v1-bind.sh"
    assert_success
    assert_output_contains "PASS: weston + qdwin started"
    assert_output_contains "PASS: bound qdwin_nested_manager_v1 v2"
    assert_output_contains "PASS: received configured(800x600)"
    assert_output_contains "PASS: set_title/set_app_id/set_geometry accepted"
    assert_output_contains "PASS: §6.8 S0 stub probe"
    assert_output_contains "PASS: §6.8 S0 qdwin_nested_v1 bind + advertise_toplevel"
}

@test "s6.6-cursor-sprite-install: solid-colour sprite assignment no-crash" {
    skip "$SKIP_LEGACY_QDSHELL_PY"
    vm_run "bash /root/s20-cursor-sprite-install.sh"
    assert_success
    assert_output_contains "PASS: weston + qdwin started (cursor-solid enabled)"
    assert_output_contains "PASS: set_shape ran with QDWIN_CURSOR_SPRITE_SOLID=1 branch"
    assert_output_contains "PASS: sprite install path executed"
    assert_output_contains "PASS: weston survived cursor-shape sprite install"
    assert_output_contains "PASS: §6.6 follow-up cursor-shape solid sprite install end-to-end"
}

@test "s6.8-s1-s2-s3-nested-publish: publisher + proxy curtain + input-sink PING" {
    vm_run "bash /root/s23-nested-publish.sh 2>/dev/null"
    assert_success
    assert_output_contains "PASS: outer qdwin started"
    assert_output_contains "PASS: nested weston publisher mode ready"
    assert_output_contains "PASS: publisher bound qdwin_nested_manager_v1 on outer"
    assert_output_contains "PASS: outer received advertise_toplevel from publisher"
    assert_output_contains "PASS: nested publisher logged the advertise"
    assert_output_contains "PASS: pw_node carries weston.pipewire:<pid>:<output-name>"
    assert_output_contains "PASS: §6.8 S2 outer proxy toplevel + curtain created"
    assert_output_contains "PASS: §6.8 S3 outer connected to input sink + sent PING"
    assert_output_contains "PASS: §6.8 S3 nested received PING — wire format proven"
    assert_output_contains "PASS: §6.8 S1 nested-side publish + advertise end-to-end"
}

@test "s6.8-s3b-nested-input-decode: per-toplevel inner-seat replays motion/button/key/axis/focus" {
    vm_run "bash /root/s25-nested-input-decode.sh 2>/dev/null"
    assert_success
    assert_output_contains "PASS: outer qdwin started (S3b synthetic burst armed)"
    assert_output_contains "PASS: nested publisher up"
    assert_output_contains "PASS: §6.8 S3b outer sent synthetic event burst"
    assert_output_contains "PASS: nested decoded — input-sink PING handle="
    assert_output_contains "PASS: nested decoded — inner-seat 'qdwin-nested-T"
    assert_output_contains "PASS: nested decoded — focus handle=.* focused=1"
    assert_output_contains "PASS: nested decoded — button handle=.* btn=0x110 state=1"
    assert_output_contains "PASS: nested decoded — button handle=.* btn=0x110 state=0"
    assert_output_contains "PASS: nested decoded — key handle=.* key=30 state=1"
    assert_output_contains "PASS: nested decoded — key handle=.* key=30 state=0"
    assert_output_contains "PASS: nested decoded — focus handle=.* focused=0"
    assert_output_contains "PASS: §6.8 S3b input-sink decoder + per-toplevel inner-seat replay end-to-end"
}

@test "s6.8-s4-nested-broker-gate: allow + deny verdicts gate the proxy lifecycle" {
    skip "$SKIP_LEGACY_NESTED_QDSHELL"
    vm_run "systemctl start qdistro-admin-broker.service && bash /root/s24-nested-broker-gate.sh 2>/dev/null"
    assert_success
    assert_output_contains "PASS: outer qdwin + qdshell up"
    assert_output_contains "PASS: §6.8 S4 ALLOW path — broker allowed nested-proxy"
    assert_output_contains "PASS: §6.8 S4 ALLOW released proxy from held to normal"
    assert_output_contains "PASS: §6.8 S4 DENY path — broker denied nested-proxy"
    assert_output_contains "PASS: §6.8 S4 DENY destroyed proxy"
    assert_output_contains "PASS: §6.8 S4 admin-broker gate (allow + deny) end-to-end"
}

@test "s6.8-s2b-nested-pixel-bind: pixelfeed consumer binds proxy pixels" {
    skip "$SKIP_LEGACY_NESTED_QDSHELL"
    vm_run "systemctl start qdistro-admin-broker.service && bash /root/s26-nested-pixel-bind.sh 2>/dev/null"
    assert_success
    assert_output_contains "PASS: §6.8 S2b qdshell received nested_proxy_pixel_source"
    assert_output_contains "PASS: §6.8 S2b qdshell spawned qdistro-nested-pixelfeed"
    assert_output_contains "PASS: §6.8 S2b outer received bind_proxy_pixels (curtain swapped)"
    assert_output_contains "PASS: §6.8 S2b mvp pixel-feed bind end-to-end"
}

@test "s6.8-cursor-sprites-v10: shell-registered cursor sprite cache + destroy" {
    vm_run "bash /root/s29-cursor-sprites.sh 2>/dev/null"
    assert_success
    assert_output_contains "PASS: outer qdwin started"
    assert_output_contains "PASS: §6.8 cursor-sprite registered (POINTER) on outer"
    assert_output_contains "PASS: helper completed registration cleanly"
    assert_output_contains "PASS: §6.8 cursor-sprite cleared after helper exit"
    assert_output_contains "PASS: §6.8 cursor-sprite v10 protocol + cache + destroy-listener end-to-end"
}

@test "s6.8-s3c-nested-keyboard-grab: notify_key drives QDNI through default grab" {
    vm_run "bash /root/s28-nested-keyboard-grab.sh 2>/dev/null"
    assert_success
    assert_output_contains "PASS: §6.8 S3c keyboard-grab install fired post-RDP-peer"
    assert_output_contains "PASS: §6.8 S3c outer drove notify_key through default keyboard grab"
    assert_output_contains "PASS: §6.8 S3c notify_key reached at least one seat keyboard"
    assert_output_contains "PASS: nested decoded — key handle=.* key=31 state=1"
    assert_output_contains "PASS: nested decoded — key handle=.* key=31 state=0"
    assert_output_contains "PASS: §6.8 S3c keyboard-grab end-to-end"
}

@test "s6.8-s2c-nested-pw-pixels: pixelfeed pulls real PipeWire frames" {
    skip "$SKIP_LEGACY_NESTED_QDSHELL"
    vm_run "systemctl start qdistro-admin-broker.service && bash /root/s27-nested-pw-pixels.sh 2>/dev/null"
    assert_success
    assert_output_contains "PASS: §6.8 S2c qdshell spawned pixelfeed"
    assert_output_contains "PASS: §6.8 S2c pixelfeed log present"
    assert_output_contains "PASS: §6.8 S2c pixelfeed received PW frames (tick logged)"
    assert_output_contains "PASS: §6.8 S2c real PipeWire pixels end-to-end"
}

@test "s6.8-pixelfeed-dmabuf: dmabuf zero-copy plumbing bound + outer dmabuf advertised" {
    skip "$SKIP_LEGACY_NESTED_QDSHELL"
    # Skips cleanly on no-GPU VMs (s31 emits "SKIP:" and exits 0 in that path).
    vm_run "systemctl start qdistro-admin-broker.service && bash /root/s31-pixelfeed-dmabuf.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "no virtio-gpu accel3d on this VM"
    fi
    assert_output_contains "PASS: outer advertises zwp_linux_dmabuf_v1"
    assert_output_contains "PASS: pixelfeed bound zwp_linux_dmabuf_v1"
    assert_output_contains "PASS: §6.8 dmabuf zero-copy end-to-end"
}

@test "spec25-phase2-layered-identity-urgent-banner: GetPending exposes layered fields + banner shown/cleared" {
    skip "$SKIP_LEGACY_DEPLOY_FILES"
    vm_run "bash /root/s30-approval-app-phase2.sh"
    assert_success
    assert_output_contains "PASS: GetPending exposes layered identity (sha256 + selinux + cgroup)"
    assert_output_contains "PASS: approval app running"
    assert_output_contains "PASS: urgent banner shown"
    assert_output_contains "PASS: urgent banner cleared after queue drained"
    assert_output_contains "PASS: spec/25 §Phase-2 layered identity + urgent banner end-to-end"
}

@test "s6.6-approval-app-tray: SNI item + badge + ListHistory" {
    skip "$SKIP_LEGACY_DEPLOY_FILES"
    vm_run "bash /root/s19-approval-app-tray.sh"
    assert_success
    assert_output_contains "PASS: broker active"
    assert_output_contains "PASS: SNI watcher alive"
    assert_output_contains "PASS: approval app running"
    assert_output_contains "PASS: watcher has SNI item registered"
    assert_output_contains "PASS: initial SNI state is calm (Passive, no badge)"
    assert_output_contains "PASS: SNI title + status reflect pending count"
    assert_output_contains "PASS: ListHistory returned"
    assert_output_contains "PASS: §6.6 admin-approval-app SNI tray badge + ListHistory end-to-end"
}
