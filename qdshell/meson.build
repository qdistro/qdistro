# qdshell is QML at heart — but Phase 5+ needs a tiny native QML
# plugin that binds qdwin_shell_v1 and exposes it to QML, so we ship
# one C++ artifact alongside the QML tree. The QML files themselves
# install with their own pipeline (install-qdwin-session-for-vm.sh
# rsyncs Modules/, Services/, Commons/, etc. directly to
# /usr/share/quickshell/qdshell); this meson build only handles the
# native plugin under qml-plugin/.

project('qdshell', ['c', 'cpp'],
  version: '0.1.0',
  license: 'GPL-3.0-or-later',
  default_options: [
    'c_std=gnu11',
    'cpp_std=c++20',
    'warning_level=2',
    'werror=false',
  ],
)

subdir('qml-plugin')

node = find_program('node', required: false)
if node.found()
  test('clipboard-silo', node, args: files('tests/test_clipboard_silo.js'))
  test('clipboard-actions', node, args: files('tests/test_clipboard_actions.js'))
  test('clipboard-id-guard', node, args: files('tests/test_clipboard_id_guard.js'))
  test('clipboard-broker', node, args: files('tests/test_clipboard_broker.js'))
  test('broker-gate', node, args: files('tests/test_broker_gate.js'))
  test('gate-process-queue', node, args: files('tests/test_gate_process_queue.js'))
  test('gate-process-runtime', node, args: files('tests/test_gate_process_runtime.js'))
  test('capture-binding-invariants', node, args: files('tests/test_capture_binding_invariants.js'))
  test('pointer-parse', node, args: files('tests/test_pointer_parse.js'))
  test('pointer-overrides', node, args: files('tests/test_pointer_overrides.js'))
  test('pointer-config', node, args: files('tests/test_pointer_config.js'))
  test('keyboard-xkb', node, args: files('tests/test_keyboard_xkb.js'))
  test('taskbar-logic', node, args: files('tests/test_taskbar_logic.js'))
  test('window-manager-policy', node, args: files('tests/test_window_manager_policy.js'))
  test('settings-tree', node, args: files('tests/test_settings_tree.js'))
  test('gtk-settings', node, args: files('tests/test_gtk_settings.js'))
  test('shortcut-conflicts', node, args: files('tests/test_shortcut_conflicts.js'))
  test('tray-known-items', node, args: files('tests/test_tray_known_items.js'))
  test('notification-detail', node, args: files('tests/test_notification_detail.js'))
  test('notification-theme', node, args: files('tests/test_notification_theme.js'))
  test('mime-associations', node, args: files('tests/test_mime_associations.js'))
  test('session-model', node, args: files('tests/test_session_model.js'))
  test('silo-egress', node, args: files('tests/test_silo_egress.js'))
  test('capture-state', node, args: files('tests/test_capture_state.js'))
  test('launcher-items', node, args: files('tests/test_launcher_items.js'))
  test('launcher-navigation', node, args: files('tests/test_launcher_navigation.js'))
  test('settings-recovery', node, args: files('tests/test_settings_recovery.js'))
  test('presentation-policy', node, args: files('tests/test_presentation_policy.js'))
  test('brightness-policy', node, args: files('tests/test_brightness_policy.js'))
  test('system-clock', node, args: files('tests/test_system_clock.js'))
  test('desktop-icons', node, args: files('tests/test_desktop_icons.js'))
  test('removable-media', node, args: files('tests/test_removable_media.js'))
  test('output-layout', node, args: files('tests/test_output_layout.js'))
  test('remote-display-lease', node, args: files('tests/test_remote_display_lease.js'))
  test('r9-input-gate', node, args: files('tests/test_r9_input_gate.js'))
  test('qdwin-only-guard', node, args: files('tests/test_qdwin_only_guard.js'))
  # Mirror/logic + misc tests that existed on disk but were never wired into
  # `meson test` (they passed locally but never ran in CI). The drift guard is
  # the load-bearing protection for the security-relevant QML/JS mirrors
  # (silo identity, tier classification) — it MUST run in CI (04/F6,
  # maturity-review #5).
  test('drift-guard', node, args: files('tests/test_drift_guard.js'))
  # Cross-repo complement to drift-guard: assert qdshell's mirrored qdistro
  # identifiers (tier secctx prefixes, AdminBroker1/SessionManager1 D-Bus names,
  # broker reply fields) match qdistro's contract fixture (the monorepo root's
  # tests/contracts/). Skips loudly if qdshell runs outside the monorepo.
  test('qdistro-contract-drift', node, args: files('tests/test_qdistro_contract_drift.js'))
  test('silo-chrome', node, args: files('tests/test_silo_chrome.js'))
  test('remote-machine', node, args: files('tests/test_remote_machine.js'))
  test('remote-machine-binding', node, args: files('tests/test_remote_machine_binding.js'))
  test('colorkeys', node, args: files('tests/test_colorkeys.js'))
  test('fuzzysort', node, args: files('tests/test_fuzzysort.js'))
  test('timeformat', node, args: files('tests/test_timeformat.js'))
  test('widget-helpers', node, args: files('tests/test_widget_helpers.js'))
  test('app1apps-nameowner', node, args: files('tests/test_app1apps_nameowner.js'))
  test('podapps-provider', node, args: files('tests/test_podapps_provider.js'))
  test('clipboard-focus-clear', node, args: files('tests/test_clipboard_focus_clear.js'))
  test('clipboard-deny-coalesce', node, args: files('tests/test_clipboard_deny_coalesce.js'))
  test('display-primary', node, args: files('tests/test_display_primary.js'))
  test('idle-policy', node, args: files('tests/test_idle_policy.js'))
  test('pointer-live-scope', node, args: files('tests/test_pointer_live_scope.js'))
  test('tooltip-text', node, args: files('tests/test_tooltip_text.js'))
  test('wm-accel', node, args: files('tests/test_wm_accel.js'))
  test('workspace-names', node, args: files('tests/test_workspace_names.js'))
endif
