import QtQuick
import Quickshell
import qs.Commons
import qs.Services.Qdistro

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
// **App catalogue** is hardcoded today. Unlike tier-2's PodAppsProvider
// which scans .desktop files inside each running container via
// /var/lib/qdistro/podapps/<container>/apps.json, tier-5 doesn't have
// a per-spawn scan helper yet — the base qcow2 is built once and
// ships a fixed set of apps (currently Firefox; weston-terminal as a
// fallback test app). Future: auto-scan
// /usr/share/applications/ inside qdistro-tier5-base.qcow2 at image-
// build time and ship the result as
// /usr/share/qdistro/vmapps/base.json. See
// doc/containers.md "Future work — Tier-5 VMApps service".
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
            "Launching " + row.appId + " in a fresh tier-5 VM");
          VMApps.launch(row);
        });
      },
    }));
  }
}
