const assert = require("assert");
const RM = require("../Services/Qdshell/RemovableMedia.js");

// --- policy normalization ------------------------------------------------
assert.strictEqual(RM.normalizeMountPolicy("manual"), "manual");
assert.strictEqual(RM.normalizeMountPolicy("prompt"), "prompt");
assert.strictEqual(RM.normalizeMountPolicy("garbage"), "prompt");
assert.strictEqual(RM.normalizeMountPolicy(""), "prompt");

assert.strictEqual(RM.normalizeAutorunPolicy("ignore"), "ignore");
assert.strictEqual(RM.normalizeAutorunPolicy("prompt"), "prompt");
assert.strictEqual(RM.normalizeAutorunPolicy("open"), "open");
assert.strictEqual(RM.normalizeAutorunPolicy("run"), "prompt"); // no "run"
assert.strictEqual(RM.normalizeAutorunPolicy("execute"), "prompt");

// --- insertion decision: autorun NEVER executes --------------------------
// Whatever the policy combination, decideOnInsert can only ever return
// ignore / prompt / mount(+open). It can NEVER return an execute action.
const allActions = new Set();
["manual", "prompt", "weird"].forEach((mp) => {
    ["ignore", "prompt", "open", "run", "execute", "autorun"].forEach((ap) => {
        const d = RM.decideOnInsert(mp, ap);
        allActions.add(d.action);
        assert.notStrictEqual(d.action, "execute");
        assert.notStrictEqual(d.action, "run");
        assert.notStrictEqual(d.action, "autorun");
        assert.ok(["ignore", "prompt", "mount"].indexOf(d.action) >= 0,
            "unexpected action: " + d.action);
    });
});
// Sanity: the only actions ever produced are the inert three.
allActions.forEach((a) => assert.ok(["ignore", "prompt", "mount"].indexOf(a) >= 0));

// --- specific policy mappings --------------------------------------------
// Default (prompt, prompt) → show the prompt.
assert.deepStrictEqual(RM.decideOnInsert("prompt", "prompt"), { action: "prompt" });
// prompt mount + ignore autorun → do nothing (notify only).
assert.deepStrictEqual(RM.decideOnInsert("prompt", "ignore"), { action: "ignore" });
// prompt mount + open autorun → mount then open (no execution).
assert.deepStrictEqual(RM.decideOnInsert("prompt", "open"), { action: "mount", thenOpen: true });
// manual mount never auto-mounts: open/prompt both surface a prompt.
assert.deepStrictEqual(RM.decideOnInsert("manual", "open"), { action: "prompt" });
assert.deepStrictEqual(RM.decideOnInsert("manual", "prompt"), { action: "prompt" });
// manual + ignore → truly nothing.
assert.deepStrictEqual(RM.decideOnInsert("manual", "ignore"), { action: "ignore" });

// --- prompt choices contain no "run" -------------------------------------
const choices = RM.promptChoices();
assert.deepStrictEqual(choices, ["mount", "open", "nothing"]);
assert.strictEqual(choices.indexOf("run"), -1);
assert.strictEqual(choices.indexOf("autorun"), -1);
assert.strictEqual(choices.indexOf("execute"), -1);

// --- request frame building ----------------------------------------------
const req = RM.buildMediaRequest("mount", "/dev/sdb1", {
    label: "MYUSB", fstype: "vfat", uuid: "ABCD-1234",
});
assert.strictEqual(req.op, "mount");
assert.strictEqual(req.device, "/dev/sdb1");
assert.strictEqual(req.label, "MYUSB");
assert.strictEqual(req.fstype, "vfat");
assert.strictEqual(req.uuid, "ABCD-1234");

// An untrusted label is carried verbatim (display only) and is NOT mixed
// into op/device — there is no command interpolation here at all.
const evil = "; rm -rf / #$(reboot)";
const req2 = RM.buildMediaRequest("mount", "/dev/sdb1", { label: evil });
assert.strictEqual(req2.label, evil);
assert.strictEqual(req2.device, "/dev/sdb1");
assert.strictEqual(req2.op, "mount");

// Missing metadata defaults to empty strings.
const req3 = RM.buildMediaRequest("unmount", "/dev/sdb1");
assert.strictEqual(req3.label, "");
assert.strictEqual(req3.fstype, "");
assert.strictEqual(req3.uuid, "");

// --- reply parsing (fail-closed) -----------------------------------------
assert.deepStrictEqual(
    RM.parseMediaReply('{"type":"result","ok":true,"mountpoint":"/run/media/u/MYUSB","device":"/dev/sdb1"}'),
    { ok: true, mountpoint: "/run/media/u/MYUSB", device: "/dev/sdb1", error: "" }
);
assert.deepStrictEqual(
    RM.parseMediaReply('{"type":"result","ok":false,"error":"request denied"}'),
    { ok: false, mountpoint: "", device: "", error: "request denied" }
);
// malformed JSON → fail closed.
assert.strictEqual(RM.parseMediaReply("not json").ok, false);
// wrong frame type → fail closed.
assert.strictEqual(RM.parseMediaReply('{"type":"stdout","data":"x"}').ok, false);
// empty → fail closed.
assert.strictEqual(RM.parseMediaReply("").ok, false);

console.log("removable-media: all assertions passed");
