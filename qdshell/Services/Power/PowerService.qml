pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Hardware
import qs.Services.Qdwin
import qs.Services.UI
import "BrightnessPolicy.js" as BrightnessPolicy
import "IdlePolicy.js" as IdlePolicy

Singleton {
  id: root

  // Capability flags — queried from logind at startup
  property bool canSuspend: false
  property bool canHibernate: false
  property bool canHybridSleep: false
  property bool canPowerOff: false

  // Whether the compositor can apply idle-timeout + display DPMS policy. qdwin
  // exposes no idle/DPMS request in qdwin_shell_v1 yet, so this is currently
  // false and the inactivity/display-off timers are persist-only. Sourced from
  // the unified CapabilityService — there is NO swayidle/wlopm/wlr-randr/
  // hyprctl dispatch (qdwin is the only supported compositor). Lid and
  // power/sleep-button handling are unaffected: those use logind + evdev, not
  // a foreign compositor.
  readonly property bool canApplyIdle: CapabilityService.idleDpms

  // Whether a lid is physically present (laptop)
  property bool hasLid: false
  // Whether AC power is connected (best-effort; defaults true on desktops)
  property bool onAC: true

  // Settings-backed policy (convenience aliases)
  readonly property string powerButtonAction:        Settings.data.power.powerButtonAction
  readonly property string sleepButtonAction:         Settings.data.power.sleepButtonAction
  readonly property string lidCloseOnBattery:         Settings.data.power.lidCloseOnBattery
  readonly property string lidCloseOnAC:              Settings.data.power.lidCloseOnAC
  readonly property bool   lidIgnoreExternalDisplay:  Settings.data.power.lidIgnoreExternalDisplay
  readonly property int    inactivityTimeoutBattery:  Settings.data.power.inactivityTimeoutBattery
  readonly property int    inactivityTimeoutAC:       Settings.data.power.inactivityTimeoutAC
  readonly property string inactivityAction:          Settings.data.power.inactivityAction
  readonly property int    criticalBatteryLevel:      Settings.data.power.criticalBatteryLevel
  readonly property string criticalBatteryAction:     Settings.data.power.criticalBatteryAction
  readonly property int    displayOffBattery:         Settings.data.power.displayOffBattery
  readonly property int    displayOffAC:              Settings.data.power.displayOffAC

  // ─── Initialisation ──────────────────────────────────────────────
  function init() {
    Logger.i("PowerService", "Service started");
    queryCapabilities();
    detectLid(); // triggers startLidMonitor() via onHasLidChanged when a lid is found
    detectACState();
    startButtonMonitor();
    checkCriticalBattery();
    applyIdlePolicy();  // arm idle/display-off if the backend supports it
  }

  // ─── Capability detection (loginctl) ─────────────────────────────
  Process {
    id: canSuspendProc
    command: ["sh", "-c", "out=$(busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanSuspend 2>/dev/null) || { echo yes; exit 0; }; echo \"$out\" | sed 's/^s \"//;s/\"$//'"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        root.canSuspend = String(text || "").trim() === "yes";
        Logger.d("PowerService", "CanSuspend:", root.canSuspend);
      }
    }
    stderr: StdioCollector {}
  }

  Process {
    id: canHibernateProc
    command: ["sh", "-c", "out=$(busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanHibernate 2>/dev/null) || { echo no; exit 0; }; echo \"$out\" | sed 's/^s \"//;s/\"$//'"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        root.canHibernate = String(text || "").trim() === "yes";
        Logger.d("PowerService", "CanHibernate:", root.canHibernate);
      }
    }
    stderr: StdioCollector {}
  }

  Process {
    id: canHybridSleepProc
    command: ["sh", "-c", "out=$(busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanHybridSleep 2>/dev/null) || { echo no; exit 0; }; echo \"$out\" | sed 's/^s \"//;s/\"$//'"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        root.canHybridSleep = String(text || "").trim() === "yes";
        Logger.d("PowerService", "CanHybridSleep:", root.canHybridSleep);
      }
    }
    stderr: StdioCollector {}
  }

  Process {
    id: canPowerOffProc
    command: ["sh", "-c", "out=$(busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanPowerOff 2>/dev/null) || { echo yes; exit 0; }; echo \"$out\" | sed 's/^s \"//;s/\"$//'"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        root.canPowerOff = String(text || "").trim() === "yes";
        Logger.d("PowerService", "CanPowerOff:", root.canPowerOff);
      }
    }
    stderr: StdioCollector {}
  }

  function queryCapabilities() {
    canSuspendProc.running = true;
    canHibernateProc.running = true;
    canHybridSleepProc.running = true;
    canPowerOffProc.running = true;
  }

  // ─── Lid detection ───────────────────────────────────────────────
  Process {
    id: lidDetectProc
    command: ["sh", "-c", "{ test -e /proc/acpi/button/lid/LID0/state || test -e /proc/acpi/button/lid/LID/state; } && echo yes || echo no"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        root.hasLid = String(text || "").trim() === "yes";
        Logger.d("PowerService", "hasLid:", root.hasLid);
      }
    }
    stderr: StdioCollector {}
  }

  function detectLid() {
    lidDetectProc.running = true;
  }

  // ─── AC state detection ──────────────────────────────────────────
  Process {
    id: acDetectProc
    command: ["sh", "-c", "online=; for p in /sys/class/power_supply/*; do t=$(cat \"$p/type\" 2>/dev/null); if [ \"$t\" = Mains ] || [ \"$t\" = USB ]; then v=$(cat \"$p/online\" 2>/dev/null); [ \"$v\" = 1 ] && online=1; [ -z \"$online\" ] && [ \"$v\" = 0 ] && online=0; fi; done; echo \"$online\""]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        var val = String(text || "").trim();
        if (val === "1")
          root.onAC = true;
        else if (val === "0")
          root.onAC = false;
        // else keep default (true for desktops)
        Logger.d("PowerService", "onAC:", root.onAC);
      }
    }
    stderr: StdioCollector {}
  }

  Timer {
    id: acPollTimer
    interval: 30000 // 30 seconds
    repeat: true
    running: true
    onTriggered: detectACState()
  }

  function detectACState() {
    acDetectProc.running = true;
  }

  // ─── Lid switch handling ─────────────────────────────────────────
  // logind decides the default lid action from /etc/systemd/logind.conf
  // (HandleLidSwitch*), which a user session cannot change. To make the
  // qdshell lid policy effective we take an inhibitor lock on the handle
  // events so logind defers to us, then poll the kernel lid state and apply
  // the configured action ourselves. Power/sleep button actions likewise
  // require either logind config or evdev access; we surface them in the UI
  // and apply them when the user invokes them through qdshell, but logind
  // remains the authority for the physical keys.
  property string _lidState: "open"

  // Hold an inhibitor lock so logind does not run its own lid handler.
  Process {
    id: lidInhibitProc
    running: false
    command: ["sh", "-c", "systemd-inhibit --what=handle-lid-switch --who=qdshell --why='qdshell power manager lid policy' --mode=block sleep infinity"]
    stderr: StdioCollector {}
  }

  Process {
    id: lidStateProc
    command: ["sh", "-c", "cat /proc/acpi/button/lid/LID0/state /proc/acpi/button/lid/LID/state 2>/dev/null | head -1"]
    running: false
    stdout: StdioCollector {
      onStreamFinished: {
        var line = String(text || "").trim().toLowerCase();
        var newState = line.indexOf("closed") !== -1 ? "closed" : "open";
        if (newState !== root._lidState) {
          var wasOpen = root._lidState === "open";
          root._lidState = newState;
          if (wasOpen && newState === "closed")
            root.onLidClosed();
        }
      }
    }
    stderr: StdioCollector {}
  }

  Timer {
    id: lidPollTimer
    interval: 2000
    repeat: true
    running: false
    onTriggered: lidStateProc.running = true
  }

  function onLidClosed() {
    Logger.i("PowerService", "Lid closed");
    if (IdleInhibitorService.isInhibited)
      return;
    if (lidIgnoreExternalDisplay && Quickshell.screens && Quickshell.screens.length > 1) {
      Logger.i("PowerService", "Lid close ignored — external display connected");
      return;
    }
    var action = onAC ? lidCloseOnAC : lidCloseOnBattery;
    executeAction(action);
  }

  function startLidMonitor() {
    if (!hasLid)
      return;
    lidInhibitProc.running = true;
    lidPollTimer.start();
  }

  onHasLidChanged: {
    if (hasLid && !lidPollTimer.running)
      startLidMonitor();
  }

  // ─── Power / Sleep button handling ───────────────────────────────
  // As with the lid, logind owns the physical keys by default. We take
  // inhibitor locks on handle-power-key / handle-suspend-key so logind
  // defers to qdshell, then watch evdev (via libinput debug-events) for
  // KEY_POWER / KEY_SLEEP presses and apply the configured action. If
  // libinput is unavailable or unreadable, the locks are released so
  // logind resumes its default behaviour (never leaving the keys dead).
  property bool _buttonMonitorActive: false

  Process {
    id: powerKeyInhibitProc
    running: false
    command: ["sh", "-c", "systemd-inhibit --what=handle-power-key:handle-suspend-key --who=qdshell --why='qdshell power manager button policy' --mode=block sleep infinity"]
    stderr: StdioCollector {}
  }

  Process {
    id: buttonMonitorProc
    running: false
    // libinput emits e.g. "KEY_POWER (116) pressed" lines on KEYBOARD_KEY events.
    command: ["sh", "-c", "command -v libinput >/dev/null 2>&1 || exit 1; libinput debug-events 2>/dev/null"]
    stdout: SplitParser {
      onRead: data => {
        var line = String(data || "");
        if (line.indexOf("pressed") === -1)
          return;
        if (line.indexOf("KEY_POWER") !== -1) {
          Logger.i("PowerService", "Power button pressed");
          root.executeAction(root.powerButtonAction);
        } else if (line.indexOf("KEY_SLEEP") !== -1 || line.indexOf("KEY_SUSPEND") !== -1) {
          Logger.i("PowerService", "Sleep button pressed");
          root.executeAction(root.sleepButtonAction);
        }
      }
    }
    stderr: StdioCollector {}
    onExited: function (exitCode) {
      // libinput missing or not permitted — release the inhibitor locks so
      // logind keeps handling the keys with its own configuration.
      if (root._buttonMonitorActive) {
        Logger.w("PowerService", "Button monitor unavailable (exit", exitCode + "); releasing key inhibitors, logind will handle power/sleep keys");
        root._buttonMonitorActive = false;
        if (powerKeyInhibitProc.running)
          powerKeyInhibitProc.signal(15);
      }
    }
  }

  function startButtonMonitor() {
    buttonMonitorProc.running = true;
    // Only take the inhibitor locks if the monitor actually started; if it
    // exits immediately the onExited handler releases them.
    powerKeyInhibitProc.running = true;
    _buttonMonitorActive = true;
  }

  // ─── Actions ─────────────────────────────────────────────────────
  function executeSuspend() {
    if (!canSuspend) {
      Logger.w("PowerService", "Suspend not available");
      return;
    }
    Logger.i("PowerService", "Executing suspend");
    Quickshell.execDetached(["sh", "-c", "systemctl suspend || loginctl suspend"]);
  }

  function executeHibernate() {
    if (!canHibernate) {
      Logger.w("PowerService", "Hibernate not available");
      return;
    }
    Logger.i("PowerService", "Executing hibernate");
    Quickshell.execDetached(["sh", "-c", "systemctl hibernate || loginctl hibernate"]);
  }

  function executeHybridSleep() {
    if (!canHybridSleep) {
      Logger.w("PowerService", "Hybrid sleep not available");
      return;
    }
    Logger.i("PowerService", "Executing hybrid sleep");
    Quickshell.execDetached(["sh", "-c", "systemctl hybrid-sleep || loginctl hybrid-sleep"]);
  }

  function executePowerOff() {
    Logger.i("PowerService", "Executing power off");
    Quickshell.execDetached(["sh", "-c", "systemctl poweroff || loginctl poweroff"]);
  }

  function executeAction(action) {
    switch (action) {
    case "suspend":
      executeSuspend();
      break;
    case "hibernate":
      executeHibernate();
      break;
    case "hybrid-sleep":
      executeHybridSleep();
      break;
    case "shutdown":
      executePowerOff();
      break;
    case "ask":
      // Open the session menu to let the user choose
      Logger.i("PowerService", "Opening session menu for user choice");
      PanelService.getPanel("sessionMenuPanel")?.toggle();
      break;
    case "nothing":
    default:
      break;
    }
  }

  // ─── Idle timeout + display-off policy (live as of qdwin v26) ─────
  // Driven by the standard ext-idle-notify-v1: qdwin observes real input idle
  // and the binding arms two notifications (slot 0 = inactivity action,
  // slot 1 = display-off). On `idled` we run the inactivity action /
  // DPMS-off the displays; on `resumed` (input wakes the compositor) we
  // DPMS them back on. Display-off uses the v26 set_display_power request.
  // Gated on CapabilityService.idleDpms (a >= v26 bind AND ext_idle_notifier
  // + a wl_seat); persist-only otherwise. NO swayidle / wlopm / wlr-randr —
  // qdwin is the only supported compositor.
  //
  // (Critical-battery, lid and power/sleep-button actions are separate: they
  // are driven by logind capability + evdev, not by the compositor idle API.)
  readonly property int activeInactivityTimeout: onAC ? inactivityTimeoutAC : inactivityTimeoutBattery
  readonly property int activeDisplayOffTimeout: onAC ? displayOffAC : displayOffBattery

  // Idle notification slots (must match the slot ids passed to the binding).
  readonly property int _idleSlotInactivity: 0
  readonly property int _idleSlotDisplayOff: 1

  // (Re)arm or cancel both idle notifications from the current policy. A
  // timeout of 0 means "never" (cancel). Presentation mode suppresses BOTH —
  // the user explicitly asked to stay awake, and qdwin's ext-idle-notify
  // respects only Wayland idle-inhibitors, not our systemd-inhibit presentation
  // path, so we must gate here. Minutes → ms.
  function applyIdlePolicy() {
    if (!canApplyIdle)
      return;
    var arm = IdlePolicy.resolveArming(inactivityAction, activeInactivityTimeout,
                                       activeDisplayOffTimeout,
                                       IdleInhibitorService.presentationModeActive);
    Qdwin.setIdleNotification(_idleSlotInactivity, arm.inactivityMs);
    Qdwin.setIdleNotification(_idleSlotDisplayOff, arm.displayOffMs);
    Logger.i("PowerService", "idle policy armed: inactivity=" + arm.inactivityMs
             + "ms (" + inactivityAction + "), displayOff=" + arm.displayOffMs
             + "ms, presentation=" + IdleInhibitorService.presentationModeActive);
  }

  function _onIdleState(slot, idle) {
    // Re-check presentation mode at fire time: applyIdlePolicy() cancels both
    // slots when it turns on, but an `idled` already in flight could still
    // race the cancel. Suppress idle=true actions while inhibited; always
    // honour resume (DPMS-on) so the screen can't get stuck off.
    var suppress = IdleInhibitorService.presentationModeActive;
    if (slot === _idleSlotInactivity) {
      if (idle && !suppress) {
        Logger.i("PowerService", "inactivity idle reached -> " + inactivityAction);
        executeAction(inactivityAction);
      }
    } else if (slot === _idleSlotDisplayOff) {
      // Off on idle (unless inhibited), back on when the user returns.
      if (idle && suppress)
        return;
      Logger.i("PowerService", "display-off idle " + (idle ? "-> DPMS off" : "resume -> DPMS on"));
      Qdwin.setDisplayPower(!idle);
    }
  }

  Connections {
    target: Qdwin
    function onIdleStateChanged(slot, idle) { root._onIdleState(slot, idle); }
  }

  // Re-arm whenever the policy, capability, AC source, or presentation mode
  // changes (the effective timeouts depend on onAC).
  onActiveInactivityTimeoutChanged: applyIdlePolicy()
  onActiveDisplayOffTimeoutChanged: applyIdlePolicy()
  onInactivityActionChanged: applyIdlePolicy()
  onCanApplyIdleChanged: applyIdlePolicy()
  Connections {
    target: IdleInhibitorService
    function onPresentationModeActiveChanged() { root.applyIdlePolicy(); }
  }

  // ─── Per-power-source brightness (xfce4-power-manager parity) ─────
  // When automatic battery reduction is enabled, dropping to battery applies
  // the reduced level and returning to AC restores the normal level. This is
  // LIVE-APPLY (not compositor-gated): BrightnessService drives a real backlight
  // via brightnessctl/ddcutil/asdbctl. It is effectively gated by
  // brightnessControlAvailable — if no backlight is controllable, setBrightness
  // is a no-op. All maths (clamp 0..100, percent->fraction, transition
  // decision) lives in the pure BrightnessPolicy.js so it is unit-tested.
  readonly property bool autoReduceBrightnessOnBattery: Settings.data.brightness.autoReduceOnBattery
  readonly property int  acBrightnessLevel:             Settings.data.brightness.acBrightnessLevel
  readonly property int  batteryBrightnessLevel:        Settings.data.brightness.batteryBrightnessLevel

  // Whether at least one monitor can actually have its brightness set.
  readonly property bool brightnessControllable: {
    var ms = BrightnessService.monitors;
    if (!ms)
      return false;
    for (var i = 0; i < ms.length; i++) {
      if (ms[i] && ms[i].brightnessControlAvailable)
        return true;
    }
    return false;
  }

  function applyPerSourceBrightness() {
    if (!autoReduceBrightnessOnBattery)
      return;
    if (!brightnessControllable) {
      Logger.d("PowerService", "Per-source brightness: no controllable backlight, skipping");
      return;
    }
    var res = BrightnessPolicy.resolveTransition(onAC, autoReduceBrightnessOnBattery, acBrightnessLevel, batteryBrightnessLevel);
    if (!res.apply)
      return;
    Logger.i("PowerService", "Applying per-source brightness:", Math.round(res.fraction * 100) + "%", "(onAC:", onAC + ")");
    BrightnessService.setBrightness(res.fraction);
  }

  // React to AC<->battery transitions.
  onOnACChanged: {
    applyPerSourceBrightness();
    applyIdlePolicy();  // effective idle/display-off timeouts depend on onAC
  }

  // ─── Critical battery handling ───────────────────────────────────
  // Watch the primary battery and trigger the configured action once when the
  // level drops to/below the configured threshold while discharging.
  property bool _criticalActionTaken: false

  Connections {
    target: BatteryService
    function onBatteryPercentageChanged() {
      root.checkCriticalBattery();
    }
    function onBatteryChargingChanged() {
      root.checkCriticalBattery();
    }
    function onLaptopBatteriesChanged() {
      root.checkCriticalBattery();
    }
  }

  // Safety-net poll in case a laptop battery is not the primary device and
  // its change signals are not observed through the aggregate properties.
  Timer {
    id: criticalBatteryPoll
    interval: 60000
    repeat: true
    running: true
    onTriggered: root.checkCriticalBattery()
  }

  // Resolve an effective critical-battery action, falling back when the
  // configured one is unavailable so protection still happens.
  function effectiveCriticalAction() {
    var action = criticalBatteryAction;
    if (action === "hibernate" && !canHibernate)
      action = "suspend";
    if (action === "suspend" && !canSuspend)
      action = "shutdown";
    return action;
  }

  function checkCriticalBattery() {
    // Only consider the laptop/internal battery — never a Bluetooth
    // peripheral (mouse/headset), which would otherwise suspend a desktop.
    var batteries = BatteryService.laptopBatteries;
    if (!batteries || batteries.length === 0) {
      _criticalActionTaken = false;
      return;
    }
    var dev = batteries[0];

    var pct = BatteryService.getPercentage(dev);
    var charging = BatteryService.isCharging(dev);
    var plugged = BatteryService.isPluggedIn(dev);

    // Reset latch once charging or comfortably above the threshold.
    if (charging || plugged || pct > criticalBatteryLevel + 2) {
      _criticalActionTaken = false;
      return;
    }

    if (!BatteryService.isDevicePresent(dev) || !BatteryService.isDeviceReady(dev))
      return;

    if (_criticalActionTaken)
      return;

    if (pct <= criticalBatteryLevel) {
      _criticalActionTaken = true;
      var action = effectiveCriticalAction();
      Logger.w("PowerService", "Critical battery level reached, executing:", action);
      executeAction(action);
    }
  }

  // ─── Cleanup ─────────────────────────────────────────────────────
  Component.onDestruction: {
    if (lidInhibitProc.running)
      lidInhibitProc.signal(15);
    if (powerKeyInhibitProc.running)
      powerKeyInhibitProc.signal(15);
    if (buttonMonitorProc.running)
      buttonMonitorProc.signal(15);
  }
}
