// qdistro browser-bridge popup — Phase 9a/9b.
//
// Communicates with background.js via chrome.runtime.sendMessage.
// Shows bridge connection status, credential picker for the current
// tab's URL, and a manual save-credential form.
//
// Cross-browser: chrome.* with `browser` fallback.

const api = (typeof browser !== "undefined") ? browser : chrome;

// ---- DOM refs -------------------------------------------------------

const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");

const credSection = document.getElementById("cred-section");
const credList = document.getElementById("cred-list");
const credMsg = document.getElementById("cred-msg");

const saveSection = document.getElementById("save-section");
const saveUrlInput = document.getElementById("save-url");
const saveUsernameInput = document.getElementById("save-username");
const savePasswordInput = document.getElementById("save-password");
const saveBtn = document.getElementById("save-btn");
const saveMsg = document.getElementById("save-msg");

const outPre = document.getElementById("out");
const pingBtn = document.getElementById("ping");
const fillBtn = document.getElementById("fill-btn");
const showSaveBtn = document.getElementById("show-save");
const toggleDebugBtn = document.getElementById("toggle-debug");

let _currentTabUrl = "";

// ---- helpers --------------------------------------------------------

function setStatus(text, cls) {
  statusText.textContent = text;
  statusDot.className = "status-dot " + (cls || "pending");
}

function showMsg(el, text, cls) {
  el.textContent = text;
  el.className = "msg " + (cls || "info");
  el.classList.remove("hidden");
}

function hideMsg(el) {
  el.classList.add("hidden");
}

function render(payload) {
  outPre.textContent = JSON.stringify(payload, null, 2);
}

function sendToBackground(msg) {
  const isFirefox = (typeof browser !== "undefined");
  if (isFirefox) {
    // Firefox: browser.runtime.sendMessage returns a Promise
    return api.runtime.sendMessage(msg).then(
      (resp) => resp || { ok: false, error: "no_response" },
      (_err) => ({ ok: false, error: "no_response" })
    );
  }
  // Chrome: callback-based
  return new Promise((resolve) => {
    api.runtime.sendMessage(msg, (resp) => {
      resolve(resp || { ok: false, error: "no_response" });
    });
  });
}

// ---- status check ---------------------------------------------------

async function checkStatus() {
  const resp = await sendToBackground({ action: "get_status" });
  if (resp.connected && resp.handshakeComplete) {
    setStatus("connected", "ok");
  } else if (resp.connected) {
    setStatus("connected (handshake pending)", "pending");
  } else {
    setStatus("disconnected", "err");
  }
}

// ---- credential fill ------------------------------------------------

async function fillForCurrentTab() {
  credSection.classList.remove("hidden");
  credList.innerHTML = "";
  showMsg(credMsg, "fetching...", "info");

  const resp = await sendToBackground({
    action: "pwd.fill",
    url: _currentTabUrl,
  });

  render(resp);

  if (!resp.ok) {
    showMsg(credMsg, resp.error || "fill failed", "err");
    return;
  }

  const creds = resp.credentials || [];
  if (!creds.length) {
    showMsg(credMsg, "no saved credentials for this site", "info");
    return;
  }

  hideMsg(credMsg);
  creds.forEach((cred) => {
    const item = document.createElement("div");
    item.className = "cred-item";
    const user = document.createElement("div");
    user.className = "username";
    user.textContent = cred.username || "(no username)";
    const domain = document.createElement("div");
    domain.className = "domain";
    domain.textContent = cred.domain || _currentTabUrl;
    item.appendChild(user);
    item.appendChild(domain);
    item.addEventListener("click", () => {
      fillCredential(cred);
    });
    credList.appendChild(item);
  });
}

