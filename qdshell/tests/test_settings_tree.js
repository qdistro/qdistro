const assert = require("assert");
const TreeModel = require("../Modules/Panels/Settings/Tabs/Advanced/SettingsTreeModel.js");

// ─── inferType: one assertion per type ───────────────────────────────
assert.strictEqual(TreeModel.inferType(true), "bool");
assert.strictEqual(TreeModel.inferType(false), "bool");
assert.strictEqual(TreeModel.inferType(0), "number");
assert.strictEqual(TreeModel.inferType(42), "number");
assert.strictEqual(TreeModel.inferType(3.14), "number");
assert.strictEqual(TreeModel.inferType("hello"), "string");
assert.strictEqual(TreeModel.inferType(""), "string");
assert.strictEqual(TreeModel.inferType([1, 2, 3]), "array");
assert.strictEqual(TreeModel.inferType([]), "array");
assert.strictEqual(TreeModel.inferType({ a: 1 }), "object");
// null / undefined fall back to "string" so the leaf is editable, never dropped.
assert.strictEqual(TreeModel.inferType(null), "string");
assert.strictEqual(TreeModel.inferType(undefined), "string");

// ─── flatten: nested objects → ordered dotted rows ───────────────────
const nested = {
    power: {
        lidCloseOnAC: "suspend",
        criticalBatteryLevel: 5,
        nested: {
            deep: true,
        },
    },
    ui: {
        fontDefault: "Inter",
    },
    tags: ["a", "b"],
};

const rows = TreeModel.flatten(nested);
const byPath = {};
rows.forEach(r => { byPath[r.path] = r; });

// Every leaf path is present with the right dotted form.
assert.ok(byPath["power.lidCloseOnAC"], "power.lidCloseOnAC missing");
assert.ok(byPath["power.criticalBatteryLevel"], "power.criticalBatteryLevel missing");
assert.ok(byPath["power.nested.deep"], "deep nested path missing");
assert.ok(byPath["ui.fontDefault"], "ui.fontDefault missing");
assert.ok(byPath["tags"], "array leaf missing");

// Values and inferred types are carried through.
assert.strictEqual(byPath["power.lidCloseOnAC"].value, "suspend");
assert.strictEqual(byPath["power.lidCloseOnAC"].type, "string");
assert.strictEqual(byPath["power.criticalBatteryLevel"].value, 5);
assert.strictEqual(byPath["power.criticalBatteryLevel"].type, "number");
assert.strictEqual(byPath["power.nested.deep"].value, true);
assert.strictEqual(byPath["power.nested.deep"].type, "bool");
assert.deepStrictEqual(byPath["tags"].value, ["a", "b"]);
assert.strictEqual(byPath["tags"].type, "array");

// Order is stable / depth-first by key insertion order.
const paths = rows.map(r => r.path);
assert.deepStrictEqual(paths, [
    "power.lidCloseOnAC",
    "power.criticalBatteryLevel",
    "power.nested.deep",
    "ui.fontDefault",
    "tags",
]);

// ─── flatten edge cases: empty object, null values, empty nested obj ──
assert.deepStrictEqual(TreeModel.flatten({}), []);
assert.deepStrictEqual(TreeModel.flatten(null), []);
assert.deepStrictEqual(TreeModel.flatten(undefined), []);
// A top-level array is not a container — flatten of a non-object yields nothing.
assert.deepStrictEqual(TreeModel.flatten([1, 2]), []);

// null leaves are emitted (not dropped) and typed as string.
const withNull = TreeModel.flatten({ a: null, b: { c: null }, empty: {} });
const nullByPath = {};
withNull.forEach(r => { nullByPath[r.path] = r; });
assert.ok("a" in nullByPath, "null leaf 'a' dropped");
assert.strictEqual(nullByPath["a"].value, null);
assert.strictEqual(nullByPath["a"].type, "string");
assert.ok("b.c" in nullByPath, "nested null leaf dropped");
// Empty nested object emits no row.
assert.ok(!("empty" in nullByPath), "empty object should emit no row");
assert.strictEqual(withNull.length, 2);

// ─── filterRows: match / no-match / case-insensitive ─────────────────
const all = TreeModel.flatten(nested);

