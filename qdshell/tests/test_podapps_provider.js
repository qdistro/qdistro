const assert = require("assert");
const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "..");
const provider = fs.readFileSync(
    path.join(repo, "Modules/Panels/Launcher/Providers/PodAppsProvider.qml"),
    "utf8"
);
const core = fs.readFileSync(
    path.join(repo, "Modules/Panels/Launcher/LauncherCore.qml"),
    "utf8"
);

// Ensures: an async apps.json scan updates a launcher that is already open.
assert.ok(provider.includes("target: PodApps"));
assert.ok(provider.includes("function onRefreshed()"));
assert.ok(provider.includes("root.launcher.updateResults();"));

const service = fs.readFileSync(
    path.join(repo, "Services/Qdistro/PodApps.qml"),
    "utf8"
);
assert.ok(service.includes("_scanProcess.running = false;"),
    "overlapping refreshes must restart the cache reader");
assert.ok(service.includes("signal refreshed()"));
assert.ok(service.indexOf("root.apps.clear();") > service.indexOf("onStreamFinished:"),
    "the published model must not clear until scan completion");

// Ensures: tier-2 entries retain a non-text silo signal even when their app
// icon is absent from the host theme.
assert.ok(provider.includes('"badgeIcon":   "container"'));
assert.ok(provider.includes('"badgeColor":  "#ce93d8"'));
assert.strictEqual(
    (core.match(/modelData\.badgeColor \|\| Color\.mSurfaceVariant/g) || []).length,
    2,
    "both list and grid delegates must render provider badge colors"
);
assert.strictEqual(
    (core.match(/modelData\.badgeIconColor \|\| Color\.mOnSurfaceVariant/g) || []).length,
    2,
    "both list and grid delegates must render readable badge glyph colors"
);

console.log("podapps-provider: async refresh and tier-2 badge invariants passed");

// --- launch routing (qdistro tracker J12 Fix A) ---------------------------
// A launcher click must NOT fork spawn-tier2 from this process. qdshell is the
// unprivileged admin session, so a spawn forked here has no root launcher
// parent — which is precisely what qdistro-secctx-exec needs to stamp the
// app's identity on the Wayland wire. Forked from here, the app's window
// arrived with no wp_security_context_v1 at all (the compositor could not tell
// which silo/app it was, so the launcher badge and the cold-start placeholder
// could never resolve), and on a hardened profile spawn-tier2 refused the
// launch outright — clicking a pod app did nothing at all. The click now goes
// through SessionManager1.LaunchPodApp, which starts a User=root unit.
const launchFn = service.slice(
    service.indexOf("function launch(row)"),
    service.indexOf("function _registerPlaceholder"));
assert.ok(launchFn.length > 0, "PodApps.launch must exist");
assert.ok(!/spawn-tier2|qdistro-tier2-spawn/.test(launchFn),
    "a launcher click must not fork spawn-tier2 from the unprivileged shell");
assert.ok(launchFn.includes("LaunchPodApp"),
    "the click must route through SessionManager1.LaunchPodApp");
assert.ok(launchFn.includes("root.sessionBus")
          && service.includes('"org.qdistro.SessionManager1"'),
    "the call must be addressed to the session manager on the system bus");
assert.ok(launchFn.includes("--system"),
    "the session manager owns its name on the SYSTEM bus");
// The placeholder must key on the token the daemon returned in the reply —
// under a systemd unit spawn-tier2's stdout is the journal, not our pipe, so
// there is no LAUNCH_TOKEN line to read any more.
assert.ok(!service.includes("LAUNCH_TOKEN="),
    "the launch token now arrives in the D-Bus reply, not on spawn stdout");
assert.ok(launchFn.includes("[0-9a-f]{32}"),
    "the reply parser must pin the daemon's token shape");
assert.ok(launchFn.includes("_registerPlaceholder"));
