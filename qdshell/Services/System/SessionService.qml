pragma Singleton

import QtQuick
import Quickshell
import qs.Commons
import qs.Services.Qdwin
import "SessionModel.js" as SessionModel

/// SessionService — saved/current session model (xfce4-session parity).
///
/// What is FULLY working today:
///   • Enumerate currently-running apps from the live `Qdwin.windows`
///     ListModel (appId/title) → `currentApps`.
///   • Save a named snapshot (de-duped app list) into
///     `Settings.data.session.savedSessions` (qdshell JSON).
///   • List / delete saved snapshots.
///   • Toggle save-on-logout / restore-on-login INTENT (persisted).
///
/// What is PERSIST-ONLY / capability-gated:
///   • Actual placement/launch-policy APPLY (relaunching saved apps in
///     their prior window geometry/workspace) needs qdistro session
///     writers + a qdwin live-apply request that do NOT exist yet — see
///     CapabilityService.workspaceMutation / wmPolicy (both false). The
///     UI shows a "not yet supported by qdwin" banner. `restore()` below
///     still LAUNCHES the saved apps' commands (the part we *can* do),
///     but cannot restore geometry/workspace placement.
///
/// SECURITY: appId/title strings from Qdwin.windows are UNTRUSTED.
/// restore() builds a SAFE argv per app via SessionModel.buildLaunchArgv,
/// which ONLY launches valid freedesktop desktop-ids via
/// ["gtk-launch", id] (an array, never a `sh -c` string) and returns
/// null for anything else (paths / metacharacter strings are skipped, so
/// a hostile appId can neither inject a shell nor be exec'd as a path).
/// See SessionModel.js / tests/test_session_model.js.
Singleton {
    id: root

    // Live list of currently-running apps (from Qdwin.windows). Each
    // entry: { command, appId, title }. De-duped by command.
    property var currentApps: []

    // Saved sessions (mirror of Settings.data.session.savedSessions),
    // normalized. Each: { name, created, apps:[{command,appId,title}] }.
    property var savedSessions: []

    // True when geometry/workspace placement can be applied live. qdwin
    // exposes neither workspace mutation nor wm-policy yet, so saved
    // placement is persist-only. Launching the apps themselves still works.
    readonly property bool canApplyPlacement:
        CapabilityService.workspaceMutation && CapabilityService.wmPolicy

    function init() {
        refreshCurrent();
        reloadSaved();
        Logger.i("SessionService", "started; saved=" + savedSessions.length
                 + " canApplyPlacement=" + canApplyPlacement);
    }

    // Rebuild currentApps from the live window list.
    function refreshCurrent() {
        var wins = [];
        var model = Qdwin.windows;
        if (model) {
            for (var i = 0; i < model.count; i++) {
                var w = model.get(i);
                wins.push({ appId: w.appId || "", title: w.title || "" });
            }
        }
        currentApps = SessionModel.appsFromWindows(wins);
    }

    // Reload savedSessions from persisted settings (normalizing shape).
    function reloadSaved() {
        var raw = (Settings.data.session && Settings.data.session.savedSessions)
                ? Settings.data.session.savedSessions : [];
        savedSessions = SessionModel.deserializeSessions(raw);
    }

    function _persist(list) {
        savedSessions = list;
        Settings.data.session.savedSessions = list;
    }

    // Validate a proposed name against existing saved sessions.
    // `selfName` (optional) allows overwriting that exact session.
    function validateName(name, selfName) {
        return SessionModel.validateName(name, savedSessions, selfName);
    }

    // Save the CURRENT running apps under `name`. Replaces an existing
    // same-name session. Returns true on success, false on invalid name.
    function saveCurrent(name, selfName) {
        refreshCurrent();
        var wins = [];
        for (var i = 0; i < currentApps.length; i++)
            wins.push({ appId: currentApps[i].appId, title: currentApps[i].title });
        var snap = SessionModel.buildSnapshot(name, wins, savedSessions, Date.now(), selfName);
        if (!snap)
            return false;
        _persist(SessionModel.upsertSession(savedSessions, snap));
        Logger.i("SessionService", "saved session '" + snap.name
                 + "' apps=" + snap.apps.length);
        return true;
    }

    // Delete a saved session by name.
    function deleteSession(name) {
        _persist(SessionModel.removeSession(savedSessions, name));
        Logger.i("SessionService", "deleted session '" + name + "'");
    }

    // Restore (launch) the apps of a saved session. Geometry/workspace
    // placement is NOT applied (capability-gated). Each app is launched
    // with a SAFE argv array — never an interpolated shell string.
    function restore(name) {
        var session = SessionModel.findSession(savedSessions, name);
        if (!session) {
            Logger.w("SessionService", "restore: no session '" + name + "'");
            return;
        }
        var launched = 0;
        for (var i = 0; i < session.apps.length; i++) {
            var argv = SessionModel.buildLaunchArgv(session.apps[i]);
            if (!argv || !SessionModel.isSafeArgv(argv))
                continue;
            Quickshell.execDetached(argv);
            launched++;
        }
        Logger.i("SessionService", "restored '" + session.name + "' launched="
                 + launched + (canApplyPlacement ? "" : " (placement persist-only)"));
    }

    // Keep currentApps fresh as windows come and go.
    Connections {
        target: Qdwin
        function onWindowListChanged() {
            root.refreshCurrent();
        }
    }

    Connections {
        target: Settings
        function onSettingsLoaded() {
            root.reloadSaved();
        }
    }

    Component.onCompleted: {
        Qt.callLater(function () {
            if (Settings.isLoaded)
                root.reloadSaved();
        });
    }
}
