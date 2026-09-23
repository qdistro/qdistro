const assert = require("assert");
const LI = require("../Services/UI/LauncherItems.js");

// ── validation: reject empty name or empty command ──
(function testValidation() {
  assert.strictEqual(LI.isValidItem({ name: "Files", icon: "folder", command: "thunar" }), true);
  assert.strictEqual(LI.isValidItem({ name: "", command: "thunar" }), false, "empty name rejected");
  assert.strictEqual(LI.isValidItem({ name: "Files", command: "" }), false, "empty command rejected");
  assert.strictEqual(LI.isValidItem({ name: "   ", command: "  " }), false, "whitespace-only rejected");
  assert.strictEqual(LI.isValidItem({ name: "Files" }), false, "missing command rejected");
  assert.strictEqual(LI.isValidItem(null), false);
  assert.strictEqual(LI.isValidItem("nope"), false);
  // icon is optional
  assert.strictEqual(LI.isValidItem({ name: "X", command: "x" }), true);
})();

// ── normalizeItem trims fields and yields the {name, icon, command} shape ──
(function testNormalizeItem() {
  const n = LI.normalizeItem({ name: "  Web\nBrowser ", icon: " Firefox ", command: "  firefox  " });
  assert.strictEqual(n.name, "Web Browser");
  assert.strictEqual(n.icon, "firefox");
  assert.strictEqual(n.command, "firefox");
  // garbage input -> all empty, never throws
  assert.deepStrictEqual(LI.normalizeItem(undefined), { name: "", icon: "", command: "" });
})();

// ── normalizeList drops invalid entries, preserves order ──
(function testNormalizeList() {
  const dirty = [
    { name: "A", icon: "folder", command: "a" },
    { name: "", command: "skip-me" },         // no name -> dropped
    { name: "B", command: "" },               // no command -> dropped
    { name: "C", icon: "rocket", command: "c" },
  ];
  const out = LI.normalizeList(dirty);
  assert.strictEqual(out.length, 2);
  assert.strictEqual(out[0].name, "A");
  assert.strictEqual(out[1].name, "C");
  // non-array input -> []
  assert.deepStrictEqual(LI.normalizeList("nope"), []);
  assert.deepStrictEqual(LI.normalizeList(null), []);
})();

// ── add / remove ──
(function testAddRemove() {
  let list = [];
  list = LI.addItem(list, { name: "Term", icon: "terminal", command: "xterm" });
  assert.strictEqual(list.length, 1);
  // adding an invalid item is a no-op
  list = LI.addItem(list, { name: "", command: "bad" });
  assert.strictEqual(list.length, 1, "invalid add rejected");
  list = LI.addItem(list, { name: "Editor", command: "kate" });
  assert.strictEqual(list.length, 2);
  // remove middle/out-of-range
  list = LI.removeItem(list, 5);
  assert.strictEqual(list.length, 2, "out-of-range remove is no-op");
  list = LI.removeItem(list, 0);
  assert.strictEqual(list.length, 1);
  assert.strictEqual(list[0].name, "Editor");
})();

// ── updateItem ──
(function testUpdate() {
  let list = LI.addItem([], { name: "Old", command: "old" });
  list = LI.updateItem(list, 0, { name: "New", icon: "star", command: "new" });
  assert.strictEqual(list[0].name, "New");
  assert.strictEqual(list[0].command, "new");
  // invalid replacement -> unchanged
  list = LI.updateItem(list, 0, { name: "", command: "" });
  assert.strictEqual(list[0].name, "New", "invalid update rejected");
  // out-of-range -> unchanged
  list = LI.updateItem(list, 9, { name: "X", command: "x" });
  assert.strictEqual(list.length, 1);
})();

// ── reorder: moveItem / moveUp / moveDown ──
(function testReorder() {
  let list = LI.normalizeList([
    { name: "1", command: "c1" },
    { name: "2", command: "c2" },
    { name: "3", command: "c3" },
  ]);
  let out = LI.moveItem(list, 0, 2);
  assert.deepStrictEqual(out.map(i => i.name), ["2", "3", "1"]);
  out = LI.moveUp(list, 2);
  assert.deepStrictEqual(out.map(i => i.name), ["1", "3", "2"]);
  out = LI.moveDown(list, 0);
  assert.deepStrictEqual(out.map(i => i.name), ["2", "1", "3"]);
  // no-op cases
  assert.deepStrictEqual(LI.moveUp(list, 0).map(i => i.name), ["1", "2", "3"], "move up first is no-op");
  assert.deepStrictEqual(LI.moveDown(list, 2).map(i => i.name), ["1", "2", "3"], "move down last is no-op");
  assert.deepStrictEqual(LI.moveItem(list, 0, 9).map(i => i.name), ["1", "2", "3"], "bad target is no-op");
})();

