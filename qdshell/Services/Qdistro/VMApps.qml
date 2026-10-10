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

    // ---- catalogue (dynamic manifest) ------------------------------------
    // Shared tier-5 app catalogue. The launcher (VMAppsProvider) surfaces
    // these and the Settings "Sandboxed VM apps" tab lists them for
    // per-app lifecycle overrides — both read this single source so the
    // policy key (appId) lines up.
    //
    // The catalogue is DISCOVERED from the guest base, not baked here:
    // tier5-vm/vmapps-scan.py scans the base's .desktop files at image
    // build time (and on base changes via the
    // qdistro-tier5-vmapps-scan service/path/timer) and publishes
    // /usr/share/qdistro/vmapps/base.json. This loader watches that
    // manifest; on any missing/unparseable/empty/stale manifest the
    // catalogue falls back to exactly one prebaked entry — foot, the
    // operator escape hatch (the guest base always ships it, and the
    // scanner refuses to publish a catalogue-bearing manifest for a
    // base that lacks it).
    //
    // Each entry: appId, desktopId, name, iconName, comment, execArgv
    // (JSON-stringified array of strings). appId values are the stable
    // historical keys (tier5/konsole, tier5/firefox, ...) produced by
    // the scanner's alias map — Settings overrides, --policy-key, VM
    // naming and broker selectors all key on them.
    readonly property string manifestPath: "/usr/share/qdistro/vmapps/base.json"
    property var catalogue: root._footRows()
    property bool _manifestMissing: false

    // Emitted after every complete catalogue swap (manifest load OR a
    // fallback transition). Providers refresh an open launcher on it;
    // the swap is atomic — catalogue never holds a partial model.
    signal refreshed()

    function _footRows() {
        return [{
            "appId":     "tier5/foot",
            "desktopId": "foot",
            "name":      "Foot Terminal (VM)",
            "iconName":  "utilities-terminal",
            "comment":   "Isolated foot terminal in a per-app VM",
            "execArgv":  JSON.stringify(["foot"]),
        }];
    }

    // A manifest row is usable only with a stable appId, a name, and an
    // execArgv that parses to a nonempty array of strings.
    function _validRow(row) {
        if (!row || typeof row !== "object") return false;
        if (typeof row.appId !== "string" || !row.appId) return false;
        if (typeof row.name !== "string" || !row.name) return false;
        let argv;
        try { argv = JSON.parse(row.execArgv); } catch (e) { return false; }
        return Array.isArray(argv) && argv.length > 0
               && argv.every(a => typeof a === "string") && !!argv[0];
    }

    function _loadManifest() {
        let doc = null;
        try { doc = JSON.parse(manifestFile.text()); } catch (e) { doc = null; }
        if (!doc || doc.schema !== 1 || !!doc.stale
                || !Array.isArray(doc.apps) || doc.apps.length === 0) {
            const why = !doc ? "unparseable"
                      : doc.schema !== 1 ? "schema != 1"
                      : doc.stale ? "stale: " + doc.stale
                      : "empty catalogue";
            Logger.w("VMApps", "manifest " + why + " — foot-only fallback");
            root._manifestMissing = false;
            root.catalogue = root._footRows();
            root.refreshed();
            return;
        }
        const rows = [];
        for (const r of doc.apps) {
            if (!root._validRow(r)) {
                Logger.w("VMApps", "manifest row malformed — foot-only fallback");
                root._manifestMissing = false;
                root.catalogue = root._footRows();
                root.refreshed();
                return;
            }
            rows.push({
                "appId":     r.appId,
                "desktopId": typeof r.desktopId === "string" ? r.desktopId : "",
                "name":      r.name,
                "iconName":  typeof r.iconName === "string" ? r.iconName : "",
                "comment":   typeof r.comment === "string" ? r.comment : "",
                "execArgv":  r.execArgv,
            });
        }
        const launchable = root._dropDomainCollisions(rows);
        if (launchable.length === 0) {
            Logger.e("VMApps", "manifest left no launchable apps "
                     + "— foot-only fallback");
            root._manifestMissing = false;
            root.catalogue = root._footRows();
            root.refreshed();
            return;
        }
        root._manifestMissing = false;
        root.catalogue = launchable;
        Logger.i("VMApps", "manifest loaded: " + launchable.length
                 + " app(s)");
        root.refreshed();
    }

    FileView {
        id: manifestFile
        path: root.manifestPath
        watchChanges: true
        printErrors: false
        onLoaded: root._loadManifest()
        onLoadFailed: function (error) {
            Logger.w("VMApps", "manifest " + root.manifestPath
                        + " unreadable (" + error + ") — foot-only fallback");
            root._manifestMissing = true;
            root.catalogue = root._footRows();
            root.refreshed();
        }
        onFileChanged: {
            // Re-parsed on the next tick; the write side publishes
            // atomically (temp+rename), so what reload() reads is always
            // a complete manifest.
            reload();
        }
    }

    // QFileSystemWatcher cannot watch a file that does not exist yet, so
    // a manifest staged AFTER session start (rebuilt guest base +
    // reconcile run while qdshell is up) would never fire onFileChanged.
    // Poll only while the manifest is missing — once it exists, the
    // watcher takes over and this timer stops.
    Timer {
        id: manifestRetry
        interval: 15000
        repeat: true
        running: root._manifestMissing
        onTriggered: manifestFile.reload()
    }

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

    // Deterministic per-app VM name — the tier-5 guest is the SILO for
    // the catalogue appId, so every launch of the same app names the same
    // domain. spawn-tier5.sh's hold-lock protocol lets a relaunch adopt
    // the still-running VM instead of paying a cold start, keeps
    // concurrent same-app launches on one guest (same trust domain: the
    // silo tag IS the VM name), and hands teardown to whichever session
    // is last out of the idle window. The wrapper validates
    // [a-zA-Z0-9][a-zA-Z0-9_-]{0,62}.
    //
    // The name is "t5-" + a human-readable stub + "-" + a 64-bit hash of
    // the FULL appId. The hash narrows collisions to the practically
    // impossible; the GUARANTEE that two distinct appIds never share a
    // guest is _dropDomainCollisions() at catalogue load — a collision
    // would run the second app inside the first app's guest and break
    // per-app isolation, so colliders are dropped, never launched
    // (sol r1/r2 P1).
    function _appIdHash(appId) {
        // Two independent 32-bit hashes (FNV-1a + djb2-xor) concatenated.
        let fnv = 0x811c9dc5;
        let djb = 0x00001505;
        for (let i = 0; i < appId.length; i++) {
            const c = appId.charCodeAt(i);
            fnv = Math.imul(fnv ^ c, 0x01000193) >>> 0;
            djb = (Math.imul(djb, 33) ^ c) >>> 0;
        }
        const pad = h => ("0000000" + h.toString(16)).slice(-8);
        return pad(fnv) + pad(djb);
    }

    function _vmNameForApp(appId) {
        const id = appId || "vmapp";
        const stub = id.replace(/[^a-zA-Z0-9_-]/g, "-")
                       .replace(/-{2,}/g, "-")
                       .replace(/^-+|-+$/g, "")
                       .slice(0, 43);
        // 3 + <=43 + 1 + 16 = <=63, inside the wrapper's name budget.
        return "t5-" + (stub || "vmapp") + "-" + _appIdHash(id);
    }

    // Derive every row's domain name and drop rows whose name is claimed
    // by a DIFFERENT appId — both colliders go, since catalogue order is
    // no trust signal (which row is the attacker's?). nameFn is
    // injectable so the detection is unit-testable under a forced
    // collision; production passes the real _vmNameForApp.
    function _dropDomainCollisions(rows, nameFn) {
        const nf = nameFn || (id => root._vmNameForApp(id));
        const byName = ({});
        const bad = new Set();
        for (const r of rows) {
            const n = nf(r.appId);
            if (byName[n] !== undefined && byName[n] !== r.appId) {
                bad.add(n);
                Logger.e("VMApps", "domain-name collision " + n
                         + " claimed by " + byName[n] + " and " + r.appId
                         + " — both dropped");
            } else {
                byName[n] = r.appId;
            }
        }
        return rows.filter(r => !bad.has(nf(r.appId)));
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
    // spawn-tier5.sh --vm <per-app-name> -- <argv...>; the helper emits
    // LAUNCH_TOKEN=<hex> early on stdout. We register a placeholder
    // keyed on the token; it's resolved when the inner toplevel
    // arrives carrying instance_id == launchToken (set by spawn-
    // tier5.sh's qdistro-secctx-exec wrap). The VM name is the appId's
    // deterministic silo name — relaunches adopt the running guest.
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
        // The ONLY name source is the deterministic derivation — a
        // manifest-supplied name would bypass _dropDomainCollisions and
        // could pin two apps to one guest (sol r2 P1).
        const vmName = root._vmNameForApp(row.appId);
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
