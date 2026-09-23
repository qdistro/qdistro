pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin

// VMApps — tier-5 (per-app VM, waypipe-over-AF_VSOCK) toplevel filter
// + launcher for the qdshell launcher / taskbar / containers panel.
//
// Unlike PodApps (tier-2), tier-5 apps do NOT arrive via
// qdwin_nested_manager_v1. They're regular xdg_toplevels on the
// outer compositor — connections from the host-side `waypipe-client`
// half of the waypipe-over-vsock bridge — tagged with
// wp_security_context_v1 fields (engine + app_id + instance_id) that
// spawn-tier5.sh plants by wrapping waypipe-client with
// qdistro-secctx-exec. instance_id == the LAUNCH_TOKEN spawn-tier5.sh
// emits on stdout, which is how we correlate cold-start placeholders
// to the eventual real toplevel.
//
// What this service does:
//   1. **Filter:** watches Qdwin.windows and exposes the tier-5
//      subset (secctxAppId starts with `qdistro.tier5.`) as
//      `tier5Windows`. Each row carries a derived `silo = vm-<tag>`.
//   2. **Launch:** `launch(row)` shells out to `qdistro-tier5-spawn
//      --vm <auto-name> -- <execArgv>`, parses LAUNCH_TOKEN from
//      stdout, seeds a `placeholders` row keyed on it.
//   3. **Cold-start placeholder resolution:** listens to
//      `Qdwin.windowSecctxResolved`; when a tier-5 toplevel's
//      instance_id matches a known launchToken, drops the placeholder
//      and emits `placeholderResolved`.
//
// See:
//   - qdistro/doc/isolation-tiers.md "Tier 5 — per-app VM windowed"
//   - qdistro/doc/containers.md "Why tier-2 first" (UI vocabulary
//     parity with PodApps; transport differs)
//   - qdistro/doc/ui.md "silo-badges" (badge ring colour for tier-5)
//   - qdistro/tier5-vm/spawn-tier5.sh (LAUNCH_TOKEN emission +
//     qdistro-secctx-exec wrap)
Singleton {
    id: root

    Component.onCompleted: Logger.i("VMApps", "service started")

    // The reverse-DNS engine prefix that identifies a tier-5 app.
    // Matches TIER5_SECCTX_ENGINE default in spawn-tier5.sh.
    readonly property string tier5Prefix: "qdistro.tier5."

    // Spawn helper path. spawn-tier5.sh installs as this symlink by
    // scripts/install/install-tier5-for-vm.sh. Requires root (libvirt
    // domain define + virsh start + qemu launch), so we invoke via
    // pkexec — install-tier5-for-vm.sh ships a polkit policy that
    // lets the active admin session pkexec it without re-auth
    // (allow_active=yes per qdistro single-tenant convention).
    readonly property string spawnHelper: "qdistro-tier5-spawn"
    readonly property string spawnLauncher: "pkexec"

    // Shared tier-5 app catalogue. The launcher (VMAppsProvider) surfaces these
    // and the Settings "Sandboxed VM apps" tab lists them for per-app lifecycle
    // overrides — both read this single source so the policy key (appId) lines
    // up. Hardcoded today (the base qcow2 ships a fixed app set); see
    // VMAppsProvider for the future auto-scan note. Each entry: appId, name,
    // iconName, comment, execArgv (JSON-stringified array of strings).
    readonly property var catalogue: [
        { "appId": "tier5/firefox",          "name": "Firefox (VM)",       "iconName": "firefox",                 "comment": "Isolated Firefox in a per-app VM",                        "execArgv": JSON.stringify(["firefox"]) },
        { "appId": "tier5/weston-terminal",  "name": "Terminal (VM)",      "iconName": "utilities-terminal",      "comment": "Isolated weston-terminal in a per-app VM (test)",          "execArgv": JSON.stringify(["weston-terminal"]) },
        { "appId": "tier5/baobab",           "name": "Disk Usage (VM)",    "iconName": "org.gnome.baobab",        "comment": "GNOME disk usage analyzer (GTK4/libadwaita, CSD)",         "execArgv": JSON.stringify(["baobab"]) },
        { "appId": "tier5/gnome-text-editor","name": "Text Editor (VM)",   "iconName": "org.gnome.TextEditor",    "comment": "GNOME text editor (GTK4/libadwaita, CSD)",                 "execArgv": JSON.stringify(["gnome-text-editor"]) },
        { "appId": "tier5/nautilus",         "name": "Files (VM)",         "iconName": "org.gnome.Nautilus",      "comment": "GNOME file manager (GTK4/libadwaita, CSD)",                "execArgv": JSON.stringify(["nautilus"]) },
        { "appId": "tier5/gnome-calculator", "name": "Calculator (VM)",    "iconName": "org.gnome.Calculator",    "comment": "GNOME calculator (GTK4/libadwaita, CSD)",                  "execArgv": JSON.stringify(["gnome-calculator"]) },
        { "appId": "tier5/dolphin",          "name": "Dolphin (VM)",       "iconName": "system-file-manager",     "comment": "KDE file manager (Qt6/KDE Frameworks, SSD)",               "execArgv": JSON.stringify(["dolphin"]) },
        { "appId": "tier5/konsole",          "name": "Konsole (VM)",       "iconName": "utilities-terminal",      "comment": "KDE terminal (Qt6/KDE Frameworks, SSD)",                   "execArgv": JSON.stringify(["konsole"]) },
        { "appId": "tier5/kate",             "name": "Kate (VM)",          "iconName": "accessories-text-editor", "comment": "KDE text editor (Qt6/KDE Frameworks, SSD)",                "execArgv": JSON.stringify(["kate"]) },
        { "appId": "tier5/kcalc",            "name": "KCalc (VM)",         "iconName": "accessories-calculator",  "comment": "KDE calculator (Qt6, SSD)",                                "execArgv": JSON.stringify(["kcalc"]) },
    ]

    // ---- toplevel filter (existing v1 surface) ---------------------------
    // Each row mirrors Qdwin.windows + adds `silo` ("vm-<tag>").
    property ListModel tier5Windows: ListModel {}
    property var _siloByHandle: ({})

    signal tier5WindowAdded(int handle, string silo, string appId)
    signal tier5WindowRemoved(int handle, string silo)

    // ---- cold-start placeholders -----------------------------------------
    // Each entry: { launchToken, appId, name, iconName, silo, since }
    // Inserted on spawn, removed on matching toplevel_security_context
    // event (instanceId == launchToken) OR after placeholderTimeoutMs.
    // Tier-5 boot is slow (guest VM cold-start ~30-60s), so the timeout
    // is longer than PodApps's 15s default.
    property ListModel placeholders: ListModel {}
    property int placeholderTimeoutMs: 90000

    signal placeholderAdded(string launchToken, string appId,
                            string name, string iconName, string silo)
    signal placeholderResolved(string launchToken, int handle)
    signal placeholderTimedOut(string launchToken, string appId)

    // ---- helpers ---------------------------------------------------------
    function siloFromSecctx(secctxAppId) {
        if (!secctxAppId || !secctxAppId.startsWith(root.tier5Prefix))
            return "";
        const tag = secctxAppId.slice(root.tier5Prefix.length);
        if (!tag) return "";
        return "vm-" + tag;
    }

    function isTier5(secctxAppId) {
        return !!secctxAppId && secctxAppId.startsWith(root.tier5Prefix);
    }

    // Generate a fresh VM name for a launch. spawn-tier5.sh validates
    // [a-zA-Z0-9][a-zA-Z0-9_-]{0,62}, so use a short hex suffix.
    function _generateVmName(appId) {
        const stub = (appId || "vmapp").replace(/[^a-zA-Z0-9_-]/g, "-")
                                       .slice(0, 24);
        const suffix = Math.floor(Math.random() * 0xFFFFFF)
                           .toString(16).padStart(6, "0");
        return "t5-" + stub + "-" + suffix;
    }

    function rebuild() {
        const fresh = [];
        const seenHandles = new Set();
        const wm = Qdwin.windows;
        if (!wm) return;
        for (let i = 0; i < wm.count; i++) {
            const w = wm.get(i);
            if (!root.isTier5(w.secctxAppId)) continue;
            const silo = root.siloFromSecctx(w.secctxAppId);
            fresh.push({
                handle:       w.handle,
                ownerUid:     w.ownerUid,
                appId:        w.appId,
                title:        w.title,
                isXwayland:   w.isXwayland,
                workspaceId:  w.workspaceId,
                sandboxEngine: w.sandboxEngine,
                secctxAppId:  w.secctxAppId,
                instanceId:   w.instanceId,
                silo:         silo,
            });
            seenHandles.add(w.handle);
        }

        const prevHandles = new Set();
        for (let i = 0; i < root.tier5Windows.count; i++)
            prevHandles.add(root.tier5Windows.get(i).handle);

        root.tier5Windows.clear();
        const nextSiloByHandle = ({});
        for (const row of fresh) {
            root.tier5Windows.append(row);
            nextSiloByHandle[row.handle] = row.silo;
            if (!prevHandles.has(row.handle))
                root.tier5WindowAdded(row.handle, row.silo, row.appId);
        }
        for (const h of prevHandles) {
            if (!seenHandles.has(h))
                root.tier5WindowRemoved(h, root._siloByHandle[h] || "");
        }
        root._siloByHandle = nextSiloByHandle;
    }

    // ---- launch ----------------------------------------------------------
    // Called from Launcher / Taskbar click handlers. Forks
    // spawn-tier5.sh --vm <auto-name> -- <argv...>; the helper emits
    // LAUNCH_TOKEN=<hex> early on stdout. We register a placeholder
    // keyed on the token; it's resolved when the inner toplevel
    // arrives carrying instance_id == launchToken (set by spawn-
    // tier5.sh's qdistro-secctx-exec wrap).
    //
    // row shape: { appId, name, iconName, execArgv (string JSON-encoded
    // array of strings) }
    function launch(row) {
        if (!row || !row.appId) {
            Logger.w("VMApps", "launch: missing appId");
            return;
        }
        let argv = [];
        try { argv = JSON.parse(row.execArgv); } catch (e) { argv = []; }
        if (argv.length === 0) {
            Logger.w("VMApps", "launch: empty execArgv for " + row.appId);
            return;
        }
        const vmName = row.vmName || root._generateVmName(row.appId);
        // Build: pkexec qdistro-tier5-spawn --vm <vmName> --policy-key <appId>
        //        -- <argv...>
        // --policy-key carries the STABLE catalogue appId so the wrapper can
        // resolve this app's per-app lifecycle policy from
        // ~/.config/qdistro/tier5-lifecycle.conf (env can't ride through pkexec,
        // but argv does; the flag is parsed before `--` so it never reaches the
        // guest argv). pkexec passes stdin/stdout/stderr through so the
        // LAUNCH_TOKEN= line on stdout still reaches our SplitParser.
        const cmd = [root.spawnLauncher, root.spawnHelper,
                     "--vm", vmName, "--policy-key", row.appId,
                     "--"].concat(argv);

        const proc = launchProcessComp.createObject(root, {
            "command":   cmd,
            "_appId":    row.appId,
            "_name":     row.name,
            "_iconName": row.iconName || "",
            "_silo":     "vm-" + vmName,
        });
        proc.running = true;
    }

    // Internal helper Process component. One per launch.
    //
    // spawn-tier5.sh stays in the foreground for the VM's lifetime
    // (waypipe-client teardown == toplevel close), so stdout stays
    // open until the user closes the window. SplitParser delivers
    // the LAUNCH_TOKEN= line as soon as spawn-tier5.sh emits it
    // (which is right after CID + port allocation, well before
    // qga and the inner app start).
    Component {
        id: launchProcessComp
        Process {
            id: launchProc
            property string _appId
            property string _name
            property string _iconName
            property string _silo
            property bool   _tokenSeen: false
            stdout: SplitParser {
                onRead: data => {
                    const m = String(data).match(/^LAUNCH_TOKEN=([0-9a-fA-F]+)/);
                    if (m && !launchProc._tokenSeen) {
                        launchProc._tokenSeen = true;
                        root._registerPlaceholder(m[1], launchProc._appId,
                                                  launchProc._name,
                                                  launchProc._iconName,
                                                  launchProc._silo);
                    }
                }
            }
            stderr: SplitParser {
                onRead: data => {
                    if (data && String(data).length > 0)
                        Logger.w("VMApps", "spawn stderr (" + launchProc._appId
                                            + "): " + data);
                }
            }
            onExited: {
                if (!launchProc._tokenSeen)
                    Logger.w("VMApps", "launch: no LAUNCH_TOKEN before spawn exit for "
                                        + launchProc._appId);
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

    // Garbage-collect placeholders that never matched a toplevel
    // within placeholderTimeoutMs. Per the tier-5 cold-start budget
    // this is 90s — significantly longer than PodApps's 15s since
    // guest boot dominates.
    Timer {
        id: placeholderGcTimer
        interval: 5000
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

    // ---- wire-up ---------------------------------------------------------
    Connections {
        target: Qdwin
        function onWindowListChanged() { root.rebuild(); }
        function onWindowSecctxResolved(handle, sandboxEngine, secctxAppId, instanceId) {
            // Filter rebuild for any tier-5 toplevel arrival.
            if (root.isTier5(secctxAppId))
                root.rebuild();
            else if (root._siloByHandle[handle])
                root.rebuild();

            // Placeholder resolution: instance_id should match a
            // pending LAUNCH_TOKEN. spawn-tier5.sh defaults
            // TIER5_SECCTX_INSTANCE=LAUNCH_TOKEN, so instanceId
            // is the launchToken modulo the wp_security_context_v1
            // wire round-trip.
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