// ── serialize round-trip ──
(function testSerializeRoundTrip() {
  const list = LI.normalizeList([
    { name: "Files", icon: "folder-open", command: "thunar ~/Documents" },
    { name: "Editor", icon: "edit", command: "code" },
  ]);
  const json = LI.serialize(list);
  assert.strictEqual(typeof json, "string");
  const back = LI.deserialize(json);
  assert.deepStrictEqual(back, list, "round-trip preserves the list");
  // deserialize tolerates an already-decoded array
  assert.deepStrictEqual(LI.deserialize(list), list);
  // deserialize tolerates garbage
  assert.deepStrictEqual(LI.deserialize("not json"), []);
  assert.deepStrictEqual(LI.deserialize(""), []);
  assert.deepStrictEqual(LI.deserialize(undefined), []);
})();

// ── icon sanitization ──
(function testIconSanitization() {
  assert.strictEqual(LI.sanitizeIcon("folder-open"), "folder-open");
  assert.strictEqual(LI.sanitizeIcon("  Rocket  "), "rocket");
  assert.strictEqual(LI.sanitizeIcon("Foo Bar"), "foo-bar", "spaces become single hyphen");
  assert.strictEqual(LI.sanitizeIcon("a--b"), "a-b", "collapses repeated separators");
  assert.strictEqual(LI.sanitizeIcon("-edge-"), "edge", "trims leading/trailing separators");
  assert.strictEqual(LI.sanitizeIcon(""), "");
  assert.strictEqual(LI.sanitizeIcon(null), "");
  // a number coerces to an inert digit-string (still a safe charset)
  assert.strictEqual(LI.sanitizeIcon(123), "123");
  assert.strictEqual(LI.sanitizeIcon("../../etc/passwd"), "etc-passwd", "path separators neutralized");
  // a 100-char icon name is rejected
  assert.strictEqual(LI.sanitizeIcon("a".repeat(100)), "");
})();

// ── buildExec uses ONLY the command field, as a single argv element ──
(function testBuildExec() {
  const argv = LI.buildExec({ name: "Files", icon: "folder", command: "thunar ~/Documents" });
  assert.deepStrictEqual(argv, ["sh", "-lc", "thunar ~/Documents"]);
  // no command -> null (caller must not exec)
  assert.strictEqual(LI.buildExec({ name: "X", command: "" }), null);
  assert.strictEqual(LI.buildExec(null), null);
})();

// ── INJECTION SAFETY: a malicious name/icon cannot alter the executed argv ──
(function testInjectionSafety() {
  const evilName = "Files'; rm -rf ~ #";
  const evilIcon = "$(reboot)`whoami`;rm -rf /";
  const cmd = "thunar";
  const item = { name: evilName, icon: evilIcon, command: cmd };

  // The exec argv is built from command ONLY — name/icon appear NOWHERE in it.
  const argv = LI.buildExec(item);
  assert.deepStrictEqual(argv, ["sh", "-lc", "thunar"]);
  argv.forEach(function (part) {
    assert.ok(part.indexOf("reboot") === -1, "icon must not leak into argv");
    assert.ok(part.indexOf("rm -rf") === -1, "name must not leak into argv");
    assert.ok(part.indexOf("whoami") === -1, "icon must not leak into argv");
  });

  // The malicious icon is reduced to an inert charset (no shell metachars,
  // no backticks, no $, no quotes, no semicolons).
  const safeIcon = LI.sanitizeIcon(evilIcon);
  assert.ok(/^[a-z0-9-]*$/.test(safeIcon), "sanitized icon has no metacharacters: " + safeIcon);
  assert.ok(safeIcon.indexOf("$") === -1 && safeIcon.indexOf("`") === -1);
  assert.ok(safeIcon.indexOf(";") === -1 && safeIcon.indexOf("'") === -1);

  // The name is stored verbatim as a label but is NEVER part of any argv.
  const n = LI.normalizeItem(item);
  assert.strictEqual(n.name, evilName, "name preserved verbatim as an inert label");
  assert.strictEqual(n.command, "thunar");

  // escapeLabel renders a markup-looking name inert in a RichText context
  // (the tooltip now escapes centrally, but the utility's contract is unchanged).
  const esc = LI.escapeLabel('<b>Files</b> & "x" <br>');
  assert.ok(esc.indexOf("<") === -1 && esc.indexOf(">") === -1, "angle brackets escaped");
  assert.strictEqual(esc, "&lt;b&gt;Files&lt;/b&gt; &amp; &quot;x&quot; &lt;br&gt;");
  assert.strictEqual(LI.escapeLabel(null), "", "null label escapes to empty string");

  // The command is passed through as a SINGLE argv element so the shell
  // evaluates it exactly once — a command that itself contains shell syntax
  // is the user's own (intended), but it is not double-wrapped/re-quoted.
  const compound = LI.buildExec({ name: "n", command: "a && b || c; d" });
  assert.deepStrictEqual(compound, ["sh", "-lc", "a && b || c; d"]);
})();

console.log("launcher-items: all assertions passed");
