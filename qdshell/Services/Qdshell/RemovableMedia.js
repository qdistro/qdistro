// Pure-logic helpers for the removable-media service. No QML / no I/O
// here so it can be unit-tested under node (Services/../tests).
//
// Security contract (qdistro/doc/removable-media-design.md):
//   - Mount/unmount NEVER happen here; this module only DECIDES what to
//     ask for. The actual mount goes through qdistro-media-exec → the
//     broker. This module just builds the request frame + parses the
//     reply.
//   - Autorun NEVER yields "execute". The strongest action this module
//     will ever recommend is "open" (open a file manager at the
//     mountpoint), and only when policy explicitly says so. There is no
//     code path that returns an "execute"/"autorun" action.
//   - Device labels are untrusted display strings: this module never
//     interpolates them into a command. The QML layer renders them
//     PlainText.

// Valid policy values (anything else falls back to the safe default).
var MOUNT_POLICIES = ["manual", "prompt"];
var AUTORUN_POLICIES = ["ignore", "prompt", "open"];

function normalizeMountPolicy(p) {
    return MOUNT_POLICIES.indexOf(p) >= 0 ? p : "prompt";
}

function normalizeAutorunPolicy(p) {
    return AUTORUN_POLICIES.indexOf(p) >= 0 ? p : "prompt";
}

// Decide what to do when a device is inserted, given the persisted
// policy. Returns one of:
//   { action: "ignore" }                  -- do nothing (notify only)
//   { action: "prompt" }                  -- show the insertion prompt
//   { action: "mount", thenOpen: bool }   -- ask the broker to mount,
//                                            optionally open afterwards
// NEVER returns anything that executes a file off the device.
function decideOnInsert(mountPolicy, autorunPolicy) {
    var mp = normalizeMountPolicy(mountPolicy);
    var ap = normalizeAutorunPolicy(autorunPolicy);

    // Manual mount policy: never auto-mount. If autorun wants the device
    // opened we still must mount first, so surface a prompt instead of
    // silently mounting against the user's "manual" choice.
    if (mp === "manual") {
        if (ap === "ignore")
            return { action: "ignore" };
        // prompt or open both require a user decision under manual mount
        return { action: "prompt" };
    }

    // mountPolicy === "prompt": mounting itself is brokered (the broker
    // prompts the admin). The autorun policy then decides the follow-up.
    if (ap === "ignore")
        return { action: "ignore" };
    if (ap === "open")
        return { action: "mount", thenOpen: true };
    // ap === "prompt"
    return { action: "prompt" };
}

// What the insertion-prompt offers. Deliberately three inert choices.
// "Run"/"autorun" is intentionally absent.
function promptChoices() {
    return ["mount", "open", "nothing"];
}

// Build the JSON request frame for qdistro-media-exec. `device` should
// be a kernel /dev path (or a /dev/disk/by-* symlink); the helper
// re-validates and canonicalizes it server-side. label/fstype/uuid are
// untrusted display metadata carried for the admin prompt only.
function buildMediaRequest(op, device, meta) {
    meta = meta || {};
    return {
        op: op,
        device: String(device || ""),
        label: String(meta.label || ""),
        fstype: String(meta.fstype || ""),
        uuid: String(meta.uuid || ""),
    };
}

// Parse a qdistro-media-exec reply line ({"type":"result",...}). Always
// returns { ok: bool, mountpoint, device, error } with safe defaults.
// Fail-closed: malformed / non-result → ok:false.
function parseMediaReply(line) {
    var obj;
    try {
        obj = JSON.parse(String(line || "").trim());
    } catch (e) {
        return { ok: false, error: "malformed reply", mountpoint: "", device: "" };
    }
    if (!obj || obj.type !== "result")
        return { ok: false, error: "unexpected reply", mountpoint: "", device: "" };
    return {
        ok: Boolean(obj.ok),
        mountpoint: String(obj.mountpoint || ""),
        device: String(obj.device || ""),
        error: String(obj.error || ""),
    };
}

if (typeof module !== "undefined") {
    module.exports = {
        MOUNT_POLICIES: MOUNT_POLICIES,
        AUTORUN_POLICIES: AUTORUN_POLICIES,
        normalizeMountPolicy: normalizeMountPolicy,
        normalizeAutorunPolicy: normalizeAutorunPolicy,
        decideOnInsert: decideOnInsert,
        promptChoices: promptChoices,
        buildMediaRequest: buildMediaRequest,
        parseMediaReply: parseMediaReply,
    };
}
