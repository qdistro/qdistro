import QtQuick
import Quickshell
import qs.Commons
import qs.Services.Qdistro

// App1AppsProvider — surface running org.qdistro.App1 receivers in
// the launcher.
//
// Reads from Services.Qdistro.App1Apps. Entries are running user-uid
// apps (qterminator, qnotebook, qfileman + any third-party app that
// registers the App1 contract) so the launcher doubles as a "what's
// already alive in which silo" map. Silo badge follows the doc/ui.md
// convention — rendered as a "[silo]" chip prefix on the description
// line until the dedicated badge overlay lands.
Item {
  id: root

  property var launcher: null
  property string name: I18n.tr("launcher.providers.podapps")
  property bool handleSearch: true
  property var entries: []
  property string supportedLayouts: "both"
  property bool isDefaultProvider: false
  property bool ignoreDensity: false
  property bool showsCategories: false

  function init() {
    App1Apps.refresh();
    App1Apps.refreshSilos();
  }

  function onOpened() {
    App1Apps.refresh();
    App1Apps.refreshSilos();
  }

  function getResults(query) {
    const all = [];
    for (let i = 0; i < App1Apps.apps.count; i++) {
      all.push(App1Apps.apps.get(i));
    }
    let filtered = all;
    if (query && query.trim() !== "") {
      const q = query.toLowerCase();
      filtered = all.filter(r =>
        (r.name || "").toLowerCase().includes(q)
        || (r.service || "").toLowerCase().includes(q)
        || (r.silo || "").toLowerCase().includes(q)
      );
    }
    return filtered.slice(0, 20).map(row => ({
      "appId":       row.service,
      "name":        row.name,
      "description": (row.silo ? "[" + row.silo + "] " : "")
                      + "uid " + row.uid + " · " + row.service,
      "icon":        "application-x-executable",
      "isImage":     false,
      "_score":      0,
      "provider":    root,
      "onActivate":  function () {
        if (launcher && launcher.closeImmediately)
          launcher.closeImmediately();
        Qt.callLater(() => {
          Logger.d("App1AppsProvider",
            "Launching " + row.service + " for uid " + row.uid);
          App1Apps.launch(row);
        });
      },
    }));
  }
}
