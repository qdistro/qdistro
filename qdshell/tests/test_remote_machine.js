const assert = require("assert");
const fs = require("fs");
const path = require("path");
const RM = require("../Services/Qdwin/RemoteMachine.js");

// Tests for the multi-machine remote-window identity/chrome logic
// (Services/Qdwin/RemoteMachine.js), shared by RemoteMachineWindows.qml.
//
// The ORIGIN is the load-bearing identity, derived from the wp_security_context
// app_id (qdistro.mm.<origin>.<stream>) — never from window title or remote
// pixels (impl-30 Q6). Getting parsing/colour wrong means remote windows could be
// mis-attributed or the non-spoofable trust chrome painted inconsistently.

// ── isRemoteMachine ──
(function () {
  assert.strictEqual(RM.isRemoteMachine("qdistro.mm.vm-a.streamA"), true);
  assert.strictEqual(RM.isRemoteMachine("qdistro.mm."), true, "bare prefix is mm-shaped");
  assert.strictEqual(RM.isRemoteMachine("qdistro.tier4.vm-a"), false);
  assert.strictEqual(RM.isRemoteMachine("org.gnome.Files"), false);
  assert.strictEqual(RM.isRemoteMachine(""), false);
  assert.strictEqual(RM.isRemoteMachine(null), false);
  assert.strictEqual(RM.isRemoteMachine(undefined), false);
})();

// ── protected surface wiring: identity is visibly separate from title ──
(function () {
  const active = fs.readFileSync(path.join(
    __dirname, "../Modules/Bar/Widgets/ActiveWindow.qml"), "utf8");
  const service = fs.readFileSync(path.join(
    __dirname, "../Services/Qdwin/RemoteMachineWindows.qml"), "utf8");
  assert.ok(active.includes(
    "RemoteMachineWindows.badgeForHandle(Qdwin.focusedHandle)"));
  assert.ok(active.includes("id: originBadge"));
  assert.ok(active.includes("text: protectedOriginBadge"));
  assert.ok(!active.includes("text: windowTitle + protectedOriginBadge"),
    "client title cannot precede or redefine the trusted badge");
  assert.ok(service.includes("function badgeForHandle(handle)"));
  assert.ok(service.includes("RM.UNVERIFIED_BADGE"),
    "unpaired remote-shaped windows retain explicit neutral chrome");
})();

// ── originFromSecctx ──
(function () {
  assert.strictEqual(RM.originFromSecctx("qdistro.mm.vm-a.streamA"), "vm-a");
  assert.strictEqual(RM.originFromSecctx("qdistro.mm.vm-b.streamB"), "vm-b");
  // origin containing dots (e.g. a hostname) — stream is the LAST segment.
  assert.strictEqual(RM.originFromSecctx("qdistro.mm.host.example.com.s1"),
    "host.example.com");
  // fail closed: missing stream segment, bare prefix, non-mm, empty.
  assert.strictEqual(RM.originFromSecctx("qdistro.mm.vm-a"), "", "no stream → empty");
  assert.strictEqual(RM.originFromSecctx("qdistro.mm."), "", "bare prefix → empty");
  assert.strictEqual(RM.originFromSecctx("qdistro.mm.vm-a."), "", "trailing dot → empty");
  assert.strictEqual(RM.originFromSecctx("qdistro.tier4.vm-a"), "", "tier4 → empty");
  assert.strictEqual(RM.originFromSecctx(""), "");
  assert.strictEqual(RM.originFromSecctx(null), "");
})();

// ── isManagedRemote: prefix AND parseable origin+stream ──
(function () {
  assert.strictEqual(RM.isManagedRemote("qdistro.mm.vm-a.streamA"), true);
  assert.strictEqual(RM.isManagedRemote("qdistro.mm.host.example.com.s1"), true);
  // prefix-only / missing stream / trailing dot → NOT managed (falls through to
  // the qdwin compositor guard, never xdg-closed).
  assert.strictEqual(RM.isManagedRemote("qdistro.mm.foo"), false, "no stream");
  assert.strictEqual(RM.isManagedRemote("qdistro.mm.vm-a."), false, "trailing dot");
  assert.strictEqual(RM.isManagedRemote("qdistro.mm."), false, "bare prefix");
  assert.strictEqual(RM.isManagedRemote("qdistro.tier4.vm-a"), false, "tier4");
  assert.strictEqual(RM.isManagedRemote(""), false);
  assert.strictEqual(RM.isManagedRemote(null), false);
})();

// ── streamFromSecctx ──
(function () {
  assert.strictEqual(RM.streamFromSecctx("qdistro.mm.vm-a.streamA"), "streamA");
  assert.strictEqual(RM.streamFromSecctx("qdistro.mm.host.example.com.s1"), "s1");
  assert.strictEqual(RM.streamFromSecctx("qdistro.mm.vm-a"), "", "no stream → empty");
  assert.strictEqual(RM.streamFromSecctx("qdistro.mm.vm-a."), "", "trailing dot → empty");
  assert.strictEqual(RM.streamFromSecctx("org.gnome.Files"), "");
})();

// ── colourForOrigin: determinism + palette membership + distribution ──
(function () {
  var origins = ["vm-a", "vm-b", "work", "laptop", "phone"];
  origins.forEach(function (o) {
    assert.strictEqual(RM.colourForOrigin(o), RM.colourForOrigin(o),
      "colourForOrigin('" + o + "') deterministic");
    assert.ok(RM.MM_PALETTE.indexOf(RM.colourForOrigin(o)) !== -1,
      "colourForOrigin('" + o + "') in palette");
  });
  assert.strictEqual(RM.colourForOrigin(""), RM.MM_PALETTE[0], "empty → palette[0]");
  assert.strictEqual(RM.colourForOrigin(null), RM.MM_PALETTE[0], "null → palette[0]");
  var seen = new Set();
  for (var i = 0; i < 30; i++) seen.add(RM.colourForOrigin("vm" + i));
  assert.ok(seen.size > 2, "30 origins use >2 palette entries: " + seen.size);
})();

