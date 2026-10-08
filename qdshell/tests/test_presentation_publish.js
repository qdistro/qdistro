const assert = require("assert");
const fs = require("fs");
const path = require("path");
const P = require("../Services/Theming/PresentationPublish.js");

const MANAGED = "/var/lib/qdistro/presentation";
const STANDALONE = "/home/admin/.local/state/qdistro/presentation";
const QML = fs.readFileSync(
  path.resolve(__dirname, "../Services/Theming/AppPresentationService.qml"),
  "utf8"
);

(function testParseOwnerUid() {
  // ensures: owner uid comes from a trusted helper's integer stdout, not env/stat
  assert.strictEqual(P.parseOwnerUid("1000\n"), 1000);
  assert.strictEqual(P.parseOwnerUid("0"), 0);
  assert.strictEqual(P.parseOwnerUid("1001"), 1001);
  assert.strictEqual(P.parseOwnerUid(" 1000 "), 1000);
  assert.strictEqual(P.parseOwnerUid(""), -1);
  assert.strictEqual(P.parseOwnerUid("1000 extra"), -1);
  assert.strictEqual(P.parseOwnerUid("-1"), -1);
  assert.strictEqual(P.parseOwnerUid("0x3e8"), -1);
  assert.strictEqual(P.parseOwnerUid("1e3"), -1);
  assert.strictEqual(P.parseOwnerUid("1000\n1001"), -1);
})();

(function testManagedPublishPassesResolvedOwner() {
  // ensures: managed publication passes --owner-uid from metadata, not a hardcoded 1000
  assert.deepStrictEqual(
    P.publishArgv(MANAGED, MANAGED, 1000),
    ["qdistro-presentation-publish", "--dir", MANAGED, "--owner-uid", "1000"]
  );
  assert.deepStrictEqual(
    P.publishArgv(MANAGED, MANAGED, 1001),
    ["qdistro-presentation-publish", "--dir", MANAGED, "--owner-uid", "1001"]
  );
  assert.strictEqual(P.publishArgv(MANAGED, MANAGED, -1), null);
  assert.strictEqual(P.publishArgv(MANAGED, MANAGED, undefined), null);
  assert.strictEqual(P.publishArgv(MANAGED, MANAGED, "1000"), null);
})();

(function testStandaloneOmitsOwnerFlag() {
  // ensures: standalone XDG publication does not pass --owner-uid
  const cmd = P.publishArgv(STANDALONE, MANAGED, 1000);
  assert.deepStrictEqual(cmd, ["qdistro-presentation-publish", "--dir", STANDALONE]);
  assert.ok(!cmd.includes("--owner-uid"));
  assert.ok(!cmd.includes("1000"));
})();

(function testQmlUsesHelperAndPrintOwner() {
  // ensures: qdshell invokes the helper and --print-owner instead of hardcoding uid 1000
  assert.ok(QML.includes("PresentationPublish.publishArgv(dest, root.managedDir, root.ownerUid)"));
  assert.ok(QML.includes("PresentationPublish.parseOwnerUid(stdout.text)"));
  assert.ok(QML.includes("root.ownerUid = uid"));
  assert.ok(QML.includes("--print-owner"));
  assert.ok(!/--owner-uid"\s*,\s*"1000"/.test(QML));
  assert.ok(!/ownerUid:\s*1000/.test(QML));
  assert.ok(!QML.includes("publishArgv(dest, root.managedDir, 1000)"));
  assert.ok(QML.includes("ownerUid: -1"));
  assert.ok(QML.includes("ownerResolved"));
  assert.ok(!QML.includes('["qdistro-presentation-publish", "--dir", destinationDir()]'));
})();

console.log("test_presentation_publish.js ok");
