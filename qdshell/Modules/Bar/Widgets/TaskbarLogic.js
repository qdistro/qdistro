// Pure taskbar grouping / sort logic, extracted from Taskbar.qml so it can be
// unit-tested under Node (see tests/test_taskbar_logic.js) while still being
// imported from QML (`import "TaskbarLogic.js" as TaskbarLogic`).
//
// Everything here operates ONLY on a plain entries array plus plain
// settings/value arguments. There is NO access to the Qdwin / Process /
// Settings / Style singletons — the QML side reads those and passes the
// resulting primitives in. This keeps the functions deterministic and
// testable, and keeps the no-duplication / empty-appId guards in one place.

// Normalize an app id for case-insensitive matching. Empty/missing/non-string
// ids collapse to "" — callers MUST treat "" as "no group key" so that
// unrelated windows with a missing appId are never lumped together.
function normalizeAppId(appId) {
  if (!appId || typeof appId !== "string")
    return "";
  return appId.toLowerCase().trim();
}

// Decide whether grouping should be active right now, given a plain options
// bag (no singletons). Mirrors the QML shouldGroup():
//   - "always" -> on
//   - "never"  -> off
//   - "limited" -> on only when the ungrouped button count would overflow the
//     width budget. Only meaningful on a horizontal bar with a positive width
//     cap; otherwise off.
// opts: { groupingMode, isVerticalBar, maxTaskbarWidth, showTitle, itemSize,
//         titleWidth, marginS, marginXL }
function shouldGroup(entryCount, opts) {
  opts = opts || {};
  if (opts.groupingMode === "always")
    return true;
  if (opts.groupingMode === "limited") {
    if (opts.isVerticalBar || !(opts.maxTaskbarWidth > 0))
      return false;
    var itemSize = opts.itemSize || 0;
    var marginS = opts.marginS || 0;
    var marginXL = opts.marginXL || 0;
    var titleWidth = opts.titleWidth || 0;
    // Same estimate as the delegate's Layout.preferredWidth so "fits" matches
    // the real layout: with titles a button is itemSize + spacing + titleWidth
    // + margins; without titles it is itemSize + margins.
    var perEntry = opts.showTitle ? (itemSize + marginS + titleWidth + marginXL) : (itemSize + marginXL);
    if (!(perEntry > 0))
      return false;
    var fits = Math.max(1, Math.floor(opts.maxTaskbarWidth / perEntry));
    return entryCount > fits;
  }
  return false;
}

// Is this entry a running window button (the only thing we ever group)?
// Pinned-not-running and cold-start placeholder entries are never grouped.
function isRunningWindowEntry(e) {
  return !!(e && e.window && (e.type === "running" || e.type === "pinned-running"));
}

// Collapse entries that share a normalized appId into a single group entry,
// preserving the ORIGINAL order and emitting each entry exactly once.
//
// Guards:
//   - Windows with an empty/missing appId (normalizeAppId -> "") are NEVER
//     grouped: each stays its own individual button.
//   - Non-running entries (pinned-only / placeholder) pass through untouched.
//   - A group with a single window collapses back to the plain entry so it
//     keeps the normal single-window code paths.
//   - Each group is emitted once, at the slot of its first member (regression
//     guard: an earlier version duplicated grouped entries).
function groupApps(entries) {
  entries = entries || [];
  // Null-prototype maps so app ids like "toString" / "__proto__" / "hasOwnProperty"
  // are treated as ordinary keys and never collide with Object.prototype members
  // (which would corrupt grouping or suppress emission).
  var groups = Object.create(null);

  // First pass: accumulate members per app key.
  entries.forEach(function (e) {
    if (!isRunningWindowEntry(e))
      return;
    var key = normalizeAppId(e.appId);
    if (key === "")
      return;
    if (!groups[key]) {
      groups[key] = {
        "id": "group:" + key,
        "type": e.type,
        "window": e.window,
        "appId": e.appId,
        "title": e.title,
        "isGroup": true,
        "windows": [e.window],
        "windowEntries": [e]
      };
    } else {
      var g = groups[key];
      g.windows.push(e.window);
      g.windowEntries.push(e);
      // Prefer the focused window for the representative title/icon.
      if (e.window && e.window.isFocused) {
        g.window = e.window;
        g.title = e.title;
      }
      if (e.type === "pinned-running")
        g.type = "pinned-running";
    }
  });

  // Second pass: emit in original order, each entry exactly once.
  var result = [];
  var emittedGroups = Object.create(null);
  entries.forEach(function (e) {
    if (!isRunningWindowEntry(e)) {
      result.push(e);
      return;
    }
    var key = normalizeAppId(e.appId);
    var g = (key !== "") ? groups[key] : null;
    if (!g) {
      // Empty/missing appId — never grouped, emit as an individual button.
      result.push(e);
      return;
    }
    if (emittedGroups[key])
      return;
    emittedGroups[key] = true;
    if (g.windows.length === 1)
      result.push(g.windowEntries[0]);
    else
      result.push(g);
  });
  return result;
}

