// RemoteMachine.js — pure identity/chrome logic for multi-machine remote
// (RDP-backed) managed windows, shared by the QML service
// RemoteMachineWindows.qml (`import "RemoteMachine.js" as RM`) and the Node unit
// test (require()). Phase-2 rung-1 FOLD, codex impl-34 Q4/Q5 + impl-30 Q5/Q6.
//
// Remote windows arrive on the viewer compositor as ordinary xdg_toplevels from a
// windowed FreeRDP client launched by qdistro-mm-rdp-client-wrapper under
// qdistro-secctx-exec, planting wp_security_context_v1:
//   engine      = qdistro.mm
//   app_id      = qdistro.mm.<origin_machine_id>.<stream_id>
//   instance_id = <origin>-<stream>-<nonce>
//
// The ORIGIN is derived from the secctx app_id, NEVER from the window title or
// remote pixels (impl-30 Q6: secctx is the load-bearing, non-spoofable identity).
// The per-origin border colour is compositor-owned trust chrome (impl-30 Q5):
// remote content cannot draw or cover it. A close-pending variant dims the colour
// (impl-34 Q3 — there is no qdwin badge-text request in this rung).
//
// Dual CommonJS / QML module (same shape as the sibling Services/**/*.js): keep
// every symbol a TOP-LEVEL declaration so a QML `import ... as RM` exposes
// `RM.fn`, and guard `module.exports` for Node.
//
// SPDX-License-Identifier: GPL-3.0-or-later

var MM_PREFIX = "qdistro.mm.";
var UNVERIFIED_COLOUR = "#616161";
var UNVERIFIED_BADGE = "REMOTE UNVERIFIED";
var IDENTITY_PART = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

// Per-origin border palette (distinct, saturated; identity chrome reads at a
// glance which machine a window belongs to). Deterministic per origin name.
var MM_PALETTE = [
    "#e53935",  // red
    "#1e88e5",  // blue
    "#43a047",  // green
    "#fb8c00",  // orange
    "#8e24aa",  // purple
    "#00acc1",  // teal
    "#fdd835",  // yellow
    "#6d4c41",  // brown
];

function isRemoteMachine(appId) {
    return !!appId && appId.indexOf(MM_PREFIX) === 0;
}

// A FULLY-ATTRIBUTABLE remote-machine app_id: the qdistro.mm.* prefix AND both an
// origin and a stream parse out of it. The close interception in Qdwin.qml uses
// THIS (not the bare prefix) so its notion of "remote" matches what
// RemoteMachineWindows can actually attribute — a malformed `qdistro.mm.foo`
// would otherwise be intercepted (no xdg-close) but dropped by the service,
// black-holing close (codex impl-36 MED). A malformed qdistro.mm.* window thus
// falls through to the normal path, where the qdwin compositor guard still
// refuses request_close for engine=qdistro.mm (a safe no-op, never an orphan).
function isManagedRemote(appId) {
    return isRemoteMachine(appId)
        && originFromSecctx(appId) !== ""
        && streamFromSecctx(appId) !== "";
}

// qdistro.mm.<origin>.<stream> → origin. The stream_id is the LAST dot-segment;
// the origin is everything between the prefix and that last dot (so an origin
// containing dots, e.g. a hostname, still parses). Returns "" if either the
// origin or the stream segment is missing (fail closed — an unattributable
// window gets no origin identity).
function originFromSecctx(appId) {
    if (!isRemoteMachine(appId)) return "";
    var tail = appId.slice(MM_PREFIX.length);
    var dot = tail.lastIndexOf(".");
    if (dot <= 0 || dot >= tail.length - 1) return "";   // need origin AND stream
    return tail.slice(0, dot);
}

// qdistro.mm.<origin>.<stream> → stream_id (the last dot-segment). "" if absent.
function streamFromSecctx(appId) {
    if (!isRemoteMachine(appId)) return "";
    var tail = appId.slice(MM_PREFIX.length);
    var dot = tail.lastIndexOf(".");
    if (dot <= 0 || dot >= tail.length - 1) return "";
    return tail.slice(dot + 1);
}

// Deterministic origin → palette colour (same char-sum hash shape as the
// tier-3/4 silo chrome, so the mapping is stable across restarts — journal/test
// grepping relies on determinism).
function colourForOrigin(origin) {
    if (!origin) return MM_PALETTE[0];
    var h = 0;
    for (var i = 0; i < origin.length; i++) {
        h = (h * 31 + origin.charCodeAt(i)) >>> 0;
    }
    return MM_PALETTE[h % MM_PALETTE.length];
}

