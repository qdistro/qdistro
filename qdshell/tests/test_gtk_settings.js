const assert = require("assert");
const G = require("../Services/Theming/GtkSettings.js");

// --- INI merge: preserves unrelated keys, updates managed keys ----------------
{
    const existing = "[Settings]\n" +
        "gtk-icon-theme-name=Papirus\n" +
        "gtk-application-prefer-dark-theme=1\n" +
        "gtk-font-name=Sans 10\n";
    const merged = G.mergeIni(existing, {
        "gtk-theme-name": "Adwaita-dark",
        "gtk-xft-antialias": 1
    });
    const parsed = G.parseIni(merged);
    // Unrelated keys untouched
    assert.strictEqual(parsed.keys["gtk-icon-theme-name"], "Papirus");
    assert.strictEqual(parsed.keys["gtk-application-prefer-dark-theme"], "1");
    assert.strictEqual(parsed.keys["gtk-font-name"], "Sans 10");
    // Managed keys inserted
    assert.strictEqual(parsed.keys["gtk-theme-name"], "Adwaita-dark");
    assert.strictEqual(parsed.keys["gtk-xft-antialias"], "1");
    assert.ok(parsed.hasSettingsHeader);
    assert.ok(merged.indexOf("[Settings]") === 0);
}

// --- INI merge: update existing managed key in place --------------------------
{
    const existing = "[Settings]\ngtk-theme-name=Foo\ngtk-font-name=Sans 10\n";
    const merged = G.mergeIni(existing, { "gtk-theme-name": "Bar" });
    const parsed = G.parseIni(merged);
    assert.strictEqual(parsed.keys["gtk-theme-name"], "Bar");
    assert.strictEqual(parsed.keys["gtk-font-name"], "Sans 10");
    // order preserved (theme-name stays before font-name)
    assert.deepStrictEqual(parsed.order, ["gtk-theme-name", "gtk-font-name"]);
}

// --- INI merge: null value removes a key (revert to default) ------------------
{
    const existing = "[Settings]\ngtk-theme-name=Foo\ngtk-xft-dpi=98304\n";
    const merged = G.mergeIni(existing, { "gtk-xft-dpi": null });
    const parsed = G.parseIni(merged);
    assert.ok(!("gtk-xft-dpi" in parsed.keys));
    assert.strictEqual(parsed.keys["gtk-theme-name"], "Foo");
}

// --- INI merge: empty/fresh file gets a [Settings] header ---------------------
{
    const merged = G.mergeIni("", { "gtk-theme-name": "Adwaita" });
    assert.strictEqual(merged, "[Settings]\ngtk-theme-name=Adwaita\n");
}

// --- gtkFontKeys mapping ------------------------------------------------------
{
    const k = G.gtkFontKeys({ antialias: true, hinting: true, hintstyle: "full", rgba: "bgr", dpi: 96 });
    assert.strictEqual(k["gtk-xft-antialias"], 1);
    assert.strictEqual(k["gtk-xft-hinting"], 1);
    assert.strictEqual(k["gtk-xft-hintstyle"], "hintfull");
    assert.strictEqual(k["gtk-xft-rgba"], "bgr");
    assert.strictEqual(k["gtk-xft-dpi"], 96 * 1024);

    // antialias off, no dpi -> dpi removed
    const k2 = G.gtkFontKeys({ antialias: false, hinting: false, hintstyle: "none", rgba: "rgb", dpi: 0 });
    assert.strictEqual(k2["gtk-xft-antialias"], 0);
    assert.strictEqual(k2["gtk-xft-hinting"], 0); // none -> hinting disabled
    assert.strictEqual(k2["gtk-xft-hintstyle"], "hintnone");
    assert.strictEqual(k2["gtk-xft-dpi"], null);

    // out-of-range enums clamp to safe defaults
    const k3 = G.gtkFontKeys({ hintstyle: "evil$(rm)", rgba: "; rm -rf" });
    assert.strictEqual(k3["gtk-xft-hintstyle"], "hintslight");
    assert.strictEqual(k3["gtk-xft-rgba"], "rgb");
}

