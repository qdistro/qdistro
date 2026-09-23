// PointerInputParse — pure, side-effect-free helpers extracted from
// PointerInputService.qml. NO Process / FileView / Settings / Quickshell
// access: only string/array transforms. Usable from both QML
// (import "PointerInputParse.js" as PointerInputParse) and Node
// (require("./PointerInputParse.js")) so the parsing logic can be unit-tested
// headless.
//
// Responsibility: device classification — turn raw `libinput list-devices`
// output OR /proc/bus/input/devices text into a list of pointer devices
// { id, name, type, hasTap, hasNaturalScroll, hasDisableWhileTyping,
//   hasScrollMethod } where type is one of
// mouse|touchpad|trackpoint|tablet|pointer. Tablet/Wacom digitizers are now
// detected as their own type so the Mouse tab can expose tablet-area mapping.
// Keyboards and touchscreens are excluded.
//
// It also hosts the pure advanced-settings helpers used by the Mouse tab:
//   * resolveDeviceSettings(global, overrides, id) — fold a per-device override
//     onto the global pointer policy (fallback to global for missing keys / id).
//   * normalizeTabletMapping(raw) — clamp/normalize the tablet area-to-output
//     mapping object to a safe canonical form.
//   * filterEnabledDevices(devices, disabledIds) — drop devices the user has
//     disabled, matching by device id.
// These advanced helpers are persist-only: NONE of them build or emit a command
// string. Device ids are opaque data; they are never shell-interpolated anywhere
// in qdshell.
//
// qdshell is qdwin-only. Global pointer live apply goes through
// qdwin_shell_v1.set_pointer_config when v28 is available; this parser module
// must still never build live-apply commands (the previous sway `swaymsg input
// …` builder was removed when the foreign-compositor dispatch was dropped).

// ─── libinput list-devices parsing ──────────────────────────────────
// Devices are separated by blank lines; each block has "Device:",
// "Capabilities:", "Tap:", "Natural scrolling:", "Scroll methods:",
// "Disable while typing:" fields.
function parseLibinput(lines) {
    var out = [];
    var cur = null;

    function commit() {
        var nlc = cur ? cur.name.toLowerCase() : "";
        // A graphics tablet / Wacom digitizer is a distinct device type: libinput
        // advertises "Capabilities: tablet" (it is not "pointer"-capable in the
        // mouse sense). We also accept it by the well-known "wacom" name so the
        // mapping UI appears even when the capability string is terse.
        var isTablet = cur && (cur._hasTablet || nlc.indexOf("wacom") !== -1 || nlc.indexOf(" tablet") !== -1 || nlc.indexOf("digitizer") !== -1 || nlc.indexOf("pen ") !== -1);
        if (cur && isTablet) {
            out.push({
                "id": cur.name,
                "name": cur.name,
                "type": "tablet",
                // Tablet-area mapping is the only tablet control; the libinput
                // pointer toggles do not apply, so report them unavailable.
                "hasTap": false,
                "hasNaturalScroll": false,
                "hasDisableWhileTyping": false,
                "hasScrollMethod": false
            });
            cur = null;
            return;
        }
        if (cur && cur._isPointer) {
            // Classify type now that all fields are collected. libinput reports a
            // touchpad as "Capabilities: pointer gesture" (NOT "touch" — that is a
            // touchscreen) and uniquely exposes non-n/a tap-to-click /
            // disable-while-typing fields. So a pointer device is a touchpad when
            // it advertises the gesture capability or any touchpad-only field;
            // everything else is a mouse (refined to trackpoint by name below).
            var type = "mouse";
            if (cur._hasGesture || cur.hasTap || cur.hasDisableWhileTyping)
                type = "touchpad";
            var nl = cur.name.toLowerCase();
            if (type === "mouse" && (nl.indexOf("trackpoint") !== -1 || nl.indexOf("track point") !== -1 || nl.indexOf("pointing stick") !== -1))
                type = "trackpoint";

            // libinput has no stable id; use the device name as the identifier.
            out.push({
                "id": cur.name,
                "name": cur.name,
                "type": type,
                "hasTap": cur.hasTap,
                "hasNaturalScroll": cur.hasNaturalScroll,
                "hasDisableWhileTyping": cur.hasDisableWhileTyping,
                "hasScrollMethod": cur.hasScrollMethod
            });
        }
        cur = null;
    }

    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        var t = line.trim();
        if (t === "") {
            commit();
            continue;
        }

        if (t.indexOf("Device:") === 0) {
            commit();
            cur = {
                "name": t.substring(7).trim(),
                "_isPointer": false,
                "_hasGesture": false,
                "_hasTablet": false,
                "hasTap": false,
                "hasNaturalScroll": false,
                "hasDisableWhileTyping": false,
                "hasScrollMethod": false
            };
            continue;
        }
        if (!cur)
            continue;

        if (t.indexOf("Capabilities:") === 0) {
            var caps = t.substring(13).toLowerCase();
            // Only "pointer"-capable devices are mice/touchpads/trackpoints. A
            // device that is "touch" but not "pointer" is a touchscreen — skip it.
            if (caps.indexOf("pointer") !== -1)
                cur._isPointer = true;
            if (caps.indexOf("gesture") !== -1)
                cur._hasGesture = true;
            if (caps.indexOf("tablet") !== -1)
                cur._hasTablet = true;
        } else if (t.toLowerCase().indexOf("tap-to-click:") === 0) {
            cur.hasTap = (t.toLowerCase().indexOf("n/a") === -1);
        } else if (t.toLowerCase().indexOf("natural scrolling:") === 0) {
            cur.hasNaturalScroll = (t.toLowerCase().indexOf("n/a") === -1);
        } else if (t.toLowerCase().indexOf("disable-w-typing:") === 0 || t.toLowerCase().indexOf("disable while typing:") === 0) {
            cur.hasDisableWhileTyping = (t.toLowerCase().indexOf("n/a") === -1);
        } else if (t.toLowerCase().indexOf("scroll methods:") === 0) {
            cur.hasScrollMethod = (t.toLowerCase().indexOf("n/a") === -1);
        }
    }
    commit();

    return out;
}