// ── broker-vouched trust-domain chrome + fail-closed reply parsing ──
(function () {
  assert.strictEqual(RM.colourForTrustDomain(""), RM.UNVERIFIED_COLOUR);
  assert.strictEqual(RM.colourForTrustDomain(null), RM.UNVERIFIED_COLOUR);
  assert.strictEqual(RM.colourForTrustDomain("owner-machines"),
    RM.colourForOrigin("owner-machines"));
  assert.strictEqual(RM.colourForTrustedOrigin("", "owner-machines"),
    RM.UNVERIFIED_COLOUR);
  assert.strictEqual(RM.colourForTrustedOrigin("vm-a", ""),
    RM.UNVERIFIED_COLOUR);
  assert.strictEqual(RM.colourForTrustedOrigin("vm-a", "owner-machines"),
    RM.colourForOrigin("owner-machines:vm-a"));
  assert.notStrictEqual(
    RM.colourForTrustedOrigin("vm-a", "owner-machines"),
    RM.colourForTrustedOrigin("vm-b", "owner-machines"),
    "two origins in one trust domain retain distinct chrome");
  assert.strictEqual(
    RM.badgeForTrustedOrigin("vm-a", "owner-machines"),
    "REMOTE vm-a @ owner-machines");
  assert.strictEqual(RM.badgeForTrustedOrigin("vm-a", ""),
    RM.UNVERIFIED_BADGE);
  assert.strictEqual(RM.badgeForTrustedOrigin("vm-a\u202e", "owner-machines"),
    RM.UNVERIFIED_BADGE, "bidi/control-shaped origin cannot enter protected chrome");
  assert.strictEqual(RM.badgeForTrustedOrigin("vm-a", "owner machines"),
    RM.UNVERIFIED_BADGE, "unstructured trust text cannot enter protected chrome");

  var identity = {
    handle: 42, origin: "vm-a", stream_id: "source-minted-a", generation: 51,
    trust_domain_id: "owner-machines", allow_input: 1
  };
  var wire = JSON.stringify({type: "s", data: [JSON.stringify(identity)]});
  assert.deepStrictEqual(RM.parseBindIdentity(wire), identity);
  ["", "not-json", JSON.stringify({data: []}),
   JSON.stringify({data: [""]}),
   JSON.stringify({data: [JSON.stringify({...identity, trust_domain_id: ""})]}),
   JSON.stringify({data: [JSON.stringify({...identity, allow_input: 2})]}),
   JSON.stringify({data: [JSON.stringify({...identity, handle: 0})]})
  ].forEach(function (bad) {
    assert.strictEqual(RM.parseBindIdentity(bad), null,
      "malformed/untrusted broker reply fails closed: " + bad);
  });
})();

// ── MM_PALETTE: valid hex ──
(function () {
  assert.ok(RM.MM_PALETTE.length >= 4);
  RM.MM_PALETTE.forEach(function (c, i) {
    assert.ok(/^#[0-9a-f]{6}$/.test(c), "palette[" + i + "] valid #rrggbb: " + c);
  });
})();

// ── hexToRgba: bit-packing + safe fallback (mirrors SiloChrome / tier4_chrome) ──
(function () {
  assert.strictEqual(RM.hexToRgba("#e53935"), 0xe53935ff >>> 0);
  assert.strictEqual(RM.hexToRgba("#ffffff"), 0xffffffff >>> 0);
  assert.strictEqual(RM.hexToRgba("#000000"), 0x000000ff >>> 0);
  assert.strictEqual((RM.hexToRgba("#1e88e5") & 0xFF), 0xFF, "alpha always 0xFF");
  // bad input → 0, never throws.
  ["", null, undefined, "#fff", "ffffff", "#gggggg", "#ff000000"].forEach(function (bad) {
    assert.strictEqual(RM.hexToRgba(bad), 0, "bad input → 0: " + bad);
  });
  // every palette colour packs to a non-zero rgba.
  RM.MM_PALETTE.forEach(function (hex) {
    assert.ok(RM.hexToRgba(hex) > 0, hex + " → non-zero rgba");
  });
})();

// ── dim + pendingColourForOrigin ──
(function () {
  assert.strictEqual(RM.dim("#ffffff", 0.5), "#808080", "white dimmed 0.5");
  assert.strictEqual(RM.dim("#000000", 0.5), "#000000", "black stays black");
  assert.strictEqual(RM.dim("#102030", 0.5), "#081018", "channels halved + padded");
  assert.strictEqual(RM.dim("bad", 0.5), "bad", "bad input unchanged");
  // pending colour is a darker variant of the origin colour, still valid hex,
  // and distinct from the bright colour (so close-pending is visible).
  var o = "vm-a";
  var bright = RM.colourForOrigin(o);
  var pending = RM.pendingColourForOrigin(o);
  assert.ok(/^#[0-9a-f]{6}$/.test(pending), "pending is valid hex: " + pending);
  assert.notStrictEqual(pending, bright, "pending differs from bright (visible)");
  assert.ok(RM.hexToRgba(pending) !== RM.hexToRgba(bright));
})();

// ── no cross-contamination with tier3/tier4 ──
(function () {
  assert.ok(!RM.isRemoteMachine("qdistro.tier3.user1"));
  assert.ok(!RM.isRemoteMachine("qdistro.tier4.vm-dev"));
  assert.strictEqual(RM.originFromSecctx("qdistro.tier4.vm-dev"), "");
})();

console.log("remote-machine: all assertions passed");
