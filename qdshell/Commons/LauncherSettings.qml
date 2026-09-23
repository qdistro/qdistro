pragma Singleton

import QtQuick
import qs.Commons

QtObject {
  id: root

  readonly property var data: Settings.data.appLauncher || ({})

  readonly property bool enableClipboardHistory: data.enableClipboardHistory === true
  readonly property bool autoPasteClipboard: data.autoPasteClipboard === true
  readonly property bool enableClipPreview: data.enableClipPreview !== false
  readonly property bool clipboardWrapText: data.clipboardWrapText !== false
  readonly property string position: data.position || "center"
  readonly property var pinnedApps: data.pinnedApps || []
  readonly property bool useApp2Unit: data.useApp2Unit === true
  readonly property bool sortByMostUsed: data.sortByMostUsed !== false
  readonly property string terminalCommand: data.terminalCommand || "alacritty -e"
  readonly property bool customLaunchPrefixEnabled: data.customLaunchPrefixEnabled === true
  readonly property string customLaunchPrefix: data.customLaunchPrefix || ""
  readonly property string viewMode: data.viewMode || "list"
  readonly property bool showCategories: data.showCategories !== false
  readonly property string iconMode: data.iconMode || "tabler"
  readonly property bool showIconBackground: data.showIconBackground === true
  readonly property bool enableSettingsSearch: data.enableSettingsSearch !== false
  readonly property bool enableWindowsSearch: data.enableWindowsSearch !== false
  readonly property bool enableSessionSearch: data.enableSessionSearch !== false
  readonly property bool ignoreMouseInput: data.ignoreMouseInput === true
  readonly property string screenshotAnnotationTool: data.screenshotAnnotationTool || ""
  readonly property bool overviewLayer: data.overviewLayer === true
  readonly property string density: data.density || "default"

  function setPinnedApps(apps) {
    if (Settings.data.appLauncher) {
      Settings.data.appLauncher.pinnedApps = apps;
    }
  }

  function toggleViewMode() {
    if (Settings.data.appLauncher) {
      Settings.data.appLauncher.viewMode = viewMode === "grid" ? "list" : "grid";
    }
  }
}
