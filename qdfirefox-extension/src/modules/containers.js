// containers module — Firefox contextual identities.
//
// Firefox-only. browser.contextualIdentities exposes the Multi-Account
// Containers primitive (color/icon-tagged cookie stores). There's no
// Chromium analogue, which is the reason this extension exists as a
// separate repo from qdchrome-extension instead of a build target.
//
// Bridge → extension ops:
//   - containers.list   → enumerate identities (name/color/icon/cookieStoreId)
//   - containers.create → make a new one with the given name/color/icon
//   - containers.remove → delete by cookieStoreId
//
// Extension → bridge ops:
//   - tabs.open with cookie_store_id is the consumption side — see tabs.js.
//
// Permission: `contextualIdentities` (declared in manifest).
//
// @ts-check
(function (root) {
  "use strict";
  const api = root.qdistroApi;
  const dispatcher = root.qdistroDispatcher;

  function unavailable() {
    return !api.contextualIdentities;
  }

  function serialize(c) {
    return {
      cookie_store_id: c.cookieStoreId,
      name: c.name || "",
      color: c.color || "",
      color_code: c.colorCode || "",
      icon: c.icon || "",
      icon_url: c.iconUrl || "",
    };
  }

  async function list() {
    if (unavailable()) return [];
    const ids = await api.contextualIdentities.query({});
    return (ids || []).map(serialize);
  }

  async function create(name, color, icon) {
    if (unavailable()) throw new Error("contextualIdentities_unavailable");
    const c = await api.contextualIdentities.create({
      name: String(name || "qdistro"),
      color: String(color || "blue"),
      icon: String(icon || "fingerprint"),
    });
    return serialize(c);
  }

  async function remove(cookieStoreId) {
    if (unavailable()) throw new Error("contextualIdentities_unavailable");
    const c = await api.contextualIdentities.remove(String(cookieStoreId));
    return serialize(c);
  }

  dispatcher.register("containers.list", async (_msg) => {
    if (unavailable()) {
      return { ok: false, error: "contextualIdentities_unavailable", containers: [] };
    }
    return { containers: await list() };
  });

  dispatcher.register("containers.create", async (msg) => {
    if (unavailable()) {
      return { ok: false, error: "contextualIdentities_unavailable" };
    }
    const c = await create(msg.name, msg.color, msg.icon);
    return { container: c };
  });

  dispatcher.register("containers.remove", async (msg) => {
    if (unavailable()) {
      return { ok: false, error: "contextualIdentities_unavailable" };
    }
    const id = String(msg.cookie_store_id || "");
    if (!id) return { ok: false, error: "missing_cookie_store_id" };
    const c = await remove(id);
    return { container: c };
  });

  root.qdistroContainers = { list, create, remove, serialize };
})(typeof self !== "undefined" ? self : globalThis);
