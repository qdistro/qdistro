pragma Singleton

import QtQuick
import Quickshell
import qs.Commons
import qs.Services.Qdwin

// Tier3sApps — tier-3s (gVisor/runsc sandbox + waypipe bridge,
// Experimental / dev-profile only) toplevel filter for the qdshell
// launcher / taskbar / containers panel. Direct copy of Tier3Apps.qml
// with the tier3s prefix, model and journal tags (paravirt ΔB6).
//
// Tier-3s apps arrive on the outer compositor as regular xdg_toplevels
// — connections from the *admin*-side `waypipe client` half of the
// per-launch AF_UNIX bridge that qdistro/tier3s/spawn-tier3s.sh stands
// up. spawn-tier3s.sh wraps waypipe-client with qdistro-secctx-exec,
// planting wp_security_context_v1:
//   engine  = qdistro.tier3s
//   app_id  = qdistro.tier3s.<silo>     (e.g. qdistro.tier3s.smoke)
//   instance_id = <TIER3S_LAUNCH_TOKEN> (32 hex chars; the launch token
//                                        the manager wrote into the
//                                        /run/qdistro/tier3s-launch stanza)
//
// What this service does (v1):
//   1. **Filter:** watches Qdwin.windows + emits the tier-3s subset
//      (secctxAppId starts with `qdistro.tier3s.`) as `tier3sWindows`.
//      Each row exposes `silo` (the bare <silo> name — the canonical
//      clipboard/launch-record key; "tier3s/<silo>" is NEVER invented)
//      derived from the secctx app_id, NOT from window title scraping.
//      Title is a pass-through from the inner app (waypipe's
//      --title-prefix "[3s:<silo>] " is cosmetic); secctx is the
//      load-bearing identity per spec/02.
//   2. **Per-silo colour:** same deterministic palette + hash as
//      Tier3Apps/Tier4Apps, so a silo that exists in more than one
//      tier paints the same chrome everywhere. The colour is logged
//      per newly-observed handle (see below).
//   3. **Launch / placeholders:** deferred, same as Tier3Apps v1.
//
// Prefix note: `qdistro.tier3s.` never collides with the tier-3 prefix
// `qdistro.tier3.` — the trailing '.' in each prefix disambiguates —
// so this service and Tier3Apps can never both claim one toplevel.
//
// Test contract (paravirt ΔB9 s124-tier3s-app.sh):
//   one journal line per tier3s toplevel observed:
//     "[tier3s] toplevel observed silo=<silo> secctx=<app_id> color=<#hex> handle=<N>"
//   and one per-observation colour line:
//     "[tier3s] silo=<silo> color=#RRGGBB"
//   The strings above are part of the wire contract; don't rewrite
//   them without updating the driver in lockstep.
//
// See:
//   - qdistro/tier3s/CONTRACT.md (the tier-3s launch/secctx contract)
//   - qdistro/tier3s/spawn-tier3s.sh (TIER3S_LAUNCH_TOKEN + secctx triple)
//   - qdshell/Services/Qdistro/Tier3Apps.qml (the tier-3 sibling)
Singleton {
    id: root

    Component.onCompleted: Logger.i("Tier3sApps", "service started")

    readonly property string tier3sPrefix: "qdistro.tier3s."

    // ---- toplevel filter ------------------------------------------------
    // Mirrors Qdwin.windows rows + adds `silo`. handle is unique per
    // outer-compositor wl_resource; ownerUid will be the ADMIN uid
    // (the admin-side waypipe client's wl_client), not a sandbox uid —
    // hence the secctx-derived silo property.
    property ListModel tier3sWindows: ListModel {}
    property var _siloByHandle: ({})

    signal tier3sWindowAdded(int handle, string silo, string appId, string colour)
    signal tier3sWindowRemoved(int handle, string silo)

    // ---- silo colour palette (QML-local copy) ---------------------------
    // 10 visible hex colours that survive both light and dark themes.
    // Same palette as Tier3Apps/Tier4Apps so a silo paints the same
    // colour whichever tier backs it.
    readonly property var siloPalette: [
        "#4caf50",  // green
        "#ffb300",  // amber/yellow
        "#2196f3",  // blue
        "#ab47bc",  // magenta/purple
        "#26c6da",  // cyan
        "#8bc34a",  // bright green
        "#ffe54c",  // bright yellow
        "#64b5f6",  // bright blue
        "#ce93d8",  // bright magenta
        "#80deea",  // bright cyan
    ]

    function colourForSilo(silo) {
        if (!silo) return root.siloPalette[0];
        // Deterministic char-sum hash → palette index. Same shape as
        // Tier3Apps/Tier4Apps: same silo always maps to the same index.
        let h = 0;
        for (let i = 0; i < silo.length; i++) {
            h = (h * 31 + silo.charCodeAt(i)) >>> 0;
        }
        return root.siloPalette[h % root.siloPalette.length];
    }

    // ---- helpers --------------------------------------------------------
    function siloFromSecctx(secctxAppId) {
        if (!secctxAppId || !secctxAppId.startsWith(root.tier3sPrefix))
            return "";
        const tag = secctxAppId.slice(root.tier3sPrefix.length);
        if (!tag) return "";
        return tag;  // the tier3s silo key is the BARE name — clipboard,
                      // launch records and broker lineage all key on
                      // <silo>, never "tier3s/<silo>" (paravirt 03 step 4).
    }

    function isTier3s(secctxAppId) {
        return !!secctxAppId && secctxAppId.startsWith(root.tier3sPrefix);
    }

    function rebuild() {
        const fresh = [];
        const seenHandles = new Set();
        const wm = Qdwin.windows;
        if (!wm) return;
        for (let i = 0; i < wm.count; i++) {
            const w = wm.get(i);
            if (!root.isTier3s(w.secctxAppId)) continue;
            const silo = root.siloFromSecctx(w.secctxAppId);
            const colour = root.colourForSilo(silo);
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
                colour:       colour,
            });
            seenHandles.add(w.handle);
        }

        const prevHandles = new Set();
        for (let i = 0; i < root.tier3sWindows.count; i++)
            prevHandles.add(root.tier3sWindows.get(i).handle);

        root.tier3sWindows.clear();
        const nextSiloByHandle = ({});
        for (const row of fresh) {
            root.tier3sWindows.append(row);
            nextSiloByHandle[row.handle] = row.silo;
            if (!prevHandles.has(row.handle)) {
                // Wire-contract log lines (s124 greps these). Same
                // new-handle-only dedup posture as Tier3Apps: the
                // singleton lives for the whole session, so a
                // once-per-silo dedup would hide the lines for every
                // launch after the first.
                Logger.i("Tier3sApps",
                    "[tier3s] toplevel observed silo=" + row.silo
                    + " secctx=" + row.secctxAppId
                    + " color=" + row.colour
                    + " handle=" + row.handle);
                Logger.i("Tier3sApps",
                    "[tier3s] silo=" + row.silo
                    + " color=" + row.colour);
                root.tier3sWindowAdded(row.handle, row.silo, row.appId, row.colour);
            }
        }
        for (const h of prevHandles) {
            if (!seenHandles.has(h))
                root.tier3sWindowRemoved(h, root._siloByHandle[h] || "");
        }
        root._siloByHandle = nextSiloByHandle;
    }

    // ---- wire-up --------------------------------------------------------
    Connections {
        target: Qdwin
        function onWindowListChanged() { root.rebuild(); }
        function onWindowSecctxResolved(handle, sandboxEngine, secctxAppId, instanceId) {
            if (root.isTier3s(secctxAppId))
                root.rebuild();
            else if (root._siloByHandle[handle])
                root.rebuild();
        }
    }
}
