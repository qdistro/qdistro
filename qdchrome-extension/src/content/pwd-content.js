// pwd autofill content-script.
//
// Wired in manifest.json via `content_scripts` at document_idle on
// <all_urls>. Talks to the background event page via
// browser.runtime.sendMessage; never owns a native-messaging port
// (only the background does — one port per session, per spec/14).
//
// Flow:
//
//   1. Listen for an *explicit, trusted user click* on
//      <input type="password"> (event.isTrusted === true). We do NOT
//      fire on blanket keydown (that spammed a fresh request_fill per
//      keystroke and re-anchored the picker over the field the user
//      was typing into) and we do NOT fire on focus (focus can be
//      moved by page script). A bare programmatic/synthetic event is
//      ignored — a phishing page must not be able to trigger
//      credential delivery (finding #10).
//      We also suppress the request when a picker is already open for
//      the same input or the field is already non-empty, so a normal
//      type-your-password flow produces ZERO fill requests.
//   2. Send {kind: "pwd.request_fill", url, username?} to background.
//   3. Background derives the URL from sender.tab.url (rejecting any
//      page-supplied mismatch), mints an intent token, calls
//      qdistroPwd.fill, replies with credential METADATA ONLY:
//      {ok:true, credentials:[{username, url}, ...], fill_token}.
//      The rows carry NO password — the daemon's two-phase design
//      withholds the secret until phase 2 (see doc/password-manager.md).
//   4. We NEVER silently auto-fill. Even for a single match we render
//      a confirmation picker; credentials only land in the page DOM
//      after the user clicks a row (another trusted gesture). This
//      keeps the standalone extensions in line with the qdbrowser
//      model, where credential delivery requires explicit consent
//      rather than a focus event.
//   4b. PHASE 2: on that trusted row click we send
//      {kind: "pwd.request_fill_confirm", url, username, fill_token}
//      to the background, which redeems the single-use fill_token via
//      pwd.fill_confirm and returns {credentials:[{username, password,
//      url}]}. Only then does the password reach the page DOM. (The
//      pre-two-phase code read cred.password straight off the phase-1
//      row, which is always absent — it filled an empty string.)
//   5. Listen for `submit` on the surrounding <form>. If the
//      password differs from the last filled value (or we didn't
//      fill anything), send {kind: "pwd.request_save", url,
//      username, password} to background.
//
// Open items tracked in todo/03-pwd-content-script.md (cross-frame
// support, shadow-DOM picker, save-prompt UX, phishing surface).
//
// @ts-check
(function () {
  "use strict";
  const api = (typeof browser !== "undefined") ? browser : chrome;
  if (!api || !api.runtime) return;

  // Track the most recent fill so we can decide whether `submit`
  // actually carries new credentials worth saving.
  let lastFilledValue = null;
  let lastFilledFor = null; // password input element

  function log(...args) {
    try { console.debug("[qdistro/pwd-content]", ...args); } catch (_) {}
  }

  function findUsernameInput(passwordInput) {
    // The username field is typically a text/email input above the
    // password input in the same form. Walk backward through form
    // controls; pick the first text-like input that isn't the
    // password one.
    const form = passwordInput.form;
    if (!form) return null;
    const inputs = Array.from(form.elements);
    const passIdx = inputs.indexOf(passwordInput);
    for (let i = passIdx - 1; i >= 0; i--) {
      const el = inputs[i];
      if (!(el instanceof HTMLInputElement)) continue;
      const t = (el.type || "text").toLowerCase();
      if (t === "text" || t === "email" || t === "tel" || t === "" || t === "username") {
        return el;
      }
    }
    return null;
  }

  // Write the confirmed credential into the page DOM. `username` and
  // `password` come from the phase-2 pwd.request_fill_confirm reply —
  // the phase-1 picker row carries no secret.
  function fillCredential(passwordInput, username, password) {
    const usernameInput = findUsernameInput(passwordInput);
    if (usernameInput && username) {
      usernameInput.value = username;
      usernameInput.dispatchEvent(new Event("input", { bubbles: true }));
      usernameInput.dispatchEvent(new Event("change", { bubbles: true }));
    }
    passwordInput.value = password || "";
    passwordInput.dispatchEvent(new Event("input", { bubbles: true }));
    passwordInput.dispatchEvent(new Event("change", { bubbles: true }));
    lastFilledValue = passwordInput.value;
    lastFilledFor = passwordInput;
  }

  // PHASE 2: redeem the single-use fill_token for the actual password,
  // then write it into the page DOM. Runs on a trusted picker-row
  // click. The background derives the URL from sender.tab.url; we pass
  // location.href only for the mismatch check there.
  async function confirmAndFill(passwordInput, username, fillToken) {
    try {
      const resp = await api.runtime.sendMessage({
        kind: "pwd.request_fill_confirm",
        url: location.href,
        username,
        fill_token: fillToken,
      });
      if (!resp || !resp.ok) {
        log("fill_confirm rejected", resp && resp.error);
        return;
      }
      const creds = (resp.response && resp.response.credentials) || [];
      const confirmed = creds[0];
      if (!confirmed) {
        log("fill_confirm returned no credential");
        return;
      }
      // The daemon echoes the username it released the secret for;
      // refuse a mismatch rather than filling the wrong account.
      const confirmedUser = confirmed.username || "";
      if (confirmedUser !== username) {
        log("fill_confirm username mismatch", confirmedUser, username);
        return;
      }
      fillCredential(passwordInput, confirmedUser, confirmed.password || "");
    } catch (e) {
      log("fill_confirm failed", e && e.message);
    }
  }

  // The password input the picker is currently anchored to (or null).
  // Used to avoid re-requesting/re-rendering while a picker is already
  // open for the same field.
  let pickerOpenFor = null;

  function removePicker() {
    const old = document.getElementById("qdistro-pwd-picker");
    if (old) old.remove();
    pickerOpenFor = null;
  }

  function renderPicker(passwordInput, credentials, fillToken) {
    removePicker();
    pickerOpenFor = passwordInput;
    const rect = passwordInput.getBoundingClientRect();
    const box = document.createElement("div");
    box.id = "qdistro-pwd-picker";
    Object.assign(box.style, {
      position: "fixed",
      top: `${rect.bottom + 2}px`,
      left: `${rect.left}px`,
      minWidth: `${rect.width}px`,
      background: "#fff",
      color: "#000",
      border: "1px solid #888",
      borderRadius: "4px",
      boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
      font: "12px sans-serif",
      zIndex: "2147483647",
      maxHeight: "200px",
      overflow: "auto",
    });
    for (const cred of credentials) {
      const row = document.createElement("div");
      row.textContent = cred.username || "(no username)";
      Object.assign(row.style, {
        padding: "6px 10px", cursor: "pointer",
      });
      row.addEventListener("mouseenter", () => { row.style.background = "#eef"; });
      row.addEventListener("mouseleave", () => { row.style.background = "#fff"; });
      row.addEventListener("mousedown", (e) => {
        // Only a genuine user click delivers the credential into the
        // page DOM. A page-dispatched synthetic mousedown must not be
        // able to harvest it (finding #10).
        if (e.isTrusted !== true) return;
        e.preventDefault(); // don't blur the password input
        // PHASE 2: this trusted pick redeems the fill_token for the
        // actual password (async); the row carries username only.
        removePicker();
        confirmAndFill(passwordInput, cred.username || "", fillToken);
      });
      box.appendChild(row);
    }
    document.body.appendChild(box);
    // Close on click outside.
    setTimeout(() => {
      document.addEventListener("mousedown", function onOutside(e) {
        if (!box.contains(e.target)) {
          removePicker();
          document.removeEventListener("mousedown", onOutside);
        }
      });
    }, 0);
  }

  // Track the last password input the user gestured on so we can
  // anchor the picker even if a later async reply arrives.
  let lastGestureAt = 0;

  // Resolve the password input for a user-gesture event: either the
  // event target is the password input, or it sits inside the same
  // form as one (e.g. the user clicks a "show password"/icon button).
  function passwordInputForGesture(ev) {
    const el = ev.target;
    if (el instanceof HTMLInputElement && el.type === "password") return el;
    if (el instanceof Element) {
      const form = el.closest && el.closest("form");
      if (form) {
        const pw = Array.from(form.elements).find(
          (e) => e instanceof HTMLInputElement && e.type === "password");
        if (pw) return pw;
      }
    }
    return null;
  }

  // Entry point: an EXPLICIT, TRUSTED user click on/near a password
  // field. Untrusted (script-dispatched) events are ignored — a page
  // must not be able to provoke credential delivery without a real
  // user action. We request credentials but never auto-fill; the user
  // must confirm by clicking a picker row.
  async function onPasswordGesture(ev) {
    if (!ev || ev.isTrusted !== true) return; // reject synthetic events
    const el = passwordInputForGesture(ev);
    if (!el) return;
    // Don't re-request / re-render while a picker is already open for
    // this same input — otherwise the picker would flicker over the
    // field on every interaction (finding #10 follow-up).
    if (pickerOpenFor === el && document.getElementById("qdistro-pwd-picker")) {
      return;
    }
    // The user is offering to *fill* an empty field. If the field is
    // already non-empty (e.g. they are typing a password by hand, or
    // already chose a credential), do nothing: no fill request, no
    // picker over the text they're entering.
    if (el.value) return;
    // Throttle: one in-flight request per gesture burst.
    const now = Date.now();
    if (now - lastGestureAt < 250) return;
    lastGestureAt = now;
    log("password gesture", location.href);
    try {
      const usernameInput = findUsernameInput(el);
      const resp = await api.runtime.sendMessage({
        kind: "pwd.request_fill",
        url: location.href,
        username: usernameInput ? usernameInput.value || null : null,
      });
      if (!resp || !resp.ok) return;
      const creds = (resp.response && resp.response.credentials) || [];
      if (creds.length === 0) return;
      // The fill_token gates phase 2 (pwd.request_fill_confirm). With
      // no token the picker would be useless — the daemon could never
      // release a password — so bail rather than render dead rows.
      const fillToken = resp.response && resp.response.fill_token;
      if (!fillToken || typeof fillToken !== "string") {
        log("fill reply missing fill_token");
        return;
      }
      // SECURITY: never silently auto-fill, even for a single match.
      // A confirmation picker keeps credential delivery behind an
      // explicit user click (finding #10).
      renderPicker(el, creds, fillToken);
    } catch (e) {
      log("fill failed", e && e.message);
    }
  }

  function onSubmit(ev) {
    // Gesture gate (finding #10): only a TRUSTED submit (a real
    // click on the submit button, Enter in a field, or the browser's
    // own form submission) may offer to save a credential. A page can
    // call form.dispatchEvent(new Event("submit")) or form.submit()
    // with attacker-chosen username/password; honoring that would let
    // it silently poison the user's credential store for the REAL
    // origin. Reject synthetic submits.
    if (!ev || ev.isTrusted !== true) return;
    const form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    const passwordInput = Array.from(form.elements).find(
      (el) => el instanceof HTMLInputElement && el.type === "password");
    if (!passwordInput || !passwordInput.value) return;
    // Only save if the value differs from what we filled (or we
    // never filled anything in this input). Otherwise the bridge
    // would see a save request every successful login.
    if (lastFilledFor === passwordInput && lastFilledValue === passwordInput.value) {
      return;
    }
    const usernameInput = findUsernameInput(passwordInput);
    const payload = {
      kind: "pwd.request_save",
      url: location.href,
      username: usernameInput ? usernameInput.value || null : null,
      password: passwordInput.value,
    };
    api.runtime.sendMessage(payload).catch((e) => {
      log("save failed", e && e.message);
    });
  }

  // Trigger on an explicit, real user CLICK only. We deliberately do
  // NOT listen for:
  //   - `focus`: focus can be moved programmatically by page script,
  //     which would let a phishing page provoke credential delivery
  //     without any user action (finding #10).
  //   - `keydown`: a blanket keydown listener fired a fresh
  //     pwd.request_fill on every keystroke into the field and
  //     re-rendered the picker over the text the user was typing.
  //     Normal typing must produce ZERO fill requests (finding #10
  //     follow-up). A click on the (empty) field is the explicit
  //     intent to autofill.
  document.addEventListener("click", onPasswordGesture, true);
  document.addEventListener("submit", onSubmit, true);
  log("pwd content-script loaded");
})();
