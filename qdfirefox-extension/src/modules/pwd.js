// pwd module.
//
// The pwd autofill is a TWO-PHASE flow by design (see
// doc/password-manager.md, user-intent attestation):
//
//   - pwd.fill (phase 1): on a trusted user gesture on an
//     <input type="password">, a content script messages the
//     background which calls
//     qdistroDispatcher.request("pwd.fill", {url, username?, intent_token}).
//     The daemon replies with credential METADATA ONLY —
//     `{credentials: [{username, url}, ...], fill_token}`. No password
//     is released yet.
//
//   - pwd.fill_confirm (phase 2): after the user PICKS one credential
//     from the metadata list (another trusted gesture), the content
//     script asks for the password via
//     qdistroDispatcher.request("pwd.fill_confirm", {url, username,
//     fill_token, intent_token}). The daemon validates the single-use
//     fill_token (origin/username/peer-bound) and only then returns
//     `{credentials: [{username, password, url}]}`. Calling `fill`
//     alone yields rows with NO `password` — the second phase is
//     mandatory.
//
//   - pwd.save: on form submit with new credentials, same path with
//     `{url, username, password, intent_token}`.
//
// No bridge-initiated direction — fills are always user-initiated.
// Intent token is opaque here; intent.js mints it.
//
// @ts-check
(function (root) {
  "use strict";
  const dispatcher = root.qdistroDispatcher;

  async function fill(url, username, intentToken) {
    return await dispatcher.request("pwd.fill", {
      url, username: username || null, intent_token: intentToken,
    });
  }

  async function fillConfirm(url, username, fillToken, intentToken) {
    return await dispatcher.request("pwd.fill_confirm", {
      url, username, fill_token: fillToken, intent_token: intentToken,
    });
  }

  async function save(url, username, password, intentToken) {
    return await dispatcher.request("pwd.save", {
      url, username, password, intent_token: intentToken,
    });
  }

  root.qdistroPwd = { fill, fillConfirm, save };
})(typeof self !== "undefined" ? self : globalThis);
