import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin
import qs.Services.UI

Item {
  id: root

  // Lock is triggered via qdlocker's ctrl socket
  // ($XDG_RUNTIME_DIR/qdlocker.sock), NOT the deprecated
  // PanelService.lockScreen / WlSessionLock path which qdwin doesn't
  // implement. This mirrors Modules/Bar/Widgets/LockButton.qml.
  Process {
    id: lockProc
    command: ["sh", "-c",
              "printf 'lock\\n' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/qdlocker.sock"]
    stdout: StdioCollector {}
    stderr: StdioCollector {}
  }

  // Provider metadata
  property string name: I18n.tr("tooltips.session-menu")
  property var launcher: null
  property bool handleSearch: LauncherSettings.enableSessionSearch
  property string supportedLayouts: "list"
  property string iconMode: LauncherSettings.iconMode

  // Session actions with search keywords
  readonly property var sessionActions: [
    {
      "action": "lock",
      "labelKey": "common.lock",
      "icon": iconMode === "tabler" ? "lock" : "system-lock-screen",
      "keywords": ["lock", "screen", "secure"]
    },
    {
      "action": "suspend",
      "labelKey": "common.suspend",
      "icon": iconMode === "tabler" ? "suspend" : "system-suspend",
      "keywords": ["suspend", "sleep", "standby"]
    },
    {
      "action": "hibernate",
      "labelKey": "common.hibernate",
      "icon": iconMode === "tabler" ? "hibernate" : "system-suspend-hibernate",
      "keywords": ["hibernate", "disk"]
    },
    {
      "action": "reboot",
      "labelKey": "common.reboot",
      "icon": iconMode === "tabler" ? "reboot" : "system-reboot",
      "keywords": ["reboot", "restart", "reload"]
    },
    {
      "action": "logout",
      "labelKey": "common.logout",
      "icon": iconMode === "tabler" ? "logout" : "system-log-out",
      "keywords": ["logout", "sign out", "exit", "leave"]
    },
    {
      "action": "shutdown",
      "labelKey": "common.shutdown",
      "icon": iconMode === "tabler" ? "shutdown" : "system-shutdown",
      "keywords": ["shutdown", "power off", "turn off", "poweroff"]
    }
  ]

  function init() {
    Logger.d("SessionProvider", "Initialized");
  }

  function getEnabledActions() {
    var powerOptions = Settings.data.sessionMenu.powerOptions || [];
    var enabledSet = {};

    for (var i = 0; i < powerOptions.length; i++) {
      if (powerOptions[i].enabled) {
        enabledSet[powerOptions[i].action] = powerOptions[i];
      }
    }

    var enabled = [];
    for (var j = 0; j < sessionActions.length; j++) {
      var action = sessionActions[j];
      if (enabledSet[action.action]) {
        enabled.push({
                       "action": action.action,
                       "labelKey": action.labelKey,
                       "icon": action.icon,
                       "keywords": action.keywords,
                       "command": enabledSet[action.action].command || ""
                     });
      }
    }

    return enabled;
  }

  function getResults(query) {
    if (!query)
      return [];

    var trimmed = query.trim();
    if (!trimmed || trimmed.length < 2)
      return [];

    var enabledActions = getEnabledActions();
    if (enabledActions.length === 0)
      return [];

    // Build searchable items with resolved translations
    var items = [];
    for (var i = 0; i < enabledActions.length; i++) {
      var action = enabledActions[i];
      var label = I18n.tr(action.labelKey);
      items.push({
                   "action": action.action,
                   "icon": action.icon,
                   "label": label,
                   "command": action.command,
                   "searchText": [label.toLowerCase()].concat(action.keywords).join(" ")
                 });
    }

    var results = FuzzySort.go(trimmed, items, {
                                 "keys": ["label", "searchText"],
                                 "limit": 6
                               });

    var launcherItems = [];
    for (var j = 0; j < results.length; j++) {
      var entry = results[j].obj;
      var score = results[j].score;

      launcherItems.push({
                           "name": entry.label,
                           "description": I18n.tr("tooltips.session-menu"),
                           "icon": entry.icon,
                           "isTablerIcon": true,
                           "isImage": false,
                           "_score": score - 1,
                           "provider": root,
                           "onActivate": createActivateHandler(entry.action, entry.command)
                         });
    }

    return launcherItems;
  }

  function createActivateHandler(action, command) {
    return function () {
      if (launcher)
        launcher.close();

      Qt.callLater(() => {
                     executeAction(action);
                   });
    };
  }

  function executeAction(action) {
    // Default behavior or custom command handled by Qdwin
    switch (action) {
    case "lock":
      // Fire lock_requested(reason=3=manual) over the qdlocker ctrl
      // socket (same path as the bar's LockButton).
      lockProc.running = true;
      break;
    case "suspend":
      if (Settings.data.general.lockOnSuspend) {
        Qdwin.lockAndSuspend();
      } else {
        Qdwin.suspend();
      }
      break;
    case "hibernate":
      Qdwin.hibernate();
      break;
    case "reboot":
      Qdwin.reboot();
      break;
    case "logout":
      Qdwin.logout();
      break;
    case "shutdown":
      Qdwin.shutdown();
      break;
    }
  }
}
