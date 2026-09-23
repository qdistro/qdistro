// KeyboardXkb — pure, side-effect-free helpers extracted from
// KeyboardInputService.qml. NO Process / FileView / Settings / Quickshell
// access: only string/array transforms. Usable from both QML
// (import "KeyboardXkb.js" as KeyboardXkb) and Node
// (require("./KeyboardXkb.js")) so the XKB parsing and command building can be
// unit-tested headless.
//
// Three responsibilities:
//   1. evdev.lst / layout-list parsing — turn the raw XKB rules list text into
//      structured { models, layouts, variants, options }.
//   2. setxkbmap argv building — given model/layouts/variants/options, produce
//      the exact fully-tokenised argv array (no shell string, so a malicious
//      layout/option can never inject — it stays a single argv token).
//   3. `xset r rate` argv building — map repeat delay/rate onto its argv.

// ─── evdev.lst / layout-list parsing ────────────────────────────────
// evdev.lst lists models, layouts, variants and options in `! section`
// blocks. Returns:
//   { models: [{key,name}], layouts: [{key,name}],
//     variants: { "<layout>": [{key,name}], ... },
//     options: [{ group, name, options: [{key,name}] }] (sorted by name) }
function parseXkbList(text) {
    var models = [];
    var layouts = [];
    var variants = ({});
    var optionGroups = ({}); // groupKey -> { name, options: [] }
    var section = "";

    var lines = String(text || "").split("\n");
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        var trimmed = line.trim();
        if (trimmed === "")
            continue;
        if (trimmed.charAt(0) === "!") {
            // e.g. "! model", "! layout", "! variant", "! option"
            section = trimmed.substring(1).trim();
            continue;
        }
        // Each entry: <key><whitespace><description...>
        var m = trimmed.match(/^(\S+)\s+(.*)$/);
        if (!m)
            continue;
        var key = m[1];
        var name = m[2].trim();

        if (section === "model") {
            models.push({ "key": key, "name": name });
        } else if (section === "layout") {
            layouts.push({ "key": key, "name": name });
        } else if (section === "variant") {
            // Variant key format: "<variant>" with description "<Lang>: <desc>";
            // the owning layout is the trailing token in the description's colon
            // group. evdev.lst variant lines look like:
            //   intl    us: English (US, intl., with dead keys)
            // The layout code is the token before the colon.
            var colon = name.indexOf(":");
            var layoutCode = colon > 0 ? name.substring(0, colon).trim() : "";
            if (layoutCode === "")
                continue;
            if (!variants[layoutCode])
                variants[layoutCode] = [];
            variants[layoutCode].push({ "key": key, "name": name });
        } else if (section === "option") {
            // Option keys are "group" or "group:option". Group headers have no
            // colon; member options are "group:something".
            if (key.indexOf(":") === -1) {
                if (!optionGroups[key])
                    optionGroups[key] = { "name": name, "options": [] };
                else
                    optionGroups[key].name = name;
            } else {
                var grp = key.substring(0, key.indexOf(":"));
                if (!optionGroups[grp])
                    optionGroups[grp] = { "name": grp, "options": [] };
                optionGroups[grp].options.push({ "key": key, "name": name });
            }
        }
    }

    var optionsOut = [];
    for (var g in optionGroups) {
        optionsOut.push({ "group": g, "name": optionGroups[g].name, "options": optionGroups[g].options });
    }
    optionsOut.sort(function (a, b) { return a.name.localeCompare(b.name); });

    return {
        "models": models,
        "layouts": layouts,
        "variants": variants,
        "options": optionsOut
    };
}

// ─── setxkbmap argv building ────────────────────────────────────────
// Build the fully-tokenised setxkbmap argv from the persisted settings. Every
// user-provided token (layout/variant/model/option) is its OWN array element,
// so shell metacharacters in a layout or option string remain inert data —
// they can never be folded into a shell command line.
//
// settings: {
//   model: string,
//   layouts: [string, ...],
//   variants: { "<layout>": "<variant>", ... },
//   xkbOptions: [string, ...],
//   switchShortcut: string,
//   composeKey: string
// }
//
// Returns the argv array (starts with "setxkbmap"). Mirrors the QML
// _setxkbmapCmd() exactly: default layout "us", positional comma-joined
// variants, only emit -variant when at least one is non-empty, and always an
// initial empty -option (to clear previously-set options) followed by each
// desired option.
function setxkbmapArgv(settings) {
    settings = settings || {};
    var layouts = settings.layouts;
    var variants = settings.variants;
    var model = settings.model;
    var xkbOptions = settings.xkbOptions;
    var switchShortcut = settings.switchShortcut;
    var composeKey = settings.composeKey;

    var ls = (layouts && layouts.length > 0) ? layouts.slice() : ["us"];
    // Variants are positional and comma-joined to match the layout list.
    var vs = [];
    for (var i = 0; i < ls.length; i++) {
        var code = ls[i];
        vs.push((variants && variants[code]) ? variants[code] : "");
    }

    var argv = ["setxkbmap"];
    if (model && model !== "") {
        argv.push("-model");
        argv.push(model);
    }
    argv.push("-layout");
    argv.push(ls.join(","));
    // Only pass -variant if at least one variant is non-empty.
    var anyVariant = vs.some(function (v) { return v !== ""; });
    if (anyVariant) {
        argv.push("-variant");
        argv.push(vs.join(","));
    }

    // Collect XKB options: explicit option list + switch shortcut + compose.
    var opts = [];
    if (xkbOptions) {
        for (var j = 0; j < xkbOptions.length; j++) {
            if (xkbOptions[j] && xkbOptions[j] !== "")
                opts.push(xkbOptions[j]);
        }
    }
    if (switchShortcut && switchShortcut !== "")
        opts.push(switchShortcut);
    if (composeKey && composeKey !== "")
        opts.push(composeKey);
    // Always pass an initial empty -option so previously-set options are
    // cleared from the X server even when the user removed the last option;
    // each desired option is then re-added. (setxkbmap accumulates options
    // otherwise, so a removal in the UI would never take effect live.)
    argv.push("-option");
    argv.push("");
    for (var k = 0; k < opts.length; k++) {
        argv.push("-option");
        argv.push(opts[k]);
    }
    return argv;
}