// Apply the configured sort order. "none" preserves the launch/session order
// (handled by the caller, so we return the entries unchanged here); "title"
// sorts by visible title; "group" sorts by appId then title. A non-mutating
// copy is returned for the sorting modes.
function applySortMode(entries, sortMode) {
  entries = entries || [];
  if (sortMode === "title") {
    return entries.slice().sort(function (a, b) {
      return ((a && a.title) || "").toLowerCase().localeCompare(((b && b.title) || "").toLowerCase());
    });
  }
  if (sortMode === "group") {
    return entries.slice().sort(function (a, b) {
      var ka = normalizeAppId(a && a.appId);
      var kb = normalizeAppId(b && b.appId);
      if (ka !== kb)
        return ka.localeCompare(kb);
      return ((a && a.title) || "").toLowerCase().localeCompare(((b && b.title) || "").toLowerCase());
    });
  }
  // "none" — preserve session/launch order.
  return entries;
}

// --- qdistro isolation menu (D16 v1) -------------------------------------
// A per-window "qdistro" section for the taskbar context menu: shows the
// window's silo identity (silo, isolation tier, secctx) and offers
// snapshot / dispose / permissions actions. Pure: identity in, menu-model
// rows out — the QML side reads Qdwin window fields and passes the
// primitives, and dispatches the returned actions.

// Stable tier KEY from the secctx identity — a short, language-independent
// token the QML side maps to an I18n key (bar.taskbar.isolation.tier-<key>)
// for display. The app_id / sandbox_engine prefix encodes the tier (see
// doc/isolation-tiers.md and the secctx contract): qdistro.disp.<token> = a
// tier-2 disposable; qdistro.tier4.<vm> = per-app VM; qdistro.tier3.<silo> =
// waypipe VM app; qdistro.tier2 = rootless container. An empty identity is a
// native window.
function siloTierKey(secctxAppId, sandboxEngine) {
  var id = (secctxAppId || "") + "";
  var eng = (sandboxEngine || "") + "";
  if (id.indexOf("qdistro.disp.") === 0)
    return "disposable";
  if (id.indexOf("qdistro.tier5.") === 0 || eng.indexOf("qdistro.tier5") === 0)
    return "tier5";
  if (id.indexOf("qdistro.tier4.") === 0 || eng.indexOf("qdistro.tier4") === 0)
    return "tier4";
  if (id.indexOf("qdistro.tier3.") === 0 || eng.indexOf("qdistro.tier3") === 0)
    return "tier3";
  if (eng.indexOf("qdistro.tier2") === 0 || id.indexOf("qdistro.tier2") === 0)
    return "tier2";
  if (!id && !eng)
    return "native";
  return "sandboxed";
}

// English fallback labels per tier key. Used by siloTierLabel() (and the JS
// unit tests) so a node-side caller without I18n still gets a readable string;
// the QML side prefers the I18n key (bar.taskbar.isolation.tier-<key>).
var _TIER_LABELS_EN = {
  "disposable": "disposable (tier 2)",
  "tier5": "tier 5 (VM)",
  "tier4": "tier 4 (VM)",
  "tier3": "tier 3 (VM app)",
  "tier2": "tier 2 (container)",
  "native": "native (tier 0/1)",
  "sandboxed": "sandboxed",
};

