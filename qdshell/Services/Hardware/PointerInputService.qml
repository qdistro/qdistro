pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin
import qs.Services.UI
import "PointerInputParse.js" as PointerInputParse
import "PointerInputConfig.js" as PointerCfg

// PointerInputService — enumerates pointer input devices (mice, touchpads,
// trackpoints) for the Settings > Mouse tab.
//
// Backend model (qdwin-only, mirrors WindowManagerService / PowerService):
//   * qdshell runs on exactly one compositor — qdwin (via libweston). qdwin
//     owns libinput configuration. qdwin_shell_v1 v28 exposes a global
//     pointer-config request for the fields in PointerInputConfig.toBindingArgs;
//     older binds stay persist-only behind CapabilityService.pointerConfig.
//     Per-device policy, click method, horizontal scroll and tablet mapping are
//     persisted for future protocol support and never dispatched as commands.
//   * There is NO probing for or dispatch to any non-qdwin compositor
//     (swaymsg / hyprctl / …) — qdwin is the only supported compositor.
//
// Device enumeration is the only live operation: it prefers
// `libinput list-devices` (richest, usually needs permissions) and falls back
// to parsing `/proc/bus/input/devices` (always readable), classifying devices
// by their evdev capability bits. Both are read-only system queries, not
// compositor dispatch. Device names from enumeration are treated as untrusted
// input and are never interpolated raw into `sh -c` strings.
Singleton {
  id: root

  // ─── Public state ────────────────────────────────────────────────
  // Detected pointer devices. Each entry:
  //   { id, name, type ("mouse"|"touchpad"|"trackpoint"|"tablet"|"pointer"),
  //     hasTap (bool), hasNaturalScroll (bool), hasDisableWhileTyping (bool),
  //     hasScrollMethod (bool) }
  // Tablet/Wacom digitizers are enumerated as type "tablet" so the Mouse tab
  // can expose area-to-output mapping for them.
  property list<var> devices: []

  // Devices the user has not disabled (disabledDevices filtered out), and the
  // subset that are tablets — both derived purely so the UI can bind directly.
  readonly property var enabledDevices: PointerInputParse.filterEnabledDevices(devices, Settings.data.pointer.disabledDevices)
  readonly property var tabletDevices: (devices || []).filter(function (d) {
    return d && d.type === "tablet";
  })
  readonly property bool hasTablet: tabletDevices.length > 0

  // Whether the backend can apply pointer settings live. Live as of
  // qdwin_shell_v1 v28 (set_pointer_config); sourced from the unified
  // CapabilityService (a >= v28 bind), not from probing any compositor. When
  // false the global live fields are persist-only: values are stored and
  // surfaced behind a capability note, and apply automatically once this flips
  // true. Some advanced fields remain persist-only even when this is true; see
  // applyToCompositor() / PointerInputConfig.toBindingArgs.
  readonly property bool canApply: CapabilityService.pointerConfig

  // Which source enumerated the device list (for the "no devices" UI state).
  //   "libinput" | "proc" | "none"
  property string enumSource: ""
  readonly property bool hasDevices: devices.length > 0

  // Has enumeration completed at least once?
  property bool ready: false

  // ─── Settings convenience aliases ────────────────────────────────
  // Global (single-profile) pointer policy. Per-device override is not
  // exposed yet; XFCE's per-device model maps cleanly onto these globals for
  // the common single-mouse + single-touchpad case.
  readonly property string accelProfile: Settings.data.pointer.accelProfile
  readonly property real pointerSpeed: Settings.data.pointer.pointerSpeed
  readonly property bool naturalScroll: Settings.data.pointer.naturalScroll
  readonly property string scrollMethod: Settings.data.pointer.scrollMethod
  readonly property bool tapToClick: Settings.data.pointer.tapToClick
  readonly property bool disableWhileTyping: Settings.data.pointer.disableWhileTyping
  readonly property bool leftHanded: Settings.data.pointer.leftHanded
  readonly property bool horizontalScroll: Settings.data.pointer.horizontalScroll
  readonly property int doubleClickTime: Settings.data.pointer.doubleClickTime
  readonly property int doubleClickDistance: Settings.data.pointer.doubleClickDistance
  readonly property int dragThreshold: Settings.data.pointer.dragThreshold
  readonly property string clickMethod: Settings.data.pointer.clickMethod
  readonly property bool middleClickEmulation: Settings.data.pointer.middleClickEmulation

  // ─── Advanced per-device helpers (pure, persist-only) ────────────
  // Effective settings for a device id: global policy folded with that device's
  // override (or the globals when none). qdwin will read these once it gains a
  // pointer-config request; nothing here builds or dispatches a command.
  function effectiveSettings(deviceId) {
    var g = {
      "accelProfile": Settings.data.pointer.accelProfile,
      "pointerSpeed": Settings.data.pointer.pointerSpeed,
      "naturalScroll": Settings.data.pointer.naturalScroll,
      "scrollMethod": Settings.data.pointer.scrollMethod,
      "tapToClick": Settings.data.pointer.tapToClick,
      "disableWhileTyping": Settings.data.pointer.disableWhileTyping,
      "leftHanded": Settings.data.pointer.leftHanded,
      "horizontalScroll": Settings.data.pointer.horizontalScroll
    };
    return PointerInputParse.resolveDeviceSettings(g, Settings.data.pointer.perDeviceOverrides, deviceId);
  }

  function hasOverride(deviceId) {
    return PointerInputParse.hasDeviceOverride(Settings.data.pointer.perDeviceOverrides, deviceId);
  }

  function isDisabled(deviceId) {
    return PointerInputParse.isDeviceDisabled(Settings.data.pointer.disabledDevices, deviceId);
  }

  // Set a single override key for a device (or clear the whole override when
  // value === undefined). Reassigns the map so QML change-notifies and persists.
  // Uses null-prototype clones so an UNTRUSTED device id such as "__proto__" or
  // "toString" becomes a plain own key instead of mutating object internals, and
  // only whitelisted OVERRIDABLE_KEYS may be persisted (canonical settings).
  function setOverride(deviceId, key, value) {
    if (deviceId === undefined || deviceId === null)
      return;
    if (value !== undefined && PointerInputParse.OVERRIDABLE_KEYS.indexOf(key) === -1) {
      Logger.w("PointerInputService", "ignoring non-overridable key", key);
      return;
    }
    var src = Settings.data.pointer.perDeviceOverrides || {};
    var map = Object.create(null);
    var keys = Object.keys(src);
    for (var i = 0; i < keys.length; i++)
      map[keys[i]] = src[keys[i]];
    if (value === undefined) {
      delete map[deviceId];
    } else {
      var prev = (Object.prototype.hasOwnProperty.call(map, deviceId) && map[deviceId] && typeof map[deviceId] === "object") ? map[deviceId] : {};
      var entry = Object.create(null);
      // Carry forward only whitelisted keys so a stale/manual non-overridable
      // key cannot survive in persisted state (canonical settings).
      for (var j = 0; j < PointerInputParse.OVERRIDABLE_KEYS.length; j++) {
        var ok = PointerInputParse.OVERRIDABLE_KEYS[j];
        if (Object.prototype.hasOwnProperty.call(prev, ok))
          entry[ok] = prev[ok];
      }
      entry[key] = value;
      map[deviceId] = entry;
    }
    Settings.data.pointer.perDeviceOverrides = map;
  }

  // Clear all overrides for a device (revert it to the global policy).
  function clearOverride(deviceId) {
    setOverride(deviceId, undefined, undefined);
  }

  // Enable/disable a device by id (persist-only filter; never shelled).
  function setDeviceEnabled(deviceId, enabled) {
    if (deviceId === undefined || deviceId === null)
      return;
    var list = (Settings.data.pointer.disabledDevices || []).slice();
    var idx = list.indexOf(deviceId);
    if (enabled && idx !== -1) {
      list.splice(idx, 1);
    } else if (!enabled && idx === -1) {
      list.push(deviceId);
    }
    Settings.data.pointer.disabledDevices = list;
  }

  // Normalize + persist the tablet mapping (clamps area into 0..1).
  function setTabletMapping(raw) {
    Settings.data.pointer.tabletMapping = PointerInputParse.normalizeTabletMapping(raw);
  }

  // ─── Live apply (qdwin_shell_v1.set_pointer_config, v28) ─────────
  // Push the global pointer policy to the compositor as one idempotent
  // snapshot. No-op (persist-only) until CapabilityService.pointerConfig is
  // live (a >= v28 bind); the compositor clamps/normalises out-of-range
  // values fail-safe. Per-device overrides are persist-only for now — the
  // request carries a single process-global policy, matching what the
  // compositor applies to every device.
  function applyToCompositor() {
    if (!canApply)
      return;
    var a = PointerCfg.toBindingArgs(Settings.data.pointer);
    Qdwin.applyPointerConfig(a.accelSpeed, a.accelProfile, a.naturalScroll,
                             a.tapToClick, a.leftHanded, a.middleEmulation,
                             a.disableWhileTyping, a.scrollMethod);
    Logger.i("PointerInputService", "applied pointer config accelSpeed="
             + a.accelSpeed + " profile=" + a.accelProfile + " natural="
             + a.naturalScroll + " tap=" + a.tapToClick + " scroll="
             + a.scrollMethod);
  }

  // Re-apply on any policy edit (debounced so a slider drag doesn't spam the
  // wire) and on the capability flipping live (shell (re)bind at >= v28).
  Timer {
    id: applyDebounce
    interval: 300
    repeat: false
    onTriggered: root.applyToCompositor()
  }
  function _requestApply() {
    if (canApply)
      applyDebounce.restart();
  }
  onAccelProfileChanged: _requestApply()
  onPointerSpeedChanged: _requestApply()
  onNaturalScrollChanged: _requestApply()
  onScrollMethodChanged: _requestApply()
  onTapToClickChanged: _requestApply()
  onDisableWhileTypingChanged: _requestApply()
  onLeftHandedChanged: _requestApply()
  onMiddleClickEmulationChanged: _requestApply()
  onCanApplyChanged: {
    if (canApply)
      applyToCompositor();
  }

  // ─── Init ────────────────────────────────────────────────────────
  function init() {
    Logger.i("PointerInputService", "Service started (qdwin live-apply: "
             + (canApply ? "active" : "persist-only until shell binds >= v28")
             + ")");
    refresh();
    // Push the persisted policy if the shell is already bound at >= v28.
    applyToCompositor();
  }

  // ─── Enumeration ─────────────────────────────────────────────────
  // First try libinput list-devices; if it is missing or unreadable, fall
  // back to /proc/bus/input/devices. The marker line lets the parser know
  // which format it received.
  function refresh() {
    enumProc.running = false;
    enumProc.command = ["sh", "-c", "if command -v libinput >/dev/null 2>&1 && libinput list-devices >/dev/null 2>&1; then " + "echo '@@SRC:libinput'; libinput list-devices 2>/dev/null; " + "else " + "echo '@@SRC:proc'; cat /proc/bus/input/devices 2>/dev/null; " + "fi"];
    enumProc.running = true;
  }

  Process {
    id: enumProc
    property string _stdout: ""
    onStarted: _stdout = ""
    stdout: StdioCollector {
      onStreamFinished: enumProc._stdout = text
    }
    stderr: StdioCollector {}
    onExited: (code, status) => {
      root._parseEnum(enumProc._stdout);
    }
  }

  // Parsing/classification lives in the pure PointerInputParse.js module
  // (dual QML/Node), so it is unit-testable headless. This wrapper only wires
  // the parsed result into the singleton's reactive state.
  function _parseEnum(out) {
    var res = PointerInputParse.parseEnum(out);
    root.enumSource = res.source;
    root.devices = res.devices;
    root.ready = true;
    Logger.i("PointerInputService", "enumerated", res.devices.length, "pointer device(s) via", root.enumSource || "none");
  }
}
