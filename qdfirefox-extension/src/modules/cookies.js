// cookies module.
//
// Extension-initiated only. User clicks "Export session" in the popup
// → popup runtime.sendMessage → background → mint intent token, read
// cookies for the current tab's URL → forward `cookies.export` to
// the bridge.
//
// Firefox cookies API differs from Chromium in two relevant ways:
//
//   - First-party isolation (privacy.firstparty.isolate): when enabled,
//     callers must pass firstPartyDomain explicitly or use null to
//     match all. We pass null so the export covers everything.
//
//   - Per-container cookies: callers can pass `storeId` (a.k.a.
//     cookieStoreId) to scope to a contextual identity. We accept an
//     optional cookie_store_id on the wire and forward it.
//
// Intent token (5s TTL) is required. The bridge's
// _handle_cookies_export verifies the token via the daemon and
// audit-logs the event.
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const dispatcher = root.qdistroDispatcher;

  async function getAllForUrl(url, opts) {
    if (!api.cookies || !api.cookies.getAll) {
      throw new Error("cookies_api_unavailable");
    }
    const query = { url, firstPartyDomain: null };
    if (opts && opts.cookieStoreId) query.storeId = String(opts.cookieStoreId);
    return await api.cookies.getAll(query);
  }

  function serialize(c) {
    return {
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      secure: !!c.secure,
      http_only: !!c.httpOnly,
      same_site: c.sameSite || "no_restriction",
      expires: typeof c.expirationDate === "number"
        ? Math.floor(c.expirationDate) : null,
      session: !!c.session,
      store_id: c.storeId || null,
      first_party_domain: c.firstPartyDomain || null,
    };
  }

  async function exportForUrl(url, intentToken, opts) {
    if (!intentToken) throw new Error("intent_token_required");
    const cookies = await getAllForUrl(url, opts || {});
    return await dispatcher.request("cookies.export", {
      url,
      intent_token: intentToken,
      cookie_store_id: (opts && opts.cookieStoreId) || null,
      cookies: cookies.map(serialize),
    }, { timeoutMs: 15000 });
  }

  root.qdistroCookies = { exportForUrl, getAllForUrl, serialize };
})(typeof self !== "undefined" ? self : globalThis);
