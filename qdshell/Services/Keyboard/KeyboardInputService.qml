pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin
import qs.Services.UI
import "KeyboardXkb.js" as KeyboardXkb

/// KeyboardInputService — keyboard repeat, cursor blink, layout, NumLock,
/// Compose key and XKB option management for the Settings > Keyboard tab.
///
/// Apply mechanism + capability gating (mirrors PowerService): under a pure
/// Wayland compositor (qdwin) the compositor owns xkb + key repeat and exposes
/// no QML/IPC hook for it (Qdwin.cycleKeyboardLayout() is a stub and
/// qdwin_shell_v1 has no repeat/xkb requests). So we DETECT what is actually
/// applicable:
///   - X11 / Xwayland reachable (DISPLAY set, xset/setxkbmap present) ⇒ we can
///     apply repeat (xset r rate), blink, layout/variant/model/options/compose
///     (setxkbmap) and NumLock (numlockx) to the X server. Xwayland clients
///     pick these up; native Wayland clients honour the compositor's own xkb.
///   - Otherwise ⇒ persist only and surface a capability note. We never pretend
///     to apply when no supporting backend exists.
///
/// All settings persist regardless (Settings.data.keyboard.*) so they take
/// effect the moment a supporting backend (e.g. a future qdwin xkb request)
/// reads them. Apply runs at startup and on user changes.
Singleton {
  id: root

  // ─── Capability flags ────────────────────────────────────────────
  // Whether an X server (real X11 or Xwayland) is reachable for apply.
  property bool hasXServer: false
  // Individual tool availability.
  property bool hasSetxkbmap: false
  property bool hasXset: false
  property bool hasNumlockx: false
  property bool hasLocalectl: false
  // Whether the running session is Wayland (informational, for the note).
  readonly property bool isWayland: (Quickshell.env("WAYLAND_DISPLAY") || "") !== ""
  // True once capability detection has finished its first pass.
  property bool capabilitiesReady: false

  // Can we apply keyboard settings right now? The only implemented apply path
  // is the X server (real X11 / XWayland: setxkbmap/xset/numlockx) — NOT a
  // foreign Wayland compositor, so it is allowed under the qdwin-only rule.
  //
  // The compositor (qdwin) key-repeat capability is tracked centrally in
  // CapabilityService.xkbRepeat (live as of qdwin_shell_v1 v28's
  // set_key_repeat). canApplyRepeat below ORs it with the X path so the
  // Keyboard tab is no longer persist-only for repeat once the shell binds at
  // >= v28: native Wayland clients honour the compositor repeat (qdwin),
  // XWayland clients honour the X server repeat (xset). The X server path
  // ALSO carries layout/model/options/numlock, which qdwin_shell_v1 does not
  // (only repeat), so canApply (the X-only gate) is kept for those.
  readonly property bool canApply: hasXServer && (hasSetxkbmap || hasXset)
  // Whether key-repeat (rate/delay) can be applied live, via EITHER backend.
  readonly property bool canApplyRepeat: canApply || CapabilityService.xkbRepeat
  // Are we limited to persisting (no X apply backend)? This gates the
  // Keyboard tab's capability note. It stays tied to the X path (canApply):
  // most of the tab (layout/model/options via setxkbmap, numlock via
  // numlockx, cursor blink) has ONLY the X backend. Key-repeat additionally
  // has the qdwin path (canApplyRepeat / applyRepeatToCompositor), but that
  // one live field does not make the rest of the tab applyable — so we do
  // NOT clear the note just because qdwin repeat is live, which would
  // mislead the user about numlock/layout still being persist-only.
  readonly property bool persistOnly: capabilitiesReady && !canApply

  // ─── Discovered XKB data (for the UI pickers) ────────────────────
  // models: [{ key, name }], layouts: [{ key, name }],
  // variants keyed by layout code: { "us": [{ key, name }], ... },
  // options grouped: [{ group, name, options: [{ key, name }] }]
  property var availableModels: []
  property var availableLayouts: []
  property var availableVariants: ({})
  property var availableOptions: []
  property bool xkbDataLoaded: false

  // Convenience settings aliases.
  readonly property int repeatDelay: Settings.data.keyboard.repeatDelay
  readonly property int repeatRate: Settings.data.keyboard.repeatRate
  readonly property bool cursorBlink: Settings.data.keyboard.cursorBlink
  readonly property int cursorBlinkRate: Settings.data.keyboard.cursorBlinkRate
  readonly property bool restoreNumLock: Settings.data.keyboard.restoreNumLock
  readonly property bool useSystemDefaults: Settings.data.keyboard.useSystemDefaults
  readonly property string keyboardModel: Settings.data.keyboard.model
  readonly property var layouts: Settings.data.keyboard.layouts
  readonly property var variants: Settings.data.keyboard.variants
  readonly property string switchShortcut: Settings.data.keyboard.switchShortcut
  readonly property string composeKey: Settings.data.keyboard.composeKey
  readonly property var xkbOptions: Settings.data.keyboard.xkbOptions

  // ─── Initialisation ──────────────────────────────────────────────
  function init() {
    Logger.i("KeyboardInput", "Service started");
    queryCapabilities();
  }

  Component.onCompleted: {
    // Self-init so the singleton works even if shell.qml does not call init().
    queryCapabilities();
  }

  // Shell-safe quoting now lives in the pure KeyboardXkb.js module
  // (KeyboardXkb.shellQuote / setxkbmapShellCmd), which quotes every user value
  // by construction for the chained `sh -c` apply path.

  // ─── Capability detection ────────────────────────────────────────
  Process {
    id: capProc
    running: false
    // Emit one line per probe so we can parse a stable key=value set.
    command: ["sh", "-c",
      // Reachable X server: DISPLAY is set AND at least one X client tool can
      // talk to it. Probe with setxkbmap -query first (it is what we use to
      // apply), falling back to xset q, so a session missing one tool but
      // having the other is still detected as applyable.
      "x=no; if [ -n \"$DISPLAY\" ]; then " +
      "{ command -v setxkbmap >/dev/null 2>&1 && setxkbmap -query >/dev/null 2>&1 && x=yes; } || " +
      "{ command -v xset >/dev/null 2>&1 && xset q >/dev/null 2>&1 && x=yes; }; fi; echo \"xserver=$x\"; " +
      "command -v setxkbmap >/dev/null 2>&1 && echo setxkbmap=yes || echo setxkbmap=no; " +
      "command -v xset >/dev/null 2>&1 && echo xset=yes || echo xset=no; " +
      "command -v numlockx >/dev/null 2>&1 && echo numlockx=yes || echo numlockx=no; " +
      "command -v localectl >/dev/null 2>&1 && echo localectl=yes || echo localectl=no"]
    stdout: StdioCollector {
      onStreamFinished: {
        var lines = String(text || "").trim().split("\n");
        for (var i = 0; i < lines.length; i++) {
          var kv = lines[i].split("=");
          if (kv.length !== 2)
            continue;
          var v = kv[1].trim() === "yes";
          switch (kv[0].trim()) {
          case "xserver": root.hasXServer = v; break;
          case "setxkbmap": root.hasSetxkbmap = v; break;
          case "xset": root.hasXset = v; break;
          case "numlockx": root.hasNumlockx = v; break;
          case "localectl": root.hasLocalectl = v; break;
          }
        }
        root.capabilitiesReady = true;
        Logger.d("KeyboardInput", "caps: xServer=" + root.hasXServer
                 + " setxkbmap=" + root.hasSetxkbmap + " xset=" + root.hasXset
                 + " numlockx=" + root.hasNumlockx);
        // Now that caps are known, load the XKB tables and apply.
        root.loadXkbData();
        root.applyAll();
      }
    }
    stderr: StdioCollector {}
  }

  function queryCapabilities() {
    // Idempotent: shell.qml calls init() and the singleton also self-inits in
    // Component.onCompleted; only the first probe runs.
    if (capProc.running || capabilitiesReady)
      return;
    capProc.running = true;
  }

  // ─── XKB data loading ────────────────────────────────────────────
  // Parse /usr/share/X11/xkb/rules/evdev.lst, which lists models, layouts,
  // variants and options in `! section` blocks. This is the same table XFCE
  // and setxkbmap consult. We avoid localectl here because evdev.lst is the
  // richest source for variants grouped per layout.
  Process {
    id: xkbProc
    running: false
    command: ["sh", "-c",
      "f=/usr/share/X11/xkb/rules/evdev.lst; [ -f \"$f\" ] || f=/usr/share/X11/xkb/rules/base.lst; cat \"$f\" 2>/dev/null"]
    property string _buf: ""
    onStarted: _buf = ""
    stdout: SplitParser {
      onRead: data => xkbProc._buf += data + "\n"
    }
    onExited: (code, status) => {
      root._parseXkbList(xkbProc._buf);
    }
    stderr: StdioCollector {}
  }

  function loadXkbData() {
    xkbProc.running = true;
  }

  // Parsing lives in the pure KeyboardXkb.js module (dual QML/Node) so it is
  // unit-testable headless. This wrapper only wires the parsed result into the
  // singleton's reactive state.
  function _parseXkbList(text) {
    var res = KeyboardXkb.parseXkbList(text);
    root.availableModels = res.models;
    root.availableLayouts = res.layouts;
    root.availableVariants = res.variants;
    root.availableOptions = res.options;
    root.xkbDataLoaded = true;
    Logger.d("KeyboardInput", "xkb data: " + res.models.length + " models, "
             + res.layouts.length + " layouts");
  }

  // Human-readable name for a layout code (falls back to the code itself).
  function layoutName(code) {
    for (var i = 0; i < availableLayouts.length; i++) {
      if (availableLayouts[i].key === code)
        return availableLayouts[i].name;
    }
    return code;
  }

  function variantsFor(code) {
    return availableVariants[code] || [];
  }

  // ─── Apply ───────────────────────────────────────────────────────
  Process {
    id: applyProc
    running: false
    stderr: StdioCollector {}
  }

  // Build the `setxkbmap` invocation from the persisted settings. The shell
  // string is produced by the pure KeyboardXkb.setxkbmapShellCmd builder, which
  // quotes EVERY user value by construction (model/layout/variant/options/
  // switch/compose) — unconditionally, so a value beginning with "-" or
  // carrying shell metacharacters stays inert data and can never inject. The
  // chained `sh -c` apply path consumes this string.
  function _setxkbmapCmd() {
    return KeyboardXkb.setxkbmapShellCmd({
      "model": keyboardModel,
      "layouts": layouts,
      "variants": variants,
      "xkbOptions": xkbOptions,
      "switchShortcut": switchShortcut,
      "composeKey": composeKey
    }, hasSetxkbmap);
  }

  function _xsetRepeatCmd() {
    // xset r rate <delay-ms> <rate-hz>
    return KeyboardXkb.xsetRepeatShellCmd(repeatDelay, repeatRate, hasXset);
  }

  function _numlockCmd() {
    if (!restoreNumLock || !hasNumlockx)
      return "";
    return "numlockx on";
  }

  // Apply everything appropriate for the current capabilities.
  //
  // useSystemDefaults follows XFCE semantics: it scopes ONLY the layout block
  // (model/layout/variant/options/compose/switch). Key repeat, cursor blink
  // and NumLock are behavior settings that stay independently editable and
  // applied regardless of the layout-defaults toggle.
  function applyAll() {
    if (!capabilitiesReady)
      return;
    // Live key-repeat via qdwin (native Wayland clients) — independent of the
    // X server. No-op until CapabilityService.xkbRepeat is live (>= v28 bind);
    // the compositor clamps rate/delay fail-safe.
    applyRepeatToCompositor();
    if (!canApply) {
      if (!canApplyRepeat)
        Logger.i("KeyboardInput", "No apply backend (persist-only); settings saved for a supporting compositor");
      // X server is unavailable: the qdwin repeat push above is the only live
      // apply; the layout block (setxkbmap) + numlock stay persist-only.
      return;
    }
    var parts = [];
    // Layout block — skipped when deferring to system layout defaults.
    if (!useSystemDefaults)
      parts.push(_setxkbmapCmd());
    // Behavior — always applied (not part of "use system defaults").
    parts.push(_xsetRepeatCmd());
    parts.push(_numlockCmd());
    _runChain(parts);
  }

  // Push the key-repeat rate/delay to qdwin (set_key_repeat, v28). No-op
  // unless CapabilityService.xkbRepeat is live. Reaches native Wayland clients
  // (the X path only reaches XWayland clients).
  function applyRepeatToCompositor() {
    if (!CapabilityService.xkbRepeat)
      return;
    var a = KeyboardXkb.repeatToQdwinArgs(repeatRate, repeatDelay);
    Qdwin.applyKeyRepeat(a.rate, a.delay);
    Logger.i("KeyboardInput", "applied qdwin key-repeat rate=" + a.rate
             + " delay=" + a.delay);
  }

  function _runChain(parts) {
    var nonEmpty = parts.filter(function (p) { return p && p !== ""; });
    if (nonEmpty.length === 0)
      return;
    // Each part is independent; do not abort the chain if one tool is missing.
    var cmd = nonEmpty.join("; ");
    applyProc.command = ["sh", "-c", cmd];
    applyProc.running = true;
    Logger.d("KeyboardInput", "apply: " + cmd);
  }

  // React to setting changes (debounced) so edits in the UI take effect.
  Timer {
    id: applyDebounce
    interval: 400
    repeat: false
    onTriggered: root.applyAll()
  }

  function requestApply() {
    if (capabilitiesReady)
      applyDebounce.restart();
  }

  // Re-push when the qdwin key-repeat capability flips live (shell (re)bind at
  // >= v28), so the persisted rate/delay reach a freshly-bound compositor.
  Connections {
    target: CapabilityService
    function onXkbRepeatChanged() {
      if (CapabilityService.xkbRepeat)
        root.applyRepeatToCompositor();
    }
  }

  onRepeatDelayChanged: requestApply()
  onRepeatRateChanged: requestApply()
  onRestoreNumLockChanged: requestApply()
  onUseSystemDefaultsChanged: requestApply()
  onKeyboardModelChanged: requestApply()
  onLayoutsChanged: requestApply()
  onVariantsChanged: requestApply()
  onSwitchShortcutChanged: requestApply()
  onComposeKeyChanged: requestApply()
  onXkbOptionsChanged: requestApply()

  // ─── Layout list helpers (used by the UI) ────────────────────────
  function addLayout(code, variant) {
    var ls = (layouts || []).slice();
    if (ls.indexOf(code) !== -1)
      return; // already present
    ls.push(code);
    Settings.data.keyboard.layouts = ls;
    if (variant && variant !== "") {
      var vmap = _cloneVariants();
      vmap[code] = variant;
      Settings.data.keyboard.variants = vmap;
    }
  }

  function removeLayout(code) {
    var ls = (layouts || []).slice();
    var idx = ls.indexOf(code);
    if (idx === -1)
      return;
    ls.splice(idx, 1);
    if (ls.length === 0)
      ls = ["us"]; // never leave an empty layout list
    Settings.data.keyboard.layouts = ls;
    var vmap = _cloneVariants();
    if (vmap[code] !== undefined) {
      delete vmap[code];
      Settings.data.keyboard.variants = vmap;
    }
  }

  function moveLayout(fromIdx, toIdx) {
    var ls = (layouts || []).slice();
    if (fromIdx < 0 || fromIdx >= ls.length || toIdx < 0 || toIdx >= ls.length)
      return;
    var item = ls.splice(fromIdx, 1)[0];
    ls.splice(toIdx, 0, item);
    Settings.data.keyboard.layouts = ls;
  }

  function setVariant(code, variant) {
    var vmap = _cloneVariants();
    if (!variant || variant === "")
      delete vmap[code];
    else
      vmap[code] = variant;
    Settings.data.keyboard.variants = vmap;
  }

  function variantOf(code) {
    return (variants && variants[code]) ? variants[code] : "";
  }

  function _cloneVariants() {
    var out = ({});
    if (variants) {
      for (var k in variants)
        out[k] = variants[k];
    }
    return out;
  }
}
