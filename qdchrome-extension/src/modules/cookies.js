// cookies module — 9d.
//
// Extension-initiated only. User clicks "Export session" in the
// popup → popup calls runtime.sendMessage with a request to the
// background → background mints an intent token, reads cookies for
// the current tab's URL, then forwards `cookies.export` to the
// bridge.
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

  function getAllForUrl(url) {
    return new Promise((resolve, reject) => {
      if (!api.cookies || !api.cookies.getAll) {
        reject(new Error("cookies_api_unavailable"));
        return;
      }
      api.cookies.getAll({ url }, (cookies) => {
        const err = api.runtime.lastError;
        if (err) return reject(new Error(err.message));
        resolve(cookies || []);
      });
    });
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
    };
  }

  async function exportForUrl(url, intentToken) {
    if (!intentToken) throw new Error("intent_token_required");
    const cookies = await getAllForUrl(url);
    return await dispatcher.request("cookies.export", {
      url,
      intent_token: intentToken,
      cookies: cookies.map(serialize),
    }, { timeoutMs: 15000 });
  }

  root.qdistroCookies = { exportForUrl, getAllForUrl, serialize };
})(typeof self !== "undefined" ? self : globalThis);
