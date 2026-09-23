pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// Accessibility backend service.
//
// Mirrors xfce4-accessibility-settings:
//   * Find-cursor pointer highlight  — implemented fully in qdshell as a shell
//     overlay (see Modules/Accessibility/FindCursorOverlay.qml). This service
//     only exposes the trigger signal; no external backend is needed.
//   * Sticky / slow / bounce keys, mouse keys — these need an X AccessX
//     backend. We detect whether `xkbset` + an X/XWayland server is reachable
//     and gate the UI accordingly. When present we apply the controls and their
//     delays/speed live; otherwise the settings are persisted only.
//   * Assistive technology (AT-SPI) autostart — toggled by writing/removing an
//     XDG autostart .desktop entry, following AutostartService file patterns.
//
// Capability detection is done here and exposed as boolean properties the
// Accessibility settings tab binds to (the PowerService capability-gating
// pattern).
Singleton {
  id: root

  // ─── Capability flags ────────────────────────────────────────────
  // Whether an AccessX backend (xkbset on an X/XWayland server) is reachable so
  // the keyboard accessibility options (sticky/slow/bounce/mouse keys) can be
  // applied live via the X server. Persist-only when false. This is the X path
  // only (not a foreign Wayland compositor), so it is allowed under qdwin-only.
  //
  // The compositor (qdwin) xkb capability is tracked centrally in
  // CapabilityService.xkbRepeat (false until qdwin_shell_v1 gains the request).
  // It is intentionally NOT OR-ed into the apply gate yet: this service has no
  // qdwin apply path, so claiming capability before one exists would enable the
  // controls while changes silently fail to apply. Wire a qdwin apply path and
  // gate it on that flag when the request lands.
  property bool keyboardBackendAvailable: false
  // Overall capability for the UI — currently just the X path.
  readonly property bool canApplyKeyboard: keyboardBackendAvailable
  // Whether an AT-SPI accessibility stack appears installed so the assistive
  // technology autostart can take effect. Persist-only when false.
  property bool assistiveTechBackendAvailable: false

  // ─── Find-cursor trigger ─────────────────────────────────────────
  // Emitted when the cursor highlight should flash. The FindCursorOverlay
  // listens for this. Driven by the IPC handler (`findCursor show`) or the
  // settings "Test" button.
  signal findCursorRequested

  function triggerFindCursor() {
    if (!Settings.data.accessibility.findCursorEnabled) {
      Logger.d("AccessibilityService", "find-cursor disabled, ignoring trigger");
      return;
    }
    Logger.i("AccessibilityService", "find-cursor highlight requested");
    findCursorRequested();
  }

  // Force-show ignoring the enabled toggle (used by the settings preview).
  function previewFindCursor() {
    Logger.i("AccessibilityService", "find-cursor preview requested");
    findCursorRequested();
  }

  // ─── Initialisation ──────────────────────────────────────────────
  function init() {
    Logger.i("AccessibilityService", "Service started");
    detectKeyboardBackend();
    detectAssistiveTechBackend();
  }

  // ─── Shell-safe quoting (AutostartService pattern) ───────────────
  function _q(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
  }

  // Resolved absolute path to the AT-SPI bus launcher, or "" if not found.
  // Written into the autostart entry so it works regardless of PATH.
  property string _atspiLauncherPath: ""

  // ─── Capability detection ────────────────────────────────────────
  Process {
    id: keyboardBackendProc
    // `xkbset` is the actual AccessX control tool: it can enable Sticky/Slow/
    // Bounce/Mouse keys *and* set their delays/speed at runtime (unlike
    // `setxkbmap -option accessx:*`, which does not reliably change the keymap).
    // It needs a reachable X / XWayland server.
    command: ["sh", "-c", "command -v xkbset >/dev/null 2>&1 && [ -n \"$DISPLAY\" ] && echo yes || echo no"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        root.keyboardBackendAvailable = String(text || "").trim() === "yes";
        Logger.d("AccessibilityService", "keyboardBackendAvailable:", root.keyboardBackendAvailable);
        if (root.keyboardBackendAvailable)
          root.applyKeyboardOptions();
      }
    }
    stderr: StdioCollector {}
  }

  function detectKeyboardBackend() {
    keyboardBackendProc.running = true;
  }

  Process {
    id: assistiveBackendProc
    // AT-SPI autostart is only meaningful if the bus launcher executable we
    // write into the .desktop entry actually exists. Resolve its absolute path
    // (PATH first, then the common libexec locations) and report it; empty
    // means the backend is unavailable.
    command: ["sh", "-c", "p=$(command -v at-spi-bus-launcher 2>/dev/null); case \"$p\" in /*) echo \"$p\"; exit 0;; esac; for d in /usr/libexec /usr/lib/at-spi2-core /usr/lib64/at-spi2-core /usr/lib/*/at-spi2-core; do [ -x \"$d/at-spi-bus-launcher\" ] && { echo \"$d/at-spi-bus-launcher\"; exit 0; }; done"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        root._atspiLauncherPath = String(text || "").trim();
        root.assistiveTechBackendAvailable = root._atspiLauncherPath !== "";
        Logger.d("AccessibilityService", "assistiveTechBackendAvailable:", root.assistiveTechBackendAvailable, "launcher:", root._atspiLauncherPath);
      }
    }
    stderr: StdioCollector {}
  }

  function detectAssistiveTechBackend() {
    assistiveBackendProc.running = true;
  }

  // ─── Keyboard accessibility (AccessX via xkbset) ─────────────────
  // When `xkbset` and an X / XWayland server are present we enable/disable the
  // AccessX controls *and* push the configured slow/bounce delays and mouse-
  // keys speed. `xkbset` operates the live X server AccessX controls directly,
  // so the numeric delays/speed are actually applied (not just persisted).
  //
  // This is genuinely backend-limited: under a pure Wayland session with no
  // XWayland there is no portable user-session API for these controls, so the
  // UI is capability-gated and the settings are always persisted regardless.
  //
  // All values passed to xkbset are either fixed control names or integers we
  // clamp from settings (never raw user strings), so the argv is injection-safe.
  Process {
    id: applyKeyboardProc
    running: false
    stderr: StdioCollector {}
  }

  function _clampInt(v, lo, hi) {
    var n = parseInt(v);
    if (isNaN(n))
      n = lo;
    return Math.max(lo, Math.min(hi, n));
  }

  function applyKeyboardOptions() {
    if (!keyboardBackendAvailable)
      return;

    const a = Settings.data.accessibility;
    // Build a sequence of xkbset invocations. Each control is enabled (with its
    // numeric parameter where applicable) or explicitly disabled with the
    // leading-dash form, so toggling off at runtime works too. `xkbset exp` is
    // then used to keep the enabled controls from auto-expiring.
    var cmds = [];
    var keep = [];

    if (a.stickyKeys) {
      cmds.push(["xkbset", "sticky", "-twokey", "-latchlock"]);
      keep.push("=sticky", "=twokey", "=latchlock");
    } else {
      cmds.push(["xkbset", "-sticky"]);
    }

    if (a.slowKeys) {
      cmds.push(["xkbset", "slowkeys", String(_clampInt(a.slowKeysDelayMs, 0, 2000))]);
      keep.push("=slowkeys");
    } else {
      cmds.push(["xkbset", "-slowkeys"]);
    }

    if (a.bounceKeys) {
      cmds.push(["xkbset", "bouncekeys", String(_clampInt(a.bounceKeysDelayMs, 0, 2000))]);
      keep.push("=bouncekeys");
    } else {
      cmds.push(["xkbset", "-bouncekeys"]);
    }

    if (a.mouseKeys) {
      cmds.push(["xkbset", "mousekeys"]);
      // ma = mouse-keys acceleration, taking five integers:
      //   delay interval time_to_max max_accel curve
      // The user-configurable "speed" maps to max_accel (the peak pointer
      // speed); the others keep sensible defaults.
      var maxAccel = _clampInt(a.mouseKeysSpeed, 1, 100);
      cmds.push(["xkbset", "ma", "30", "10", "10", String(maxAccel), "1"]);
      keep.push("=mousekeys", "=mousekeysaccel");
    } else {
      cmds.push(["xkbset", "-mousekeys"]);
      // Also clear acceleration so an off state is clean.
      cmds.push(["xkbset", "-ma"]);
    }

    if (keep.length > 0) {
      // Prevent AccessX controls from timing out (default ~2 min) by clearing
      // their expiry. `xkbset exp =<ctrl>` means "never expire this control".
      cmds.push(["xkbset", "exp", "1"].concat(keep));
    }

    // Chain the invocations into a single sh -c so they run in order. Each
    // argv element is a fixed name or a clamped integer string.
    var parts = [];
    for (var i = 0; i < cmds.length; i++) {
      var quoted = [];
      for (var j = 0; j < cmds[i].length; j++)
        quoted.push(_q(cmds[i][j]));
      parts.push(quoted.join(" "));
    }
    applyKeyboardProc.command = ["sh", "-c", parts.join("; ")];
    applyKeyboardProc.running = true;
    Logger.i("AccessibilityService", "Applied AccessX controls via xkbset (" + cmds.length + " ops)");
  }

  // React to keyboard accessibility setting changes (toggles + numeric values).
  readonly property bool _stickyKeys: Settings.data.accessibility.stickyKeys
  readonly property bool _slowKeys: Settings.data.accessibility.slowKeys
  readonly property int _slowKeysDelayMs: Settings.data.accessibility.slowKeysDelayMs
  readonly property bool _bounceKeys: Settings.data.accessibility.bounceKeys
  readonly property int _bounceKeysDelayMs: Settings.data.accessibility.bounceKeysDelayMs
  readonly property bool _mouseKeys: Settings.data.accessibility.mouseKeys
  readonly property int _mouseKeysSpeed: Settings.data.accessibility.mouseKeysSpeed

  on_StickyKeysChanged: applyKeyboardOptions()
  on_SlowKeysChanged: applyKeyboardOptions()
  on_SlowKeysDelayMsChanged: applyKeyboardOptions()
  on_BounceKeysChanged: applyKeyboardOptions()
  on_BounceKeysDelayMsChanged: applyKeyboardOptions()
  on_MouseKeysChanged: applyKeyboardOptions()
  on_MouseKeysSpeedChanged: applyKeyboardOptions()

  // ─── Assistive technology (AT-SPI) autostart ─────────────────────
  // Toggle is backed by an XDG autostart .desktop entry that launches the
  // AT-SPI bus. We write a managed entry on enable and remove it on disable,
  // following AutostartService's file-writing approach (no user strings are
  // interpolated, so quoting is straightforward).
  readonly property string _autostartDir: (Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config")) + "/autostart"
  readonly property string _atspiDesktopFile: _autostartDir + "/qdshell-at-spi.desktop"

  Process {
    id: assistiveAutostartProc
    running: false
    stderr: StdioCollector {}
  }

  function applyAssistiveTechAutostart() {
    const enabled = Settings.data.accessibility.assistiveTechEnabled;
    const fp = _q(_atspiDesktopFile);
    const dir = _q(_autostartDir);

    if (enabled) {
      // A minimal autostart entry. Use the absolute launcher path resolved at
      // detection time so the entry works regardless of the login PATH; fall
      // back to the bare name if (somehow) unresolved. Both come from our own
      // detection, never user input.
      // Strip any stray newline/CR (defensive — the path comes from our own
      // detection) so it cannot break out of the Exec= line or the heredoc.
      var launcher = (_atspiLauncherPath !== "" ? _atspiLauncherPath : "at-spi-bus-launcher").replace(/[\r\n]/g, "");
      var content =
        "[Desktop Entry]\n" +
        "Type=Application\n" +
        "Name=Assistive Technology (qdshell)\n" +
        "Comment=Start the AT-SPI accessibility bridge\n" +
        "Exec=" + launcher + " --launch-immediately\n" +
        "OnlyShowIn=qdshell;XFCE;GNOME;\n" +
        "X-GNOME-Autostart-enabled=true\n";
      assistiveAutostartProc.command = ["sh", "-c",
        "mkdir -p " + dir + " && cat > " + fp + " << 'QDSHELL_EOF'\n" + content + "QDSHELL_EOF"];
    } else {
      assistiveAutostartProc.command = ["sh", "-c", "rm -f " + fp];
    }
    assistiveAutostartProc.running = true;
    Logger.i("AccessibilityService", "AT-SPI autostart " + (enabled ? "enabled" : "disabled"));
  }

  readonly property bool _assistiveTechEnabled: Settings.data.accessibility.assistiveTechEnabled
  on_AssistiveTechEnabledChanged: applyAssistiveTechAutostart()
}
