const assert = require("assert");
const M = require("../Services/System/MimeAssociations.js");

// --- MIME type validation -----------------------------------------------------
{
    assert.ok(M.isValidMimeType("text/plain"));
    assert.ok(M.isValidMimeType("image/svg+xml"));
    assert.ok(M.isValidMimeType("application/vnd.oasis.opendocument.text"));
    assert.ok(M.isValidMimeType("x-scheme-handler/https"));
    assert.ok(M.isValidMimeType("audio/x-wav"));

    // Invalid forms
    assert.ok(!M.isValidMimeType(""));
    assert.ok(!M.isValidMimeType("textplain"));        // no slash
    assert.ok(!M.isValidMimeType("text/plain/extra")); // two slashes
    assert.ok(!M.isValidMimeType("/plain"));           // empty type
    assert.ok(!M.isValidMimeType("text/"));            // empty subtype
    assert.ok(!M.isValidMimeType("text/../etc"));      // traversal
    assert.ok(!M.isValidMimeType(null));
    assert.ok(!M.isValidMimeType(42));

    // Narrowed charset: RFC token chars that are also shell metacharacters are
    // rejected (defense in depth for the documented "no shell metachars").
    assert.ok(!M.isValidMimeType("text/a&b"));
    assert.ok(!M.isValidMimeType("text/a#b"));
    assert.ok(!M.isValidMimeType("text/a$b"));
    assert.ok(!M.isValidMimeType("text/a b"));         // whitespace
}

// --- .desktop id validation ---------------------------------------------------
{
    assert.ok(M.isValidDesktopId("firefox.desktop"));
    assert.ok(M.isValidDesktopId("org.kde.dolphin.desktop"));
    assert.ok(M.isValidDesktopId("foo-bar_baz.desktop"));

    assert.ok(!M.isValidDesktopId(""));
    assert.ok(!M.isValidDesktopId("firefox"));            // no suffix
    assert.ok(!M.isValidDesktopId("foo/bar.desktop"));    // slash
    assert.ok(!M.isValidDesktopId("../evil.desktop"));    // traversal
    assert.ok(!M.isValidDesktopId(".desktop"));           // empty base
    assert.ok(!M.isValidDesktopId(null));
}

// --- MimeType= field parsing --------------------------------------------------
{
    const list = M.parseMimeTypeField("text/html;text/plain;image/png;");
    assert.deepStrictEqual(list, ["text/html", "text/plain", "image/png"]);

    // De-dup + drop invalid entries
    const list2 = M.parseMimeTypeField("text/plain;text/plain; ;bad entry;image/png");
    assert.deepStrictEqual(list2, ["text/plain", "image/png"]);

    assert.deepStrictEqual(M.parseMimeTypeField(""), []);
    assert.deepStrictEqual(M.parseMimeTypeField(null), []);
}

// --- mimeapps.list parse ------------------------------------------------------
{
    const body = [
        "[Default Applications]",
        "text/html=firefox.desktop",
        "image/png=gimp.desktop;eog.desktop",
        "",
        "[Added Associations]",
        "text/plain=nano.desktop"
    ].join("\n");
    const parsed = M.parseMimeappsList(body);
    assert.ok(parsed.sections["Default Applications"]);
    assert.ok(parsed.sections["Added Associations"]);

    const defs = M.defaultApplications(parsed);
    assert.strictEqual(defs["text/html"], "firefox.desktop");
    // Multi-value: first valid id wins
    assert.strictEqual(defs["image/png"], "gimp.desktop");
    // Added Associations is NOT part of defaults
    assert.strictEqual(defs["text/plain"], undefined);
}

// --- defaultApplications drops malformed/untrusted MIME keys ------------------
{
    const body = [
        "[Default Applications]",
        "text/html=firefox.desktop",
        "text/a&b=evil.desktop",      // malformed key -> dropped
        "bad key=evil.desktop"        // whitespace key -> dropped
    ].join("\n");
    const defs = M.defaultApplications(M.parseMimeappsList(body));
    assert.strictEqual(defs["text/html"], "firefox.desktop");
    assert.strictEqual(defs["text/a&b"], undefined);
    assert.strictEqual(defs["bad key"], undefined);
}

// --- firstValidDesktopId skips invalid leading entries ------------------------
{
    assert.strictEqual(M.firstValidDesktopId("evil;ok.desktop"), "ok.desktop");
    assert.strictEqual(M.firstValidDesktopId("a.desktop;b.desktop"), "a.desktop");
    assert.strictEqual(M.firstValidDesktopId("nope"), "");
}

// --- mergeDefaults (user wins over system) ------------------------------------
{
    const sys = { "text/html": "epiphany.desktop", "text/plain": "vi.desktop" };
    const usr = { "text/html": "firefox.desktop" };
    const merged = M.mergeDefaults(sys, usr);
    assert.strictEqual(merged["text/html"], "firefox.desktop");
    assert.strictEqual(merged["text/plain"], "vi.desktop");
    // inputs not mutated
    assert.strictEqual(sys["text/html"], "epiphany.desktop");
}

