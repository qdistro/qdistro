import QtQuick
import Quickshell
import qs.Commons
import qs.Modules.Panels.Settings
import qs.Services.Qdistro
import qs.Services.UI

// VMAppsProvider — surface tier-5 (per-app VM) apps in the launcher.
//
// Tier-5 apps run inside an ephemeral guest VM bridged to the outer
// compositor via waypipe-over-AF_VSOCK (per qdistro/doc/isolation-
// tiers.md). On activate, calls VMApps.launch() which forks
// qdistro-tier5-spawn --vm <auto-name> -- <argv> and registers a
// cold-start placeholder; qdshell's VMApps service resolves the
// placeholder when toplevel_security_context arrives with a matching
// instance_id (LAUNCH_TOKEN).
//
// **App catalogue** comes from VMApps.catalogue, which is loaded from
// /usr/share/qdistro/vmapps/base.json — the manifest the guest-base
// scan publishes at build time and the qdistro-tier5-vmapps-scan
// service republishes on base changes. When the manifest is missing,
// malformed, or stale the catalogue is exactly the foot fallback.
// VMApps.refreshed() fires on every complete swap; we refresh an open
// launcher's results so a republished catalogue shows up live.
//
// Visual note: the launcher's standard entry delegate renders name +
// icon + description. Until doc/ui.md's silo-badge convention lands
// as a delegate-side overlay, we prefix the description with "[VM]"
// so users can tell tier-5 entries apart from native apps.
Item {
  id: root

  property var launcher: null
  property string name: I18n.tr("launcher.providers.vms")
  property bool handleSearch: true
  property var entries: []
  property string supportedLayouts: "both"
  property bool isDefaultProvider: false
  property bool ignoreDensity: false
  property bool showsCategories: false

  // Tier-5 app catalogue — shared with the Settings "Sandboxed VM apps" tab via
  // the VMApps singleton, so the launcher and per-app lifecycle config agree on
  // the appId (the policy key). Each entry carries an execArgv (JSON-stringified
  // array of strings) that runs inside the guest VM via waypipe-server.
  readonly property var apps: VMApps.catalogue

  // Republished manifest → refresh the open launcher so new/removed
  // tier-5 entries appear without reopening it.
  Connections {
    target: VMApps
    function onRefreshed() {
      if (root.launcher && root.launcher.isOpen)
        root.launcher.updateResults();
    }
  }

  function init() {}
  function onOpened() {}

  function getResults(query) {
    let filtered = root.apps;
    if (query && query.trim() !== "") {
      const q = query.toLowerCase();
      filtered = root.apps.filter(r =>
        (r.name || "").toLowerCase().includes(q)
        || (r.comment || "").toLowerCase().includes(q)
        || (r.appId || "").toLowerCase().includes(q)
      );
    }
    return filtered.slice(0, 20).map(row => ({
      "appId":       row.appId,
      "name":        row.name,
      "description": "[VM] " + (row.comment || ""),
      "icon":        row.iconName || "application-x-executable",
      "isImage":     false,
      "_score":      0,
      "provider":    root,
      "onActivate":  function () {
        if (launcher && launcher.closeImmediately)
          launcher.closeImmediately();
        Qt.callLater(() => {
          Logger.d("VMAppsProvider",
            "Launching " + row.appId + " in its tier-5 VM");
          VMApps.launch(row);
        });
      },
    }));
  }

  // Secondary action: open this app's row in Settings → "Sandboxed VM
  // apps" (the per-app lifecycle overrides keyed by the same catalogue
  // appId). Rides the standard openToEntry pipeline; the tab is addressed
  // by its stable SettingsPanel.Tab enum id and the row by its
  // highlightKey == appId, so neither display ordering nor translated
  // labels are part of the contract.
  function getItemActions(item) {
    if (!item || !item.appId)
      return [];
    return [
          {
            "icon": "settings",
            "tooltip": I18n.tr("tooltips.vm-app-settings"),
            "action": function () {
              const screen = root.launcher ? root.launcher.screen : null;
              if (root.launcher && root.launcher.close)
                root.launcher.close();
              Qt.callLater(() => {
                SettingsPanelService.openToEntry({
                                                   "tabId": SettingsPanel.Tab.VMApps,
                                                   "labelKey": item.appId,
                                                   "subTab": -1
                                                 }, screen);
              });
            }
          }
        ];
  }
}
