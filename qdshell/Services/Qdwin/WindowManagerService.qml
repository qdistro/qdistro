pragma Singleton

import QtQuick
import Quickshell
import qs.Commons
import qs.Services.Qdwin
import qs.Services.UI
import "WindowManagerPolicy.js" as WMPolicy
import "WmAccel.js" as WmAccel

// WindowManagerService — surfaces window-manager policy (focus, placement,
// snapping, titlebar action, decoration theme, WM shortcuts) and applies it
// to the qdwin compositor via qdwin_shell_v1.set_wm_policy / request_tile /
// request_fullscreen (v25), with the WM keyboard shortcuts driven through the
// v19 register_hotkey path.
//
// Backend model:
//   * qdshell runs on exactly one compositor — qdwin (via libweston). As of
//     qdwin_shell_v1 v25 the compositor applies WM policy live; until then
//     (older compositor / pre-bind) we are PERSIST-ONLY and the UI shows a
//     capability banner. `CapabilityService.wmPolicy` (gated on the actual
//     bind version, set from Qdwin.qml on bind) is the single source of truth;
//     there is NO probing for or dispatch to sway / labwc / any other tool.
//
// All policy normalisation lives in the pure WindowManagerPolicy.js module and
// accelerator parsing in WmAccel.js (both dual QML/Node) so the logic is
// unit-testable headless. The decoration theme name and shortcut strings are
// treated as UNTRUSTED free text: validated / clamped there before they are
// ever persisted, and accelerators that don't map to a (modifier, keycode)
// register no hotkey at all (rather than a wrong one).
Singleton {
  id: root

  // ─── Capability ──────────────────────────────────────────────────
  // Whether the active backend can live-apply WM policy. Sourced from the
  // unified CapabilityService (qdwin bound at >= v25), NOT from probing for
  // any non-qdwin window manager.
  readonly property bool canApplyWmPolicy: CapabilityService.wmPolicy
  // Whether the compositor can hold our WM-shortcut keybinds.
  readonly property bool canRegisterShortcuts: CapabilityService.keybindRegistration

  // ─── Settings convenience aliases ────────────────────────────────
  readonly property string focusPolicy: Settings.data.windowManager.focusPolicy
  readonly property int focusFollowsMouseDelay: Settings.data.windowManager.focusFollowsMouseDelay
  readonly property bool raiseOnClick: Settings.data.windowManager.raiseOnClick
  readonly property bool raiseOnHover: Settings.data.windowManager.raiseOnHover
  readonly property string placement: Settings.data.windowManager.placement
  readonly property bool snapEnabled: Settings.data.windowManager.snapEnabled
  readonly property int snapDistance: Settings.data.windowManager.snapDistance
  readonly property string titlebarDoubleClick: Settings.data.windowManager.titlebarDoubleClick
  readonly property string decorationTheme: Settings.data.windowManager.decorationTheme
  readonly property string shortcutClose: Settings.data.windowManager.shortcutClose
  readonly property string shortcutToggleMaximize: Settings.data.windowManager.shortcutToggleMaximize
  readonly property string shortcutToggleFullscreen: Settings.data.windowManager.shortcutToggleFullscreen
  readonly property string shortcutTileLeft: Settings.data.windowManager.shortcutTileLeft
  readonly property string shortcutTileRight: Settings.data.windowManager.shortcutTileRight

  // ─── Hotkey id ↔ action map ──────────────────────────────────────
  // Stable shell-assigned ids for register_hotkey. Based at 7100 to stay
  // clear of any future hotkey users.
  readonly property int hkClose: 7101
  readonly property int hkToggleMaximize: 7102
  readonly property int hkToggleFullscreen: 7103
  readonly property int hkTileLeft: 7104
  readonly property int hkTileRight: 7105
  // QDWIN_TS_* bits used by the toggle shortcuts.
  readonly property int _tsMaximized: 1
  readonly property int _tsFullscreen: 2
  // Tracks which ids we currently have registered, so re-registration on an
  // accelerator edit can release stale combos first.
  property var _registeredIds: ({})

  // ─── Apply policy ────────────────────────────────────────────────
  function applyPolicy() {
    if (!canApplyWmPolicy)
      return;
    const p = WMPolicy.normalizePolicy(Settings.data.windowManager);
    const focusEnum = WMPolicy.FOCUS_POLICIES.indexOf(p.focusPolicy);
    const placeEnum = WMPolicy.PLACEMENTS.indexOf(p.placement);
    Qdwin.applyWmPolicy(focusEnum < 0 ? 0 : focusEnum,
                        p.focusFollowsMouseDelay,
                        p.raiseOnClick, p.raiseOnHover,
                        placeEnum < 0 ? 2 : placeEnum,
                        p.snapEnabled, p.snapDistance);
    Logger.i("WindowManagerService", "applied WM policy focus=" + p.focusPolicy
             + " placement=" + p.placement + " snap=" + p.snapEnabled
             + "/" + p.snapDistance);
  }

  // ─── Register WM-shortcut hotkeys ────────────────────────────────
  // Each shortcut maps to a stable hotkey id. We always release the prior
  // binding for an id first (so an accelerator edit replaces cleanly), then
  // register the new combo subject to two safety rules:
  //   * a global WM hotkey MUST carry a modifier — a bare key ("a", "F1",
  //     "Delete") would steal that key globally from every window, so
  //     mods === 0 combos are refused;
  //   * the same combo can't bind two actions — duplicates (first id wins)
  //     are skipped so one press never fires two WM actions.
  function registerShortcuts() {
    if (!canRegisterShortcuts)
      return;
    const specs = [
      { id: hkClose, accel: root.shortcutClose },
      { id: hkToggleMaximize, accel: root.shortcutToggleMaximize },
      { id: hkToggleFullscreen, accel: root.shortcutToggleFullscreen },
      { id: hkTileLeft, accel: root.shortcutTileLeft },
      { id: hkTileRight, accel: root.shortcutTileRight }
    ];
    const seen = {};
    for (let i = 0; i < specs.length; i++) {
      const id = specs[i].id;
      if (root._registeredIds[id]) {
        Qdwin.unregisterHotkey(id);
        delete root._registeredIds[id];
      }
      const combo = WmAccel.parse(specs[i].accel);
      if (!combo)
        continue;  // unmappable / empty / modifier-only
      if (combo.modifiers === 0) {
        Logger.w("WindowManagerService", "skipping modifier-less WM shortcut '"
                 + specs[i].accel + "' (would steal a bare key globally)");
        continue;
      }
      const key = combo.modifiers + ":" + combo.key;
      if (seen[key]) {
        Logger.w("WindowManagerService", "skipping duplicate WM accelerator '"
                 + specs[i].accel + "' (already bound to another action)");
        continue;
      }
      seen[key] = true;
      Qdwin.registerHotkey(id, combo.modifiers, combo.key);
      root._registeredIds[id] = true;
    }
    Logger.i("WindowManagerService", "registered WM shortcuts ("
             + Object.keys(root._registeredIds).length + " active)");
  }

  // ─── Dispatch a fired hotkey to a window-manager action ──────────
  function _onHotkey(id) {
    const h = Qdwin.focusedHandle;
    if (h <= 0)
      return;  // no focused window — nothing to act on
    const st = Qdwin.windowState(h);
    switch (id) {
    case root.hkClose:
      Qdwin.closeHandle(h);
      break;
    case root.hkToggleMaximize:
      Qdwin.requestMaximizeHandle(h, (st & root._tsMaximized) === 0);
      break;
    case root.hkToggleFullscreen:
      Qdwin.requestFullscreenHandle(h, (st & root._tsFullscreen) === 0);
      break;
    case root.hkTileLeft:
      Qdwin.requestTileHandle(h, 1);
      break;
    case root.hkTileRight:
      Qdwin.requestTileHandle(h, 2);
      break;
    default:
      break;
    }
  }

  // ─── Reactions ───────────────────────────────────────────────────
  // Re-apply the policy whenever a policy field or the capability changes.
  // set_wm_policy is idempotent so re-pushing on each edit is cheap.
  onFocusPolicyChanged: applyPolicy()
  onFocusFollowsMouseDelayChanged: applyPolicy()
  onRaiseOnClickChanged: applyPolicy()
  onRaiseOnHoverChanged: applyPolicy()
  onPlacementChanged: applyPolicy()
  onSnapEnabledChanged: applyPolicy()
  onSnapDistanceChanged: applyPolicy()
  // Capability flips true on bind → push the full policy + (re)register the
  // shortcuts; flips false on unbind → drop our id bookkeeping (the
  // compositor already released the bindings at unbind).
  onCanApplyWmPolicyChanged: {
    if (canApplyWmPolicy)
      applyPolicy();
  }
  onCanRegisterShortcutsChanged: {
    if (canRegisterShortcuts)
      registerShortcuts();
    else
      root._registeredIds = ({});
  }
  // Re-register when any accelerator changes.
  onShortcutCloseChanged: registerShortcuts()
  onShortcutToggleMaximizeChanged: registerShortcuts()
  onShortcutToggleFullscreenChanged: registerShortcuts()
  onShortcutTileLeftChanged: registerShortcuts()
  onShortcutTileRightChanged: registerShortcuts()

  Connections {
    target: Qdwin
    function onHotkeyPressed(id) { root._onHotkey(id); }
  }

  // ─── Init ────────────────────────────────────────────────────────
  function init() {
    Logger.i("WindowManagerService", "Service started (qdwin v25 live-apply: "
             + canApplyWmPolicy + ", shortcuts: " + canRegisterShortcuts + ")");
    if (canApplyWmPolicy)
      applyPolicy();
    if (canRegisterShortcuts)
      registerShortcuts();
  }
}
