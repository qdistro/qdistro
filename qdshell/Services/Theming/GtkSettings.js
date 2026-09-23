// Pure logic for the Appearance tab's GTK / fontconfig / theme-discovery
// helpers. Kept dependency-free so it runs both inside QML
// (`import "GtkSettings.js" as GtkSettings`) and under plain Node for tests.
//
// SECURITY: theme/font names originate from on-disk directory names and user
// input — treat them as UNTRUSTED. Any value that reaches a GTK settings.ini
// line, a fontconfig XML document, or (worst case) a shell string is first run
// through isSafeName()/sanitizeName(). A name like "Adwaita; rm -rf ~" or
// "../../etc" must never survive to a command line.

// ---------------------------------------------------------------------------
// Name validation / sanitization
// ---------------------------------------------------------------------------

// A safe theme/font-family name: letters, digits, space, and a small set of
// punctuation that legitimately appears in theme names (dot, underscore,
// hyphen, plus). NO shell metacharacters, NO path separators, NO "..".
function isSafeName(name) {
    if (typeof name !== "string" || name.length === 0 || name.length > 128)
        return false;
    if (name.indexOf("..") !== -1)
        return false; // reject path traversal even if only safe chars
    return (/^[A-Za-z0-9 ._+-]+$/).test(name);
}

// Return the name if safe, otherwise "". Callers treat "" as "no override".
function sanitizeName(name) {
    return isSafeName(name) ? name : "";
}

// ---------------------------------------------------------------------------
// GTK settings.ini parse / merge
// ---------------------------------------------------------------------------

// Parse an INI-ish settings.ini body into { header, keys, order } where:
//   header — text lines preceding the first key=value (incl. the [Settings]
//            line and any comments), preserved verbatim.
//   keys   — map of key -> value for every key=value line.
//   order  — the keys in first-seen order (so we can re-emit deterministically).
// Unknown sections are not modeled separately; GTK settings.ini in practice has
// only the single [Settings] group. We preserve every non-key line in `header`
// only if it appears before the first key; trailing non-key lines are dropped,
// which matches how the existing sed-based writer behaves.
function parseIni(text) {
    const lines = (text || "").split("\n");
    const keys = {};
    const order = [];
    const headerLines = [];
    let seenKey = false;
    let hasSettingsHeader = false;
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const m = line.match(/^([A-Za-z0-9_-]+)=(.*)$/);
        if (m) {
            seenKey = true;
            if (!(m[1] in keys))
                order.push(m[1]);
            keys[m[1]] = m[2];
        } else if (!seenKey) {
            if (line.trim() === "[Settings]")
                hasSettingsHeader = true;
            if (line.length > 0 || headerLines.length > 0)
                headerLines.push(line);
        }
    }
    return {
        header: headerLines,
        hasSettingsHeader: hasSettingsHeader,
        keys: keys,
        order: order
    };
}

// Merge `updates` (a key->value map) into an existing settings.ini `text`.
// Keys present in `updates` are inserted (preserving file order, new keys
// appended) or updated; a value of null/undefined REMOVES that key (revert to
// system default). All OTHER existing keys are preserved untouched.
// Always guarantees a leading [Settings] header. Returns the new file body
// (newline-terminated).
function mergeIni(text, updates) {
    const parsed = parseIni(text);
    updates = updates || {};

    for (const k in updates) {
        if (!Object.prototype.hasOwnProperty.call(updates, k))
            continue;
        const v = updates[k];
        if (v === null || v === undefined) {
            delete parsed.keys[k];
            const idx = parsed.order.indexOf(k);
            if (idx !== -1)
                parsed.order.splice(idx, 1);
        } else {
            if (!(k in parsed.keys))
                parsed.order.push(k);
            parsed.keys[k] = String(v);
        }
    }

    const out = [];
    if (parsed.hasSettingsHeader) {
        for (let i = 0; i < parsed.header.length; i++)
            out.push(parsed.header[i]);
    } else {
        out.push("[Settings]");
    }
    for (let i = 0; i < parsed.order.length; i++) {
        const k = parsed.order[i];
        out.push(k + "=" + parsed.keys[k]);
    }
    return out.join("\n") + "\n";
}

// ---------------------------------------------------------------------------
// Font-rendering value mapping
// ---------------------------------------------------------------------------

// Allowed enum values. Anything outside the allow-list is coerced to a safe
// default so a corrupt settings.json can never inject arbitrary text.
var HINT_STYLES = ["none", "slight", "medium", "full"];
var RGBA_ORDERS = ["none", "rgb", "bgr", "vrgb", "vbgr"];

function clampHintStyle(v) {
    return HINT_STYLES.indexOf(v) !== -1 ? v : "slight";
}

function clampRgba(v) {
    return RGBA_ORDERS.indexOf(v) !== -1 ? v : "rgb";
}

