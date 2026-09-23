// Golden request/reply frames for the qdistro bridge protocol.
//
// ============================ KEEP IN SYNC ============================
// This file is duplicated VERBATIM in the sibling extension repo:
//
//     qdchrome-extension/tests/fixtures/golden-frames.js
//     qdfirefox-extension/tests/fixtures/golden-frames.js
//
// There is no npm workspace linking the two repos, so the copies must
// be kept byte-identical by hand. Both extensions speak the SAME wire
// protocol to the native host (the bridge does not care which browser
// it is talking to), so a frame that one extension accepts/produces
// the other must too. If you edit one copy, edit the other and re-run
// `npm test` in BOTH repos.
//
// This is now machine-enforced: `tests/golden-frames-drift.test.js`
// fails if the sibling repo's copy (checked out side-by-side, or pointed
// at via $QDISTRO_SIBLING_GOLDEN) drifts from this one byte-for-byte. The
// single source of truth IS this file's bytes; the drift test is the guard.
// =====================================================================
//
// Two frame directions:
//
//   inbound  — the bridge SENDS this to the extension (a request with a
//              request_id). The extension's dispatcher routes it to a
//              registered handler and SENDS BACK `<op>.reply`. Each
//              entry pins the request frame and the shape the reply
//              must satisfy.
//
//   outbound — the extension PRODUCES this (a module function called by
//              the popup / a content script / an event listener). Each
//              entry pins the op and the fields the produced frame must
//              carry. The bridge is the consumer.
//
// The contract test (tests/contract.test.js) drives the REAL handlers
// and module functions against these frames — they are not stubs. For
// privileged outbound ops it also mints a REAL intent token via the
// extension's own intent.js (`intentOp` below) instead of a placeholder,
// so the produced frame carries a structurally-valid HMAC token bound to
// the operation — the same token the bridge's `verify_intent_token`
// checks. A frame that forwarded a bogus/absent token would fail there.
//
// @ts-check

// Canonical INBOUND requests (bridge → extension) and the assertions
// the resulting `<op>.reply` must satisfy. `replyMatch` is a partial
// object the reply must matchObject; `replyKeys` lists keys that must
// be present (value-agnostic, for env-dependent values like ids).
export const INBOUND = [
  {
    name: "tabs.list",
    request: { op: "tabs.list", request_id: "r-tabs-1" },
    replyOp: "tabs.list.reply",
    replyMatch: { ok: true },
    replyKeys: ["tabs"],
  },
  {
    name: "tabs.open",
    request: { op: "tabs.open", request_id: "r-tabs-2", url: "https://example.com/", active: true },
    replyOp: "tabs.open.reply",
    replyMatch: { ok: true },
    replyKeys: ["tab"],
  },
  {
    name: "tabs.close",
    request: { op: "tabs.close", request_id: "r-tabs-3", tab_ids: [1, 2] },
    replyOp: "tabs.close.reply",
    replyMatch: { ok: true, closed: [1, 2] },
  },
  {
    name: "tabs.open missing url is a deterministic error",
    request: { op: "tabs.open", request_id: "r-tabs-4" },
    replyOp: "tabs.open.reply",
    // Handler returns {ok:false,...}; dispatcher wraps with ok:true and
    // spreads the body, so the body's ok:false wins.
    replyMatch: { ok: false, error: "missing_url" },
  },
  {
    name: "notifications.show",
    request: {
      op: "notifications.show", request_id: "r-notif-1",
      title: "Build done", message: "qci is green", icon_url: "icons/icon-48.png",
    },
    replyOp: "notifications.show.reply",
    replyMatch: { ok: true },
    replyKeys: ["notification_id"],
  },
  {
    name: "mpris.control with no target tab is a deterministic error",
    request: { op: "mpris.control", request_id: "r-mpris-1", action: "play" },
    replyOp: "mpris.control.reply",
    replyMatch: { ok: false, action: "play", error: "no_target_tab" },
  },
  {
    name: "an unknown op is rejected, not dispatched",
    request: { op: "totally.bogus", request_id: "r-bogus-1" },
    replyOp: "totally.bogus.reply",
    replyMatch: { ok: false, error: "unknown_op" },
  },
];

