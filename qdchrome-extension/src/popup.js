// Popup — UI driver. Does NOT own a native-messaging port; the
// background service worker has the only port, and we drive it via
// runtime.sendMessage.
//
// This matches KDE Plasma browser integration's split: the popup is
// a UI-only surface; the background is the daemon-side.
//
// @ts-check

const api = (typeof browser !== "undefined") ? browser : chrome;

function setStatus(text, cls) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = cls || "";
}

function render(payload) {
  document.getElementById("out").textContent =
    JSON.stringify(payload, null, 2);
}

function send(kind, extra) {
  return new Promise((resolve) => {
    api.runtime.sendMessage(Object.assign({ kind }, extra || {}), (r) => {
      resolve(r || { ok: false, error: "no_response" });
    });
  });
}

async function refreshStatus() {
  const r = await send("status");
  if (r && r.ok && r.connected) setStatus("connected", "ok");
  else setStatus("disconnected", "err");
}

document.getElementById("ping").addEventListener("click", async () => {
  setStatus("ping…", "");
  const r = await send("ping");
  render(r);
  refreshStatus();
});

document.getElementById("cookies-export").addEventListener("click", async () => {
  setStatus("exporting…", "");
  // Find the active tab to scope the export.
  const tabs = await new Promise((resolve) => {
    api.tabs.query({ active: true, currentWindow: true }, (t) => resolve(t || []));
  });
  const url = tabs[0] && tabs[0].url;
  if (!url) {
    render({ ok: false, error: "no_active_tab" });
    return;
  }
  const r = await send("cookies.export", { url });
  render(r);
  refreshStatus();
});

document.getElementById("open-options").addEventListener("click", (e) => {
  e.preventDefault();
  if (api.runtime.openOptionsPage) api.runtime.openOptionsPage();
});

refreshStatus();
