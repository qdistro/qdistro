pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin

// PodApps — per-container app registry for the qdshell launcher.
//
// Reads the JSON cache that tier2/podapps-scan.sh writes under
// /var/lib/qdistro/podapps/<container>/apps.json, exposes a flat
// list-model the Launcher and Taskbar merge alongside the host's
// XDG DesktopEntries, and tracks per-launch placeholders for the
// cold-start UX.
//
// See:
//   - qdistro/doc/containers.md      (the design)
//   - qdistro/doc/window-hierarchy.md (cold-start contract)
//   - qdistro/doc/ui.md               (silo-badge convention)
Singleton {
    id: root

    Component.onCompleted: Logger.i("PodApps", "service started")

    // ---- Configuration ----------------------------------------------------
    readonly property string cacheRoot: "/var/lib/qdistro/podapps"
    // The session manager owns pod-app launches (see launch() below).
    readonly property string sessionBus: "org.qdistro.SessionManager1"
    readonly property string sessionPath: "/org/qdistro/SessionManager1"
    readonly property string sessionIface: "org.qdistro.SessionManager1"

    // ---- Public model -----------------------------------------------------
    // Each row: { appId, container, workload, name, iconName, comment,
    //             execArgv (string), silo, containerState }
    // containerState ∈ { "running", "off", "starting", "unknown" }
    property ListModel apps: ListModel {}
    // Emitted after an entire cache scan has replaced `apps`. Consumers use
    // this completion boundary instead of reacting to transient clear/append
    // count changes while a scan is still being assembled.
    signal refreshed()

    // ---- Cold-start placeholders -----------------------------------------
    // Each entry: { launchToken, appId, name, iconName, silo, since }
    // Inserted on spawn, removed on matching toplevel_security_context
    // event or after the placeholderTimeoutMs cap.
    property ListModel placeholders: ListModel {}
    property int placeholderTimeoutMs: 15000

    signal placeholderAdded(string launchToken, string appId,
                            string name, string iconName, string silo)
    signal placeholderResolved(string launchToken, int handle)
    signal placeholderTimedOut(string launchToken, string appId)

    // ---- Container state cache ------------------------------------------
    // containerName → "running" | "off"
    property var _containerStates: ({})
    signal containerStateChanged(string container, string state)

    // ---- Implementation -------------------------------------------------

    // Re-scan the cache directory. Cheap — reads ~one JSON file per
    // container. Triggered periodically + on Container state change.
    function refresh() {
        // refresh() is called by service startup, the periodic timer, provider
        // initialization and launcher open. If a scan is already running,
        // assigning true again is a no-op; the old implementation had already
        // cleared `apps`, so a late overlapping call could leave the launcher
        // empty forever. Cancel/restart deterministically and replace the model
        // only after the new collector has completed.
        _scanProcess.running = false;
        _scanProcess.command = ["sh", "-c",
            "shopt -s nullglob; " +
            "for d in " + cacheRoot + "/*/; do " +
            "  name=$(basename \"$d\"); " +
            "  if [ -f \"$d/apps.json\" ]; then " +
            "    printf '=== %s\\n' \"$name\"; " +
            "    cat \"$d/apps.json\"; " +
            "  fi; " +
            "done"];
        _scanProcess.running = true;
    }

    Process {
        id: _scanProcess
        stdout: StdioCollector {
            onStreamFinished: {
                const raw = this.text || "";
                const next = [];
                // Sections: "=== <container>\n[<json>]\n"
                const sections = raw.split(/^=== /m).filter(s => s.length > 0);
                for (const sec of sections) {
                    const nl = sec.indexOf("\n");
                    if (nl < 0) continue;
                    const container = sec.slice(0, nl).trim();
                    const jsonBody  = sec.slice(nl + 1).trim();
                    let entries = [];
                    try { entries = JSON.parse(jsonBody); }
                    catch (e) {
                        Logger.w("PodApps", "parse failed for " + container + ": " + e);
                        continue;
                    }
                    const state = root._containerStates[container] || "off";
                    for (const e of entries) {
                        next.push({
                            appId:          e.appId          || "",
                            container:      e.container      || container,
                            workload:       e.workload       || "",
                            name:           e.name           || "",
                            iconName:       e.iconName       || "",
                            comment:        e.comment        || "",
                            execArgv:       JSON.stringify(e.execArgv || []),
                            silo:           e.silo           || ("tier2/" + container),
                            containerState: state,
                        });
                    }
                }
                root.apps.clear();
                for (const row of next)
                    root.apps.append(row);
                Logger.i("PodApps", "cache refresh loaded " + next.length + " apps");
                root.refreshed();
            }
        }
    }

    // Poll podman for running containers, update containerStates,
    // re-stamp the apps model's containerState column.
    function refreshContainerStates() {
        _containerListProcess.running = false;
        _containerListProcess.command = ["sh", "-c",
            "command -v podman >/dev/null && " +
            "podman ps --format '{{.Names}}' 2>/dev/null || true"];
        _containerListProcess.running = true;
    }

    Process {
        id: _containerListProcess
        stdout: StdioCollector {
            onStreamFinished: {
                const running = new Set(
                    (this.text || "").split("\n")
                        .map(s => s.trim()).filter(s => s.length > 0));
                const next = {};
                for (const name of running) next[name] = "running";
                // Find off transitions.
                for (const name in root._containerStates) {
                    if (!(name in next)) next[name] = "off";
                }
                const changed = [];
                for (const name in next) {
                    if (root._containerStates[name] !== next[name])
                        changed.push(name);
                }
                const prevStates = root._containerStates;
                root._containerStates = next;
                // Propagate to apps model.
                for (let i = 0; i < root.apps.count; i++) {
                    const row = root.apps.get(i);
                    const s = next[row.container] || "off";
                    if (row.containerState !== s)
                        root.apps.setProperty(i, "containerState", s);
                }
                for (const c of changed) {
                    root.containerStateChanged(c, next[c]);
                    // Clear the scanned flag when a container goes
                    // away — a podman rm + podman run with the same
                    // name should re-scan in case the new image has
                    // different .desktop entries.
                    if (next[c] !== "running" && root._scannedThisSession[c])
                        delete root._scannedThisSession[c];
                    // Auto-bootstrap the apps cache for any container
                    // we just observed transitioning into "running" —
                    // covers manual `podman start` and the launcher
                    // path uniformly. Idempotent: scan rewrites
                    // apps.json atomically; once-per-session guard
                    // (_scannedThisSession) avoids re-scanning a
                    // container that flaps off/on inside one shell run.
                    if (next[c] === "running"
                        && (prevStates[c] || "off") !== "running"
                        && !root._scannedThisSession[c]) {
                        root._scannedThisSession[c] = true;
                        Logger.i("PodApps", "auto-scan: " + c
                                            + " transitioned to running");
                        root._scanContainer(c);
                    }
                }
            }
        }
    }

    // Per-session "we've already kicked a scan for this container" set;
    // reset when the user explicitly calls refresh().
    property var _scannedThisSession: ({})

    function _scanContainer(container) {
        // Container is freshly running but podman exec isn't always
        // ready immediately (entrypoint races, network namespace
        // setup). 2s is enough headroom for the weston-terminal image
        // in practice; if it isn't, scan just emits "0 entries" and
        // the cache stays whatever it was before.
        const proc = scanProcessComp.createObject(root, {
            "command": ["sh", "-c",
                        "sleep 2 && qdistro-podapps-scan " + container],
            "_container": container,
        });
        proc.running = true;
    }

    Component {
        id: scanProcessComp
        Process {
            id: scanProc
            property string _container
            stdout: SplitParser {
                onRead: data => Logger.d("PodApps", "scan(" + scanProc._container + "): " + data)
            }
            stderr: SplitParser {
                onRead: data => Logger.d("PodApps", "scan(" + scanProc._container + ") err: " + data)
            }
            onExited: code => {
                if (code === 0)
                    root.refresh();
                else
                    Logger.w("PodApps", "scan failed for " + scanProc._container + " (exit " + code + ")");
                scanProc.destroy();
            }
        }
    }

    Timer {
        id: stateTimer
        interval: 3000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: root.refreshContainerStates()
    }

    Timer {
        id: cacheRefreshTimer
        interval: 30000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    // ---- Launch -----------------------------------------------------------
    // Called from Launcher / Taskbar click handlers.
    //
    // We do NOT fork spawn-tier2 ourselves. qdshell is the unprivileged admin
    // session, so a spawn forked from here has no root launcher parent — which
    // is exactly what qdistro-secctx-exec needs in order to stamp the app's
    // identity on the Wayland wire. Forked from here, spawn-tier2 took its
    // un-tagged branch (window arrives with no wp_security_context_v1 at all,
    // so nothing can tell the compositor which silo/app it is) and on a
    // hardened profile it refused the launch outright — clicking a pod app did
    // nothing. Instead we ask the session manager (root), which starts
    // qdistro-podapp@<token>.service and hands spawn-tier2 the root-launcher
    // topology. See qdistro/session_manager/qdistro-podapp@.service.
    //
    // The D-Bus reply carries the launch token — the secctx instance-id the
    // window will arrive with — so placeholder resolution needs no access to
    // spawn-tier2's stdout (under a unit that is the journal, not our pipe).
    function launch(row) {
        if (!row || !row.appId) return;
        let argv = [];
        try { argv = JSON.parse(row.execArgv); } catch (e) { argv = []; }
        if (argv.length === 0) {
            Logger.w("PodApps", "launch: empty execArgv for " + row.appId);
            return;
        }

        const proc = launchProcessComp.createObject(root, {
            "command": ["gdbus", "call", "--system",
                        "--dest", root.sessionBus,
                        "--object-path", root.sessionPath,
                        "--method", root.sessionIface + ".LaunchPodApp",
                        row.container,
                        row.workload || "weston-terminal",
                        JSON.stringify(argv)],
            "_appId":    row.appId,
            "_name":     row.name,
            "_iconName": row.iconName || "",
            "_silo":     row.silo,
        });
        proc.running = true;
    }

    // Internal helper Process component. One per launch.
    //
    // Unlike the old direct spawn (which stayed in the foreground for the
    // container's lifetime, so its stdout never closed and the token had to be
    // read line-by-line), this is a short D-Bus call that exits as soon as the
    // unit has been started — so we can collect stdout whole. gdbus prints a
    // GVariant tuple, `('<32 hex>',)`.
    Component {
        id: launchProcessComp
        Process {
            id: launchProc
            property string _appId
            property string _name
            property string _iconName
            property string _silo
            stdout: StdioCollector { id: launchOut }
            stderr: StdioCollector { id: launchErr }
            onExited: (code, status) => {
                const err = String(launchErr.text || "").trim();
                if (code !== 0) {
                    Logger.w("PodApps", "launch: LaunchPodApp failed for "
                                        + launchProc._appId + " (exit " + code
                                        + ")" + (err ? ": " + err : ""));
                    launchProc.destroy();
                    return;
                }
                if (err)
                    Logger.w("PodApps", "launch stderr (" + launchProc._appId
                                        + "): " + err);
                // Anchor on the tuple so a stray log line cannot be mistaken
                // for the reply; the token shape is the daemon's contract.
                const m = String(launchOut.text || "").match(/\('([0-9a-f]{32})',\)/);
                if (!m) {
                    Logger.w("PodApps", "launch: no launch token in LaunchPodApp "
                                        + "reply for " + launchProc._appId);
                    launchProc.destroy();
                    return;
                }
                root._registerPlaceholder(m[1], launchProc._appId,
                                          launchProc._name,
                                          launchProc._iconName,
                                          launchProc._silo);
                launchProc.destroy();
            }
        }
    }

    function _registerPlaceholder(launchToken, appId, name, iconName, silo) {
        placeholders.append({
            launchToken: launchToken,
            appId:       appId,
            name:        name,
            iconName:    iconName,
            silo:        silo,
            since:       Date.now(),
        });
        placeholderAdded(launchToken, appId, name, iconName, silo);
    }

    Timer {
        id: placeholderGcTimer
        interval: 1000
        repeat: true
        running: true
        onTriggered: {
            const now = Date.now();
            for (let i = root.placeholders.count - 1; i >= 0; i--) {
                const ph = root.placeholders.get(i);
                if (now - ph.since > root.placeholderTimeoutMs) {
                    root.placeholderTimedOut(ph.launchToken, ph.appId);
                    root.placeholders.remove(i);
                }
            }
        }
    }

    // Wire the secctx-resolved signal from the Qdwin singleton. When
    // a toplevel arrives with an instanceId matching one of our
    // pending placeholders, drop the placeholder.
    Connections {
        target: Qdwin
        function onWindowSecctxResolved(handle, sandboxEngine, secctxAppId, instanceId) {
            if (!instanceId) return;
            for (let i = 0; i < root.placeholders.count; i++) {
                if (root.placeholders.get(i).launchToken === instanceId) {
                    root.placeholders.remove(i);
                    root.placeholderResolved(instanceId, handle);
                    return;
                }
            }
        }
    }
}
