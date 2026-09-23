// Popup — UI driver. Does NOT own a native-messaging port; the
// background event page has the only port, and we drive it via
// runtime.sendMessage. browser.* returns promises from
// runtime.sendMessage in Firefox, so no callback dance.
//
// @ts-check

const api = (typeof browser !== "undefined") ? browser : chrome;

function $(id) { return document.getElementById(id); }

function setStatus(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = cls || "";
}

function render(payload) {
  $("out").textContent = JSON.stringify(payload, null, 2);
}

async function send(kind, extra) {
  try {
    const r = await api.runtime.sendMessage(Object.assign({ kind }, extra || {}));
    return r || { ok: false, error: "no_response" };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

async function refreshStatus() {
  const r = await send("status");
  if (r && r.ok && r.connected) setStatus("connected", "ok");
  else setStatus("disconnected", "err");
}

async function loadContainers() {
  const sel = $("container-select");
  if (!api.contextualIdentities) return;
  try {
    const ids = await api.contextualIdentities.query({});
    for (const id of ids || []) {
      const opt = document.createElement("option");
      opt.value = id.cookieStoreId;
      opt.textContent = `${id.name} (${id.color})`;
      sel.appendChild(opt);
    }
  } catch (_) {
    /* permission denied or unavailable — leave the default option */
  }
}

$("ping").addEventListener("click", async () => {
  setStatus("ping…", "");
  const r = await send("ping");
  render(r);
  refreshStatus();
});

$("cookies-export").addEventListener("click", async () => {
  setStatus("exporting…", "");
  const tabs = await api.tabs.query({ active: true, currentWindow: true });
  const url = tabs && tabs[0] && tabs[0].url;
  if (!url) {
    render({ ok: false, error: "no_active_tab" });
    return;
  }
  const cookieStoreId = $("container-select").value || null;
  const r = await send("cookies.export", { url, cookie_store_id: cookieStoreId });
  render(r);
  refreshStatus();
});

$("open-options").addEventListener("click", (e) => {
  e.preventDefault();
  if (api.runtime.openOptionsPage) api.runtime.openOptionsPage();
});

refreshStatus();
loadContainers();
