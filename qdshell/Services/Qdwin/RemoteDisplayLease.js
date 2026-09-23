// Pure validation/layout helpers for the authenticated R9 qdshell executor.
// The D-Bus service authenticates qdshell's sender PID. These helpers still
// reject schema drift and build only one exact slot delta over the live output
// snapshot; controller input can never supply a general layout array.

var FIELDS = [
    "schema", "request_id", "generation", "session_id", "slot_name",
    "enabled", "logical_x", "logical_y", "width", "height", "scale",
    "expires_at"
];
var INPUT_FIELDS = [
    "schema", "request_id", "generation", "session_id", "slot_name",
    "enabled", "expires_at"
];

function parseBusctlString(stdout) {
    var text = String(stdout || "").trim();
    var match = text.match(/^s\s+("(?:[^"\\]|\\.)*")$/);
    if (!match) return null;
    var encoded;
    try { encoded = JSON.parse(match[1]); } catch (_) { return null; }
    if (encoded === "") return null;
    var request;
    try { request = JSON.parse(encoded); } catch (_) { return null; }
    return validateRequest(request, Math.floor(Date.now() / 1000))
        ? request : null;
}

function parseBusctlInput(stdout) {
    var text = String(stdout || "").trim();
    var match = text.match(/^s\s+("(?:[^"\\]|\\.)*")$/);
    if (!match) return null;
    var encoded;
    try { encoded = JSON.parse(match[1]); } catch (_) { return null; }
    if (encoded === "") return null;
    var request;
    try { request = JSON.parse(encoded); } catch (_) { return null; }
    return validateInputRequest(request, Math.floor(Date.now() / 1000))
        ? request : null;
}

function isInt(value) {
    return typeof value === "number" && isFinite(value)
        && Math.floor(value) === value;
}

function validateRequest(request, nowSeconds) {
    if (!request || typeof request !== "object" || Array.isArray(request))
        return false;
    var keys = Object.keys(request).sort();
    if (keys.length !== FIELDS.length
            || keys.join("\n") !== FIELDS.slice().sort().join("\n"))
        return false;
    if (request.schema !== "qdistro-mm-shell-layout-v1") return false;
    if (!/^[0-9a-f]{32}$/.test(request.request_id)) return false;
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(request.session_id))
        return false;
    if (!/^rdp-[0-9]{1,3}$/.test(request.slot_name)) return false;
    if (!isInt(request.generation) || request.generation <= 0) return false;
    if (typeof request.enabled !== "boolean") return false;
    if (!isInt(request.logical_x) || Math.abs(request.logical_x) > 65535)
        return false;
    if (!isInt(request.logical_y) || Math.abs(request.logical_y) > 65535)
        return false;
    if (!isInt(request.width) || request.width <= 0 || request.width > 16384)
        return false;
    if (!isInt(request.height) || request.height <= 0 || request.height > 16384)
        return false;
    if (request.width * request.height > 67108864) return false;
    if (!isInt(request.scale) || request.scale <= 0 || request.scale > 4)
        return false;
    if (!isInt(request.expires_at) || nowSeconds >= request.expires_at)
        return false;
    return true;
}

function validateInputRequest(request, nowSeconds) {
    if (!request || typeof request !== "object" || Array.isArray(request))
        return false;
    var keys = Object.keys(request).sort();
    if (keys.length !== INPUT_FIELDS.length
            || keys.join("\n") !== INPUT_FIELDS.slice().sort().join("\n"))
        return false;
    if (request.schema !== "qdistro-mm-shell-input-v1") return false;
    if (!/^[0-9a-f]{32}$/.test(request.request_id)) return false;
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(request.session_id))
        return false;
    if (!/^rdp-[0-9]{1,3}$/.test(request.slot_name)) return false;
    if (!isInt(request.generation) || request.generation <= 0) return false;
    if (typeof request.enabled !== "boolean") return false;
    if (!isInt(request.expires_at) || nowSeconds >= request.expires_at)
        return false;
    return true;
}

function buildSlotLayout(liveLayout, request) {
    if (!validateRequest(request, Math.floor(Date.now() / 1000))) return null;
    var found = 0;
    var enabledOthers = 0;
    var result = (liveLayout || []).map(function (entry) {
        var copy = {};
        for (var key in entry) copy[key] = entry[key];
        if (entry.name === request.slot_name) {
            found++;
            copy.enabled = request.enabled;
            if (request.enabled) {
                copy.x = request.logical_x;
                copy.y = request.logical_y;
                copy.width = request.width;
                copy.height = request.height;
                copy.scale = request.scale;
                copy.transform = 0;
            }
        } else if (entry.enabled) {
            enabledOthers++;
        }
        return copy;
    });
    // Exactly one pre-created slot must exist, and disabling it may never
    // produce a headless desktop with zero remaining outputs.
    if (found !== 1 || (!request.enabled && enabledOthers === 0)) return null;
    return result;
}

if (typeof module !== "undefined") {
    module.exports = {
        parseBusctlString: parseBusctlString,
        parseBusctlInput: parseBusctlInput,
        validateRequest: validateRequest,
        validateInputRequest: validateInputRequest,
        buildSlotLayout: buildSlotLayout,
    };
}