// Trust chrome is keyed by the broker-vouched trust domain, not by the
// secctx-parsed origin. Until the broker confirms a handle, paint a neutral
// border that carries no trusted-machine meaning.
function colourForTrustDomain(trustDomainId) {
    if (!trustDomainId) return UNVERIFIED_COLOUR;
    return colourForOrigin(trustDomainId);
}

function colourForTrustedOrigin(origin, trustDomainId) {
    if (!origin || !trustDomainId) return UNVERIFIED_COLOUR;
    return colourForOrigin(trustDomainId + ":" + origin);
}

// Readable identity for a shell-owned protected surface. Colour is deliberately
// not the identity: a finite palette collides. Both strings come from the
// broker-vouched binding, and strict rendering prevents controls/bidi text from
// turning the protected pill into attacker-shaped chrome.
function badgeForTrustedOrigin(origin, trustDomainId) {
    if (typeof origin !== "string" || typeof trustDomainId !== "string"
            || !IDENTITY_PART.test(origin) || !IDENTITY_PART.test(trustDomainId))
        return UNVERIFIED_BADGE;
    return "REMOTE " + origin + " @ " + trustDomainId;
}

// Parse `busctl --json=short call ... BindHandleIdentity` output. The outer
// JSON is busctl's D-Bus envelope; data[0] is the broker's JSON identity.
// Any malformed/missing field returns null so QML leaves neutral chrome.
function parseBindIdentity(raw) {
    try {
        var outer = JSON.parse(String(raw || ""));
        if (!outer || !Array.isArray(outer.data) || outer.data.length !== 1
                || typeof outer.data[0] !== "string" || !outer.data[0])
            return null;
        var id = JSON.parse(outer.data[0]);
        if (!id || typeof id !== "object"
                || !Number.isInteger(id.handle) || id.handle <= 0
                || typeof id.origin !== "string" || !id.origin
                || typeof id.stream_id !== "string" || !id.stream_id
                || !Number.isInteger(id.generation) || id.generation <= 0
                || typeof id.trust_domain_id !== "string" || !id.trust_domain_id
                || (id.allow_input !== 0 && id.allow_input !== 1))
            return null;
        return id;
    } catch (e) {
        return null;
    }
}

// "#rrggbb" → 0xRRGGBBAA (alpha=ff), matching tier4_chrome.hex_to_rgba /
// SiloChrome.hexToRgba so qdwin_toplevel_border_rgba() reads consistent bytes.
// Bad input → 0 (qdwin treats 0 as the neutral default border), never throws.
function hexToRgba(hex) {
    if (!hex || hex.length !== 7 || hex[0] !== "#") return 0;
    var r = parseInt(hex.slice(1, 3), 16);
    var g = parseInt(hex.slice(3, 5), 16);
    var b = parseInt(hex.slice(5, 7), 16);
    if (isNaN(r) || isNaN(g) || isNaN(b)) return 0;
    return (((r << 24) | (g << 16) | (b << 8) | 0xFF) >>> 0);
}

// Darken a "#rrggbb" by `factor` (0..1), returning "#rrggbb". Used for the
// close-pending border variant. Bad input → input unchanged.
function dim(hex, factor) {
    if (!hex || hex.length !== 7 || hex[0] !== "#") return hex;
    var f = (typeof factor === "number" && factor >= 0 && factor <= 1)
        ? factor : 0.5;
    var out = "#";
    for (var i = 1; i < 7; i += 2) {
        var v = parseInt(hex.slice(i, i + 2), 16);
        if (isNaN(v)) return hex;
        v = Math.max(0, Math.min(255, Math.round(v * f)));
        var s = v.toString(16);
        out += (s.length === 1 ? "0" : "") + s;
    }
    return out;
}

// The close-pending border colour for an origin: a dimmed variant of its colour,
// so a window awaiting source-mediated close is visibly distinct (impl-34 Q3)
// without any client-drawable chrome.
function pendingColourForOrigin(origin) {
    return dim(colourForOrigin(origin), 0.5);
}

if (typeof module !== "undefined") {
    module.exports = {
        MM_PREFIX: MM_PREFIX,
        MM_PALETTE: MM_PALETTE,
        UNVERIFIED_COLOUR: UNVERIFIED_COLOUR,
        UNVERIFIED_BADGE: UNVERIFIED_BADGE,
        isRemoteMachine: isRemoteMachine,
        isManagedRemote: isManagedRemote,
        originFromSecctx: originFromSecctx,
        streamFromSecctx: streamFromSecctx,
        colourForOrigin: colourForOrigin,
        colourForTrustDomain: colourForTrustDomain,
        colourForTrustedOrigin: colourForTrustedOrigin,
        badgeForTrustedOrigin: badgeForTrustedOrigin,
        parseBindIdentity: parseBindIdentity,
        hexToRgba: hexToRgba,
        dim: dim,
        pendingColourForOrigin: pendingColourForOrigin
    };
}