// ─── /proc/bus/input/devices parsing ────────────────────────────────
// Each device is a block of I:/N:/H:/B: lines separated by a blank line. We
// classify pointer devices using evdev capability bits: EV_REL (relative axes,
// mouse) and EV_ABS + INPUT_PROP POINTER/BUTTONPAD (touchpad). Capabilities
// here are coarser than libinput, so the per-control "has*" flags default to
// true (the compositor will ignore unsupported ones); they remain false only
// when no live backend confirms them.
function parseProc(lines) {
    var out = [];
    var cur = null;

    function commit() {
        if (cur && cur._isPointer) {
            out.push({
                "id": cur.name,
                "name": cur.name,
                "type": cur.type,
                // Unknown via /proc — assume available; these has* flags only
                // affect which UI controls show, never live apply (persist-only).
                // Tablets expose only area-mapping, so the libinput pointer
                // toggles are reported unavailable for them.
                "hasTap": cur.type === "touchpad",
                "hasNaturalScroll": cur.type !== "tablet",
                "hasDisableWhileTyping": cur.type === "touchpad",
                "hasScrollMethod": cur.type !== "tablet"
            });
        }
        cur = null;
    }

    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line.trim() === "") {
            commit();
            continue;
        }

        var tag = line.substring(0, 2);
        if (tag === "I:") {
            commit();
            cur = {
                "name": "",
                "type": "pointer",
                "_isPointer": false,
                "_ev": "",
                "_prop": "",
                "_key": ""
            };
        } else if (!cur) {
            continue;
        } else if (tag === "N:") {
            var m = line.match(/Name="(.*)"/);
            cur.name = m ? m[1] : line.substring(2).trim();
        } else if (line.indexOf("B: EV=") === 0) {
            cur._ev = line.substring(6).trim();
        } else if (line.indexOf("B: PROP=") === 0) {
            cur._prop = line.substring(8).trim();
        } else if (line.indexOf("B: KEY=") === 0) {
            cur._key = line.substring(7).trim().toLowerCase();
        } else if (line.indexOf("I: ") === 0) {
            // already handled by tag check
        }

        // Re-classify on each line so we have it once the block ends.
        if (cur) {
            var evVal = parseInt(cur._ev || "0", 16) || 0;
            var hasRel = (evVal & 0x04) !== 0;   // EV_REL bit 2
            var hasAbs = (evVal & 0x08) !== 0;   // EV_ABS bit 3
            // INPUT_PROP_POINTER (bit0) / INPUT_PROP_BUTTONPAD (bit2) => touchpad.
            var propVal = parseInt(cur._prop || "0", 16) || 0;
            var isTouchpad = hasAbs && ((propVal & 0x05) !== 0);
            var nameL = (cur.name || "").toLowerCase();
            // Graphics tablets / Wacom digitizers: EV_ABS plus a pen/stylus
            // device, recognised by the well-known names or the BTN_TOOL_PEN /
            // BTN_DIGI key range. Detect them first so they are not mis-classed
            // as touchscreens (excluded) or touchpads.
            var isTablet = nameL.indexOf("wacom") !== -1 || nameL.indexOf(" tablet") !== -1 || nameL.indexOf("digitizer") !== -1 || nameL.indexOf(" pen") !== -1 || /\bpen\b/.test(nameL);
            if (hasAbs && isTablet) {
                cur._isPointer = true;
                cur.type = "tablet";
            } else if (isTouchpad || nameL.indexOf("touchpad") !== -1) {
                cur._isPointer = true;
                cur.type = "touchpad";
            } else if (nameL.indexOf("trackpoint") !== -1 || nameL.indexOf("pointing stick") !== -1) {
                cur._isPointer = true;
                cur.type = "trackpoint";
            } else if (hasRel) {
                // Relative pointer with mouse buttons => mouse. Exclude pure
                // consumer-control / keyboard devices that also expose EV_REL.
                cur._isPointer = true;
                cur.type = "mouse";
            }
        }
    }
    commit();

    // De-duplicate by name (a single physical mouse may show several event
    // nodes); keep the first occurrence.
    // Null-prototype map so device names such as "toString"/"__proto__" cannot
    // collide with inherited object members during de-duplication.
    var seen = Object.create(null);
    var dedup = [];
    for (var k = 0; k < out.length; k++) {
        var nm = out[k].name;
        if (seen[nm])
            continue;
        seen[nm] = true;
        dedup.push(out[k]);
    }
    return dedup;
}

