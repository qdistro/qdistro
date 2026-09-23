import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Quickshell.Widgets
import qs.Commons
import qs.Services.Qdistro
import qs.Services.Qdwin
import qs.Services.System
import qs.Services.UI
import qs.Widgets
import "TaskbarLogic.js" as TaskbarLogic
import "PermissionsLogic.js" as PermissionsLogic

Item {
  id: root

  property ShellScreen screen

  // Widget properties passed from Bar.qml for per-instance settings
  property string widgetId: ""
  property string section: ""
  property int sectionWidgetIndex: -1
  property int sectionWidgetsCount: 0

  // Explicit screenName property ensures reactive binding when screen changes
  readonly property string screenName: screen ? screen.name : ""
  readonly property string barPosition: Settings.getBarPositionForScreen(screenName)
  readonly property bool isVerticalBar: barPosition === "left" || barPosition === "right"
  readonly property real barHeight: Style.getBarHeightForScreen(screenName)
  readonly property real capsuleHeight: Style.getCapsuleHeightForScreen(screenName)
  readonly property real barFontSize: Style.getBarFontSizeForScreen(screenName)

  property var widgetMetadata: BarWidgetRegistry.widgetMetadata[widgetId]
  property var widgetSettings: {
    if (section && sectionWidgetIndex >= 0 && screenName) {
      var widgets = Settings.getBarWidgetsForScreen(screenName)[section];
      if (widgets && sectionWidgetIndex < widgets.length) {
        return widgets[sectionWidgetIndex];
      }
    }
    return {};
  }

  property bool hasWindow: false
  readonly property string hideMode: (widgetSettings.hideMode !== undefined) ? widgetSettings.hideMode : widgetMetadata.hideMode
  readonly property bool onlySameOutput: (widgetSettings.onlySameOutput !== undefined) ? widgetSettings.onlySameOutput : widgetMetadata.onlySameOutput
  readonly property bool onlyActiveWorkspaces: (widgetSettings.onlyActiveWorkspaces !== undefined) ? widgetSettings.onlyActiveWorkspaces : widgetMetadata.onlyActiveWorkspaces
  readonly property bool showTitle: isVerticalBar ? false : (widgetSettings.showTitle !== undefined) ? widgetSettings.showTitle : widgetMetadata.showTitle
  readonly property bool smartWidth: (widgetSettings.smartWidth !== undefined) ? widgetSettings.smartWidth : widgetMetadata.smartWidth
  readonly property int maxTaskbarWidthPercent: (widgetSettings.maxTaskbarWidth !== undefined) ? widgetSettings.maxTaskbarWidth : widgetMetadata.maxTaskbarWidth
  readonly property real iconScale: (widgetSettings.iconScale !== undefined) ? widgetSettings.iconScale : widgetMetadata.iconScale
  readonly property int itemSize: Style.toOdd(capsuleHeight * Math.max(0.1, iconScale))

  // Maximum width for the taskbar widget to prevent overlapping with other widgets
  readonly property real maxTaskbarWidth: {
    if (!screen || isVerticalBar || !smartWidth || maxTaskbarWidthPercent <= 0)
      return 0;
    var barFloating = Settings.data.bar.floating || false;
    var barMarginH = barFloating ? Math.ceil(Settings.data.bar.marginHorizontal) : 0;
    var availableWidth = screen.width - (barMarginH * 2);
    return Math.round(availableWidth * (maxTaskbarWidthPercent / 100));
  }

  readonly property int titleWidth: {
    // First, use user-defined title width if set
    var calculatedWidth = (widgetSettings.titleWidth !== undefined) ? widgetSettings.titleWidth : widgetMetadata.titleWidth;

    // Second, shrink title width if it exceeds maxTaskbarWidth when smartWidth is enabled
    if (smartWidth && combinedModel.length > 0) {
      if (maxTaskbarWidth > 0) {
        var entriesCount = combinedModel.length;
        var maxWidthPerEntry = (maxTaskbarWidth / entriesCount) - itemSize - Style.marginS - Style.marginXL;
        calculatedWidth = Math.min(calculatedWidth, maxWidthPerEntry);
      }

      calculatedWidth = Math.max(Math.round(calculatedWidth), 20);
    }

    return calculatedWidth;
  }
  readonly property bool showPinnedApps: (widgetSettings.showPinnedApps !== undefined) ? widgetSettings.showPinnedApps : widgetMetadata.showPinnedApps
  // Window grouping: "never" | "always" | "limited" (group only when the
  // taskbar would otherwise exceed its max width). Mirrors XFCE's window
  // buttons "grouping" behavior.
  readonly property string groupingMode: (widgetSettings.groupingMode !== undefined) ? widgetSettings.groupingMode : widgetMetadata.groupingMode
  // Sort order: "none" (launch/stable order, drag-and-drop allowed) |
  // "title" (by window/app title) | "group" (by application id).
  readonly property string sortMode: (widgetSettings.sortMode !== undefined) ? widgetSettings.sortMode : widgetMetadata.sortMode

  // Context menu state - store ID instead of object reference to avoid stale references
  property string selectedWindowId: ""
  property string selectedAppId: ""
  // The taskbar item the context menu was anchored to, kept so a follow-up
  // popup (the permissions panel) can anchor to the same place after the
  // context menu has closed and the selection has been cleared.
  property var selectedAnchorItem: null

  // Helper to get the current model entry from the selected ID. Returns
  // the full entry (which may be a group) or null.
  function getSelectedEntry() {
    if (!selectedWindowId)
      return null;
    for (var i = 0; i < combinedModel.length; i++) {
      // Using loose equality on purpose (==)
      if (combinedModel[i].id == selectedWindowId) {
        return combinedModel[i];
      }
    }
    return null;
  }

  // Helper to get the current window object from ID
  function getSelectedWindow() {
    const entry = getSelectedEntry();
    return (entry && entry.window) ? entry.window : null;
  }

  // The list of window objects backing the selected entry. For a plain
  // running entry that is [window]; for a group it is every window in the
  // group; for pinned-not-running / placeholder it is [].
  function getSelectedWindowList() {
    const entry = getSelectedEntry();
    if (!entry)
      return [];
    if (entry.isGroup && entry.windows)
      return entry.windows.slice();
    if (entry.window)
      return [entry.window];
    return [];
  }

  // -- bulk window actions (XFCE "minimize/maximize/close [all]") --
  // Each guards against a missing Qdwin capability by no-op'ing when the
  // action function is absent; the menu also capability-gates entries.
  function minimizeWindows(wins) {
    if (!wins || typeof Qdwin.requestMinimize !== "function")
      return;
    wins.forEach(function (w) {
      try {
        Qdwin.requestMinimize(w);
      } catch (e) {
        Logger.e("Taskbar", "minimize failed: " + e);
      }
    });
  }
  function maximizeWindows(wins, maximized) {
    if (!wins || typeof Qdwin.requestMaximize !== "function")
      return;
    wins.forEach(function (w) {
      try {
        Qdwin.requestMaximize(w, maximized);
      } catch (e) {
        Logger.e("Taskbar", "maximize failed: " + e);
      }
    });
  }
  function closeWindows(wins) {
    if (!wins || typeof Qdwin.closeWindow !== "function")
      return;
    wins.forEach(function (w) {
      try {
        Qdwin.closeWindow(w);
      } catch (e) {
        Logger.e("Taskbar", "close failed: " + e);
      }
    });
  }
  property int modelUpdateTrigger: 0  // Dummy property to force model re-evaluation

  // Hover state
  property var hoveredWindowId: ""
  // Combined model of running windows and pinned apps
  property var combinedModel: []

  // Wheel scroll handling
  property int wheelAccumulatedDelta: 0
  property bool wheelCooldown: false

  // Drag and Drop state for visual feedback
  property int dragSourceIndex: -1
  property int dragTargetIndex: -1

  // Track the session order of apps (transient reordering)
  property var sessionAppOrder: []

  function getAppKey(appData) {
    if (!appData)
      return null;
    // prefer window object identity for running apps to distinguish instances
    if (appData.window)
      return appData.window;
    // fallback to appId for pinned-only apps
    return appData.appId;
  }

  function sortApps(apps) {
    if (!sessionAppOrder || sessionAppOrder.length === 0) {
      return apps;
    }

    const sorted = [];
    const remaining = [...apps];

    // 1. Pick apps that are in the session order
    for (let i = 0; i < sessionAppOrder.length; i++) {
      const key = sessionAppOrder[i];
      const idx = remaining.findIndex(app => getAppKey(app) === key);
      if (idx !== -1) {
        sorted.push(remaining[idx]);
        remaining.splice(idx, 1);
      }
    }

    // 2. Append any new/remaining apps
    remaining.forEach(app => sorted.push(app));

    return sorted;
  }

  // Decide whether grouping should be active right now. "never" -> off,
  // "always" -> on, "limited" -> on only when the ungrouped taskbar would
  // overflow maxTaskbarWidth (i.e. there are more entries than fit).
  function shouldGroup(entryCount) {
    // Pure decision lives in TaskbarLogic.js; pass the singleton-derived
    // values it needs (Style margins, layout metrics) as plain numbers.
    return TaskbarLogic.shouldGroup(entryCount, {
                                      "groupingMode": groupingMode,
                                      "isVerticalBar": isVerticalBar,
                                      "maxTaskbarWidth": maxTaskbarWidth,
                                      "showTitle": showTitle,
                                      "itemSize": itemSize,
                                      "titleWidth": titleWidth,
                                      "marginS": Style.marginS,
                                      "marginXL": Style.marginXL
                                    });
  }

  // Collapse entries that share a normalized appId into a single group
  // entry. Pinned-not-running and placeholder entries are never grouped
  // (each stays its own button). Group entries carry a `windows` array of
  // the underlying window objects; `window` points at the focused (or
  // first) window so the icon/title/focus-indicator still render.
  function groupApps(entries) {
    return TaskbarLogic.groupApps(entries);
  }

  // Apply the configured sort order to the model. "none" keeps the
  // launch/session order (drag-and-drop friendly); "title" sorts by
  // visible title; "group" sorts by appId then title.
  function applySortMode(entries) {
    return TaskbarLogic.applySortMode(entries, sortMode);
  }

  function reorderApps(fromIndex, toIndex) {
    Logger.d("Taskbar", "Reordering apps from " + fromIndex + " to " + toIndex);
    if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0 || fromIndex >= combinedModel.length || toIndex >= combinedModel.length)
      return;

    const list = [...combinedModel];
    const item = list.splice(fromIndex, 1)[0];
    list.splice(toIndex, 0, item);

    combinedModel = list;
    sessionAppOrder = combinedModel.map(getAppKey);
    savePinnedOrder();
  }

  function savePinnedOrder() {
    const currentPinned = Settings.data.dock.pinnedApps || [];
    const newPinned = [];
    const seen = new Set();

    // Extract pinned apps in their current visual order
    combinedModel.forEach(app => {
                            if (app.appId && !seen.has(app.appId)) {
                              const isPinned = currentPinned.some(p => normalizeAppId(p) === normalizeAppId(app.appId));

                              if (isPinned) {
                                newPinned.push(app.appId);
                                seen.add(app.appId);
                              }
                            }
                          });

    // Check if any pinned apps were missed (e.g. filtered out by workspace)
    currentPinned.forEach(p => {
                            if (!seen.has(p)) {
                              newPinned.push(p);
                              seen.add(p);
                            }
                          });

    if (JSON.stringify(currentPinned) !== JSON.stringify(newPinned)) {
      Settings.data.dock.pinnedApps = newPinned;
    }
  }

  // Helper function to normalize app IDs for case-insensitive matching
  function normalizeAppId(appId) {
    return TaskbarLogic.normalizeAppId(appId);
  }

  // Helper function to check if an app ID matches a pinned app (case-insensitive)
  function isAppIdPinned(appId, pinnedApps) {
    if (!appId || !pinnedApps || pinnedApps.length === 0)
      return false;
    const normalizedId = normalizeAppId(appId);
    return pinnedApps.some(pinnedId => normalizeAppId(pinnedId) === normalizedId);
  }

  // Helper function to get app name from desktop entry
  function getAppNameFromDesktopEntry(appId) {
    if (!appId)
      return appId;

    try {
      if (typeof DesktopEntries !== 'undefined' && DesktopEntries.heuristicLookup) {
        const entry = DesktopEntries.heuristicLookup(appId);
        if (entry && entry.name) {
          return entry.name;
        }
      }

      if (typeof DesktopEntries !== 'undefined' && DesktopEntries.byId) {
        const entry = DesktopEntries.byId(appId);
        if (entry && entry.name) {
          return entry.name;
        }
      }
    } catch (e)
      // Fall through to return original appId
    {}

    // Return original appId if we can't find a desktop entry
    return appId;
  }

  // Helper function to get desktop entry ID from an app ID
  function getDesktopEntryId(appId) {
    if (!appId)
      return appId;

    // Try to find the desktop entry using heuristic lookup
    if (typeof DesktopEntries !== 'undefined' && DesktopEntries.heuristicLookup) {
      try {
        const entry = DesktopEntries.heuristicLookup(appId);
        if (entry && entry.id) {
          return entry.id;
        }
      } catch (e)
        // Fall through to return original appId
      {}
    }

    // Try direct lookup
    if (typeof DesktopEntries !== 'undefined' && DesktopEntries.byId) {
      try {
        const entry = DesktopEntries.byId(appId);
        if (entry && entry.id) {
          return entry.id;
        }
      } catch (e)
        // Fall through to return original appId
      {}
    }

    // Return original appId if we can't find a desktop entry
    return appId;
  }

  // Helper function to check if an app is pinned
  function isAppPinned(appId) {
    if (!appId)
      return false;
    const pinnedApps = Settings.data.dock.pinnedApps || [];
    const normalizedId = normalizeAppId(appId);
    return pinnedApps.some(pinnedId => normalizeAppId(pinnedId) === normalizedId);
  }

  // Helper function to toggle app pin/unpin
  function toggleAppPin(appId) {
    if (!appId)
      return;

    // Get the desktop entry ID for consistent pinning
    const desktopEntryId = getDesktopEntryId(appId);
    const normalizedId = normalizeAppId(desktopEntryId);

    let pinnedApps = (Settings.data.dock.pinnedApps || []).slice(); // Create a copy

    // Find existing pinned app with case-insensitive matching
    const existingIndex = pinnedApps.findIndex(pinnedId => normalizeAppId(pinnedId) === normalizedId);
    const isPinned = existingIndex >= 0;

    if (isPinned) {
      // Unpin: remove from array
      pinnedApps.splice(existingIndex, 1);
    } else {
      // Pin: add desktop entry ID to array
      pinnedApps.push(desktopEntryId);
    }

    // Update the settings
    Settings.data.dock.pinnedApps = pinnedApps;
  }

  // Function to update the combined model
  function updateCombinedModel() {
    const runningWindows = [];
    const pinnedApps = Settings.data.dock.pinnedApps || [];
    const processedAppIds = new Set();

    // First pass: Add all running windows. Also collect each window's
    // wp_security_context_v1 instanceId so the placeholder pass below
    // can suppress its own row when the real toplevel has already
    // arrived (avoids the brief double-render between toplevel_added
    // and toplevel_security_context).
    const seenInstanceIds = new Set();
    try {
      const total = Qdwin.windows.count || 0;
      const activeIds = Qdwin.getActiveWorkspaces().map(function (ws) {
        return ws.id;
      });

      for (var i = 0; i < total; i++) {
        var w = Qdwin.windows.get(i);
        if (!w)
          continue;
        var passOutput = (!onlySameOutput) || (w.output == screen?.name);
        var passWorkspace = (!onlyActiveWorkspaces) || (activeIds.includes(w.workspaceId));
        if (passOutput && passWorkspace) {
          const isPinned = isAppIdPinned(w.appId, pinnedApps);
          runningWindows.push({
                                "id": w.id,
                                "type": isPinned ? "pinned-running" : "running",
                                "window": w,
                                "appId": w.appId,
                                "title": w.title || getAppNameFromDesktopEntry(w.appId)
                              });
          processedAppIds.add(normalizeAppId(w.appId));
          if (w.instanceId) seenInstanceIds.add(w.instanceId);
        }
      }
    } catch (e)
      // Ignore errors
    {}

    // Second pass: Add non-running pinned apps (only if showPinnedApps is enabled)
    if (showPinnedApps) {
      pinnedApps.forEach(pinnedAppId => {
                           const normalizedPinnedId = normalizeAppId(pinnedAppId);
                           if (!processedAppIds.has(normalizedPinnedId)) {
                             const appName = getAppNameFromDesktopEntry(pinnedAppId);
                             runningWindows.push({
                                                   "id": pinnedAppId,
                                                   "type": "pinned",
                                                   "window": null,
                                                   "appId": pinnedAppId,
                                                   "title": appName
                                                 });
                           }
                         });
    }

    // Third pass: Add cold-start placeholders for tier-2 podapps that
    // are spawning but haven't yet emitted toplevel_security_context.
    // PodApps removes the entry on instanceId match, so the placeholder
    // is replaced by the real toplevel automatically. Per
    // qdistro/doc/containers.md "Cold-start contract".
    //
    // Skip placeholders whose launchToken matches a window's instanceId
    // already collected above. toplevelAdded fires before
    // toplevelSecurityContext, so during the gap (~100-300ms) the real
    // toplevel exists in Qdwin.windows with empty instanceId and BOTH
    // entries render. Once secctx attaches the instanceId on the
    // window's row, this filter collapses the placeholder away.
    try {
      const phCount = PodApps.placeholders.count || 0;
      for (let i = 0; i < phCount; i++) {
        const ph = PodApps.placeholders.get(i);
        if (seenInstanceIds.has(ph.launchToken)) continue;
        runningWindows.push({
                              "id":           "podapp-placeholder:" + ph.launchToken,
                              "type":         "placeholder",
                              "window":       null,
                              "appId":        ph.appId,
                              "title":        ph.name || ph.appId,
                              "iconName":     ph.iconName || "",
                              "silo":         ph.silo || "",
                              "launchToken":  ph.launchToken,
                            });
      }
    } catch (e) {}

    // Apply window grouping before ordering so the session/title/group
    // sort operates on the final button set.
    var entries = runningWindows;
    if (shouldGroup(runningWindows.length)) {
      entries = groupApps(runningWindows);
    }

    // Ordering. "none" preserves the user's drag/session order; the other
    // modes sort deterministically and disable session reordering.
    if (sortMode === "none") {
      combinedModel = sortApps(entries);
    } else {
      combinedModel = applySortMode(entries);
    }

    // Sync session order if needed (e.g. first run or new apps added)
    if (!sessionAppOrder || sessionAppOrder.length === 0 || sessionAppOrder.length !== combinedModel.length) {
      sessionAppOrder = combinedModel.map(getAppKey);
    }
    updateHasWindow();
  }

  // Function to launch a pinned app
  function launchPinnedApp(appId) {
    if (!appId)
      return;

    try {
      const app = DesktopEntries.byId(appId);

      if (Settings.data.appLauncher.customLaunchPrefixEnabled && Settings.data.appLauncher.customLaunchPrefix) {
        // Use custom launch prefix
        const prefix = Settings.data.appLauncher.customLaunchPrefix.split(" ");

        if (app.runInTerminal) {
          const terminal = Settings.data.appLauncher.terminalCommand.split(" ");
          const command = prefix.concat(terminal.concat(app.command));
          Quickshell.execDetached(command);
        } else {
          const command = prefix.concat(app.command);
          Quickshell.execDetached(command);
        }
      } else if (Settings.data.appLauncher.useApp2Unit && ProgramCheckerService.app2unitAvailable && app.id) {
        Logger.d("Taskbar", `Using app2unit for: ${app.id}`);
        if (app.runInTerminal)
          Quickshell.execDetached(["app2unit", "--", app.id + ".desktop"]);
        else
          Quickshell.execDetached(["app2unit", "--"].concat(app.command));
      } else {
        // Fallback logic when app2unit is not used
        if (app.runInTerminal) {
          Logger.d("Taskbar", "Executing terminal app manually: " + app.name);
          const terminal = Settings.data.appLauncher.terminalCommand.split(" ");
          const command = terminal.concat(app.command);
          Qdwin.spawn(command);
        } else if (app.command && app.command.length > 0) {
          Qdwin.spawn(app.command);
        } else if (app.execute) {
          app.execute();
        } else {
          Logger.w("Taskbar", `Could not launch: ${app.name}. No valid launch method.`);
        }
      }
    } catch (e) {
      Logger.e("Taskbar", "Failed to launch app: " + e);
    }
  }

  // Build the right-click context menu model for the currently selected
  // entry. Shared by the reactive `contextMenu.model` binding and the
  // imperative openTaskbarContextMenu() path so both stay in sync.
  function buildContextMenuModel() {
    var items = [];
    if (root.selectedWindowId) {
      const entry = root.getSelectedEntry();
      const wins = root.getSelectedWindowList();
      const isGroup = entry && entry.isGroup === true;
      // Capability flags — gate actions Qdwin can't perform yet.
      const canMinimize = typeof Qdwin.requestMinimize === "function";
      const canMaximize = typeof Qdwin.requestMaximize === "function";
      const canClose = typeof Qdwin.closeWindow === "function";

      // Focus item (for running apps)
      items.push({
                   "label": I18n.tr("common.focus"),
                   "action": "focus",
                   "icon": "eye"
                 });

      // Pin/Unpin item (always available when right-clicking an app)
      const isPinned = root.isAppPinned(root.selectedAppId);
      items.push({
                   "label": !isPinned ? I18n.tr("common.pin") : I18n.tr("common.unpin"),
                   "action": "pin",
                   "icon": !isPinned ? "pin" : "unpin"
                 });

      // Bulk window actions (XFCE: minimize / maximize / close). For a
      // group these act on every window in the group, so the labels switch
      // to the "all windows" wording. The actions all operate on
      // getSelectedWindowList(), which already returns every group window.
      if (wins.length > 0) {
        items.push({
                     "label": isGroup ? I18n.tr("bar.taskbar.minimize-all-in-group") : I18n.tr("common.minimize"),
                     "action": "minimize",
                     "icon": "chevron-down",
                     "enabled": canMinimize
                   });
        items.push({
                     "label": I18n.tr("common.maximize"),
                     "action": "maximize",
                     "icon": "chevron-up",
                     "enabled": canMaximize
                   });
        items.push({
                     "label": I18n.tr("common.unmaximize"),
                     "action": "unmaximize",
                     "icon": "chevron-down",
                     "enabled": canMaximize
                   });
      }

      // Close item (single window or group "close all").
      items.push({
                   "label": isGroup ? I18n.tr("bar.taskbar.close-all-in-group") : I18n.tr("common.close"),
                   "action": "close",
                   "icon": "x",
                   "enabled": canClose
                 });

      // Add desktop entry actions (like "New Window", "Private Window", etc.)
      if (typeof DesktopEntries !== 'undefined' && DesktopEntries.byId && root.selectedAppId) {
        const dentry = (DesktopEntries.heuristicLookup) ? DesktopEntries.heuristicLookup(root.selectedAppId) : DesktopEntries.byId(root.selectedAppId);
        if (dentry != null && dentry.actions) {
          dentry.actions.forEach(function (action) {
            items.push({
                         "label": action.name,
                         "action": "desktop-action-" + action.name,
                         "icon": "chevron-right",
                         "desktopAction": action
                       });
          });
        }
      }

      // qdistro isolation section (D16 v1): per-window silo identity rows +
      // snapshot / dispose / permissions actions, built from the already-
      // available secctx identity chain. Returns [] for native windows, so
      // the menu is unchanged for non-silo apps. Shown only for a SINGLE
      // selected window — a multi-window group can mix silos/identities, so
      // a per-window isolation view (and especially its dispose action)
      // would be ambiguous and could act on the wrong window.
      const _qdWin = root.getSelectedWindow();
      if (_qdWin && (!isGroup || wins.length <= 1)) {
        const _qdItems = TaskbarLogic.buildIsolationMenuItems({
                                                                "secctxAppId": _qdWin.secctxAppId,
                                                                "sandboxEngine": _qdWin.sandboxEngine,
                                                                "silo": root.qdSiloForWindow(_qdWin, entry)
                                                              });
        for (var _qi = 0; _qi < _qdItems.length; _qi++)
          items.push(root.qdLocalizeIsolationItem(_qdItems[_qi]));
      }
    }
    items.push({
                 "label": I18n.tr("actions.widget-settings"),
                 "action": "widget-settings",
                 "icon": "settings"
               });
    return items;
  }

  // Localize one isolation-menu row built by TaskbarLogic. The pure JS builder
  // emits a `labelKey` (+ optional `labelParams`) plus an English `label`
  // fallback; here we resolve the key through I18n.tr so the menu honors the
  // active language, and keep the English string if the key is missing. The
  // tier row carries a nested `tierKey` (a localisable enum) that we resolve
  // first into the `tier` param. The original item is shallow-copied so the
  // builder's output (and the JS tests' English labels) stay untouched.
  function qdLocalizeIsolationItem(item) {
    if (!item || !item.labelKey)
      return item;
    var out = {};
    for (var k in item)
      out[k] = item[k];
    var params = {};
    if (item.labelParams) {
      for (var p in item.labelParams)
        params[p] = item.labelParams[p];
    }
    // Resolve the nested tier enum (bar.taskbar.isolation.tier-<key>) before
    // the outer template so the localized tier name is interpolated in.
    if (item.tierKey) {
      var tierKeyFull = "bar.taskbar.isolation.tier-" + item.tierKey;
      var localizedTier = I18n.tr(tierKeyFull);
      // I18n.tr returns the key (or !!key!!) when unresolved — fall back to the
      // English tier already in labelParams in that case.
      if (localizedTier && localizedTier.indexOf("!!") !== 0 && localizedTier !== tierKeyFull)
        params.tier = localizedTier;
    }
    var localized = I18n.tr(item.labelKey, params);
    if (localized && localized.indexOf("!!") !== 0 && localized !== item.labelKey)
      out.label = localized;
    return out;
  }

  // Derive the silo identity for a window the same way the rest of the
  // shell does (Qdwin._siloForWindow -> ClipboardSilo.fromSecctx): Qdwin
  // window rows carry secctx fields but NO `silo` role, so we cannot read it
  // off the row directly. entry.silo (set for cold-start placeholders) is a
  // fallback. Returns "" when nothing usable is derivable.
  function qdSiloForWindow(win, entry) {
    if (entry && entry.silo)
      return entry.silo;
    if (win && typeof Qdwin._siloForWindow === "function") {
      const s = Qdwin._siloForWindow(win);
      // _siloForWindow returns "unknown" when it cannot derive one.
      return (s && s !== "unknown") ? s : "";
    }
    return "";
  }

  function qdSiloForSelected() {
    return root.qdSiloForWindow(root.getSelectedWindow(), root.getSelectedEntry());
  }

  // Snapshot-now (D16/P2c): ask the broker to take a Snapper snapshot via its
  // existing SnapshotBefore(config, description) method (broker -> Snapper; no
  // new protocol). `config` is a SNAPPER CONFIG NAME, not a qdshell silo label,
  // so the menu item carries the resolved config (TaskbarLogic
  // .snapshotConfigForWindow strips the "tier2/" prefix of a persistent
  // tier-2 silo). The Snapshot item is only OFFERED for windows whose silo maps
  // to a real config (persistent tier-2): disposables (ephemeral home) and VM
  // tiers (their snapshot story is VM-disk, not host Snapper) get no item. The
  // result is toasted honestly — a config that still does not exist on this
  // host surfaces the broker's real error rather than a false "done".
  Process {
    id: qdSnapshotProc
    property string configName: ""
    stdout: StdioCollector {
      id: qdSnapshotOut
    }
    stderr: StdioCollector {
      id: qdSnapshotErr
    }
    onExited: (code, status) => {
      if (code === 0) {
        ToastService.showNotice(I18n.tr("toast.snapshot.title"), I18n.tr("toast.snapshot.taken", { "config": qdSnapshotProc.configName }), "camera", 3000);
      } else {
        const msg = (qdSnapshotErr.text || "").trim() || I18n.tr("toast.snapshot.broker-error");
        ToastService.showError(I18n.tr("toast.snapshot.failed"), msg, 6000);
      }
    }
  }

  function qdSnapshotNow(config) {
    if (!config) {
      ToastService.showWarning(I18n.tr("toast.snapshot.title"), I18n.tr("toast.snapshot.no-config"), 4000);
      return;
    }
    // Final guard on the resolved CONFIG name (not the raw silo). A real
    // Snapper config is a simple name; reject anything with a '/', a leading
    // '-' (would be parsed as a gdbus option), or an out-of-shape character.
    // TaskbarLogic.snapshotConfigForWindow already enforces this, but the
    // invariant is re-checked here so a config can never reach the broker
    // un-validated.
    if (config.indexOf("/") !== -1 || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(config)) {
      ToastService.showWarning(I18n.tr("toast.snapshot.title"), I18n.tr("toast.snapshot.bad-config"), 4000);
      return;
    }
    qdSnapshotProc.configName = config;
    qdSnapshotProc.command = ["gdbus", "call", "--system",
                              "--dest", "org.qdistro.AdminBroker1",
                              "--object-path", "/org/qdistro/AdminBroker1",
                              "--method", "org.qdistro.AdminBroker1.SnapshotBefore",
                              config, "qdistro: manual snapshot from taskbar"];
    qdSnapshotProc.running = true;
  }

  // Dispose (D16/P2b): tear the disposable's CONTAINER down explicitly via the
  // session manager's DisposeByToken, rather than relying on window-close +
  // --rm alone. The window exposes its launch token as `instanceId` (== the
  // container's qdistro_tier2_token label); the session manager resolves that
  // to the container and removes it (admin-gated, name-revalidated, audited,
  // fail-closed). The result is toasted HONESTLY: `gdbus call` exits 0 even
  // when DisposeByToken returns false (e.g. `podman rm` failed), so success
  // requires BOTH exit 0 AND a `(true,)` return — a `(false,)` is a failure.
  // We do NOT eagerly close the window on the token path: a successful teardown
  // removes the container, which makes qdwin drop the window on its own, so a
  // FAILED teardown leaves the window visible (an honest, recoverable handle)
  // instead of being masked by --rm.
  Process {
    id: qdDisposeProc
    property string siloLabel: ""
    stdout: StdioCollector {
      id: qdDisposeOut
    }
    stderr: StdioCollector {
      id: qdDisposeErr
    }
    onExited: (code, status) => {
      if (code === 0 && /\(\s*true\s*,/.test(qdDisposeOut.text || "")) {
        ToastService.showNotice(I18n.tr("toast.dispose.done-title"), I18n.tr("toast.dispose.done", { "silo": qdDisposeProc.siloLabel || I18n.tr("toast.dispose.silo") }), "trash-2", 3000);
      } else {
        const msg = (qdDisposeErr.text || "").trim()
                  || (code === 0 ? I18n.tr("toast.dispose.incomplete") : I18n.tr("toast.dispose.sm-error"));
        ToastService.showError(I18n.tr("toast.dispose.failed"), msg, 6000);
      }
    }
  }

  function qdDisposeSelected() {
    if (qdDisposeProc.running) {
      // Single non-reentrant Process; a destructive op must not be silently
      // dropped (which would also mislabel the in-flight toast).
      ToastService.showNotice(I18n.tr("toast.dispose.title"), I18n.tr("toast.dispose.in-progress"), "trash-2", 2000);
      return;
    }
    const win = root.getSelectedWindow();
    if (!win)
      return;
    const plan = TaskbarLogic.disposeWindowPlan({
                                                  "secctxAppId": win.secctxAppId,
                                                  "instanceId": win.instanceId
                                                });
    if (!plan.dispose)
      return;
    const label = root.qdSiloForSelected();
    if (!plan.byToken) {
      // No launch token on the wire (untagged spawn): close the window, which
      // exits the app so --rm / the startup reaper tears the container down.
      // This is the only teardown mechanism available without a token.
      root.closeWindows([win]);
      ToastService.showNotice(I18n.tr("toast.dispose.done-title"), I18n.tr("toast.dispose.done", { "silo": label || I18n.tr("toast.dispose.silo") }), "trash-2", 3000);
      return;
    }
    qdDisposeProc.siloLabel = label;
    qdDisposeProc.command = ["gdbus", "call", "--system",
                             "--dest", "org.qdistro.SessionManager1",
                             "--object-path", "/org/qdistro/SessionManager1",
                             "--method", "org.qdistro.SessionManager1.DisposeByToken",
                             plan.token];
    qdDisposeProc.running = true;
  }

  // Permissions panel (D16/P2c): a READ-ONLY view of the broker rules that
  // apply to the selected window's silo. Queries the admin-only
  // org.qdistro.AdminBroker1.ListRules over the system bus (busctl
  // --json=short), filters the rule set to this window's app_id /
  // sandbox_engine (PermissionsLogic), and shows the result as disabled
  // (informational) rows in a popup menu. ListRules is admin-gated, so on a
  // non-admin session busctl exits non-zero and we toast honestly rather than
  // showing an empty panel.
  property var qdPermIdentity: ({})  // {secctxAppId, sandboxEngine} captured at open
  Process {
    id: qdPermProc
    stdout: StdioCollector {
      id: qdPermOut
    }
    stderr: StdioCollector {
      id: qdPermErr
    }
    onExited: (code, status) => {
      if (code !== 0) {
        const msg = (qdPermErr.text || "").trim() || I18n.tr("toast.permissions.broker-error");
        ToastService.showError(I18n.tr("toast.permissions.title"), msg, 6000);
        return;
      }
      const rules = PermissionsLogic.parseListRules(qdPermOut.text || "");
      const items = PermissionsLogic.buildPermissionsMenu(rules, root.qdPermIdentity);
      // Localize each row's labelKey the same way the isolation menu does.
      const localized = [];
      for (var i = 0; i < items.length; i++)
        localized.push(root.qdLocalizeIsolationItem(items[i]));
      permissionsMenu.model = localized;
      if (root.selectedAnchorItem)
        PanelService.showContextMenu(permissionsMenu, root, root.screen, root.selectedAnchorItem);
    }
  }

  function qdOpenPermissions(win) {
    if (!win)
      return;
    if (qdPermProc.running)
      return;  // a query is already in flight
    root.qdPermIdentity = {
      "secctxAppId": win.secctxAppId || "",
      "sandboxEngine": win.sandboxEngine || ""
    };
    qdPermProc.command = ["busctl", "--system", "--json=short", "call",
                          "org.qdistro.AdminBroker1",
                          "/org/qdistro/AdminBroker1",
                          "org.qdistro.AdminBroker1", "ListRules"];
    qdPermProc.running = true;
  }

  // The permissions panel popup. Read-only: every row is disabled, so
  // onTriggered just closes it (no live actions).
  NPopupContextMenu {
    id: permissionsMenu
    onTriggered: (action, item) => {
                   permissionsMenu.close();
                   PanelService.closeContextMenu(root.screen);
                 }
  }

  NPopupContextMenu {
    id: contextMenu
    model: {
      // Reference modelUpdateTrigger to make binding reactive
      const _ = root.modelUpdateTrigger;
      return root.buildContextMenuModel();
    }
    onTriggered: (action, item) => {
                   contextMenu.close();
                   PanelService.closeContextMenu(root.screen);

                   // Look up the window(s) fresh each time to avoid stale references
                   const selectedWindow = root.getSelectedWindow();
                   const selectedWindows = root.getSelectedWindowList();

                   if (action === "focus" && selectedWindow) {
                     Qdwin.focusWindow(selectedWindow);
                   } else if (action === "pin" && root.selectedAppId) {
                     root.toggleAppPin(root.selectedAppId);
                   } else if (action === "minimize") {
                     root.minimizeWindows(selectedWindows);
                   } else if (action === "maximize") {
                     root.maximizeWindows(selectedWindows, true);
                   } else if (action === "unmaximize") {
                     root.maximizeWindows(selectedWindows, false);
                   } else if (action === "close") {
                     root.closeWindows(selectedWindows);
                   } else if (action === "widget-settings") {
                     BarService.openWidgetSettings(root.screen, root.section, root.sectionWidgetIndex, root.widgetId, root.widgetSettings);
                   } else if (action === "qd-snapshot") {
                     // The menu item carries the resolved Snapper config
                     // (TaskbarLogic stripped the "tier2/" prefix); pass it
                     // directly rather than re-deriving the silo label.
                     root.qdSnapshotNow(item && item.snapConfig ? item.snapConfig : "");
                   } else if (action === "qd-dispose") {
                     // Explicit lease teardown of a tier-2 disposable: ask the
                     // session manager to remove the CONTAINER by its launch
                     // token (window.instanceId), not just close the window.
                     // The isolation section is single-window gated, so this
                     // acts on the one selected disposable window; a no-token
                     // window falls back to window-close inside the helper.
                     root.qdDisposeSelected();
                   } else if (action === "qd-permissions") {
                     // Open the read-only per-silo permissions panel. Capture
                     // the window identity NOW (selected* is cleared below) so
                     // the async ListRules result can be filtered to this silo.
                     if (selectedWindow)
                       root.qdOpenPermissions(selectedWindow);
                   } else if (action.startsWith("desktop-action-") && item && item.desktopAction) {
                     if (item.desktopAction.command && item.desktopAction.command.length > 0) {
                       Quickshell.execDetached(item.desktopAction.command);
                     } else if (item.desktopAction.execute) {
                       item.desktopAction.execute();
                     }
                   }
                   root.selectedWindowId = "";
                   root.selectedAppId = "";
                 }
  }

  function updateHasWindow() {
    // Check if we have any items in the combined model (windows or pinned apps)
    hasWindow = combinedModel.length > 0;
  }

  Connections {
    target: Qdwin
    function onActiveWindowChanged() {
      updateCombinedModel();
    }
    function onWindowListChanged() {
      updateCombinedModel();
    }
    function onWorkspaceChanged() {
      updateCombinedModel();
    }
  }

  Connections {
    target: Settings.data.dock
    function onPinnedAppsChanged() {
      updateCombinedModel();
    }
  }

  // Rebuild when a podapp launch starts/ends (cold-start placeholder).
  Connections {
    target: PodApps.placeholders
    function onCountChanged() {
      updateCombinedModel();
    }
  }

  Component.onCompleted: {
    updateCombinedModel();
  }
  onScreenChanged: updateCombinedModel()
  // Rebuild immediately when grouping/sort policy changes in settings so
  // the visible taskbar reflows without waiting for a window event.
  onGroupingModeChanged: updateCombinedModel()
  onSortModeChanged: updateCombinedModel()

  // Debounce timer for wheel interactions
  Timer {
    id: wheelDebounce
    interval: 150
    repeat: false
    onTriggered: {
      root.wheelCooldown = false;
      root.wheelAccumulatedDelta = 0;
    }
  }

  // Scroll to switch between windows
  WheelHandler {
    id: wheelHandler
    target: root
    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
    onWheel: function (event) {
      if (root.wheelCooldown || root.combinedModel.length === 0)
        return;
      var dy = event.angleDelta.y;
      var dx = event.angleDelta.x;
      var useDy = Math.abs(dy) >= Math.abs(dx);
      var delta = useDy ? dy : dx;
      root.wheelAccumulatedDelta += delta;
      var step = 120;
      if (Math.abs(root.wheelAccumulatedDelta) >= step) {
        var direction = root.wheelAccumulatedDelta > 0 ? -1 : 1;
        // Find the focused window or first running window
        var currentIndex = -1;
        for (var i = 0; i < root.combinedModel.length; i++) {
          if (root.combinedModel[i].window && root.combinedModel[i].window.isFocused) {
            currentIndex = i;
            break;
          }
        }
        if (currentIndex < 0) {
          // No focused window, find first running window
          for (var j = 0; j < root.combinedModel.length; j++) {
            if (root.combinedModel[j].window) {
              currentIndex = j;
              break;
            }
          }
        }
        if (currentIndex >= 0) {
          var nextIndex = (currentIndex + direction + root.combinedModel.length) % root.combinedModel.length;
          var nextItem = root.combinedModel[nextIndex];
          if (nextItem && nextItem.window) {
            try {
              Qdwin.focusWindow(nextItem.window);
            } catch (error) {
              Logger.e("Taskbar", "Failed to focus window: " + error);
            }
          }
        }
        root.wheelCooldown = true;
        wheelDebounce.restart();
        root.wheelAccumulatedDelta = 0;
        event.accepted = true;
      }
    }
  }

  // "visible": Always Visible, "hidden": Hide When Empty, "transparent": Transparent When Empty
  visible: hideMode !== "hidden" || hasWindow
  opacity: ((hideMode !== "hidden" && hideMode !== "transparent") || hasWindow) ? 1.0 : 0.0
  Behavior on opacity {
    NumberAnimation {
      duration: Style.animationNormal
      easing.type: Easing.OutCubic
    }
  }

  // Content dimensions for implicit sizing
  readonly property real contentWidth: {
    if (!visible)
      return 0;
    if (isVerticalBar)
      return barHeight;

    var calculatedWidth = showTitle ? taskbarLayout.implicitWidth : taskbarLayout.implicitWidth + Style.marginXL;

    // Apply maximum width constraint when smartWidth is enabled
    if (smartWidth && maxTaskbarWidth > 0) {
      return Math.min(calculatedWidth, maxTaskbarWidth);
    }

    return Math.round(calculatedWidth);
  }
  readonly property real contentHeight: visible ? (isVerticalBar ? Math.round(taskbarLayout.implicitHeight + Style.marginS * 2) : capsuleHeight) : 0

  implicitWidth: contentWidth
  implicitHeight: contentHeight

  // Visual capsule centered in parent
  Rectangle {
    id: visualCapsule
    width: root.contentWidth
    height: root.contentHeight
    anchors.centerIn: parent
    radius: Style.radiusM
    color: Style.capsuleColor
    border.color: Style.capsuleBorderColor
    border.width: Style.capsuleBorderWidth

    GridLayout {
      id: taskbarLayout

      // Pixel-perfect centering
      x: isVerticalBar ? Style.pixelAlignCenter(parent.width, width) : ((root.showTitle) ? Style.pixelAlignCenter(parent.width, width) : Style.marginM)
      y: Style.pixelAlignCenter(parent.height, height)

      // Configure GridLayout to behave like RowLayout or ColumnLayout
      rows: isVerticalBar ? -1 : 1 // -1 means unlimited
      columns: isVerticalBar ? 1 : -1 // -1 means unlimited

      rowSpacing: isVerticalBar ? Style.marginXXS : 0
      columnSpacing: isVerticalBar ? 0 : Style.marginXXS

      Repeater {
        model: root.combinedModel
        delegate: Item {
          id: taskbarItem
          required property var modelData
          required property int index
          property ShellScreen screen: root.screen

          readonly property bool isRunning: modelData.window !== null
          readonly property bool isPinned: modelData.type === "pinned" || modelData.type === "pinned-running"
          readonly property bool isPlaceholder: modelData.type === "placeholder"
          // Grouped button representing multiple windows of one application.
          readonly property bool isGroup: modelData.isGroup === true
          readonly property int groupCount: (modelData.windows !== undefined && modelData.windows !== null) ? modelData.windows.length : 0
          // Drag-to-reorder only makes sense in the launch-order ("none")
          // sort mode; the other modes re-sort on every rebuild so a manual
          // reorder would be discarded. Grouped buttons are also not
          // reorderable (their position is derived from member windows).
          readonly property bool reorderable: root.sortMode === "none" && !isGroup
          readonly property bool isFocused: isRunning && modelData.window && modelData.window.isFocused
          readonly property bool isPinnedRunning: isPinned && isRunning && !isFocused
          readonly property bool isHovered: root.hoveredWindowId === modelData.id

          readonly property bool shouldShowTitle: root.showTitle && modelData.type !== "pinned"
          readonly property real itemSpacing: Style.marginS
          readonly property real contentWidth: shouldShowTitle ? root.itemSize + itemSpacing + root.titleWidth : root.itemSize

          readonly property string title: modelData.title || modelData.appId || "Unknown application"
          readonly property color titleBgColor: (isHovered || isFocused) ? Color.mHover : Style.capsuleColor
          readonly property color titleFgColor: (isHovered || isFocused) ? Color.mOnHover : Color.mOnSurface

          Layout.preferredWidth: root.isVerticalBar ? root.barHeight : (root.showTitle ? Math.round(contentWidth + Style.marginXL) : Math.round(contentWidth)) // Add margins for both pinned and running apps
          Layout.preferredHeight: root.isVerticalBar ? root.itemSize : root.barHeight
          Layout.alignment: Qt.AlignCenter

          // Ensure dragged item is on top
          z: (root.dragSourceIndex === index) ? 1000 : 1

          property int modelIndex: index
          objectName: "taskbarAppItem"

          DropArea {
            anchors.fill: parent
            enabled: taskbarItem.reorderable
            keys: ["taskbar-app"]
            onEntered: function (drag) {
              if (drag.source && drag.source.objectName === "taskbarAppItem") {
                root.dragTargetIndex = taskbarItem.modelIndex;
              }
            }
            onExited: function () {
              if (root.dragTargetIndex === taskbarItem.modelIndex) {
                root.dragTargetIndex = -1;
              }
            }
            onDropped: function (drop) {
              root.dragSourceIndex = -1;
              root.dragTargetIndex = -1;
              Logger.d("Taskbar", "Dropped! Source: " + (drop.source ? drop.source.objectName : "null") + " Index: " + (drop.source ? drop.source.modelIndex : "?") + " -> Target Index: " + taskbarItem.modelIndex);
              if (drop.source && drop.source.objectName === "taskbarAppItem" && drop.source !== taskbarItem) {
                root.reorderApps(drop.source.modelIndex, taskbarItem.modelIndex);
              } else {
                Logger.d("Taskbar", "Drop ignored. Source objectName: " + (drop.source ? drop.source.objectName : "null"));
              }
            }
          }

          Item {
            id: draggableContent
            width: parent.width
            height: parent.height
            anchors.centerIn: dragging ? undefined : parent

            // Visual shifting logic
            readonly property bool isDragged: root.dragSourceIndex === index
            property real shiftOffset

            // Calculate shift based on drag state
            // If I am NOT the dragged item, but I am in the path of the drag
            Binding on shiftOffset {
              value: {
                if (root.dragSourceIndex !== -1 && root.dragTargetIndex !== -1 && !draggableContent.isDragged) {
                  if (root.dragSourceIndex < root.dragTargetIndex) {
                    // Dragging Right: Items between source and target shift Left
                    if (index > root.dragSourceIndex && index <= root.dragTargetIndex) {
                      return -1 * (root.isVerticalBar ? root.itemSize : draggableContent.width); // Simple approximation, could be refined
                    }
                  } else if (root.dragSourceIndex > root.dragTargetIndex) {
                    // Dragging Left: Items between target and source shift Right
                    if (index >= root.dragTargetIndex && index < root.dragSourceIndex) {
                      return (root.isVerticalBar ? root.itemSize : draggableContent.width);
                    }
                  }
                }
                return 0;
              }
            }

            transform: Translate {
              x: !root.isVerticalBar ? draggableContent.shiftOffset : 0
              y: root.isVerticalBar ? draggableContent.shiftOffset : 0

              Behavior on x {
                NumberAnimation {
                  duration: Style.animationFast
                  easing.type: Easing.OutQuad
                }
              }
              Behavior on y {
                NumberAnimation {
                  duration: Style.animationFast
                  easing.type: Easing.OutQuad
                }
              }
            }

            property bool dragging: taskbarMouseArea.drag.active
            onDraggingChanged: {
              if (dragging) {
                root.dragSourceIndex = index;
              } else {
                // Don't reset immediately on release to allow drop to handle it,
                // or use a timer if needed, but drop handler usually fires.
                // However, if dropped outside, we need to reset.
                // Let's reset if not handled by drop area quickly?
                // Actually, drag.active becomes false on release.
                // We might want to clear it if no drop happened.
                if (root.dragSourceIndex === index) {
                  // Slight delay/check? For now, let DropArea handle reset on success.
                  // If cancelled (dropped nowhere), we should reset.
                  Qt.callLater(() => {
                                 if (!taskbarMouseArea.drag.active && root.dragSourceIndex === index) {
                                   root.dragSourceIndex = -1;
                                   root.dragTargetIndex = -1;
                                 }
                               });
                }
              }
            }

            Drag.active: dragging
            Drag.source: taskbarItem
            Drag.hotSpot.x: width / 2
            Drag.hotSpot.y: height / 2
            Drag.keys: ["taskbar-app"]

            z: dragging ? 1000 : 0
            scale: dragging ? 1.05 : 1.0
            Behavior on scale {
              NumberAnimation {
                duration: Style.animationFast
              }
            }

            Rectangle {
              id: titleBackground
              visible: shouldShowTitle
              anchors.centerIn: parent
              width: parent.width
              height: root.capsuleHeight
              color: titleBgColor
              radius: Style.radiusM

              Behavior on color {
                ColorAnimation {
                  duration: Style.animationFast
                  easing.type: Easing.InOutQuad
                }
              }
            }

            Rectangle {
              anchors.centerIn: parent
              width: taskbarItem.contentWidth
              height: parent.height
              color: "transparent"

              RowLayout {
                id: itemLayout
                anchors.fill: parent
                spacing: taskbarItem.itemSpacing

                Item {
                  Layout.preferredWidth: root.itemSize
                  Layout.preferredHeight: root.itemSize
                  Layout.alignment: Qt.AlignVCenter | Qt.AlignLeft

                  IconImage {
                    id: appIcon
                    anchors.fill: parent

                    source: ThemeIcons.iconForAppId(taskbarItem.modelData.appId)
                    smooth: true
                    asynchronous: true
                    // Cold-start placeholders dim the icon to differentiate
                    // them from real toplevels (per cold-start contract in
                    // qdistro/doc/containers.md).
                    opacity: taskbarItem.isPlaceholder ? 0.5 : 1.0

                    // Apply dock shader to all taskbar icons
                    layer.enabled: widgetSettings.colorizeIcons !== false
                    layer.effect: ShaderEffect {
                      property color targetColor: Settings.data.colorSchemes.darkMode ? Color.mOnSurface : Color.mSurfaceVariant
                      property real colorizeMode: 0.0 // Dock mode (grayscale)

                      fragmentShader: Qt.resolvedUrl(Quickshell.shellDir + "/Shaders/qsb/appicon_colorize.frag.qsb")
                    }
                  }

                  // Busy spinner overlay for cold-start placeholders.
                  // Removed when PodApps resolves the placeholder on
                  // wp_security_context_v1.instance_id match.
                  BusyIndicator {
                    visible: taskbarItem.isPlaceholder
                    running: visible
                    anchors.centerIn: parent
                    width: parent.width * 0.7
                    height: parent.height * 0.7
                  }

                  Rectangle {
                    id: iconBackground
                    visible: !shouldShowTitle
                    anchors.bottomMargin: -2
                    anchors.bottom: parent.bottom
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: Style.toOdd(root.itemSize * 0.25)
                    height: 4
                    color: taskbarItem.isFocused ? Color.mPrimary : (taskbarItem.isHovered ? Color.mHover : "transparent")
                    radius: Math.min(Style.radiusXXS, width / 2)

                    Behavior on color {
                      ColorAnimation {
                        duration: Style.animationFast
                        easing.type: Easing.OutCubic
                      }
                    }
                  }

                  // Window-count badge for grouped buttons (XFCE shows the
                  // number of windows collapsed into a single button).
                  Rectangle {
                    visible: taskbarItem.isGroup && taskbarItem.groupCount > 1
                    anchors.top: parent.top
                    anchors.right: parent.right
                    width: Math.max(badgeText.implicitWidth + Style.marginXXS * 2, height)
                    height: Math.round(root.itemSize * 0.42)
                    radius: height / 2
                    color: Color.mPrimary

                    NText {
                      id: badgeText
                      anchors.centerIn: parent
                      text: taskbarItem.groupCount > 99 ? "99+" : String(taskbarItem.groupCount)
                      pointSize: Style.fontSizeXS
                      color: Color.mOnPrimary
                      verticalAlignment: Text.AlignVCenter
                      horizontalAlignment: Text.AlignHCenter
                    }
                  }
                }

                NText {
                  id: titleText
                  visible: shouldShowTitle
                  Layout.preferredWidth: root.titleWidth
                  Layout.preferredHeight: root.itemSize
                  Layout.alignment: Qt.AlignVCenter | Qt.AlignLeft
                  Layout.fillWidth: false

                  text: taskbarItem.title
                  elide: Text.ElideRight
                  verticalAlignment: Text.AlignVCenter
                  horizontalAlignment: Text.AlignLeft

                  pointSize: barFontSize
                  color: titleFgColor
                  opacity: Style.opacityFull
                }
              }
            }
          }

          MouseArea {
            id: taskbarMouseArea
            objectName: "taskbarMouseArea"
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            acceptedButtons: Qt.LeftButton | Qt.RightButton

            drag.target: taskbarItem.reorderable ? draggableContent : null
            drag.axis: root.isVerticalBar ? Drag.YAxis : Drag.XAxis
            preventStealing: true

            onPressed: {
              // Constrain drag to roughly the taskbar area but allow some freedom
              // Or just let it be free since we only care about drops
            }

            onReleased: {
              if (draggableContent.Drag.active) {
                draggableContent.Drag.drop();
              }
            }

            onClicked: mouse => {
                         if (!modelData)
                         return;
                         if (mouse.button === Qt.LeftButton) {
                           if (isGroup && groupCount > 1) {
                             // Grouped button with multiple windows - reveal
                             // the per-app window list to pick which to focus.
                             TooltipService.hide();
                             root.selectedWindowId = modelData.id;
                             root.selectedAppId = modelData.appId;
                             root.openGroupWindowList(modelData, taskbarItem);
                           } else if (isRunning && modelData.window) {
                             // Running app - focus it
                             try {
                               Qdwin.focusWindow(modelData.window);
                             } catch (error) {
                               Logger.e("Taskbar", "Failed to activate toplevel: " + error);
                             }
                           } else if (isPinned) {
                             // Pinned app not running - launch it
                             root.launchPinnedApp(modelData.appId);
                           }
                         } else if (mouse.button === Qt.RightButton) {
                           TooltipService.hide();
                           // Only show context menu for running apps
                           if (isRunning && modelData.window) {
                             root.selectedWindowId = modelData.id;
                             root.selectedAppId = modelData.appId;
                             root.openTaskbarContextMenu(taskbarItem);
                           }
                         }
                       }
            onEntered: {
              root.hoveredWindowId = taskbarItem.modelData.id;
              TooltipService.show(taskbarItem, taskbarItem.title, BarService.getTooltipDirection(root.screen?.name));
            }
            onExited: {
              root.hoveredWindowId = "";
              TooltipService.hide();
            }
          }
        }
      }
    }
  }

  function openTaskbarContextMenu(item) {
    // Set the model directly (shared builder keeps it in sync with the
    // reactive contextMenu.model binding).
    contextMenu.model = root.buildContextMenuModel();

    // Remember the anchor so a follow-up popup (permissions panel) can reuse it.
    root.selectedAnchorItem = item;

    // Anchor to root (stable) but center horizontally on the clicked item
    PanelService.showContextMenu(contextMenu, root, screen, item);
  }

  // Popup listing the individual windows of a grouped button. Activating
  // an entry focuses that window. Built from the selected group entry's
  // window list (each item's "action" is the window handle as a string).
  NPopupContextMenu {
    id: groupMenu
    onTriggered: (action, item) => {
                   groupMenu.close();
                   PanelService.closeContextMenu(root.screen);
                   if (item && item.window) {
                     try {
                       Qdwin.focusWindow(item.window);
                     } catch (e) {
                       Logger.e("Taskbar", "group focus failed: " + e);
                     }
                   }
                 }
  }

  // Open the per-group window list for the given group entry, anchored to
  // the clicked taskbar item.
  function openGroupWindowList(entry, item) {
    if (!entry || !entry.windowEntries)
      return;
    var items = [];
    entry.windowEntries.forEach(function (we) {
      items.push({
                   "label": we.title || we.appId || I18n.tr("common.unknown"),
                   "action": "window:" + we.id,
                   "icon": we.window && we.window.isFocused ? "eye" : "chevron-right",
                   "window": we.window
                 });
    });
    groupMenu.model = items;
    PanelService.showContextMenu(groupMenu, root, screen, item);
  }
}
