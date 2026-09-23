pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "NameOwnerParse.js" as NameOwnerParse

// App1Apps — discover ``org.qdistro.App1`` receivers via the broker.
//
// Where PodApps surfaces container-scanned tier-2 apps, App1Apps
// surfaces *running* user-uid apps that have registered themselves on
// the session bus as ``org.qdistro.<Name>.uid<NNNN>`` and answered
// the App1 contract (GetName / GetSilo / CanReceive / ReceivePayload).
// The data source is the admin broker's ``ListReceivers`` method,
// which side-channels into every uid's UserRelay to enumerate names.
//
// Each row exposed in :prop:`apps`:
//   { uid, service, name, silo }
//
// Refresh strategy: event-driven, on two fronts.
//   1. Broker up/down: a long-running ``gdbus monitor`` subscribes to
//      ``org.freedesktop.DBus.NameOwnerChanged`` and reacts when the
//      broker (``org.qdistro.AdminBroker1``) or the session manager
//      (``org.qdistro.SessionManager1``) gains or loses an owner —
//      owner-acquired triggers a discovery refresh, owner-lost drops us
//      to the graceful empty state.
//   2. Inventory mutation inside a living broker: a second ``gdbus
//      monitor --dest org.qdistro.AdminBroker1`` watches the broker's
//      payload-free ``ReceiversChanged`` signal — fired when a receiver
//      registers/unregisters inside a running silo (relayed up from each
//      uid's UserRelay) — and re-runs ListReceivers on it. This is what
//      makes a receiver appearing in an already-running session show up
//      in the launcher without waiting for the safety-net poll.
// We also do an immediate probe at startup (and on Launcher open) and
// keep a long safety-net poll that both reconciles missed transitions
// and restarts the monitors if they die.
// Silo badge convention follows ``qdistro/doc/ui.md``; the launcher
// provider renders the silo as a chip in front of the comment so
// users can tell two instances of the same app in different silos
// apart.
Singleton {
    id: root

    Component.onCompleted: {
        Logger.i("App1Apps", "service started");
        // Initial one-shot probe so the launcher has data before the
        // first NameOwnerChanged signal ever arrives.
        root.refreshSilos();
        root.refresh();
        _ownerMonitor.running = true;
        _receiversMonitor.running = true;
    }

    // The well-known bus names whose presence we track. AdminBroker1
    // owns ListReceivers (the app inventory); SessionManager1 owns
    // ListSilos (the silo chips). Both live on the system bus.
    readonly property string _brokerName: "org.qdistro.AdminBroker1"
    readonly property string _sessionName: "org.qdistro.SessionManager1"

    // Each row: { uid (int), service (str), name (str), silo (str) }
    property ListModel apps: ListModel {}

    // ``true`` when the most recent busctl probe succeeded; the
    // Launcher provider hides itself when the broker isn't on the
    // system bus rather than showing an empty section.
    property bool brokerReachable: false

    signal refreshed()

    function refresh() {
        _scan.running = false;
        _scan.command = ["sh", "-c",
            "busctl --system --json=short call " +
            "org.qdistro.AdminBroker1 " +
            "/org/qdistro/AdminBroker1 " +
            "org.qdistro.AdminBroker1 " +
            "ListReceivers 2>/dev/null || echo ''"];
        _scan.running = true;
    }

    Process {
        id: _scan
        stdout: StdioCollector {
            onStreamFinished: {
                const raw = (this.text || "").trim();
                if (!raw) {
                    root.brokerReachable = false;
                    root.apps.clear();
                    root.refreshed();
                    return;
                }
                root.brokerReachable = true;
                let parsed = null;
                try { parsed = JSON.parse(raw); }
                catch (e) {
                    Logger.w("App1Apps", "parse failed: " + e + " raw=" + raw);
                    return;
                }
                // busctl --json=short shape:
                //   {"type":"a(iss)","data":[[[uid,svc,friendly],...]]}
                let rows = [];
                if (parsed && parsed.data && parsed.data.length > 0)
                    rows = parsed.data[0];
                root.apps.clear();
                for (const r of rows) {
                    const uid = parseInt(r[0]);
                    const svc = String(r[1]);
                    const friendly = String(r[2]);
                    // GetSilo per-row is a second round trip; for the
                    // launcher we fall back to the silo column from
                    // the friendly name's uid suffix mapping. The
                    // broker side already passes through whatever
                    // UserRelay knows, so the silo is best-effort.
                    const silo = root._siloFor(uid);
                    root.apps.append({
                        uid:     uid,
                        service: svc,
                        name:    friendly,
                        silo:    silo,
                    });
                }
                root.refreshed();
            }
        }
    }

    // Map uid → silo label. Pulled from SessionManager1.ListSilos via
    // a separate refresh so we don't pay one busctl per row on every
    // launcher open. Empty string when unknown — the launcher renders
    // no chip rather than a "[]" placeholder.
    property var _uidSilo: ({})

    function _siloFor(uid) {
        const s = root._uidSilo[String(uid)];
        return s || "";
    }

    function refreshSilos() {
        _siloScan.running = false;
        _siloScan.command = ["sh", "-c",
            "busctl --system --json=short call " +
            "org.qdistro.SessionManager1 " +
            "/org/qdistro/SessionManager1 " +
            "org.qdistro.SessionManager1 " +
            "ListSilos 2>/dev/null || echo ''"];
        _siloScan.running = true;
    }

    Process {
        id: _siloScan
        stdout: StdioCollector {
            onStreamFinished: {
                const raw = (this.text || "").trim();
                if (!raw) return;
                let parsed = null;
                try { parsed = JSON.parse(raw); }
                catch (e) { return; }
                // SessionManager1.ListSilos returns a single string
                // (JSON-encoded). Drill in.
                let rows = [];
                if (parsed && parsed.data && parsed.data.length > 0) {
                    try { rows = JSON.parse(String(parsed.data[0])); }
                    catch (e) { rows = []; }
                }
                const next = {};
                for (const row of rows) {
                    if (row && typeof row.uid !== "undefined")
                        next[String(row.uid)] = row.name || "";
                }
                root._uidSilo = next;
                // Re-stamp existing apps so the next launcher open
                // sees up-to-date silo chips even without a full
                // refresh().
                for (let i = 0; i < root.apps.count; i++) {
                    const r = root.apps.get(i);
                    const s = root._siloFor(r.uid);
                    if (r.silo !== s)
                        root.apps.setProperty(i, "silo", s);
                }
            }
        }
    }

    // Event-driven discovery. ``gdbus monitor`` on org.freedesktop.DBus
    // emits one single line per signal, e.g.:
    //   /org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged \
    //       ('org.qdistro.AdminBroker1', '', ':1.42')
    // (old_owner, new_owner) — new_owner non-empty == acquired,
    // new_owner empty == lost. We filter to the two names we care
    // about and react instead of blind polling. Matches the streaming
    // SplitParser idiom used by PowerService (libinput debug-events)
    // and PodApps (spawn monitor).
    Process {
        id: _ownerMonitor
        running: false
        command: ["sh", "-c",
            "command -v gdbus >/dev/null 2>&1 || exit 1; " +
            "exec gdbus monitor --system --dest org.freedesktop.DBus"]
        stdout: SplitParser {
            onRead: data => {
                // Parsing/classification lives in the pure NameOwnerParse.js
                // module (unit-tested under Node). It maps a raw monitor line
                // to one of: refresh-apps / empty-apps / refresh-silos / ignore.
                const action = NameOwnerParse.classifyOwnerChange(String(data || ""), root._brokerName, root._sessionName);
                if (action === "refresh-apps") {
                    Logger.d("App1Apps", "broker appeared on bus; refreshing");
                    root.refresh();
                } else if (action === "empty-apps") {
                    Logger.d("App1Apps", "broker left bus; empty state");
                    root.brokerReachable = false;
                    root.apps.clear();
                    root.refreshed();
                } else if (action === "refresh-silos") {
                    Logger.d("App1Apps", "session manager appeared; refreshing silos");
                    root.refreshSilos();
                }
            }
        }
        stderr: StdioCollector {}
        onExited: function (exitCode) {
            // gdbus missing or the monitor died. The safety-net timer
            // below restarts it on its next tick (and reconciles state
            // via a direct probe in the meantime).
            Logger.w("App1Apps", "owner monitor exited (" + exitCode
                                 + "); safety-net poll will restart it");
        }
    }

    // Inventory-mutation monitor. ``gdbus monitor --dest
    // org.qdistro.AdminBroker1`` streams one line per signal the broker
    // emits; we watch for its payload-free ReceiversChanged, e.g.:
    //   /org/qdistro/AdminBroker1: org.qdistro.AdminBroker1.ReceiversChanged ()
    // and re-run ListReceivers. This catches receivers that register or
    // unregister inside an already-running silo (the broker relays each
    // uid's UserRelay.LocalReceiversChanged up to this single system-bus
    // signal) — transitions the broker up/down monitor above never sees.
    // Same streaming-Process + parser idiom; classification lives in the
    // unit-tested NameOwnerParse.isReceiversChanged.
    Process {
        id: _receiversMonitor
        running: false
        command: ["sh", "-c",
            "command -v gdbus >/dev/null 2>&1 || exit 1; " +
            "exec gdbus monitor --system --dest org.qdistro.AdminBroker1"]
        stdout: SplitParser {
            onRead: data => {
                if (NameOwnerParse.isReceiversChanged(String(data || ""))) {
                    Logger.d("App1Apps", "broker ReceiversChanged; refreshing");
                    root.refresh();
                }
            }
        }
        stderr: StdioCollector {}
        onExited: function (exitCode) {
            // Mirrors _ownerMonitor: the safety-net timer restarts it.
            Logger.w("App1Apps", "receivers monitor exited (" + exitCode
                                 + "); safety-net poll will restart it");
        }
    }

    // Safety net: a slow reconcile that (a) restarts either monitor if
    // it died and (b) catches any NameOwnerChanged / ReceiversChanged
    // transition missed while a monitor was down. Steady-state discovery
    // is event-driven via _ownerMonitor (broker up/down) and
    // _receiversMonitor (inventory mutation inside a living broker); this
    // is deliberately infrequent, a backstop rather than the primary
    // path.
    Timer {
        id: _safetyNet
        interval: 60000
        repeat: true
        running: true
        triggeredOnStart: false
        onTriggered: {
            if (!_ownerMonitor.running)
                _ownerMonitor.running = true;
            if (!_receiversMonitor.running)
                _receiversMonitor.running = true;
            root.refreshSilos();
            root.refresh();
        }
    }

    // Launch helper. App1Apps entries are user-uid binaries that
    // already exist in the silo's environment — no spawn-tier2
    // gymnastics needed; we just exec the friendly name as the
    // canonical binary (mapping QFileMan → qfileman etc.) inside the
    // target silo via the session-manager's StartSilo + a per-app
    // unit. Cold-start: no LAUNCH_TOKEN handshake yet (these apps
    // don't carry a wp_security_context_v1 tag), so the launcher
    // shows the toplevel as it arrives.
    function launch(row) {
        if (!row || !row.service) return;
        const binary = root._binaryFor(row.name);
        if (!binary) {
            Logger.w("App1Apps", "no binary mapping for " + row.name);
            return;
        }
        Logger.d("App1Apps", "launching " + binary + " for uid " + row.uid
                              + " (silo=" + row.silo + ")");
        const proc = launchProcessComp.createObject(root, {
            "command": ["sh", "-c",
                "QDISTRO_SILO=" + (row.silo || "") + " " +
                "exec " + binary + " >/dev/null 2>&1 &"],
        });
        proc.running = true;
    }

    Component {
        id: launchProcessComp
        Process {
            onExited: this.destroy()
        }
    }

    function _binaryFor(friendly) {
        // Friendly → binary mapping for the P03 first-party set; new
        // App1 entries can register a desktop file with a
        // ``X-Qdistro-Binary=...`` field once that convention lands.
        const map = {
            "QTerminator": "qterminator",
            "QNotebook":   "qnotebook",
            "QFileMan":    "qfileman",
        };
        return map[friendly] || friendly.toLowerCase();
    }
}
