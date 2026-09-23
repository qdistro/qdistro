pragma Singleton

import QtQuick
import Quickshell
import qs.Commons
import qs.Services.Qdwin

// Tier4Apps — tier-4 (whole-VM-as-a-window, SPICE) toplevel filter for
// the qdshell launcher / taskbar / containers panel. P05a sibling of
// Tier3Apps. Mirrors VMApps' shape (tier-5) since both render a VM
// in a single chromed window.
//
// Tier-4 windows arrive on the outer compositor as regular
// xdg_toplevels from the admin-side `virt-viewer`. spawn-tier4.sh
// wraps virt-viewer with qdistro-secctx-exec, planting
// wp_security_context_v1:
//   engine      = qdistro.tier4
//   app_id      = qdistro.tier4.<vm-name>   (silo == VM name)
//   instance_id = <vm-name>-<pid>            (cold-start correlation)
//
// What this service does:
//   1. **Filter:** watches Qdwin.windows + emits the tier-4 subset
//      (secctxAppId starts with `qdistro.tier4.`) as `tier4Windows`.
//      Each row exposes `silo` (e.g. "work-vm") derived from the
//      secctx app_id, NOT title scraping — secctx is the load-bearing
//      identity per spec/02 row 4.
//   2. **Per-silo colour:** computes a stable hex colour per silo via
//      the same palette + hash Tier3Apps uses, so the same silo name
//      paints the same chrome in tier-3 and tier-4. The colour is
//      logged once per first-observed silo (load-bearing assertion
//      for s107-tier4-chrome bats).
//   3. **Chrome paint:** on each `onWindowSecctxResolved` for a
//      tier-4 toplevel, calls QdwinBinding.setBorderColor(handle,
//      rgba). qdwin stores this per-toplevel via
//      qdwin_toplevel_border_rgba(), so a re-issued
//      attach_decoration call by the SSD code path doesn't drop the
//      silo colour. P05a Phase A wire.
//
// Test contract:
//   s107-tier4-chrome.sh asserts this file exists at install and
//   carries both the tier-4 secctx prefix string AND a siloPalette
//   reference (the bats grep). Don't rewrite without updating s107.
//
// See:
//   - qdistro/doc/isolation-tiers.md "Tier 4 — VM, whole window"
//   - qdistro/tier4-vm/spawn-tier4.sh (secctx triple)
//   - qdshell/Services/Qdistro/Tier3Apps.qml (the tier-3 sibling)
//   - qdwin/qdwin/qdwin.c::qdwin_toplevel_border_rgba (P05a)
Singleton {
    id: root

    Component.onCompleted: Logger.i("Tier4Apps", "service started")

    readonly property string tier4Prefix: "qdistro.tier4."

    // ---- toplevel filter ------------------------------------------------
    // Mirrors Tier3Apps.tier3Windows row shape, plus the silo colour.
    property ListModel tier4Windows: ListModel {}
    property var _siloByHandle: ({})

    signal tier4WindowAdded(int handle, string silo, string appId, string colour)
    signal tier4WindowRemoved(int handle, string silo)

    // ---- silo colour palette --------------------------------------------
    // Same palette as Tier3Apps so a given silo paints the same colour
    // across both tiers — a user running both tier-3 and tier-4 silos
    // sees consistent identity chrome regardless of which sandboxing
    // shape backs the app.
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
        // Deterministic char-sum hash. Same shape as Tier3Apps and the
        // Python-side tier4_chrome.silo_color_hex.
        let h = 0;
        for (let i = 0; i < silo.length; i++) {
            h = (h * 31 + silo.charCodeAt(i)) >>> 0;
        }
        return root.siloPalette[h % root.siloPalette.length];
    }

    // Pack "#rrggbb" → 0xRRGGBBAA (alpha=ff). Matches
    // tier4_chrome.hex_to_rgba in the qdistro tier4-vm/ module so the
    // unit tests in tests/unit/test_tier4_chrome.py exercise the same
    // arithmetic the QML path uses. Returns 0 on bad input — qdwin's
    // qdwin_toplevel_border_rgba() treats 0 as "use neutral default".
    function _hexToRgba(hex) {
        if (!hex || hex.length !== 7 || hex[0] !== "#") return 0;
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        if (isNaN(r) || isNaN(g) || isNaN(b)) return 0;
        // QML's bitwise ops are 32-bit signed; force unsigned with >>> 0.
        return (((r << 24) | (g << 16) | (b << 8) | 0xFF) >>> 0);
    }

    // ---- helpers --------------------------------------------------------
    function siloFromSecctx(secctxAppId) {
        if (!secctxAppId || !secctxAppId.startsWith(root.tier4Prefix))
            return "";
        const tag = secctxAppId.slice(root.tier4Prefix.length);
        if (!tag) return "";
        return tag;  // tier-4 silo == vm name (qdistro.tier4.<vm>)
    }

    function isTier4(secctxAppId) {
        return !!secctxAppId && secctxAppId.startsWith(root.tier4Prefix);
    }

    // repaintAll: when true, re-issue setBorderColor for *every* tier-4
    // toplevel even if its handle is already known. Used on the qdwin
    // bind transition (Qdwin.shellBound) so pre-bind windows whose
    // initial setBorderColor() was dropped (no binding) get repainted —
    // the normal new-handle-only path would skip them since they are
    // already in tier4Windows / _siloByHandle. The added-signal +
    // wire-contract log lines stay gated on new handles, so a replay
    // doesn't re-emit tier4WindowAdded or spam s107's grepped lines.
    function rebuild(repaintAll) {
        const fresh = [];
        const seenHandles = new Set();
        const wm = Qdwin.windows;
        if (!wm) return;
        for (let i = 0; i < wm.count; i++) {
            const w = wm.get(i);
            if (!root.isTier4(w.secctxAppId)) continue;
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
        for (let i = 0; i < root.tier4Windows.count; i++)
            prevHandles.add(root.tier4Windows.get(i).handle);

        root.tier4Windows.clear();
        const nextSiloByHandle = ({});
        for (const row of fresh) {
            root.tier4Windows.append(row);
            nextSiloByHandle[row.handle] = row.silo;
            const isNew = !prevHandles.has(row.handle);
            if (isNew) {
                // Wire-contract log lines (s107 / future bats grep
                // these). Format mirrors the tier-3 cousin so the
                // log analyser doesn't need a tier-specific branch.
                Logger.i("Tier4Apps",
                    "[tier4] toplevel observed silo=" + row.silo
                    + " secctx=" + row.secctxAppId
                    + " handle=" + row.handle);
                Logger.i("Tier4Apps",
                    "[tier4] silo=" + row.silo
                    + " color=" + row.colour);
                root.tier4WindowAdded(row.handle, row.silo, row.appId, row.colour);
            }

            // P05a Phase A wire: push the silo colour into the qdwin
            // per-toplevel border state so the SSD paint helper
            // (currently a flat default) reads the silo-coloured rgba.
            // The Qdwin singleton's setBorderColor wrapper guards
            // against a null binding (qdshell startup race) so
            // missing-bind degrades to a log line rather than a
            // TypeError; the SSD then falls back to
            // qdwin_toplevel_border_rgba()'s `fallback` (=0 → neutral
            // chrome) until the binding lands. Normally fires for new
            // handles only; on repaintAll (qdwin bind transition) it
            // re-issues for already-known handles too, since a pre-bind
            // window's first setBorderColor() was dropped while unbound.
            if (isNew || repaintAll) {
                const rgba = root._hexToRgba(row.colour);
                if (rgba !== 0) {
                    Qdwin.setBorderColor(row.handle, rgba);
                }
            }
        }
        for (const h of prevHandles) {
            if (!seenHandles.has(h))
                root.tier4WindowRemoved(h, root._siloByHandle[h] || "");
        }
        root._siloByHandle = nextSiloByHandle;
    }

    // ---- wire-up --------------------------------------------------------
    Connections {
        target: Qdwin
        function onWindowListChanged() { root.rebuild(); }
        function onWindowSecctxResolved(handle, sandboxEngine, secctxAppId, instanceId) {
            if (root.isTier4(secctxAppId))
                root.rebuild();
            else if (root._siloByHandle[handle])
                root.rebuild();
        }
        // qdwin_shell_v1 just bound (false→true). Any tier-4 window that
        // appeared before the binding landed had its border paint
        // dropped (Qdwin.setBorderColor: no binding). Replay with
        // repaintAll so those pre-bind handles get their silo colour
        // re-issued instead of staying neutral.
        function onShellBound() { root.rebuild(true); }
    }
}