async function fillCredential(cred) {
  showMsg(credMsg, "filling...", "info");

  // Two-step: request the actual password via fill_confirm
  const resp = await sendToBackground({
    action: "pwd.fill_confirm",
    url: _currentTabUrl,
    username: cred.username || "",
    fill_token: cred.fill_token || "",
  });

  render(resp);

  if (!resp.ok) {
    showMsg(credMsg, resp.error || "fill_confirm failed", "err");
    return;
  }

  // Send fill command to the content script via the active tab
  const tabs = await api.tabs.query({ active: true, currentWindow: true });
  if (tabs.length) {
    try {
      await api.tabs.sendMessage(tabs[0].id, {
        action: "fill_credentials",
        username: cred.username || "",
        password: resp.password || "",
      });
      showMsg(credMsg, "filled!", "ok");
    } catch (e) {
      // Content script might not be injected on this page
      showMsg(credMsg, "could not fill (content script unavailable)", "err");
    }
  }
}

// ---- save credential ------------------------------------------------

function showSaveForm() {
  saveSection.classList.remove("hidden");
  saveUrlInput.value = _currentTabUrl;
  saveUsernameInput.value = "";
  savePasswordInput.value = "";
  hideMsg(saveMsg);
  saveUsernameInput.focus();
}

async function doSave() {
  const url = saveUrlInput.value;
  const username = saveUsernameInput.value.trim();
  const password = savePasswordInput.value;
  if (!username || !password) {
    showMsg(saveMsg, "username and password required", "err");
    return;
  }
  showMsg(saveMsg, "saving...", "info");
  const resp = await sendToBackground({
    action: "pwd.save",
    url: url,
    username: username,
    password: password,
  });
  render(resp);
  if (resp.ok) {
    showMsg(saveMsg, "saved!", "ok");
    savePasswordInput.value = "";
    // Dismiss the pending save offer if we just saved it
    await sendToBackground({ action: "dismiss_pending_save" });
  } else {
    showMsg(saveMsg, resp.error || "save failed", "err");
  }
}

async function checkPendingSave() {
  const resp = await sendToBackground({ action: "get_pending_save" });
  if (resp.offer && resp.offer.url) {
    saveSection.classList.remove("hidden");
    saveUrlInput.value = resp.offer.url;
    saveUsernameInput.value = resp.offer.username || "";
    savePasswordInput.value = resp.offer.password || "";
    showMsg(saveMsg, "new credentials detected — save to vault?", "info");
  }
}

// ---- ping -----------------------------------------------------------

async function doPing() {
  setStatus("pinging...", "pending");
  const resp = await sendToBackground({ action: "ping" });
  render(resp);
  if (resp.ok || resp.pong) {
    setStatus("pong", "ok");
  } else {
    setStatus(resp.error || "ping failed", "err");
  }
}

// ---- debug toggle ---------------------------------------------------

function toggleDebug() {
  outPre.classList.toggle("show");
}

// ---- content script message listener --------------------------------
// The content script can also ask the popup to refresh via runtime.onMessage
// if we ever need to. For now the popup drives the flow.

// Listen for fill_credentials messages from popup to content script.
// This listener is in the popup context — we also need the content script
// to handle it. See content.js.
api.runtime.onMessage.addListener((msg, _sender, _sendResponse) => {
  if (msg && msg.action === "fill_credentials") {
    // This is handled by content.js, not the popup.
    return false;
  }
  return false;
});

// ---- init -----------------------------------------------------------

async function init() {
  // Get current tab URL
  try {
    const tabs = await api.tabs.query({ active: true, currentWindow: true });
    if (tabs.length) {
      _currentTabUrl = tabs[0].url || "";
    }
  } catch (e) {
    _currentTabUrl = "";
  }

  checkStatus();
  checkPendingSave();

  // If we're on an http/https page, show the fill button prominently
  if (_currentTabUrl.startsWith("http://") || _currentTabUrl.startsWith("https://")) {
    fillBtn.style.display = "";
    showSaveBtn.style.display = "";
  } else {
    fillBtn.style.display = "none";
    showSaveBtn.style.display = "none";
  }
}

// ---- event listeners ------------------------------------------------

pingBtn.addEventListener("click", doPing);
fillBtn.addEventListener("click", fillForCurrentTab);
showSaveBtn.addEventListener("click", showSaveForm);
saveBtn.addEventListener("click", doSave);
toggleDebugBtn.addEventListener("click", toggleDebug);

init();
