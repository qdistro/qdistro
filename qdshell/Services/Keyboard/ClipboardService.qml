pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.UI
import "ClipboardActions.js" as ClipboardActions

// Clipboard history service using cliphist + local content cache
Singleton {
  id: root

  // Public API
  property bool active: Settings.data.appLauncher.enableClipboardHistory && cliphistAvailable
  property bool loading: false
  property var items: [] // [{id, preview, mime, isImage}]

  // Check if cliphist is available on the system
  property bool cliphistAvailable: false
  property bool dependencyChecked: false

  // Optional automatic watchers to feed cliphist DB
  property bool autoWatch: true
  property bool watchersStarted: false

  // Expose decoded thumbnails by id and a revision to notify bindings
  property var imageDataById: ({})
  property int revision: 0

  // Local content cache - stores full text content by ID
  // This avoids relying on cliphist decode which can be unreliable
  property var contentCache: ({})

  // Track the most recent clipboard content for instant access
  property string _latestTextContent: ""
  property string _latestTextId: ""

  // Approximate first-seen timestamps for entries this session (seconds)
  property var firstSeenById: ({})

  // Per-entry usage counts this session (for "most-used" ordering). Bumped
  // whenever the user copies/pastes an entry back to the clipboard.
  property var usageCountById: ({})

  // Baseline tracking for age-retention: ids present at the first list of an
  // active session for which we have NO persisted timestamp are NOT
  // timestamped (we can't date them), so max-age never deletes history we
  // can't date. Persisted first-seen timestamps (see firstSeenFilePath) let
  // age-based expiry survive shell restarts for entries we have dated.
  property bool _baselineSeeded: false
  property var _baselineIds: ({})

  // Persisted first-seen timestamps so clipboardMaxAgeDays works across
  // restarts. Keyed by cliphist id → unix seconds.
  readonly property string firstSeenFilePath: Settings.cacheDir + "clipboard_first_seen.json"
  property bool _firstSeenLoaded: false

  // Id of the most-recently-copied entry in cliphist recency order, captured
  // before any display reordering. Used to associate live clipboard content
  // with the correct entry regardless of the configured ordering.
  property string _recencyNewestId: ""

  // Internal: store callback for decode
  property var _decodeCallback: null
  property int _decodeRequestId: 0

  // Queue for base64 decodes
  property var _b64Queue: []
  property var _b64CurrentCb: null
  property string _b64CurrentMime: ""
  property string _b64CurrentId: ""

  signal listCompleted

  // Check if cliphist is available
  Component.onCompleted: {
    firstSeenFile.reload();
    checkCliphistAvailability();
  }

  // --- Persisted first-seen timestamps (for cross-restart age expiry) ------
  FileView {
    id: firstSeenFile
    path: root.firstSeenFilePath
    printErrors: false
    watchChanges: false
    onLoaded: {
      try {
        const content = text();
        if (content && content.trim() !== "") {
          const parsed = JSON.parse(content);
          if (parsed && typeof parsed === "object") {
            root.firstSeenById = parsed;
          }
        }
      } catch (e) {
        // Corrupt cache → start fresh; not fatal.
        root.firstSeenById = {};
      }
      root._firstSeenLoaded = true;
    }
    onLoadFailed: function (error) {
      root.firstSeenById = {};
      root._firstSeenLoaded = true;
    }
  }

  Timer {
    id: firstSeenSaveTimer
    interval: 1500
    repeat: false
    onTriggered: root._doSaveFirstSeen()
  }

  function _saveFirstSeen() {
    firstSeenSaveTimer.restart();
  }

  // Persist the firstSeenById map. The JSON value is fully shell-controlled
  // (numeric ids → numeric timestamps), but we still pass it via the
  // environment so no clipboard-derived data could ever reach the command
  // line, and quote the destination path with _q.
  function _doSaveFirstSeen() {
    if (!root._firstSeenLoaded)
      return;
    try {
      const content = JSON.stringify(root.firstSeenById);
      const path = root.firstSeenFilePath;
      _firstSeenSaveProc.environment = ["QD_JSON=" + content];
      _firstSeenSaveProc.command = ["sh", "-c", "mkdir -p \"$(dirname " + root._q(path) + ")\" && printf '%s' \"$QD_JSON\" > " + root._q(path)];
      _firstSeenSaveProc.running = true;
    } catch (e) {
      Logger.w("ClipboardService", "failed to persist first-seen cache:", e);
    }
  }

  Process {
    id: _firstSeenSaveProc
    stdout: StdioCollector {}
    stderr: StdioCollector {}
  }

  // Check dependency availability
  function checkCliphistAvailability() {
    if (dependencyChecked)
      return;
    dependencyCheckProcess.command = ["sh", "-c", "command -v cliphist"];
    dependencyCheckProcess.running = true;
  }

  // Process to check if cliphist is available
  Process {
    id: dependencyCheckProcess
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      root.dependencyChecked = true;
      if (exitCode === 0) {
        root.cliphistAvailable = true;
        // Start watchers if feature is enabled
        if (root.active) {
          startWatchers();
        }
      } else {
        root.cliphistAvailable = false;
        // Show toast notification if feature is enabled but cliphist is missing
        if (Settings.data.appLauncher.enableClipboardHistory) {
          ToastService.showWarning(I18n.tr("toast.clipboard.unavailable"), I18n.tr("toast.clipboard.unavailable-desc"), 6000);
        }
      }
    }
  }

  // Start/stop watchers when enabled changes
  onActiveChanged: {
    if (root.active) {
      startWatchers();
    } else {
      stopWatchers();
      loading = false;
      items = [];
      // Re-seed the baseline next time the service becomes active.
      root._baselineSeeded = false;
      root._baselineIds = {};
      root._recencyNewestId = "";
    }
  }

  // Fallback: periodically refresh list so UI updates even if not in clip mode
  Timer {
    interval: 5000
    repeat: true
    running: root.active
    onTriggered: list()
  }

  // Internal process objects
  Process {
    id: listProc
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      const out = String(stdout.text);
      const lines = out.split('\n').filter(l => l.length > 0);
      // Set true if we stamp any newly-seen id this pass → persist afterwards.
      let newlyStamped = false;
      // cliphist list default format: "<id> <preview>" or "<id>\t<preview>"
      const parsed = lines.map(l => {
                                 let id = "";
                                 let preview = "";
                                 const m = l.match(/^(\d+)\s+(.+)$/);
                                 if (m) {
                                   id = m[1];
                                   preview = m[2];
                                 } else {
                                   const tab = l.indexOf('\t');
                                   id = tab > -1 ? l.slice(0, tab) : l;
                                   preview = tab > -1 ? l.slice(tab + 1) : "";
                                 }
                                 const lower = preview.toLowerCase();
                                 const isImage = lower.startsWith("[image]") || lower.includes(" binary data ");
                                 // Best-effort mime guess from preview
                                 var mime = "text/plain";
                                 if (isImage) {
                                   if (lower.includes(" png"))
                                   mime = "image/png";
                                   else if (lower.includes(" jpg") || lower.includes(" jpeg"))
                                   mime = "image/jpeg";
                                   else if (lower.includes(" webp"))
                                   mime = "image/webp";
                                   else if (lower.includes(" gif"))
                                   mime = "image/gif";
                                   else
                                   mime = "image/*";
                                 }
                                 // Record first-seen time for ids we observe
                                 // for the FIRST time after the baseline list.
                                 // Entries already present when this session
                                 // started AND without a persisted timestamp
                                 // (the baseline) are left undated so max-age
                                 // expiry never deletes history we can't date.
                                 // Entries with a persisted timestamp keep it,
                                 // so age expiry survives shell restarts.
                                 if (root._baselineSeeded && root.firstSeenById[id] === undefined && !root._baselineIds[id]) {
                                   root.firstSeenById[id] = Time.timestamp;
                                   newlyStamped = true;
                                 }
                                 return {
                                   "id": id,
                                   "preview": preview,
                                   "isImage": isImage,
                                   "mime": mime
                                 };
                               });

      // Seed the baseline id set on the first successful list so that
      // pre-existing entries we cannot date are never treated as "freshly
      // copied" by the age-retention path. Ids that already carry a PERSISTED
      // first-seen timestamp are NOT added to the baseline — they remain
      // datable so age expiry survives shell restarts. Done once per active
      // session (requires the persisted cache to have loaded first).
      if (!root._baselineSeeded && root._firstSeenLoaded) {
        const base = {};
        for (let i = 0; i < parsed.length; i++) {
          const pid = parsed[i].id;
          if (root.firstSeenById[pid] === undefined) {
            base[pid] = true;
          }
        }
        root._baselineIds = base;
        root._baselineSeeded = true;
      }

      // Filter out browser junk when copying images
      let filtered = parsed.filter(item => {
                                     if (item.isImage)
                                     return true;
                                     const p = item.preview;
                                     // Skip UTF-16 encoded text (has null bytes between chars), chromium browser artifact
                                     const nullCount = (p.match(/\x00/g) || []).length;
                                     if (nullCount > p.length * 0.2)
                                     return false;
                                     // Skip browser-generated HTML wrapper, firefox
                                     if (p.toLowerCase().startsWith("<meta http-equiv="))
                                     return false;
                                     return true;
                                   });

      // Privacy: drop entries whose preview matches the user ignore pattern.
      // We physically delete the underlying cliphist entry so secrets don't
      // linger in the DB. NOTE: this is BEST-EFFORT — cliphist has already
      // stored the entry by the time we list it, and we only test the
      // truncated PREVIEW (~100 chars), so a secret beyond the preview window
      // or arriving between watcher store and the next list() poll can persist
      // briefly until matched-and-deleted. For a hard privacy boundary, pair
      // this with a watcher command that filters before `cliphist store`.
      // The preview is UNTRUSTED text used only as a length-capped RegExp
      // subject, never as a shell fragment.
      const ignorePat = Settings.data.appLauncher.clipboardIgnorePattern || "";
      const ignoreRes = ClipboardActions.applyIgnoreFilter(filtered, ignorePat, root._regexSubjectCap);
      for (let i = 0; i < ignoreRes.purged.length; i++) {
        root._purgeId(ignoreRes.purged[i]);
      }
      filtered = ignoreRes.kept;

      // Retention: drop entries older than the configured max age (best
      // effort, based on session first-seen timestamps; entries copied before
      // this shell session started have no timestamp and are left intact).
      const maxAgeDays = Number(Settings.data.appLauncher.clipboardMaxAgeDays) || 0;
      const ageRes = ClipboardActions.applyAgeExpiry(filtered, root.firstSeenById, maxAgeDays, Time.timestamp);
      for (let i = 0; i < ageRes.purged.length; i++) {
        root._purgeId(ageRes.purged[i]);
      }
      filtered = ageRes.kept;

      // Record the most-recently-copied id BEFORE any reordering. cliphist
      // lists most-recent-first, so this is filtered[0] at this point. We use
      // this stable id (not the post-sort display order) to associate the
      // current wl-paste output with the right entry, so "most-used" ordering
      // can't cache live clipboard content under an unrelated older id.
      root._recencyNewestId = (filtered.length > 0 && !filtered[0].isImage) ? filtered[0].id : "";

      // Ordering: cliphist lists most-recent-first natively. For "most-used"
      // we sort by this session's usage counts, falling back to recency.
      filtered = ClipboardActions.orderEntries(filtered, Settings.data.appLauncher.clipboardOrdering, root.usageCountById);

      // Size limit: keep only the first N entries (after ordering). Excess
      // entries are deleted from cliphist so the DB itself is bounded.
      const maxEntries = Number(Settings.data.appLauncher.clipboardMaxEntries) || 0;
      const trimRes = ClipboardActions.trimToMax(filtered, maxEntries);
      for (let i = 0; i < trimRes.purged.length; i++) {
        root._purgeId(trimRes.purged[i]);
      }
      filtered = trimRes.kept;

      items = filtered;
      loading = false;

      // Persist newly-stamped first-seen times (debounced) so age expiry
      // survives restarts.
      if (newlyStamped) {
        root._saveFirstSeen();
      }

      // Try to capture current clipboard and associate with the most-recently
      // copied entry (recency order, independent of display ordering).
      if (root._recencyNewestId !== "" && !root.contentCache[root._recencyNewestId]) {
        root.captureCurrentClipboard();
      }

      root.listCompleted();
    }
  }

  Process {
    id: decodeProc
    property int requestId: 0
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      if (requestId === root._decodeRequestId && root._decodeCallback) {
        const out = String(stdout.text);
        try {
          root._decodeCallback(out);
        } finally {
          root._decodeCallback = null;
        }
      }
    }
  }

  Process {
    id: copyProc
    stdout: StdioCollector {}
  }

  Process {
    id: pasteProc
    stdout: StdioCollector {}
  }

  Process {
    id: deleteProc
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      revision++;
      Qt.callLater(() => list());
    }
  }

  // Base64 decode pipeline (queued)
  Process {
    id: decodeB64Proc
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      const b64 = String(stdout.text).trim();
      if (root._b64CurrentCb) {
        const url = `data:${root._b64CurrentMime};base64,${b64}`;
        try {
          root._b64CurrentCb(url);
        } catch (e) {}
      }
      if (root._b64CurrentId !== "") {
        root.imageDataById[root._b64CurrentId] = `data:${root._b64CurrentMime};base64,${b64}`;
        root.revision += 1;
      }
      root._b64CurrentCb = null;
      root._b64CurrentMime = "";
      root._b64CurrentId = "";
      Qt.callLater(root._startNextB64);
    }
  }

  // Text watcher - stores to cliphist and triggers content capture
  Process {
    id: watchText
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      if (root.autoWatch && root.watchersStarted) {
        Qt.callLater(() => {
                       watchText.running = true;
                     });
      }
    }
  }

  // Image watcher
  Process {
    id: watchImage
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      if (root.autoWatch && root.watchersStarted) {
        Qt.callLater(() => {
                       watchImage.running = true;
                     });
      }
    }
  }

  // PRIMARY selection watcher (X/Wayland middle-click selection). Separate
  // from the CLIPBOARD watchers above and gated by clipboardWatchPrimary so
  // the PRIMARY selection is only captured into history when explicitly
  // enabled. The compositor-side ClipboardGate still governs cross-silo
  // PRIMARY transfers independently; this only affects local history capture.
  Process {
    id: watchPrimary
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      if (root.autoWatch && root.watchersStarted && Settings.data.appLauncher.clipboardWatchPrimary) {
        Qt.callLater(() => {
                       watchPrimary.running = true;
                     });
      }
    }
  }

  // Quiet purge process used by retention/ignore enforcement. Unlike
  // deleteProc it does NOT re-trigger list() (the caller is already inside a
  // list() result handler), avoiding a refresh loop.
  Process {
    id: purgeProc
    stdout: StdioCollector {}
  }

  // Capture current clipboard text when needed
  Process {
    id: captureTextProc
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      if (exitCode === 0) {
        const content = String(stdout.text);
        if (content.length > 0) {
          root._latestTextContent = content;
          // Associate with the most-recently-copied entry (recency order),
          // NOT the first displayed item, which under "most-used" ordering
          // may be an unrelated older entry.
          const newestId = root._recencyNewestId;
          if (newestId !== "" && !root.contentCache[newestId]) {
            root.contentCache[newestId] = content;
            root.revision++;
          }
        }
      }
    }
  }

  function startWatchers() {
    if (!root.active || !autoWatch || watchersStarted || !root.cliphistAvailable)
      return;
    watchersStarted = true;

    // Text watcher
    watchText.command = ["sh", "-c", Settings.data.appLauncher.clipboardWatchTextCommand];
    watchText.running = true;

    // Image watcher
    watchImage.command = ["sh", "-c", Settings.data.appLauncher.clipboardWatchImageCommand];
    watchImage.running = true;

    // PRIMARY selection watcher (opt-in)
    _syncPrimaryWatcher();
  }

  function stopWatchers() {
    if (!watchersStarted)
      return;
    watchText.running = false;
    watchImage.running = false;
    watchPrimary.running = false;
    watchersStarted = false;
  }

  // Start/stop the PRIMARY watcher to match the current setting. Safe to call
  // any time; only acts while the service is actively watching.
  function _syncPrimaryWatcher() {
    if (!root.watchersStarted)
      return;
    const want = Settings.data.appLauncher.clipboardWatchPrimary;
    if (want && !watchPrimary.running) {
      watchPrimary.command = ["sh", "-c", Settings.data.appLauncher.clipboardWatchPrimaryCommand];
      watchPrimary.running = true;
    } else if (!want && watchPrimary.running) {
      watchPrimary.running = false;
    }
  }

  // React to live changes of the PRIMARY-capture toggle.
  Connections {
    target: Settings.data.appLauncher
    function onClipboardWatchPrimaryChanged() {
      root._syncPrimaryWatcher();
    }
  }

  // Clear history on screen lock (privacy). Mirrors HooksService's lock-edge
  // detection: wipe when transitioning unlocked → locked.
  property bool _wasLocked: false
  Connections {
    target: PanelService
    function onLockScreenChanged() {
      if (PanelService.lockScreen) {
        _lockConn.target = PanelService.lockScreen;
        root._wasLocked = PanelService.lockScreen.active;
      }
    }
  }
  Connections {
    id: _lockConn
    target: PanelService.lockScreen
    function onActiveChanged() {
      if (!PanelService.lockScreen)
        return;
      const nowLocked = PanelService.lockScreen.active;
      if (!root._wasLocked && nowLocked && Settings.data.appLauncher.clipboardClearOnLock && root.active) {
        root.wipeAll();
      }
      root._wasLocked = nowLocked;
    }
  }

  // Capture current clipboard text and cache it
  function captureCurrentClipboard() {
    if (captureTextProc.running)
      return;
    captureTextProc.command = ["wl-paste", "--no-newline"];
    captureTextProc.running = true;
  }

  function list(maxPreviewWidth) {
    if (!root.active || !root.cliphistAvailable) {
      return;
    }
    if (listProc.running)
      return;
    loading = true;
    const width = maxPreviewWidth || 100;
    listProc.command = ["cliphist", "list", "-preview-width", String(width)];
    listProc.running = true;
  }

  // Get content for an ID - uses cache first, falls back to cliphist decode
  function getContent(id) {
    if (root.contentCache[id]) {
      return root.contentCache[id];
    }
    return null;
  }

  // Public guard: return the id as a validated numeric string, or null.
  // Callers that interpolate a clipboard id into a command MUST route through
  // this (mirrors the ClipboardActions.validId() guard used internally).
  function validId(id) {
    return ClipboardActions.validId(id);
  }

  // Async decode - checks cache first, then falls back to cliphist
  function decode(id, cb) {
    if (!root.cliphistAvailable) {
      if (cb)
        cb("");
      return;
    }

    // Check cache first
    const cached = root.contentCache[id];
    if (cached) {
      if (cb)
        cb(cached);
      return;
    }

    // Fall back to cliphist decode
    if (decodeProc.running) {
      decodeProc.running = false;
    }
    root._decodeRequestId++;
    decodeProc.requestId = root._decodeRequestId;
    root._decodeCallback = function (content) {
      // Cache the result if successful
      if (content && content.trim()) {
        root.contentCache[id] = content;
      }
      if (cb)
        cb(content);
    };
    const idStr = String(id);
    decodeProc.command = ["cliphist", "decode", idStr];
    decodeProc.running = true;
  }

  // Authoritative decode for SECURITY-SENSITIVE paths (regex actions). Unlike
  // decode(), this NEVER trusts root.contentCache — that cache is populated by
  // a best-effort newest-id heuristic that, with the PRIMARY watcher enabled,
  // could associate CLIPBOARD text with a PRIMARY entry's id (or vice versa).
  // For actions we must run against the EXACT content of the requested id, so
  // we always ask cliphist directly. Result is NOT written into contentCache.
  property var _authDecodeCb: null
  property string _authDecodeId: ""
  function decodeAuthoritative(id, cb) {
    if (!root.cliphistAvailable) {
      if (cb)
        cb("");
      return;
    }
    const idStr = ClipboardActions.validId(id);
    if (idStr === null) {
      if (cb)
        cb("");
      return;
    }
    root._authDecodeCb = cb || null;
    root._authDecodeId = idStr;
    _authDecodeProc.command = ["cliphist", "decode", idStr];
    _authDecodeProc.running = true;
  }

  Process {
    id: _authDecodeProc
    stdout: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      const out = (exitCode === 0) ? String(stdout.text) : "";
      const cb = root._authDecodeCb;
      root._authDecodeCb = null;
      root._authDecodeId = "";
      if (cb) {
        try {
          cb(out);
        } catch (e) {
          Logger.w("ClipboardService", "decodeAuthoritative callback raised:", e);
        }
      }
    }
  }

  function decodeToDataUrl(id, mime, cb) {
    if (!root.cliphistAvailable) {
      if (cb)
        cb("");
      return;
    }
    // If cached, return immediately
    if (root.imageDataById[id]) {
      if (cb)
        cb(root.imageDataById[id]);
      return;
    }
    // Queue request; ensures single process handles sequentially
    root._b64Queue.push({
                          "id": id,
                          "mime": mime || "image/*",
                          "cb": cb
                        });
    if (!decodeB64Proc.running && root._b64CurrentCb === null) {
      _startNextB64();
    }
  }

  function getImageData(id) {
    if (id === undefined) {
      return null;
    }
    return root.imageDataById[id];
  }

  function _startNextB64() {
    if (root._b64Queue.length === 0 || !root.cliphistAvailable)
      return;
    const job = root._b64Queue.shift();
    // Defence-in-depth: never interpolate a non-numeric id into `sh -c`. A bad
    // id fails the job (empty result) and we advance to the next one.
    const safeId = ClipboardActions.validId(job.id);
    if (safeId === null) {
      Logger.w("ClipboardService", "rejecting non-numeric cliphist id in decodeToDataUrl");
      if (job.cb)
        job.cb("");
      Qt.callLater(() => _startNextB64());
      return;
    }
    root._b64CurrentCb = job.cb;
    root._b64CurrentMime = job.mime;
    root._b64CurrentId = job.id;
    decodeB64Proc.command = ["sh", "-c", `cliphist decode ${safeId} | base64 -w 0`];
    decodeB64Proc.running = true;
  }

  function copyToClipboard(id) {
    if (!root.cliphistAvailable) {
      return;
    }
    const safeId = ClipboardActions.validId(id);
    if (safeId === null) {
      Logger.w("ClipboardService", "copyToClipboard: rejecting non-numeric cliphist id");
      return;
    }
    root.bumpUsage(safeId);
    copyProc.command = ["sh", "-c", `cliphist decode ${safeId} | wl-copy`];
    copyProc.running = true;
  }

  function pasteFromClipboard(id, mime) {
    if (!root.cliphistAvailable) {
      return;
    }
    const safeId = ClipboardActions.validId(id);
    if (safeId === null) {
      Logger.w("ClipboardService", "pasteFromClipboard: rejecting non-numeric cliphist id");
      return;
    }
    root.bumpUsage(safeId);
    const isImage = mime && mime.startsWith("image/");
    const typeArg = isImage ? ` --type ${mime}` : "";
    const pasteKeys = isImage ? "wtype -M ctrl -k v" : "wtype -M ctrl -M shift v";
    const cmd = `cliphist decode ${safeId} | wl-copy${typeArg} && ${pasteKeys}`;
    pasteProc.command = ["sh", "-c", cmd];
    pasteProc.running = true;
  }

  function pasteText(text) {
    if (!text)
      return;
    const escaped = text.replace(/'/g, "'\\''");
    const cmd = `printf '%s' '${escaped}' | wl-copy && wtype -M ctrl -M shift v`;
    pasteProc.command = ["sh", "-c", cmd];
    pasteProc.running = true;
  }

  function deleteById(id) {
    if (!root.cliphistAvailable) {
      return;
    }
    if (deleteProc.running) {
      return;
    }
    const idStr = ClipboardActions.validId(id);
    if (idStr === null) {
      Logger.w("ClipboardService", "deleteById: rejecting non-numeric cliphist id");
      return;
    }
    // Remove from cache
    delete root.contentCache[idStr];
    deleteProc.command = ["sh", "-c", `echo ${idStr} | cliphist delete`];
    deleteProc.running = true;
  }

  function wipeAll() {
    if (!root.cliphistAvailable) {
      return;
    }
    // Clear caches
    root.contentCache = {};
    root.imageDataById = {};
    root.firstSeenById = {};
    root.usageCountById = {};
    root._latestTextContent = "";
    root._latestTextId = "";

    Quickshell.execDetached(["cliphist", "wipe"]);
    // Persist the now-empty first-seen map so a wipe survives restart.
    root._saveFirstSeen();
    revision++;
    Qt.callLater(() => list());
  }

  // Shell-safe single-quote wrapper (mirrors AutostartService._q). Used only
  // for paths/ids we control; clipboard content is NEVER passed through this
  // into a shell — see runActionRule for the env/stdin-based contract.
  function _q(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
  }

  // Compile the user's ignore pattern into a RegExp, or null if unset/invalid.
  // The pattern itself is user-authored (trusted as a setting); the SUBJECT it
  // is tested against (clipboard preview) is untrusted but only ever used as a
  // RegExp.test() input, never as code or a shell fragment.
  function _ignoreRegex() {
    const pat = Settings.data.appLauncher.clipboardIgnorePattern || "";
    if (pat.length === 0)
      return null;
    const re = ClipboardActions.compileRegex(pat);
    if (re === null && pat.length > 0)
      Logger.w("ClipboardService", "invalid clipboardIgnorePattern; ignoring");
    return re;
  }

  // Delete an entry from cliphist WITHOUT re-listing (loop-safe). The id is
  // numeric from cliphist; we quote it defensively anyway.
  function _purgeId(id) {
    if (!root.cliphistAvailable)
      return;
    const idStr = ClipboardActions.validId(id);
    if (idStr === null)
      return;
    delete root.contentCache[idStr];
    delete root.firstSeenById[idStr];
    delete root.usageCountById[idStr];
    root._saveFirstSeen();
    Quickshell.execDetached(["sh", "-c", "echo " + root._q(idStr) + " | cliphist delete"]);
  }

  // Record that an entry was used (for "most-used" ordering).
  function bumpUsage(id) {
    const idStr = String(id);
    root.usageCountById[idStr] = (root.usageCountById[idStr] || 0) + 1;
  }

  // Upper bound on how much UNTRUSTED clipboard text we feed into a user
  // RegExp. A locally-configured catastrophic-backtracking pattern run against
  // megabytes of hostile clipboard content could otherwise freeze the QML
  // main thread; capping the subject bounds the worst case.
  readonly property int _regexSubjectCap: 16384

  function _regexSubject(text) {
    return ClipboardActions.regexSubject(text, root._regexSubjectCap);
  }

  // Return the action rules whose regex matches the given text.
  // Each rule is { name, regexPattern, command }. The text is UNTRUSTED — it is
  // only ever used as a RegExp.test() subject (length-capped), never as code.
  function matchingActions(text) {
    const actions = Settings.data.appLauncher.clipboardActions || [];
    const matched = ClipboardActions.matchingActions(actions, text, root._regexSubjectCap);
    return matched.map(m => m.rule);
  }

  // Execute a regex-action rule against UNTRUSTED clipboard text. Rule is
  // { name, regexPattern, command }; the first capture group (if any) is
  // exposed as $QD_CLIP_1 alongside the full text in $QD_CLIP.
  //
  // SECURITY INVARIANT: clipboard content (and regex capture groups) are
  // attacker-controlled. They are NEVER interpolated into the `sh -c` command
  // line. Instead:
  //   - The user-authored `command` is the ONLY thing the shell parses as a
  //     template. The matched text is exposed solely through the environment
  //     variables $QD_CLIP / $QD_CLIP_1 (set via Process.environment, never
  //     concatenated into the script). A payload like `; rm -rf ~` therefore
  //     lands as the VALUE of $QD_CLIP, not as a new command. This mirrors the
  //     AutostartService env-passing pattern. (The user MUST quote references,
  //     i.e. write "$QD_CLIP"; an unquoted $QD_CLIP still undergoes word-
  //     splitting/globbing but cannot start a new command — the settings UI
  //     documents the quoting requirement.)
  //   - The full text is also piped on stdin so commands can consume it with
  //     no interpolation whatsoever.
  // Only the clipboard text is untrusted; the `command` template is a local
  // user setting. We additionally RE-VALIDATE the rule's regex against the
  // (decoded) text here, so a preview-vs-full-content mismatch in the caller
  // can never cause an action to fire on text its pattern doesn't match.
  function runActionRule(rule, text) {
    if (!rule || !rule.command)
      return;
    // Re-validate the rule's regex against the (decoded) text so a
    // preview-vs-full-content mismatch can never cause an action to fire on
    // text its pattern doesn't match. null → do not run.
    const group1 = ClipboardActions.revalidateActionGroup(rule, text, root._regexSubjectCap);
    if (group1 === null)
      return;
    // Build the injection-safe payload: the matched text is carried ONLY via
    // env values ($QD_CLIP / $QD_CLIP_1) and stdin, never concatenated into the
    // command line. printf reads $QD_CLIP so even the stdin payload isn't
    // interpolated. A payload like `; rm -rf ~` lands as the VALUE of $QD_CLIP.
    const exec = ClipboardActions.buildActionExecution(rule, text, group1);
    _actionProc.environment = exec.environment;
    _actionProc.command = exec.command;
    _actionProc.running = true;
  }

  Process {
    id: _actionProc
    stdout: StdioCollector {}
    stderr: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      if (exitCode !== 0) {
        Logger.w("ClipboardService", "clipboard action exited", exitCode);
      }
    }
  }

  // Parse image metadata from cliphist preview string
  function parseImageMeta(preview) {
    const re = /\[\[\s*binary data\s+([\d\.]+\s*(?:KiB|MiB|GiB|B))\s+(\w+)\s+(\d+)x(\d+)\s*\]\]/i;
    const match = (preview || "").match(re);
    if (!match)
      return null;
    return {
      "size": match[1],
      "fmt": (match[2] || "").toUpperCase(),
      "w": Number(match[3]),
      "h": Number(match[4])
    };
  }
}
