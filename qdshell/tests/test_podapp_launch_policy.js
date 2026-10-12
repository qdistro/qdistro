const assert = require("assert");
const fs = require("fs");
const path = require("path");
const P = require("../Services/Qdistro/PodAppLaunch.js");

// A pod is one container: spawn-tier2 runs every launch as
// `podman run --name <container>`. A second click while it was up used to die
// at podman (exit 125) behind a placeholder that sat there for 15 s. These
// pin what a click does instead.

const T2 = P.TIER2_ENGINE;
const win = (handle, secctxAppId, sandboxEngine = T2) =>
    ({ handle, sandboxEngine, secctxAppId });

// Ensures: nothing of the pod on screen -> ask the daemon to launch.
assert.deepStrictEqual(P.decideLaunch("podapp-term", [], []), { action: "launch" });
assert.deepStrictEqual(P.decideLaunch("podapp-term", undefined, undefined),
    { action: "launch" });

// Ensures: a live window of the pod is focused, never relaunched; with
// several, the newest (last in model order) wins.
assert.deepStrictEqual(
    P.decideLaunch("podapp-term",
        [win(3, "podapp-term/foot"), win(9, "podapp-term/foot")], []),
    { action: "focus", handle: 9 });

// Ensures: the match is the whole container name, not a prefix of it, and
// only under the tier-2 engine (a tier-3 or host window that happens to
// carry a lookalike app id is not the pod).
assert.deepStrictEqual(
    P.decideLaunch("podapp-term", [win(4, "podapp-term-2/foot")], []),
    { action: "launch" });
assert.deepStrictEqual(
    P.decideLaunch("podapp-term", [win(5, "podapp-term/foot", "qdistro.tier3")], []),
    { action: "launch" });
assert.deepStrictEqual(
    P.decideLaunch("podapp-term", [win(6, undefined)], []),
    { action: "launch" });
assert.strictEqual(P.isPodWindow(win(7, "x/y"), ""), false,
    "an empty container name must not match every window");

// Ensures: a repeat click while this pod's launch is still starting is
// dropped (it would only collide), but other pods are unaffected.
assert.deepStrictEqual(
    P.decideLaunch("podapp-term", [], [{ container: "podapp-term" }]),
    { action: "pending" });
assert.deepStrictEqual(
    P.decideLaunch("podapp-term", [], [{ container: "podapp-other" }]),
    { action: "launch" });

// Ensures: the daemon's typed refusal is recognised in gdbus's stderr shape,
// and nothing else is mistaken for it.
assert.ok(P.isRunningRefusal(
    "Error: GDBus.Error:org.qdistro.SessionManager1.PodAppRunning: pod 'x' is already running"));
assert.ok(!P.isRunningRefusal(
    "Error: GDBus.Error:org.qdistro.SessionManager1.Generic: PodAppRunning"));
assert.ok(!P.isRunningRefusal(""));
assert.ok(!P.isRunningRefusal(undefined));

// Ensures: the error name matches the session manager's typed error.
const sm = fs.readFileSync(path.resolve(__dirname, "../../session_manager/qdistro_session_manager.py"), "utf8");
assert.ok(/class PodAppRunning\(SessionError\):\s*\n\s*dbus_name = "PodAppRunning"/.test(sm),
    "PodAppLaunch.RUNNING_ERROR must name the daemon's PodAppRunning error");

// Ensures: PodApps.launch consults the policy before calling the daemon, and
// placeholders remember their container so the pending check can see them.
const service = fs.readFileSync(path.resolve(__dirname, "../Services/Qdistro/PodApps.qml"), "utf8");
const launchFn = service.slice(service.indexOf("function launch(row)"),
                               service.indexOf("id: launchProcessComp"));
assert.ok(launchFn.indexOf("PodAppLaunch.decideLaunch") >= 0
          && launchFn.indexOf("PodAppLaunch.decideLaunch") < launchFn.indexOf("LaunchPodApp"),
    "the focus/pending decision must come before the D-Bus launch");
assert.ok(launchFn.includes("Qdwin.focusWindow(verdict.handle)"));
assert.ok(service.includes("PodAppLaunch.isRunningRefusal(err)"));
assert.ok(/container:\s+container,/.test(service),
    "placeholders must carry their container");

console.log("podapp-launch-policy: focus-existing / pending / refusal invariants passed");
