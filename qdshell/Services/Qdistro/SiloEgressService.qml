pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "SiloEgress.js" as SiloEgress

Singleton {
  id: root

  readonly property string sessionBus: "org.qdistro.SessionManager1"
  readonly property string sessionPath: "/org/qdistro/SessionManager1"
  readonly property string sessionIface: "org.qdistro.SessionManager1"

  property var rows: []
  property bool reachable: false
  property bool refreshInFlight: false
  property var activeRows: []
  property bool active: false
  property int activeCount: 0
  property string label: ""
  property string detail: ""

  Component.onCompleted: {
    Logger.i("SiloEgressService", "service started");
    refresh();
    _siloMonitor.running = true;
  }

  function refresh() {
    _scan.running = false;
    _scan.command = ["sh", "-c",
      "busctl --system --json=short call " +
      sessionBus + " " + sessionPath + " " + sessionIface + " " +
      "ListSilos 2>/dev/null || echo ''"];
    refreshInFlight = true;
    _scan.running = true;
  }

  function _setRows(nextRows) {
    rows = nextRows || [];
    activeRows = SiloEgress.activeEgressRows(rows);
    const s = SiloEgress.summary(rows, 2);
    active = s.active;
    activeCount = s.count;
    label = s.label;
    detail = s.detail;
  }

  Process {
    id: _scan
    stdout: StdioCollector {
      onStreamFinished: {
        const raw = (this.text || "").trim();
        root.refreshInFlight = false;
        if (!raw) {
          root.reachable = false;
          root._setRows([]);
          return;
        }
        const nextRows = SiloEgress.parseBusctlListSilos(raw);
        root.reachable = true;
        root._setRows(nextRows);
      }
    }
    stderr: StdioCollector {}
    onExited: function() {
      root.refreshInFlight = false;
    }
  }

  Process {
    id: _siloMonitor
    running: false
    command: ["sh", "-c",
      "command -v gdbus >/dev/null 2>&1 || exit 1; " +
      "exec gdbus monitor --system --dest " + sessionBus]
    stdout: SplitParser {
      onRead: data => {
        if (String(data || "").indexOf(sessionIface + ".SiloChanged") !== -1)
          root.refresh();
      }
    }
    stderr: StdioCollector {}
    onExited: function() {
      _restartMonitor.start();
    }
  }

  Timer {
    id: _poll
    interval: 30000
    repeat: true
    running: true
    onTriggered: root.refresh()
  }

  Timer {
    id: _restartMonitor
    interval: 5000
    repeat: false
    onTriggered: _siloMonitor.running = true
  }
}
