pragma Singleton

import QtQuick
import Quickshell
import qs.Commons
import qs.Services.Qdwin

// Tier3Apps — tier-3 (different-uid silo, waypipe-over-AF_UNIX)
// toplevel filter for the qdshell launcher / taskbar / containers
// panel. Mirrors VMApps.qml's tier-5 shape.
//
// Tier-3 apps arrive on the outer compositor as regular xdg_toplevels
// — connections from the *admin*-side `waypipe-client` half of the
// per-silo AF_UNIX bridge that qdistro/tier3/spawn-tier3.sh stands
// up. spawn-tier3.sh wraps waypipe-client with qdistro-secctx-exec,
// planting wp_security_context_v1:
//   engine  = qdistro.tier3
//   app_id  = qdistro.tier3.<silo>      (e.g. qdistro.tier3.user1)
//   instance_id = <LAUNCH_TOKEN>        (32 hex chars; emitted on
//                                        spawn-tier3.sh stdout for
//                                        cold-start correlation)
//
// What this service does (v1):
//   1. **Filter:** watches Qdwin.windows + emits the tier-3 subset
//      (secctxAppId starts with `qdistro.tier3.`) as `tier3Windows`.
//      Each row exposes `silo` (e.g. "user1") derived from the
//      secctx app_id, NOT from window title scraping. Title is a
//      pass-through from the inner app; secctx is the load-bearing
//      identity per spec/02 row 3.
//   2. **Per-silo colour:** computes a stable hex colour per silo
//      via a small JS string-hash over the palette in Commons (10
//      visible colours mirroring qdistro/tui/silo_colors.py). The
//      hash is local to QML, not synced with the Python palette
//      (different domain — Python keys on uid, qdshell keys on
//      silo name). Same silo always renders the same colour.
//      The colour is logged exactly once per silo (load-bearing
//      assertion for phase7-tier3-chrome bats).
//   3. **Launch / placeholders:** intentionally deferred to v2.
//      Tier-3 silos are persistent users, so cold-start is far
//      cheaper than tier-5; placeholder UI is lower priority than
//      the visible-on-arrival path that the filter + colour already
//      cover. Add when the launcher integration test (analogous to
//      tier-5's 20-tier5-vm-cold-start.md) lands.
//
// Test contract:
//   phase7-tier3-chrome (s38-tier3-chrome.sh) greps qdshell journal
//   for one line per tier-3 toplevel observed:
//     "[tier3] toplevel observed silo=user1 secctx=qdistro.tier3.user1 handle=NNN"
//   and one line per first-observed silo's colour:
//     "[tier3] silo=user1 color=#RRGGBB"
//   The strings above are part of the wire contract; don't rewrite
//   them without updating the bats driver in lockstep.
//
// See:
//   - qdistro/doc/isolation-tiers.md "Tier 3 — different user (waypipe over UNIX)"
//   - qdistro/tier3/spawn-tier3.sh (LAUNCH_TOKEN + secctx triple)
//   - qdshell/Services/Qdistro/VMApps.qml (the tier-5 sibling)
Singleton {
    id: root

    Component.onCompleted: Logger.i("Tier3Apps", "service started")

    readonly property string tier3Prefix: "qdistro.tier3."

    // ---- toplevel filter ------------------------------------------------
    // Mirrors Qdwin.windows rows + adds `silo`. handle is unique per
    // outer-compositor wl_resource; ownerUid will be the ADMIN uid
    // (the admin-side waypipe-client's wl_client), not the silo uid —
    // hence the secctx-derived silo property.
    property ListModel tier3Windows: ListModel {}
    property var _siloByHandle: ({})

    signal tier3WindowAdded(int handle, string silo, string appId, string colour)
    signal tier3WindowRemoved(int handle, string silo)

    // ---- silo colour palette (QML-local copy) ---------------------------
    // 10 visible hex colours that survive both light and dark themes.
    // Same intent as qdistro/tui/silo_colors.py — distinct enough to
    // skim, light/dark-safe enough to read text against.
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
        // Deterministic char-sum hash → palette index. Plenty of
        // entropy for a 10-colour palette; same silo always maps to
        // the same index.
        let h = 0;
        for (let i = 0; i < silo.length; i++) {
            h = (h * 31 + silo.charCodeAt(i)) >>> 0;
        }
        return root.siloPalette[h % root.siloPalette.length];
    }

    // ---- helpers --------------------------------------------------------
    function siloFromSecctx(secctxAppId) {
        if (!secctxAppId || !secctxAppId.startsWith(root.tier3Prefix))
            return "";
        const tag = secctxAppId.slice(root.tier3Prefix.length);
        if (!tag) return "";
        return tag;  // tier-3 silo names ARE the linux usernames
                      // (user1, user2, ...) — no "tier3/" prefix
                      // because tier-5 uses "vm-" for vmName-vs-silo
                      // distinction whereas tier-3 has only the uid.
    }

    function isTier3(secctxAppId) {
        return !!secctxAppId && secctxAppId.startsWith(root.tier3Prefix);
    }

    function rebuild() {
        const fresh = [];
        const seenHandles = new Set();
        const wm = Qdwin.windows;
        if (!wm) return;
        for (let i = 0; i < wm.count; i++) {
            const w = wm.get(i);
            if (!root.isTier3(w.secctxAppId)) continue;
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
        for (let i = 0; i < root.tier3Windows.count; i++)
            prevHandles.add(root.tier3Windows.get(i).handle);

        root.tier3Windows.clear();
        const nextSiloByHandle = ({});
        for (const row of fresh) {
            root.tier3Windows.append(row);
            nextSiloByHandle[row.handle] = row.silo;
            if (!prevHandles.has(row.handle)) {
                // Wire-contract log lines (s38 / s41 bats grep these).
                // Both fire per new-handle observation; intentionally
                // not deduped per silo because the singleton lives for
                // the whole qdshell session, so a once-per-silo dedup
                // hides the colour-resolve line for every spawn after
                // the first (the test would only see it on initial
                // qdshell start). Re-logging is cheap; greppability
                // wins.
                Logger.i("Tier3Apps",
                    "[tier3] toplevel observed silo=" + row.silo
                    + " secctx=" + row.secctxAppId
                    + " handle=" + row.handle);
                Logger.i("Tier3Apps",
                    "[tier3] silo=" + row.silo
                    + " color=" + row.colour);
                root.tier3WindowAdded(row.handle, row.silo, row.appId, row.colour);
            }
        }
        for (const h of prevHandles) {
            if (!seenHandles.has(h))
                root.tier3WindowRemoved(h, root._siloByHandle[h] || "");
        }
        root._siloByHandle = nextSiloByHandle;
    }

    // ---- wire-up --------------------------------------------------------
    Connections {
        target: Qdwin
        function onWindowListChanged() { root.rebuild(); }
        function onWindowSecctxResolved(handle, sandboxEngine, secctxAppId, instanceId) {
            if (root.isTier3(secctxAppId))
                root.rebuild();
            else if (root._siloByHandle[handle])
                root.rebuild();
        }
    }
}
