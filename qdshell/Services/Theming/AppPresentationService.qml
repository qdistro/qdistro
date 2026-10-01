pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "ColorPalette.js" as ColorPalette

Singleton {
  id: root

  property bool ready: false
  property bool managedDirPresent: false
  property var latestPayload: null
  property var inFlightPayload: null
  property int failCount: 0
  property string lastDiagnostic: ""

  readonly property string managedDir: "/var/lib/qdistro/presentation"
  readonly property string stateDir: {
    const xdg = Quickshell.env("XDG_STATE_HOME");
    const home = Quickshell.env("HOME");
    const rootDir = xdg && xdg.length ? xdg : (home + "/.local/state");
    return rootDir + "/qdistro/presentation";
  }

  function init() {
    Logger.i("AppPresentation", "Service started");
    dirProbe.running = true;
  }

  function destinationDir() {
    return root.managedDirPresent ? root.managedDir : root.stateDir;
  }

  function currentMode() {
    if (Color.acceptedMode === "dark" || Color.acceptedMode === "light")
      return Color.acceptedMode;
    return Settings.data.colorSchemes.darkMode ? "dark" : "light";
  }

  function buildPayload() {
    const palette = Color.acceptedPalette;
    if (!ColorPalette.completePalette(palette))
      return null;
    const ui = Settings.data.ui || {};
    const general = Settings.data.general || {};
    const appearance = Settings.data.appearance || {};
    return {
      "mode": currentMode(),
      "colors": palette,
      "settings": {
        "ui": {
          "fontDefault": ui.fontDefault || "",
          "fontFixed": ui.fontFixed || "",
          "fontDefaultScale": ui.fontDefaultScale,
          "fontFixedScale": ui.fontFixedScale,
          "tooltipsEnabled": ui.tooltipsEnabled
        },
        "general": {
          "scaleRatio": general.scaleRatio,
          "radiusRatio": general.radiusRatio,
          "iRadiusRatio": general.iRadiusRatio,
          "animationDisabled": general.animationDisabled,
          "animationSpeed": general.animationSpeed
        },
        "appearance": {
          "iconTheme": appearance.iconTheme || ""
        }
      },
      "default_ui_family": Qt.application.font.family
    };
  }

  function invalidate() {
    if (!root.ready)
      return;
    debounce.restart();
  }

  function publishNow() {
    const payload = buildPayload();
    if (!payload)
      return;
    const encoded = JSON.stringify(payload);
    if (publishProcess.running) {
      root.latestPayload = encoded;
      return;
    }
    root.inFlightPayload = encoded;
    root.latestPayload = null;
    publishProcess.command = ["qdistro-presentation-publish", "--dir", destinationDir()];
    publishProcess.running = true;
  }

  Connections {
    target: Settings
    function onSettingsLoaded() {
      root.ready = true;
      root.invalidate();
    }
    function onSettingsSaved() {
      root.invalidate();
    }
  }

  Connections {
    target: Color
    function onAcceptedTargetChanged(requestId, mode, palette) {
      root.invalidate();
    }
  }

  Timer {
    id: debounce
    interval: 100
    repeat: false
    onTriggered: root.publishNow()
  }

  Process {
    id: dirProbe
    command: ["test", "-d", root.managedDir]
    running: false
    onExited: function (exitCode) {
      root.managedDirPresent = (exitCode === 0);
      root.ready = true;
      root.invalidate();
    }
  }

  Process {
    id: publishProcess
    stdinEnabled: true
    running: false
    onStarted: {
      if (root.inFlightPayload)
        write(root.inFlightPayload);
      stdinEnabled = false;
    }
    onExited: function (exitCode) {
      stdinEnabled = true;
      if (exitCode !== 0) {
        root.failCount += 1;
        if (root.failCount <= 3 || (root.failCount % 20) === 0) {
          Logger.w("AppPresentation", "publish failed", exitCode, stderr.text);
          root.lastDiagnostic = stderr.text;
        }
      } else {
        root.failCount = 0;
      }
      if (root.latestPayload) {
        const queued = root.latestPayload;
        root.latestPayload = null;
        root.inFlightPayload = queued;
        publishProcess.running = true;
      }
    }
    stdout: StdioCollector {}
    stderr: StdioCollector {}
  }
}