// Dispatch on the @@SRC: marker the enumeration shell prepends. Pure: takes the
// raw collected stdout, returns { source, devices } where source is
// "libinput" | "proc" | "none".
function parseEnum(out) {
    out = String(out || "");
    var lines = out.split("\n");
    var src = "";
    if (lines.length > 0 && lines[0].indexOf("@@SRC:") === 0) {
        src = lines[0].substring(6).trim();
        lines = lines.slice(1);
    }

    var parsed;
    if (src === "libinput")
        parsed = parseLibinput(lines);
    else
        parsed = parseProc(lines);

    return {
        "source": parsed.length > 0 ? src : "none",
        "devices": parsed
    };
}

// ─── Advanced per-device override resolution ────────────────────────
// Global pointer policy keys that a per-device override may shadow. Anything
// outside this set in an override object is ignored so a malformed/hostile
// override map cannot inject unexpected fields into the effective settings.
var OVERRIDABLE_KEYS = [
    "accelProfile", "pointerSpeed", "naturalScroll", "scrollMethod",
    "tapToClick", "disableWhileTyping", "leftHanded", "horizontalScroll"
];

// resolveDeviceSettings(global, overrides, id) → effective settings object.
// `global` is the global pointer policy; `overrides` is a map keyed by device
// id whose values are partial override objects. The result starts from a copy
// of the overridable global keys and applies the device's override on top,
// field by field (a missing field falls back to the global value). An unknown
// id, a null/non-object override, or an empty override yields the global
// values unchanged. Device ids are treated as opaque map keys only.
function resolveDeviceSettings(global, overrides, id) {
    global = (global && typeof global === "object") ? global : {};
    var eff = {};
    for (var i = 0; i < OVERRIDABLE_KEYS.length; i++) {
        var k = OVERRIDABLE_KEYS[i];
        eff[k] = global[k];
    }
    // Guard with hasOwnProperty so a device id like "toString"/"constructor"
    // can never resolve to an inherited prototype member of a plain object map.
    var ov = (overrides && typeof overrides === "object" && id != null && Object.prototype.hasOwnProperty.call(overrides, id)) ? overrides[id] : null;
    if (ov && typeof ov === "object") {
        for (var j = 0; j < OVERRIDABLE_KEYS.length; j++) {
            var key = OVERRIDABLE_KEYS[j];
            // Only honor explicitly-present override keys; undefined/missing
            // falls back to the global value already copied above.
            if (Object.prototype.hasOwnProperty.call(ov, key) && ov[key] !== undefined && ov[key] !== null)
                eff[key] = ov[key];
        }
    }
    return eff;
}