// --- applyDefault: set, then clear, scoped to [Default Applications] ----------
{
    const body = [
        "[Default Applications]",
        "text/html=firefox.desktop",
        "",
        "[Removed Associations]",
        "text/html=badapp.desktop"
    ].join("\n");
    let parsed = M.parseMimeappsList(body);

    // Set a new type
    parsed = M.applyDefault(parsed, "image/png", "gimp.desktop");
    assert.strictEqual(M.defaultApplications(parsed)["image/png"], "gimp.desktop");
    // [Removed Associations] untouched
    assert.strictEqual(parsed.sections["Removed Associations"].keys["text/html"], "badapp.desktop");

    // Clear an existing default
    parsed = M.applyDefault(parsed, "text/html", "");
    assert.strictEqual(M.defaultApplications(parsed)["text/html"], undefined);
    // Still didn't touch Removed Associations
    assert.strictEqual(parsed.sections["Removed Associations"].keys["text/html"], "badapp.desktop");

    // Round-trips through serialize/parse
    const out = M.serializeMimeappsList(parsed);
    const reparsed = M.parseMimeappsList(out);
    assert.strictEqual(M.defaultApplications(reparsed)["image/png"], "gimp.desktop");
    assert.strictEqual(reparsed.sections["Removed Associations"].keys["text/html"], "badapp.desktop");
}

// --- applyDefault rejects invalid input (does not reach disk) -----------------
{
    let threw = false;
    try {
        M.applyDefault({ sections: {}, order: [] }, "text/plain; rm -rf ~", "firefox.desktop");
    } catch (e) {
        threw = true;
    }
    assert.ok(threw, "applyDefault must throw on malicious MIME type");

    threw = false;
    try {
        M.applyDefault({ sections: {}, order: [] }, "text/plain", "evil.desktop;reboot");
    } catch (e) {
        threw = true;
    }
    assert.ok(threw, "applyDefault must throw on malicious desktop id");
}

// --- buildMimeCatalog + search ------------------------------------------------
{
    const entries = {
        "firefox.desktop": { name: "Firefox", mimeTypes: ["text/html", "x-scheme-handler/https"] },
        "gimp.desktop": { name: "GIMP", mimeTypes: ["image/png", "image/jpeg"] },
        "evil;.desktop": { name: "Evil", mimeTypes: ["image/png"] }, // invalid id -> ignored
        "bad.desktop": { name: "Bad", mimeTypes: ["not a mime", "image/png"] } // bad mime dropped
    };
    const descriptions = { "image/png": "PNG image", "text/html": "HTML document" };
    const catalog = M.buildMimeCatalog(entries, ["audio/mpeg"], descriptions);

    const byMime = {};
    catalog.forEach(function(c) { byMime[c.mime] = c; });

    // Sorted, valid-only
    assert.ok(byMime["image/png"]);
    assert.ok(byMime["text/html"]);
    assert.ok(byMime["audio/mpeg"]);          // from extraTypes
    assert.ok(!byMime["not a mime"]);         // invalid dropped

    // handlers exclude the invalid desktop id
    assert.deepStrictEqual(byMime["image/png"].handlers, ["bad.desktop", "gimp.desktop"]);
    assert.strictEqual(byMime["image/png"].handlers.indexOf("evil;.desktop"), -1);
    assert.strictEqual(byMime["image/png"].description, "PNG image");

    // Sorted ascending
    const mimes = catalog.map(function(c) { return c.mime; });
    const sorted = mimes.slice().sort();
    assert.deepStrictEqual(mimes, sorted);

    // Search by mime substring
    const r1 = M.searchMimeCatalog(catalog, "image/");
    assert.ok(r1.length === 2 && r1.every(function(c) { return c.mime.indexOf("image/") === 0; }));

    // Search by friendly description (case-insensitive)
    const r2 = M.searchMimeCatalog(catalog, "html doc");
    assert.strictEqual(r2.length, 1);
    assert.strictEqual(r2[0].mime, "text/html");

    // Empty query returns all
    assert.strictEqual(M.searchMimeCatalog(catalog, "  ").length, catalog.length);
}

// --- buildXdgMimeDefaultArgv: SAFE argv + injection rejection -----------------
{
    const argv = M.buildXdgMimeDefaultArgv("firefox.desktop", "text/html");
    assert.deepStrictEqual(argv, ["xdg-mime", "default", "firefox.desktop", "text/html"]);

    // Malicious values yield null (never an unsafe string), and no metachar
    // ever lands in an argv element.
    assert.strictEqual(M.buildXdgMimeDefaultArgv("evil.desktop;reboot", "text/html"), null);
    assert.strictEqual(M.buildXdgMimeDefaultArgv("firefox.desktop", "text/html; rm -rf ~"), null);
    assert.strictEqual(M.buildXdgMimeDefaultArgv("$(touch pwned).desktop", "text/html"), null);
    assert.strictEqual(M.buildXdgMimeDefaultArgv("firefox.desktop", "`id`/x"), null);

    // For any valid result, no argv element contains a shell metacharacter.
    const meta = /[;&|`$()<>\\"' \t\n*?]/;
    argv.forEach(function(tok) { assert.ok(!meta.test(tok), "unsafe token: " + tok); });
}

console.log("mime-associations: all assertions passed");
