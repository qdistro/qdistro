// qdistro browser-bridge content script — Phase 9a.
//
// Detects password field focus and form submissions to trigger
// pwd.fill / pwd.save via the background service worker.
//
// Injected into all http/https pages via manifest content_scripts.
// Communicates with background.js via chrome.runtime.sendMessage.

const api = (typeof browser !== "undefined") ? browser : chrome;

// ---- state ----------------------------------------------------------

let _currentPasswordField = null;
let _fillBannerEl = null;
let _saveBannerEl = null;
let _lastFillUrl = null;    // avoid duplicate fill requests for the same page
let _credentialsFetched = false;
let _credentials = [];      // cached fill results for the current URL
let _selectedCredential = null;

// ---- password field detection ---------------------------------------

function isPasswordField(el) {
  if (!el || el.tagName !== "INPUT") return false;
  return el.type === "password";
}

function isUsernameField(el) {
  if (!el || el.tagName !== "INPUT") return false;
  if (el.type === "password" || el.type === "hidden") return false;
  const name = (el.name || el.id || el.autocomplete || "").toLowerCase();
  return /user|email|login|account|name/.test(name) ||
    el.type === "email" ||
    el.autocomplete === "username" ||
    el.autocomplete === "email";
}

// Find the username field associated with a password field by walking
// backwards through form controls or nearby siblings.
function findUsernameField(pwdField) {
  const form = pwdField.form;
  if (form) {
    const inputs = Array.from(form.querySelectorAll("input"));
    const pwdIdx = inputs.indexOf(pwdField);
    // Walk backwards from the password field
    for (let i = pwdIdx - 1; i >= 0; i--) {
      if (isUsernameField(inputs[i])) return inputs[i];
    }
    // Walk forwards as fallback
    for (let i = 0; i < inputs.length; i++) {
      if (i !== pwdIdx && isUsernameField(inputs[i])) return inputs[i];
    }
  }
  // No form — search nearby inputs
  const allInputs = document.querySelectorAll("input");
  const arr = Array.from(allInputs);
  const pwdIdx = arr.indexOf(pwdField);
  for (let i = pwdIdx - 1; i >= 0 && i >= pwdIdx - 5; i--) {
    if (isUsernameField(arr[i])) return arr[i];
  }
  return null;
}

// ---- fill banner UI -------------------------------------------------
// A small, unobtrusive bar shown below the password field offering
// to fill credentials from the qdistro vault.

function removeFillBanner() {
  if (_fillBannerEl && _fillBannerEl.parentNode) {
    _fillBannerEl.parentNode.removeChild(_fillBannerEl);
  }
  _fillBannerEl = null;
}

function createFillBanner(credentials, pwdField) {
  removeFillBanner();
  if (!credentials || !credentials.length) return;

  const banner = document.createElement("div");
  banner.id = "qdistro-fill-banner";
  banner.style.cssText = [
    "position:absolute", "z-index:2147483647",
    "background:#fff", "border:1px solid #ccc",
    "border-radius:4px", "box-shadow:0 2px 8px rgba(0,0,0,0.15)",
    "font-family:sans-serif", "font-size:13px",
    "max-width:320px", "min-width:200px",
    "padding:0", "margin:2px 0 0 0",
  ].join(";");

  const header = document.createElement("div");
  header.style.cssText = "padding:6px 10px;background:#f0f0f0;border-bottom:1px solid #ddd;font-weight:bold;font-size:11px;color:#555;";
  header.textContent = "qdistro — select credential";
  banner.appendChild(header);

  const list = document.createElement("div");
  list.style.cssText = "max-height:200px;overflow-y:auto;";

  credentials.forEach((cred) => {
    const item = document.createElement("div");
    item.style.cssText = "padding:8px 10px;cursor:pointer;border-bottom:1px solid #eee;";
    item.textContent = cred.username || "(no username)";
    item.addEventListener("mouseenter", () => { item.style.background = "#e8f0fe"; });
    item.addEventListener("mouseleave", () => { item.style.background = ""; });
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      selectCredential(cred, pwdField);
    });
    list.appendChild(item);
  });

  banner.appendChild(list);

  // Position below the password field
  const rect = pwdField.getBoundingClientRect();
  banner.style.position = "absolute";
  banner.style.left = (window.scrollX + rect.left) + "px";
  banner.style.top = (window.scrollY + rect.bottom + 2) + "px";
  document.body.appendChild(banner);
  _fillBannerEl = banner;
}

function selectCredential(cred, pwdField) {
  removeFillBanner();
  _selectedCredential = cred;
  // Request the actual password via pwd.fill_confirm (two-step fill)
  api.runtime.sendMessage({
    action: "pwd.fill_confirm",
    url: location.href,
    username: cred.username || "",
    fill_token: cred.fill_token || "",
  }, (resp) => {
    if (resp && resp.ok && resp.password) {
      fillCredentials(cred.username, resp.password, pwdField);
    } else {
      console.warn("qdistro: pwd.fill_confirm failed", resp);
    }
  });
}

function fillCredentials(username, password, pwdField) {
  // Fill the password field
  setInputValue(pwdField, password);
  // Fill the username field if found
  if (username) {
    const userField = findUsernameField(pwdField);
    if (userField) {
      setInputValue(userField, username);
    }
  }
}

function setInputValue(input, value) {
  // Use native setter to trigger framework change-detection (React, Vue, etc.)
  const nativeSetter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype, "value"
  ).set;
  nativeSetter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

