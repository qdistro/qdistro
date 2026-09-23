pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.UI
import "PresentationPolicy.js" as Policy

Singleton {
  id: root

  property bool isInhibited: false
  property string reason: I18n.tr("system.user-requested")
  property var activeInhibitors: []
  property var timeout: null // in seconds

  // Dedicated inhibitor id owned by presentation mode (see PresentationPolicy.js).
  readonly property string presentationInhibitorId: Policy.PRESENTATION_INHIBITOR_ID

  // Different inhibitor strategies
  property string strategy: "systemd" // "systemd", "wayland", or "auto"

  function init() {
    Logger.i("IdleInhibitor", "Service started");
    detectStrategy();
    // Restore persisted presentation mode on startup (xfce-style).
    if (Policy.shouldRestorePresentationMode(Settings.data.power.presentationMode)) {
      setPresentationMode(true, false);
    }
  }

  // ─── Presentation mode (xfce4-power-manager parity) ──────────────
  // Toggling presentation mode adds/removes a single dedicated inhibitor id.
  // The on/off state is persisted in Settings.data.power.presentationMode and
  // restored at startup. An optional auto-disable timer turns it back off after
  // Settings.data.power.presentationAutoDisableMinutes minutes (0 = never).
  readonly property bool presentationModeActive: activeInhibitors.includes(presentationInhibitorId)

  function setPresentationMode(enabled, persist = true) {
    // Delegate the set algebra to the pure policy so it stays testable, then
    // reassign the array so QML change-notifications fire for the viewer.
    activeInhibitors = Policy.applyPresentationMode(activeInhibitors, enabled);
    updateInhibition(enabled ? I18n.tr("panels.power.presentation-mode-reason") : reason);
    if (persist)
      Settings.data.power.presentationMode = enabled;
    if (enabled) {
      var mins = Policy.clampAutoDisableMinutes(Settings.data.power.presentationAutoDisableMinutes);
      if (mins > 0) {
        presentationAutoDisableTimer.interval = mins * 60 * 1000;
        presentationAutoDisableTimer.restart();
      } else {
        presentationAutoDisableTimer.stop();
      }
    } else {
      presentationAutoDisableTimer.stop();
    }
    Logger.i("IdleInhibitor", "Presentation mode:", enabled);
  }

  function togglePresentationMode() {
    setPresentationMode(!presentationModeActive);
    return presentationModeActive;
  }

  // Auto-disable timer: fires once after the configured window and turns
  // presentation mode back off.
  Timer {
    id: presentationAutoDisableTimer
    repeat: false
    running: false
    onTriggered: {
      if (root.presentationModeActive) {
        Logger.i("IdleInhibitor", "Presentation mode auto-disabled after timeout");
        root.setPresentationMode(false);
      }
    }
  }

  // Auto-detect the best strategy
  function detectStrategy() {
    if (strategy === "auto") {
      // Check if systemd-inhibit is available
      try {
        var systemdResult = Quickshell.execDetached(["sh", "-c", "command -v systemd-inhibit"]);
        strategy = "systemd";
        Logger.d("IdleInhibitor", "Using systemd-inhibit strategy");
        return;
      } catch (e)

        // systemd-inhibit not found, try Wayland tools
      {}

      try {
        var waylandResult = Quickshell.execDetached(["sh", "-c", "command -v wayhibitor"]);
        strategy = "wayland";
        Logger.d("IdleInhibitor", "Using wayhibitor strategy");
        return;
      } catch (e)

        // wayhibitor not found
      {}

      Logger.w("IdleInhibitor", "No suitable inhibitor found - will try systemd as fallback");
      strategy = "systemd"; // Fallback to systemd even if not detected
    }
  }

  // Add an inhibitor
  function addInhibitor(id, reason = "Application request") {
    if (activeInhibitors.includes(id)) {
      Logger.w("IdleInhibitor", "Inhibitor already active:", id);
      return false;
    }

    // Reassign (not in-place push) so QML change-notifications fire for the
    // read-only active-inhibitor viewer. Set algebra lives in the pure policy.
    activeInhibitors = Policy.addInhibitor(activeInhibitors, id);
    updateInhibition(reason);
    Logger.d("IdleInhibitor", "Added inhibitor:", id);
    return true;
  }

  // Remove an inhibitor
  function removeInhibitor(id) {
    const index = activeInhibitors.indexOf(id);
    if (index === -1) {
      Logger.w("IdleInhibitor", "Inhibitor not found:", id);
      return false;
    }

    activeInhibitors = Policy.removeInhibitor(activeInhibitors, id);
    updateInhibition();
    Logger.d("IdleInhibitor", "Removed inhibitor:", id);
    return true;
  }

  // Update the actual system inhibition
  function updateInhibition(newReason = reason) {
    const shouldInhibit = activeInhibitors.length > 0;

    if (shouldInhibit === isInhibited) {
      return;
      // No change needed
    }

    if (shouldInhibit) {
      startInhibition(newReason);
    } else {
      stopInhibition();
    }
  }

  // Start system inhibition
  function startInhibition(newReason) {
    reason = newReason;

    if (strategy === "systemd") {
      startSystemdInhibition();
    } else if (strategy === "wayland") {
      startWaylandInhibition();
    } else {
      Logger.w("IdleInhibitor", "No inhibition strategy available");
      return;
    }

    isInhibited = true;
    Logger.i("IdleInhibitor", "Started inhibition:", reason);
  }

  // Stop system inhibition
  function stopInhibition() {
    if (!isInhibited)
      return;
    if (inhibitorProcess.running) {
      inhibitorProcess.signal(15); // SIGTERM
    }

    isInhibited = false;
    Logger.i("IdleInhibitor", "Stopped inhibition");
  }

  // Systemd inhibition using systemd-inhibit
  function startSystemdInhibition() {
    inhibitorProcess.command = ["systemd-inhibit", "--what=idle", "--why=" + reason, "--mode=block", "sleep", "infinity"];
    inhibitorProcess.running = true;
  }

  // Wayland inhibition using wayhibitor or similar
  function startWaylandInhibition() {
    inhibitorProcess.command = ["wayhibitor"];
    inhibitorProcess.running = true;
  }

  // Process for maintaining the inhibition
  Process {
    id: inhibitorProcess
    running: false

    onExited: function (exitCode, exitStatus) {
      if (isInhibited) {
        Logger.w("IdleInhibitor", "Inhibitor process exited unexpectedly:", exitCode);
        isInhibited = false;
      }
    }

    onStarted: function () {
      Logger.d("IdleInhibitor", "Inhibitor process started successfully");
    }
  }

  Timer {
    id: inhibitorTimeout
    repeat: true
    interval: 1000 // 1 second
    onTriggered: function () {
      if (timeout == null) {
        inhibitorTimeout.stop();
        return;
      }

      timeout -= 1;
      if (timeout <= 0) {
        removeManualInhibitor();
        return;
      }
    }
  }

  // Manual toggle for user control
  function manualToggle() {
    // clear any existing timeout
    timeout = null;
    if (activeInhibitors.includes("manual")) {
      removeManualInhibitor();
      return false;
    } else {
      addManualInhibitor(null);
      return true;
    }
  }

  function changeTimeout(delta) {
    if (timeout == null && delta < 0) {
      // no inhibitor, ignored
      return;
    }

    if (timeout == null && delta > 0) {
      // enable manual inhibitor and set timeout
      addManualInhibitor(timeout + delta);
      return;
    }

    if (timeout + delta <= 0) {
      // disable manual inhibitor
      removeManualInhibitor();
      return;
    }

    if (timeout + delta > 0) {
      // change timeout
      addManualInhibitor(timeout + delta);
      return;
    }
  }

  function removeManualInhibitor() {
    if (timeout !== null) {
      timeout = null;
      if (inhibitorTimeout.running) {
        inhibitorTimeout.stop();
      }
    }

    if (activeInhibitors.includes("manual")) {
      removeInhibitor("manual");
      ToastService.showNotice(I18n.tr("tooltips.keep-awake"), I18n.tr("common.disabled"), "keep-awake-off");
      Logger.i("IdleInhibitor", "Manual inhibition disabled");
    }
  }

  function addManualInhibitor(timeoutSec) {
    if (!activeInhibitors.includes("manual")) {
      addInhibitor("manual", "Manually activated by user");
      ToastService.showNotice(I18n.tr("tooltips.keep-awake"), I18n.tr("common.enabled"), "keep-awake-on");
    }

    if (timeoutSec === null && timeout === null) {
      Logger.i("IdleInhibitor", "Manual inhibition enabled");
      return;
    } else if (timeoutSec !== null && timeout === null) {
      timeout = timeoutSec;
      inhibitorTimeout.start();
      Logger.i("IdleInhibitor", "Manual inhibition enabled with timeout:", timeoutSec);
      return;
    } else if (timeoutSec !== null && timeout !== null) {
      timeout = timeoutSec;
      Logger.i("IdleInhibitor", "Manual inhibition timeout changed to:", timeoutSec);
      return;
    } else if (timeoutSec === null && timeout !== null) {
      timeout = null;
      inhibitorTimeout.stop();
      Logger.i("IdleInhibitor", "Manual inhibition timeout cleared");
      return;
    }
  }

  // Clean up on shutdown
  Component.onDestruction: {
    stopInhibition();
  }
}