// Empty query returns all rows (a copy, not the original reference).
const allFiltered = TreeModel.filterRows(all, "");
assert.strictEqual(allFiltered.length, all.length);
assert.notStrictEqual(allFiltered, all);
assert.deepStrictEqual(TreeModel.filterRows(all, "   ").length, all.length);

// Substring match on path.
const lidMatch = TreeModel.filterRows(all, "lidclose");
assert.strictEqual(lidMatch.length, 1);
assert.strictEqual(lidMatch[0].path, "power.lidCloseOnAC");

// Case-insensitive.
assert.strictEqual(TreeModel.filterRows(all, "LIDCLOSE").length, 1);
assert.strictEqual(TreeModel.filterRows(all, "PoWeR").length, 3);

// Section prefix matches all of its leaves.
assert.strictEqual(TreeModel.filterRows(all, "power.").length, 3);

// No match → empty.
assert.deepStrictEqual(TreeModel.filterRows(all, "doesnotexist"), []);

// ─── isChanged: primitives ───────────────────────────────────────────
assert.strictEqual(TreeModel.isChanged("suspend", "suspend"), false);
assert.strictEqual(TreeModel.isChanged("hibernate", "suspend"), true);
assert.strictEqual(TreeModel.isChanged(5, 5), false);
assert.strictEqual(TreeModel.isChanged(6, 5), true);
assert.strictEqual(TreeModel.isChanged(true, true), false);
assert.strictEqual(TreeModel.isChanged(false, true), true);
// No default to compare against → unchanged.
assert.strictEqual(TreeModel.isChanged("anything", undefined), false);
// null vs null primitive-ish compare (null !== null is false → unchanged).
assert.strictEqual(TreeModel.isChanged(null, null), false);

// ─── isChanged: arrays / objects (deep compare) ──────────────────────
assert.strictEqual(TreeModel.isChanged([1, 2, 3], [1, 2, 3]), false);
assert.strictEqual(TreeModel.isChanged([1, 2, 3], [1, 2]), true);
assert.strictEqual(TreeModel.isChanged([1, 2], [2, 1]), true); // order matters
assert.strictEqual(TreeModel.isChanged({ a: 1, b: 2 }, { a: 1, b: 2 }), false);
assert.strictEqual(TreeModel.isChanged({ a: 1 }, { a: 2 }), true);
assert.strictEqual(TreeModel.isChanged({ a: 1, b: 2 }, { a: 1 }), true);

// ─── formatValue ─────────────────────────────────────────────────────
assert.strictEqual(TreeModel.formatValue(true, "bool"), "true");
assert.strictEqual(TreeModel.formatValue(false, "bool"), "false");
assert.strictEqual(TreeModel.formatValue(5, "number"), "5");
assert.strictEqual(TreeModel.formatValue("hi", "string"), "hi");
assert.strictEqual(TreeModel.formatValue(null, "string"), "");
assert.strictEqual(TreeModel.formatValue([1, 2], "array"), "[1,2]");
assert.strictEqual(TreeModel.formatValue({ a: 1 }, "object"), "{\"a\":1}");

// ─── parseNumber: strict full-string finite parse ───────────────────
assert.strictEqual(TreeModel.parseNumber("5"), 5);
assert.strictEqual(TreeModel.parseNumber("3.14"), 3.14);
assert.strictEqual(TreeModel.parseNumber("-2"), -2);
assert.strictEqual(TreeModel.parseNumber("0"), 0);
assert.strictEqual(TreeModel.parseNumber("  42  "), 42); // trimmed
// Rejected: partial input, separators, non-finite, empty, junk.
assert.strictEqual(TreeModel.parseNumber("12abc"), null);
assert.strictEqual(TreeModel.parseNumber("1,5"), null);
assert.strictEqual(TreeModel.parseNumber("Infinity"), null);
assert.strictEqual(TreeModel.parseNumber("NaN"), null);
assert.strictEqual(TreeModel.parseNumber(""), null);
assert.strictEqual(TreeModel.parseNumber("   "), null);
assert.strictEqual(TreeModel.parseNumber("abc"), null);
assert.strictEqual(TreeModel.parseNumber(null), null);
assert.strictEqual(TreeModel.parseNumber(undefined), null);

console.log("settings-tree: all assertions passed");