// ─── shell-string serialization (for the chained `sh -c` apply path) ─
// The argv builders above are the primary, injection-proof interface (every
// user value is its own array element). The QML apply path, however, chains
// several tools with `; ` and runs them via `sh -c`, so it needs a shell
// string. These helpers build that string by construction: literal command
// names / flags are emitted raw, and EVERY user-supplied value is wrapped with
// shellQuote() — UNCONDITIONALLY, even if the value happens to start with "-".
// We never infer "this looks like a flag, leave it unquoted" from token text,
// which would let a value like "-x; rm -rf ~" escape quoting. This reproduces
// the original inline builder's quoting (it _q()-quoted every value too) while
// being a pure, headless-testable function.

// POSIX single-quote: wrap in single quotes, escaping embedded single quotes.
function shellQuote(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

// Build the chained `setxkbmap ...` shell string. Mirrors setxkbmapArgv but
// quotes value tokens for `sh -c`. Returns "" when setxkbmap is unavailable
// (caller passes hasSetxkbmap).
function setxkbmapShellCmd(settings, hasSetxkbmap) {
    if (hasSetxkbmap === false)
        return "";
    settings = settings || {};
    var model = settings.model;
    var layouts = settings.layouts;
    var variants = settings.variants;
    var xkbOptions = settings.xkbOptions;
    var switchShortcut = settings.switchShortcut;
    var composeKey = settings.composeKey;

    var ls = (layouts && layouts.length > 0) ? layouts.slice() : ["us"];
    var vs = [];
    for (var i = 0; i < ls.length; i++) {
        var code = ls[i];
        vs.push((variants && variants[code]) ? variants[code] : "");
    }

    var cmd = "setxkbmap";
    if (model && model !== "")
        cmd += " -model " + shellQuote(model);
    cmd += " -layout " + shellQuote(ls.join(","));
    var anyVariant = vs.some(function (v) { return v !== ""; });
    if (anyVariant)
        cmd += " -variant " + shellQuote(vs.join(","));

    var opts = [];
    if (xkbOptions) {
        for (var j = 0; j < xkbOptions.length; j++) {
            if (xkbOptions[j] && xkbOptions[j] !== "")
                opts.push(xkbOptions[j]);
        }
    }
    if (switchShortcut && switchShortcut !== "")
        opts.push(switchShortcut);
    if (composeKey && composeKey !== "")
        opts.push(composeKey);
    cmd += " -option " + shellQuote("");
    for (var k = 0; k < opts.length; k++)
        cmd += " -option " + shellQuote(opts[k]);
    return cmd;
}

// Build the `xset r rate <delay> <rate>` shell string. Values are numeric
// (clamped) but still quoted for symmetry with the original.
function xsetRepeatShellCmd(repeatDelay, repeatRate, hasXset) {
    if (hasXset === false)
        return "";
    var argv = xsetRepeatArgv(repeatDelay, repeatRate);
    // argv = ["xset","r","rate", d, r]; quote only the two value tokens.
    return "xset r rate " + shellQuote(argv[3]) + " " + shellQuote(argv[4]);
}

// ─── xset r rate argv building ──────────────────────────────────────
// `xset r rate <delay-ms> <rate-hz>`. delay/rate are clamped to >= 1 and
// rounded, matching the QML _xsetRepeatCmd().
function xsetRepeatArgv(repeatDelay, repeatRate) {
    // Behavior-preserving with the original inline QML: it operated directly on
    // the typed repeatDelay/repeatRate (Settings ints, always finite), so we do
    // NOT add a non-finite fallback here — that would diverge from the source.
    var d = Math.max(1, Math.round(repeatDelay));
    var r = Math.max(1, Math.round(repeatRate));
    return ["xset", "r", "rate", String(d), String(r)];
}

// ─── qdwin_shell_v1.set_key_repeat (v28) arg building ───────────────
// Clamp the persisted repeat rate (Hz) and delay (ms) to the protocol's
// documented ranges before handing them to QdwinBinding.setKeyRepeat /
// Qdwin.applyKeyRepeat: rate 0..255 (0 = repeat off, per the wl_keyboard
// repeat_info contract), delay 1..10000 ms. The compositor clamps again
// server-side; we clamp here so the wire carries canonical values and a
// non-finite/garbage setting can never reach it. Returns { rate, delay }.
function repeatToQdwinArgs(repeatRate, repeatDelay) {
    var r = Math.round(Number(repeatRate));
    var d = Math.round(Number(repeatDelay));
    if (!isFinite(r)) r = 25;     // settings default
    if (!isFinite(d)) d = 500;    // settings default
    if (r < 0) r = 0;
    if (r > 255) r = 255;
    if (d < 1) d = 1;
    if (d > 10000) d = 10000;
    return { rate: r, delay: d };
}

if (typeof module !== "undefined") {
    module.exports = {
        parseXkbList: parseXkbList,
        setxkbmapArgv: setxkbmapArgv,
        xsetRepeatArgv: xsetRepeatArgv,
        shellQuote: shellQuote,
        setxkbmapShellCmd: setxkbmapShellCmd,
        xsetRepeatShellCmd: xsetRepeatShellCmd,
        repeatToQdwinArgs: repeatToQdwinArgs,
    };
}