// Build the GTK settings.ini key map for the given font-rendering options.
// dpi is in points*1024 for gtk-xft-dpi (GTK convention); we accept a plain
// DPI number and convert. A dpi of 0/negative means "unset" (-> null = remove).
function gtkFontKeys(opts) {
    opts = opts || {};
    const aa = opts.antialias ? 1 : 0;
    const hintEnabled = clampHintStyle(opts.hintstyle) === "none" ? 0 : (opts.hinting ? 1 : 0);
    const keys = {
        "gtk-xft-antialias": aa,
        "gtk-xft-hinting": hintEnabled,
        "gtk-xft-hintstyle": "hint" + clampHintStyle(opts.hintstyle),
        "gtk-xft-rgba": clampRgba(opts.rgba)
    };
    const dpi = Number(opts.dpi);
    if (isFinite(dpi) && dpi > 0)
        keys["gtk-xft-dpi"] = Math.round(dpi * 1024);
    else
        keys["gtk-xft-dpi"] = null; // remove -> auto
    return keys;
}

// ---------------------------------------------------------------------------
// fontconfig XML fragment
// ---------------------------------------------------------------------------

function xmlEscape(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&apos;");
}

// Build a complete ~/.config/fontconfig/fonts.conf document expressing the
// font-rendering options for non-GTK apps. Booleans map to fontconfig bool,
// hintstyle/rgba map to their fontconfig constant names. Values are clamped to
// the allow-list and any DPI is rendered as a plain double — no untrusted text
// is interpolated unescaped.
function fontconfigDoc(opts) {
    opts = opts || {};
    const aa = opts.antialias ? "true" : "false";
    const hint = opts.hinting ? "true" : "false";
    const hintstyle = "hint" + clampHintStyle(opts.hintstyle);
    const rgba = clampRgba(opts.rgba);
    const lines = [];
    lines.push("<?xml version=\"1.0\"?>");
    lines.push("<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">");
    lines.push("<!-- Managed by qdshell Appearance settings -->");
    lines.push("<fontconfig>");
    lines.push("  <match target=\"font\">");
    lines.push("    <edit name=\"antialias\" mode=\"assign\"><bool>" + aa + "</bool></edit>");
    lines.push("    <edit name=\"hinting\" mode=\"assign\"><bool>" + hint + "</bool></edit>");
    lines.push("    <edit name=\"hintstyle\" mode=\"assign\"><const>" + xmlEscape(hintstyle) + "</const></edit>");
    lines.push("    <edit name=\"rgba\" mode=\"assign\"><const>" + xmlEscape(rgba) + "</const></edit>");
    lines.push("    <edit name=\"lcdfilter\" mode=\"assign\"><const>" + (rgba === "none" ? "lcdnone" : "lcddefault") + "</const></edit>");
    lines.push("  </match>");
    const dpi = Number(opts.dpi);
    if (isFinite(dpi) && dpi > 0) {
        lines.push("  <match target=\"pattern\">");
        lines.push("    <edit name=\"dpi\" mode=\"assign\"><double>" + dpi + "</double></edit>");
        lines.push("  </match>");
    }
    lines.push("</fontconfig>");
    return lines.join("\n") + "\n";
}

// ---------------------------------------------------------------------------
// Theme discovery (pure: operates over a provided directory listing)
// ---------------------------------------------------------------------------

// Given an array of entries { dir, name, markers } where `markers` is the list
// of marker file/dir names found directly inside dir/name, return the sorted,
// de-duplicated set of theme names whose markers include at least one of the
// required `markerNames`. `dir`/`name` are the candidate root and the theme
// subdirectory name respectively. Unsafe names are filtered out so a malicious
// directory name can never become a selectable (and later shell-bound) value.
function discoverThemes(entries, markerNames) {
    entries = entries || [];
    const want = markerNames || [];
    const seen = {};
    const result = [];
    for (let i = 0; i < entries.length; i++) {
        const e = entries[i];
        if (!e || !e.name || !isSafeName(e.name))
            continue;
        const markers = e.markers || [];
        let match = false;
        for (let j = 0; j < want.length; j++) {
            if (markers.indexOf(want[j]) !== -1) {
                match = true;
                break;
            }
        }
        if (!match)
            continue;
        if (seen[e.name])
            continue;
        seen[e.name] = true;
        result.push(e.name);
    }
    result.sort(function(a, b) {
        return a.toLowerCase() < b.toLowerCase() ? -1 : (a.toLowerCase() > b.toLowerCase() ? 1 : 0);
    });
    return result;
}

if (typeof module !== "undefined") {
    module.exports = {
        isSafeName: isSafeName,
        sanitizeName: sanitizeName,
        parseIni: parseIni,
        mergeIni: mergeIni,
        clampHintStyle: clampHintStyle,
        clampRgba: clampRgba,
        gtkFontKeys: gtkFontKeys,
        xmlEscape: xmlEscape,
        fontconfigDoc: fontconfigDoc,
        discoverThemes: discoverThemes,
        HINT_STYLES: HINT_STYLES,
        RGBA_ORDERS: RGBA_ORDERS
    };
}
