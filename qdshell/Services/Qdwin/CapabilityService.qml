pragma Singleton

import QtQuick
import Quickshell
import qs.Commons

/// CapabilityService — single source of truth for what the qdwin compositor's
/// `qdwin_shell_v1` IPC can currently *apply live*.
///
/// qdshell supports exactly one compositor (qdwin via libweston). Several
/// settings areas (pointer/touchpad, keyboard xkb+repeat, window-manager
/// policy, display/output management, workspace mutation, idle/DPMS, global
/// keybind registration) describe state that only the compositor can enact.
/// Until `qdwin_shell_v1` grows the matching request, those controls are
/// PERSIST-ONLY: the value is stored and surfaced in the UI behind a
/// capability note, and applies automatically once the backend supports it.
///
/// This replaces the ad-hoc per-page capability detection that used to probe
/// for foreign compositors (swaymsg/hyprctl/…). There is NO probing for or
/// dispatch to any non-qdwin compositor — every flag is derived purely from the
/// (compile-time-fixed) qdwin backend identity, and flips to true in exactly
/// one place here when the corresponding qdwin_shell_v1 request lands.
///
/// NOTE: pages that ALSO have a legitimate non-compositor apply path keep it.
/// Keyboard and Accessibility apply via X11/XWayland tooling (setxkbmap, xset,
/// xkbset, numlockx) when an X server is reachable — that is the X server, not
/// a foreign Wayland compositor, so it is allowed and unaffected by these
/// flags. Those services OR their X capability together with the relevant flag
/// below.
Singleton {
  id: root

  // ─── qdwin_shell_v1 live-apply capabilities ──────────────────────
  // When qdwin gains a request, flip the corresponding flag here (set from
  // Qdwin.qml on bind, gated on the negotiated shell version) and every
  // consumer follows.

  // libinput pointer/touchpad configuration (accel, scroll, tap, …). Live as
  // of qdwin_shell_v1 v28 (set_pointer_config). Like wmPolicy this is gated on
  // the shell actually binding at >= v28 (an older compositor leaves it false
  // and the Mouse tab stays persist-only). Qdwin.qml calls
  // setPointerConfig() on bind. Writable to keep the import direction
  // Qdwin → CapabilityService and avoid a singleton import cycle.
  property bool pointerConfig: false
  function setPointerConfig(available) {
    if (pointerConfig !== available) {
      pointerConfig = available;
      Logger.i("CapabilityService", "pointerConfig -> " + available);
    }
  }
  // xkb key-repeat delay & rate. Live as of qdwin_shell_v1 v28
  // (set_key_repeat). Gated on a >= v28 bind, set from Qdwin.qml. (xkb
  // layout/model/options still apply via the X server path in
  // KeyboardInputService; only repeat is carried by this request.) Writable
  // to keep the Qdwin → CapabilityService import direction.
  property bool xkbRepeat: false
  function setXkbRepeat(available) {
    if (xkbRepeat !== available) {
      xkbRepeat = available;
      Logger.i("CapabilityService", "xkbRepeat -> " + available);
    }
  }
  // Window-manager policy mutation (focus, placement, snapping). Live as of
  // qdwin_shell_v1 v25 (set_wm_policy). Like outputManagement this is gated on
  // the shell actually binding at >= v25 (an older compositor leaves it false
  // and the WindowManager tab stays persist-only). Qdwin.qml calls
  // setWmPolicy() on bind. Writable to keep the import direction
  // Qdwin → CapabilityService and avoid a singleton import cycle.
  property bool wmPolicy: false
  function setWmPolicy(available) {
    if (wmPolicy !== available) {
      wmPolicy = available;
      Logger.i("CapabilityService", "wmPolicy -> " + available);
    }
  }
  // Output management (resolution, scale, rotation, position, enable/disable).
  // qdwin implements wlr-output-management-v1; unlike the other flags this is
  // LIVE. It is gated on the binding actually advertising the manager global
  // (not merely compiled in): Qdwin.qml calls setOutputManagement() once
  // QdwinBinding.outputManagementAvailable goes true (and back to false on a
  // disconnect). Writable (not readonly / not a binding on Qdwin) to keep the
  // import direction Qdwin → CapabilityService, avoiding a singleton import
  // cycle (CapabilityService imports only Commons).
  property bool outputManagement: false
  function setOutputManagement(available) {
    if (outputManagement !== available) {
      outputManagement = available;
      Logger.i("CapabilityService", "outputManagement -> " + available);
    }
  }
  // Workspace creation/switching/mutation.
  readonly property bool workspaceMutation: false
  // Idle timeout + display DPMS control. Live as of qdwin_shell_v1 v26: the
  // idle *trigger* rides the standard ext-idle-notify-v1 (a client in the
  // binding) and display-off uses the v26 set_display_power request. Gated on
  // BOTH being available (a >= v26 bind AND ext_idle_notifier_v1 + a wl_seat
  // bound), set from Qdwin.qml. Writable to keep the Qdwin → CapabilityService
  // import direction and avoid a singleton cycle.
  property bool idleDpms: false
  function setIdleDpms(available) {
    if (idleDpms !== available) {
      idleDpms = available;
      Logger.i("CapabilityService", "idleDpms -> " + available);
    }
  }
  // Global keyboard-shortcut (keybind) registration. Live as of v25 — the
  // qdwin_shell_v1 v19 register_hotkey path is now wired in the binding and
  // used by WindowManagerService for the WM keyboard shortcuts. Gated on the
  // shell binding at >= v25 (set alongside wmPolicy from Qdwin.qml on bind).
  property bool keybindRegistration: false
  function setKeybindRegistration(available) {
    if (keybindRegistration !== available) {
      keybindRegistration = available;
      Logger.i("CapabilityService", "keybindRegistration -> " + available);
    }
  }

  // ─── Shared messaging ────────────────────────────────────────────
  // Generic note for a control whose backend cannot apply yet. Pages with a
  // more specific string (e.g. mentioning the X server fallback) keep theirs.
  readonly property string persistOnlyNote: I18n.tr("capabilities.persist-only-note")

  function init() {
    Logger.i("CapabilityService", "qdwin live-apply capabilities: "
             + "pointer=" + pointerConfig + " xkb=" + xkbRepeat
             + " wm=" + wmPolicy + " output=" + outputManagement
             + " workspace=" + workspaceMutation + " idleDpms=" + idleDpms
             + " keybind=" + keybindRegistration);
  }
}
