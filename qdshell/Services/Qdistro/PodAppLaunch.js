// PodAppLaunch — pure launch-click policy for tier-2 pod apps, extracted from
// PodApps.qml so it can be unit-tested headless under Node
// (require("./PodAppLaunch.js")) while still being imported from QML
// (import "PodAppLaunch.js" as PodAppLaunch).
//
// A pod is ONE container: spawn-tier2 runs every launch as
// `podman run --name <container>`, and a templated pod's state path is
// flock'd by the running launch. A second click while the pod is up can never
// start a second copy, so it used to die at podman (exit 125) while the
// shell's placeholder sat there for 15 s. The click now does what an
// already-running app does on every desktop: bring its window forward.
//
// NO Process / Logger / Quickshell access: plain data in, a verdict out.

var TIER2_ENGINE = "qdistro.tier2";

// The session manager's typed refusal for a launch into a pod whose container
// already exists (org.qdistro.SessionManager1.PodAppRunning).
var RUNNING_ERROR = "org.qdistro.SessionManager1.PodAppRunning";

// The pod's live windows carry secctx app_id `<container>/<app>` under the
// tier-2 engine (spawn-tier2.sh SECCTX_APPID). Container names are podman
// names, which cannot contain "/", so the prefix is unambiguous.
function isPodWindow(win, container) {
    return !!win && !!container
        && win.sandboxEngine === TIER2_ENGINE
        && typeof win.secctxAppId === "string"
        && win.secctxAppId.indexOf(container + "/") === 0;
}

// windows:      [{ handle, sandboxEngine, secctxAppId }] in model order
// placeholders: [{ container }] launches still waiting for their window
// Returns one of:
//   { action: "focus", handle }  the pod is up: raise its newest window
//   { action: "pending" }        a launch into this pod is already starting
//   { action: "launch" }         nothing of this pod is visible: ask the daemon
function decideLaunch(container, windows, placeholders) {
    var handle = -1;
    for (var i = 0; i < (windows || []).length; i++) {
        if (isPodWindow(windows[i], container))
            handle = windows[i].handle;
    }
    if (handle >= 0)
        return { action: "focus", handle: handle };
    for (var j = 0; j < (placeholders || []).length; j++) {
        if (placeholders[j] && placeholders[j].container === container)
            return { action: "pending" };
    }
    return { action: "launch" };
}

// gdbus prints a refused call as
//   Error: GDBus.Error:org.qdistro.SessionManager1.PodAppRunning: <message>
function isRunningRefusal(stderr) {
    return String(stderr || "").indexOf("GDBus.Error:" + RUNNING_ERROR + ":") >= 0;
}

if (typeof module !== "undefined") {
    module.exports = {
        TIER2_ENGINE: TIER2_ENGINE,
        RUNNING_ERROR: RUNNING_ERROR,
        isPodWindow: isPodWindow,
        decideLaunch: decideLaunch,
        isRunningRefusal: isRunningRefusal,
    };
}
