// qdistro browser-bridge popup.
//
// Single-shot: send {op:'qdistro.ping', echo:'<ts>', extension_id:'<id>'},
// render the response. Cross-browser: chrome.* with a `browser`
// fallback so the same source runs under both Chromium MV3 and
// Firefox MV2. Per spec/14 §"Phase-8 MVP scope" we don't need to
// keep the port alive — connectNative + a single sendMessage is
// enough to retire the architecture risk.

const api = (typeof browser !== "undefined") ? browser : chrome;
const HOST = "qdistro";

function setStatus(text, cls) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.className = cls || "";
}

function render(payload) {
  document.getElementById("out").textContent =
    JSON.stringify(payload, null, 2);
}

document.getElementById("ping").addEventListener("click", () => {
  setStatus("ping...", "");
  // The extension's own ID — Firefox + Chromium both expose it via
  // runtime.id.
  const ext_id = api.runtime.id || "";
  const port = api.runtime.connectNative(HOST);
  let answered = false;

  port.onMessage.addListener((msg) => {
    answered = true;
    setStatus("pong", "ok");
    render(msg);
    try { port.disconnect(); } catch (e) { /* ignore */ }
  });

  port.onDisconnect.addListener(() => {
    if (answered) return;
    const err = api.runtime.lastError;
    setStatus("disconnected", "err");
    render({
      ok: false,
      error: "disconnected",
      detail: err && err.message ? err.message : null,
    });
  });

  port.postMessage({
    op: "qdistro.ping",
    echo: String(Date.now()),
    extension_id: ext_id,
  });
});