// Human-readable isolation tier from the secctx identity (English fallback).
// The canonical signal is siloTierKey(); this wraps it with the English label
// table so non-i18n callers (and the existing JS tests) still get a string.
function siloTierLabel(secctxAppId, sandboxEngine) {
  var key = siloTierKey(secctxAppId, sandboxEngine);
  return _TIER_LABELS_EN[key] || _TIER_LABELS_EN.sandboxed;
}

// Resolve the Snapper CONFIG for a window's silo, or report it as not
// snapshottable. The broker's SnapshotBefore(config, description) takes a
// SNAPPER CONFIG NAME (per-user-home / per-silo config), NOT a qdshell silo
// label — so we map only the case we can map cleanly:
//
//   * Persistent tier-2 container: the derived silo is "tier2/<name>"; the
//     Snapper config is <name> (the per-silo / per-user-home config). We strip
//     exactly one "tier2/" prefix and use <name>.
//
// Everything else is NOT offered a host-Snapper snapshot:
//   * Disposables (qdistro.disp.<token>): tmpfs /home + --rm — no durable
//     subvolume to snapshot; "Dispose" is the right action instead.
//   * VM tiers (tier3/4/5): the real story is a VM disk/state snapshot, not a
//     host Snapper config by bare name — passing a bare name could snapshot an
//     unrelated config that happens to share it. Deferred to a qdistro-owned
//     VM snapshot surface (forward item).
//   * Native (tier 0/1): no isolation identity at all.
//
// The returned config is validated to a strict config-name shape
// (^[A-Za-z0-9][A-Za-z0-9._-]*$, no '/', no leading '-') so a non-normalised or
// surprising silo label can never reach the broker as a config. Returns
// { snapshottable: bool, config: string }; config is "" when not snapshottable.
var _SNAP_CONFIG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
function snapshotConfigForWindow(identity) {
  identity = identity || {};
  // Disposables are never snapshottable (ephemeral home).
  if (isDisposableWindow(identity))
    return { "snapshottable": false, "config": "" };
  var key = siloTierKey(identity.secctxAppId, identity.sandboxEngine);
  // Only persistent tier-2 containers map to a host Snapper config.
  if (key !== "tier2")
    return { "snapshottable": false, "config": "" };
  var silo = (identity.silo || "") + "";
  // The persistent tier-2 silo is "tier2/<name>"; <name> is the config.
  if (silo.indexOf("tier2/") !== 0)
    return { "snapshottable": false, "config": "" };
  var config = silo.slice("tier2/".length);
  // Reject anything that still carries a '/' or fails the config-name shape:
  // a clean per-silo config is a simple name, and a residual '/' means we did
  // not actually normalise a tier-2 label.
  if (!config || config.indexOf("/") !== -1 || !_SNAP_CONFIG_RE.test(config))
    return { "snapshottable": false, "config": "" };
  return { "snapshottable": true, "config": config };
}

// A window is a disposable iff its secctx app_id is qdistro.disp.<token>.
// That is the authoritative, host-assigned signal (the same one the broker
// gates on). We deliberately do NOT also match on a "disp-" silo name: the
// derived silo for a disposable is "tier2/qdistro.disp.<token>", never a bare
// "disp-…", so a silo-name check would be dead code AND could false-positive
// on a persistent silo that merely happens to be named "disp-something".
function isDisposableWindow(identity) {
  if (!identity)
    return false;
  var id = (identity.secctxAppId || "") + "";
  return id.indexOf("qdistro.disp.") === 0;
}