// ---- fill flow on password field focus ------------------------------

function onPasswordFieldFocus(e) {
  const pwdField = e.target;
  if (!isPasswordField(pwdField)) return;
  _currentPasswordField = pwdField;

  const url = location.href;
  // Avoid re-fetching if we already have results for this URL
  if (_lastFillUrl === url && _credentialsFetched) {
    if (_credentials.length) {
      createFillBanner(_credentials, pwdField);
    }
    return;
  }

  _lastFillUrl = url;
  _credentialsFetched = false;
  _credentials = [];

  api.runtime.sendMessage({
    action: "pwd.fill",
    url: url,
  }, (resp) => {
    _credentialsFetched = true;
    if (resp && resp.ok && resp.credentials && resp.credentials.length) {
      _credentials = resp.credentials;
      // Only show if the field is still focused
      if (document.activeElement === pwdField) {
        createFillBanner(_credentials, pwdField);
      }
    }
  });
}

function onPasswordFieldBlur(_e) {
  // Delay removal so click on banner item registers
  setTimeout(removeFillBanner, 200);
}

// ---- form submission detection (save prompt) ------------------------

function removeSaveBanner() {
  if (_saveBannerEl && _saveBannerEl.parentNode) {
    _saveBannerEl.parentNode.removeChild(_saveBannerEl);
  }
  _saveBannerEl = null;
}

function showSaveBanner(url, username, password) {
  removeSaveBanner();

  const banner = document.createElement("div");
  banner.id = "qdistro-save-banner";
  banner.style.cssText = [
    "position:fixed", "top:0", "left:0", "right:0",
    "z-index:2147483647",
    "background:#1a73e8", "color:#fff",
    "font-family:sans-serif", "font-size:14px",
    "padding:10px 16px",
    "display:flex", "align-items:center", "justify-content:space-between",
    "box-shadow:0 2px 8px rgba(0,0,0,0.3)",
  ].join(";");

  const text = document.createElement("span");
  text.textContent = "Save password for " + (username || "(unknown)") + "?";
  banner.appendChild(text);

  const btnContainer = document.createElement("span");

  const saveBtn = document.createElement("button");
  saveBtn.textContent = "Save";
  saveBtn.style.cssText = "margin-left:12px;padding:5px 14px;background:#fff;color:#1a73e8;border:none;border-radius:3px;cursor:pointer;font-weight:bold;";
  saveBtn.addEventListener("click", () => {
    removeSaveBanner();
    api.runtime.sendMessage({
      action: "pwd.save",
      url: url,
      username: username,
      password: password,
    }, (_resp) => {
      // Save result is silent — the pwd daemon handles storage.
    });
  });

  const dismissBtn = document.createElement("button");
  dismissBtn.textContent = "Dismiss";
  dismissBtn.style.cssText = "margin-left:8px;padding:5px 14px;background:transparent;color:#fff;border:1px solid #fff;border-radius:3px;cursor:pointer;";
  dismissBtn.addEventListener("click", removeSaveBanner);

  btnContainer.appendChild(saveBtn);
  btnContainer.appendChild(dismissBtn);
  banner.appendChild(btnContainer);

  document.body.appendChild(banner);
  _saveBannerEl = banner;

  // Auto-dismiss after 15 seconds
  setTimeout(removeSaveBanner, 15000);
}

function onFormSubmit(e) {
  const form = e.target;
  if (!form || form.tagName !== "FORM") return;

  const pwdInputs = form.querySelectorAll('input[type="password"]');
  if (!pwdInputs.length) return;

  const pwdField = pwdInputs[0];
  const password = pwdField.value;
  if (!password) return;

  const userField = findUsernameField(pwdField);
  const username = userField ? userField.value : "";
  if (!username) return;  // Don't offer save without a username

  // Check if this credential was auto-filled from the vault —
  // if so, don't offer to re-save it.
  if (_selectedCredential && _selectedCredential.username === username) {
    return;
  }

  showSaveBanner(location.href, username, password);
}

// ---- event listeners ------------------------------------------------

document.addEventListener("focusin", (e) => {
  if (isPasswordField(e.target)) {
    onPasswordFieldFocus(e);
  }
}, true);

document.addEventListener("focusout", (e) => {
  if (isPasswordField(e.target)) {
    onPasswordFieldBlur(e);
  }
}, true);

document.addEventListener("submit", onFormSubmit, true);

// Listen for fill_credentials messages from the popup (when user picks
// a credential from the popup UI rather than the inline banner).
api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.action === "fill_credentials") {
    const pwdFields = document.querySelectorAll('input[type="password"]');
    if (pwdFields.length) {
      fillCredentials(msg.username || "", msg.password || "", pwdFields[0]);
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "no_password_field" });
    }
    return false;
  }
  return false;
});

// Also watch for dynamically-added password fields via MutationObserver
const observer = new MutationObserver((mutations) => {
  for (const m of mutations) {
    for (const node of m.addedNodes) {
      if (node.nodeType !== 1) continue;
      // Check if the added node itself is a password field
      if (isPasswordField(node) && document.activeElement === node) {
        onPasswordFieldFocus({ target: node });
      }
      // Check children
      if (node.querySelectorAll) {
        const pwdFields = node.querySelectorAll('input[type="password"]');
        for (const f of pwdFields) {
          if (document.activeElement === f) {
            onPasswordFieldFocus({ target: f });
          }
        }
      }
    }
  }
});

observer.observe(document.documentElement, {
  childList: true,
  subtree: true,
});