// --- fontconfig fragment generation for each hinting/rgba value ---------------
G.HINT_STYLES.forEach(function(hs) {
    const doc = G.fontconfigDoc({ antialias: true, hinting: true, hintstyle: hs, rgba: "rgb" });
    assert.ok(doc.indexOf("<const>hint" + hs + "</const>") !== -1, "hintstyle " + hs);
    assert.ok(doc.indexOf("<?xml") === 0);
    assert.ok(doc.indexOf("<fontconfig>") !== -1 && doc.indexOf("</fontconfig>") !== -1);
});
G.RGBA_ORDERS.forEach(function(order) {
    const doc = G.fontconfigDoc({ antialias: true, hinting: true, hintstyle: "slight", rgba: order });
    assert.ok(doc.indexOf("name=\"rgba\" mode=\"assign\"><const>" + order + "</const>") !== -1, "rgba " + order);
    // rgba=none must select lcdnone filter; others lcddefault
    if (order === "none")
        assert.ok(doc.indexOf("lcdnone") !== -1);
    else
        assert.ok(doc.indexOf("lcddefault") !== -1);
});
{
    // dpi rendered as double when positive, omitted otherwise
    const withDpi = G.fontconfigDoc({ dpi: 120 });
    assert.ok(withDpi.indexOf("<double>120</double>") !== -1);
    const noDpi = G.fontconfigDoc({ dpi: 0 });
    assert.ok(noDpi.indexOf("name=\"dpi\"") === -1);
    // bad enums are clamped, never interpolated raw
    const bad = G.fontconfigDoc({ hintstyle: "<script>", rgba: "&evil" });
    assert.ok(bad.indexOf("<script>") === -1);
    assert.ok(bad.indexOf("&evil") === -1);
}

// --- theme-name sanitization rejects malicious input (INJECTION-SAFETY) -------
{
    const malicious = [
        "Adwaita; rm -rf ~",
        "../../etc",
        "..",
        "foo/bar",
        "$(reboot)",
        "`id`",
        "a&b",
        "a|b",
        "a\nb",
        "a b > c",
        "",
        "x".repeat(200)
    ];
    malicious.forEach(function(name) {
        assert.strictEqual(G.isSafeName(name), false, "should reject: " + JSON.stringify(name));
        assert.strictEqual(G.sanitizeName(name), "", "should sanitize to '': " + JSON.stringify(name));
    });
    const ok = ["Adwaita", "Adwaita-dark", "Papirus_Light", "Yaru 3.0", "Breeze+Dark", "elementary"];
    ok.forEach(function(name) {
        assert.strictEqual(G.isSafeName(name), true, "should accept: " + name);
        assert.strictEqual(G.sanitizeName(name), name);
    });
}

// --- theme discovery filtering ------------------------------------------------
{
    const entries = [
        { dir: "/usr/share/themes", name: "Adwaita", markers: ["gtk-3.0", "gtk-2.0"] },
        { dir: "/usr/share/themes", name: "Breeze", markers: ["gtk-4.0"] },
        { dir: "/usr/share/themes", name: "Emacs", markers: ["emacs"] }, // no gtk marker -> excluded
        { dir: "/home/x/.themes", name: "Adwaita", markers: ["gtk-3.0"] }, // dup name -> collapsed
        { dir: "/usr/share/themes", name: "evil; rm -rf ~", markers: ["gtk-3.0"] }, // unsafe -> excluded
        { dir: "/usr/share/themes", name: "../../etc", markers: ["gtk-4.0"] }, // traversal -> excluded
        { dir: "/usr/share/themes", name: "Yaru", markers: ["gtk-3.0"] }
    ];
    const themes = G.discoverThemes(entries, ["gtk-3.0", "gtk-4.0"]);
    assert.deepStrictEqual(themes, ["Adwaita", "Breeze", "Yaru"]);
    // Sound-theme style discovery via index.theme marker
    const sounds = G.discoverThemes([
        { dir: "/usr/share/sounds", name: "freedesktop", markers: ["index.theme", "stereo"] },
        { dir: "/usr/share/sounds", name: "alsa", markers: [] }, // no index.theme
        { dir: "/usr/share/sounds", name: "Oxygen", markers: ["index.theme"] }
    ], ["index.theme"]);
    assert.deepStrictEqual(sounds, ["freedesktop", "Oxygen"]);
}

console.log("gtk-settings: all assertions passed");