// Canonical OUTBOUND frames (extension → bridge). `produce` is invoked
// (awaited) by the contract test with the loaded env; it triggers the
// real module and the produced frame is read off the fake port. `match`
// is a partial object the frame must matchObject; `keys` lists keys that
// must exist.
//
// `intentOp`: when set, the frame carries an intent token and `produce`
// mints a REAL one via `env.scope.qdistroIntent.mint(intentOp)` AND
// RETURNS it. The contract test asserts the forwarded `intent_token`
// deep-equals that returned object — i.e. the extension forwarded the
// exact token the real intent.js minted (request_id + ts + op === intentOp
// + a genuine sha256-hex hmac over the session secret), not a stub and
// not a re-fabricated look-alike the bridge would reject.
//
// `op_via`: a frame driven through a browser-event/callback path that
// differs between Chromium (callback) and Firefox (Promise); the contract
// test owns that per-browser trigger. Everything else uses `produce`.
export const OUTBOUND = [
  {
    name: "screenlock.inhibit",
    op: "screenlock.inhibit",
    produce: (env) => { void env.scope.qdistroScreenlock.inhibit("fullscreen_video"); },
    match: { op: "screenlock.inhibit", reason: "fullscreen_video" },
    keys: ["request_id"],
  },
  {
    name: "screenlock.release",
    op: "screenlock.release",
    produce: (env) => { void env.scope.qdistroScreenlock.release("fullscreen_exit"); },
    match: { op: "screenlock.release", reason: "fullscreen_exit" },
    keys: ["request_id"],
  },
  {
    name: "mpris.publish",
    op: "mpris.publish",
    produce: (env) => {
      void env.scope.qdistroMpris.update({
        title: "Song", artist: "Artist", album: "Album",
        state: "playing", position: 1.5, tab_id: 7,
      });
    },
    match: {
      op: "mpris.publish", title: "Song", artist: "Artist", album: "Album",
      playback_status: "playing", position_us: 1500000, tab_id: 7,
    },
    keys: ["request_id"],
  },
  {
    name: "pwd.fill",
    op: "pwd.fill",
    intentOp: "pwd.fill",
    produce: async (env) => {
      const token = await env.scope.qdistroIntent.mint("pwd.fill");
      void env.scope.qdistroPwd.fill("https://example.com/login", "alice", token);
      return token;
    },
    match: {
      op: "pwd.fill", url: "https://example.com/login", username: "alice",
    },
    keys: ["request_id", "intent_token"],
  },
  {
    name: "pwd.save",
    op: "pwd.save",
    intentOp: "pwd.save",
    produce: async (env) => {
      const token = await env.scope.qdistroIntent.mint("pwd.save");
      void env.scope.qdistroPwd.save("https://example.com/signup", "bob", "hunter2", token);
      return token;
    },
    match: {
      op: "pwd.save", url: "https://example.com/signup",
      username: "bob", password: "hunter2",
    },
    keys: ["request_id", "intent_token"],
  },
  {
    name: "pwd.fill_confirm",
    op: "pwd.fill_confirm",
    intentOp: "pwd.fill_confirm",
    produce: async (env) => {
      const token = await env.scope.qdistroIntent.mint("pwd.fill_confirm");
      void env.scope.qdistroPwd.fillConfirm(
        "https://example.com/login", "alice", "fill-token-gf", token);
      return token;
    },
    match: {
      op: "pwd.fill_confirm", url: "https://example.com/login",
      username: "alice", fill_token: "fill-token-gf",
    },
    keys: ["request_id", "intent_token"],
  },
  {
    name: "page.extract",
    op: "page.extract",
    intentOp: "page.extract",
    produce: async (env) => {
      const token = await env.scope.qdistroIntent.mint("page.extract");
      void env.scope.qdistroPageExtract.extract(7, "selection", token);
      return token;
    },
    match: { op: "page.extract", destination: "selection" },
    keys: ["request_id", "intent_token"],
  },
  {
    name: "downloads.notify",
    op: "downloads.notify",
    // Driven via the module's snapshot → dispatcher.request path. The
    // contract test resolves the item through the fake search.
    op_via: "downloads",
    match: { op: "downloads.notify", state: "in_progress" },
    keys: ["request_id", "download_id"],
  },
  {
    name: "cookies.export",
    op: "cookies.export",
    intentOp: "cookies.export",
    produce: async (env) => {
      const token = await env.scope.qdistroIntent.mint("cookies.export");
      void env.scope.qdistroCookies.exportForUrl("https://example.com/", token);
      return token;
    },
    match: { op: "cookies.export", url: "https://example.com/" },
    keys: ["request_id", "intent_token", "cookies"],
  },
];