// Build the qdistro section of the taskbar context menu for one window's
// identity. Returns [] for a native window (no secctx identity at all) so
// the menu is UNCHANGED for non-silo apps. Identity rows are disabled
// (informational, "show identity"); snapshot / dispose / permissions are
// live actions the QML onTriggered handler dispatches. `dispose` only
// appears for disposable windows; `snapshot` only for windows whose silo maps
// to a real Snapper config (persistent tier-2 — see snapshotConfigForWindow).
//
// i18n: each row carries BOTH a `label` (English fallback, so node-side callers
// and the existing JS tests still get a readable string) AND a `labelKey` plus
// optional `labelParams` for the QML side to resolve via I18n.tr(labelKey,
// labelParams). The identity rows interpolate the silo / tier / secctx VALUES
// as params so a translator only ever localises the surrounding text.
function buildIsolationMenuItems(identity) {
  identity = identity || {};
  var secctx = (identity.secctxAppId || "") + "";
  var engine = (identity.sandboxEngine || "") + "";
  var silo = (identity.silo || "") + "";
  // Native window: no isolation identity -> no qdistro section.
  if (!secctx && !engine && !silo)
    return [];
  var tierKey = siloTierKey(secctx, engine);
  var tier = siloTierLabel(secctx, engine);
  var siloName = silo || "(unnamed)";
  var disposable = isDisposableWindow(identity);
  var snap = snapshotConfigForWindow(identity);
  var items = [];
  // Identity (disabled, informational rows). labelKey carries the localisable
  // template; labelParams carry the raw identity values to interpolate.
  items.push({ "label": "qdistro silo", "action": "qd-header",
               "icon": "shield", "enabled": false, "isQdistro": true,
               "labelKey": "bar.taskbar.isolation.header" });
  items.push({ "label": "Silo: " + siloName,
               "action": "qd-id-silo", "enabled": false, "isQdistro": true,
               "labelKey": "bar.taskbar.isolation.silo",
               "labelParams": { "silo": siloName } });
  items.push({ "label": "Isolation: " + tier,
               "action": "qd-id-tier", "enabled": false, "isQdistro": true,
               "labelKey": "bar.taskbar.isolation.tier",
               // The tier itself is a localisable enum (tier-<key>); the QML
               // side resolves the inner key first, then the outer template.
               "tierKey": tierKey,
               "labelParams": { "tier": tier } });
  if (secctx)
    items.push({ "label": "Context: " + secctx, "action": "qd-id-secctx",
                 "enabled": false, "isQdistro": true,
                 "labelKey": "bar.taskbar.isolation.context",
                 "labelParams": { "context": secctx } });
  // Actions. Snapshot only when the silo maps to a real Snapper config.
  if (snap.snapshottable)
    items.push({ "label": "Snapshot now", "action": "qd-snapshot",
                 "icon": "camera", "isQdistro": true,
                 "labelKey": "bar.taskbar.isolation.snapshot",
                 "snapConfig": snap.config });
  if (disposable)
    items.push({ "label": "Dispose", "action": "qd-dispose",
                 "icon": "trash-2", "isQdistro": true,
                 "labelKey": "bar.taskbar.isolation.dispose" });
  items.push({ "label": "Permissions…", "action": "qd-permissions",
               "icon": "lock", "isQdistro": true,
               "labelKey": "bar.taskbar.isolation.permissions" });
  return items;
}

// Decide HOW the taskbar should dispose a window when the user picks
// "Dispose". A disposable window whose `instanceId` carries a well-formed
// launch token (== the container's `qdistro_tier2_token` label, the spawn-time
// LAUNCH_TOKEN — NOT the independent random hex inside the secctx app_id) is
// torn down by token: qdshell asks the session manager to resolve the token to
// its container and remove it (an explicit, admin-gated, audited lease
// teardown). A disposable window with no usable token on the wire (e.g. an
// untagged admin-driven spawn) falls back to window-close, which exits the app
// and lets `--rm` / the startup reaper tear the container down. A
// non-disposable window is never disposed (`dispose: false`). The token regex
// mirrors the session manager's _TOKEN_RE and doubles as an injection guard
// (no leading '-', hex only) before the value reaches the gdbus argv.
function disposeWindowPlan(identity) {
  identity = identity || {};
  if (!isDisposableWindow(identity))
    return { "dispose": false, "byToken": false, "token": "" };
  var token = (identity.instanceId || "") + "";
  if (/^[0-9a-f]{8,64}$/.test(token))
    return { "dispose": true, "byToken": true, "token": token };
  return { "dispose": true, "byToken": false, "token": "" };
}

if (typeof module !== "undefined") {
  module.exports = {
    normalizeAppId: normalizeAppId,
    shouldGroup: shouldGroup,
    isRunningWindowEntry: isRunningWindowEntry,
    groupApps: groupApps,
    applySortMode: applySortMode,
    siloTierKey: siloTierKey,
    siloTierLabel: siloTierLabel,
    snapshotConfigForWindow: snapshotConfigForWindow,
    isDisposableWindow: isDisposableWindow,
    buildIsolationMenuItems: buildIsolationMenuItems,
    disposeWindowPlan: disposeWindowPlan,
  };
}