// hasDeviceOverride(overrides, id) — whether a device currently carries any
// override fields (used to drive the per-device "customize" toggle in the UI).
function hasDeviceOverride(overrides, id) {
    if (!overrides || typeof overrides !== "object" || id == null)
        return false;
    if (!Object.prototype.hasOwnProperty.call(overrides, id))
        return false;
    var ov = overrides[id];
    if (!ov || typeof ov !== "object")
        return false;
    return Object.keys(ov).length > 0;
}

// ─── Tablet area-to-output mapping normalization ─────────────────────
// A tablet mapping object describes how the tablet's active surface maps onto
// the screen. Canonical shape:
//   { output: <string|"">, aspect: "keep"|"stretch",
//     area: { x: 0..1, y: 0..1, w: 0..1, h: 0..1 } }
// The area is a normalized rectangle (fractions of the tablet surface). We
// clamp every field into range and guarantee a non-zero width/height so a
// persisted mapping can never describe a degenerate / out-of-bounds region.
function _clamp01(v, dflt) {
    var n = Number(v);
    if (!isFinite(n))
        return dflt;
    if (n < 0)
        return 0;
    if (n > 1)
        return 1;
    return n;
}

function normalizeTabletMapping(raw) {
    raw = (raw && typeof raw === "object") ? raw : {};
    var area = (raw.area && typeof raw.area === "object") ? raw.area : {};

    var EPS = 0.01;
    var x = _clamp01(area.x, 0);
    var y = _clamp01(area.y, 0);
    var w = _clamp01(area.w === undefined ? 1 : area.w, 1);
    var h = _clamp01(area.h === undefined ? 1 : area.h, 1);

    // Fit each axis inside [0,1] while guaranteeing a non-degenerate size:
    //   * prefer to shrink the size so the origin the user picked is kept;
    //   * if shrinking would make the size smaller than EPS (origin too close
    //     to the far edge, e.g. x = 1), pull the origin back instead so the
    //     final region always satisfies origin>=0, size>=EPS and origin+size<=1.
    function fit(o, s) {
        if (o + s > 1)
            s = 1 - o;
        if (s < EPS) {
            s = EPS;
            if (o + s > 1)
                o = 1 - s;
        }
        return [o, s];
    }
    var fx = fit(x, w);
    x = fx[0];
    w = fx[1];
    var fy = fit(y, h);
    y = fy[0];
    h = fy[1];

    var aspect = (raw.aspect === "stretch") ? "stretch" : "keep";
    // Output is an opaque connector name (e.g. "HDMI-1"); coerce to string and
    // never interpret it as anything but a label. "" means "all outputs".
    var output = (typeof raw.output === "string") ? raw.output : "";

    return {
        "output": output,
        "aspect": aspect,
        "area": { "x": x, "y": y, "w": w, "h": h }
    };
}

// ─── Disabled-device filtering ───────────────────────────────────────
// Drop devices whose id is in the disabled list. `disabledIds` may be an array
// of ids; matching is by exact id equality (ids are opaque strings). Returns a
// new array, never mutating the input.
function filterEnabledDevices(devices, disabledIds) {
    if (!Array.isArray(devices))
        return [];
    // Matching delegates to isDeviceDisabled, which compares array entries by
    // string equality — no plain-object lookup, so a device id like "toString"
    // cannot collide with a prototype member.
    var out = [];
    for (var j = 0; j < devices.length; j++) {
        var d = devices[j];
        if (d && !isDeviceDisabled(disabledIds, d.id))
            out.push(d);
    }
    return out;
}

// isDeviceDisabled(disabledIds, id) — convenience predicate for the UI toggle.
function isDeviceDisabled(disabledIds, id) {
    if (!Array.isArray(disabledIds) || id == null)
        return false;
    for (var i = 0; i < disabledIds.length; i++) {
        if (String(disabledIds[i]) === String(id))
            return true;
    }
    return false;
}

if (typeof module !== "undefined") {
    module.exports = {
        parseLibinput: parseLibinput,
        parseProc: parseProc,
        parseEnum: parseEnum,
        resolveDeviceSettings: resolveDeviceSettings,
        hasDeviceOverride: hasDeviceOverride,
        normalizeTabletMapping: normalizeTabletMapping,
        filterEnabledDevices: filterEnabledDevices,
        isDeviceDisabled: isDeviceDisabled,
        OVERRIDABLE_KEYS: OVERRIDABLE_KEYS,
    };
}
